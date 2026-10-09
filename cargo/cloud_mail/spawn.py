"""Bringing a region its mail cluster without an operator."""

from __future__ import annotations

import typing

import frappe
from frappe import _

from cargo.cloud_mail.cluster.stores import POOL_MAX_CONNECTIONS
from cargo.spawn import MAX_SETUP_ATTEMPTS, report_dead_machines, run_spawner, validate_node_size

if typing.TYPE_CHECKING:
	from cargo.cloud_mail.doctype.stalwart_cluster.stalwart_cluster import StalwartCluster

CONFIG_KEY = "default_mail_cluster_config"
LOCK_NAME = "mail-spawn"
NODE = "node"
STORE_NAME = "stalwart"
BLOB_BUCKET = "mail"
# Postgres keeps connections for itself and for Cargo; the rest is the nodes' pools.
RESERVED_CONNECTIONS = 10


def ensure_mail() -> None:
	"""Give this region one mail cluster and keep it moving. Off until `default_mail_cluster_config`
	is in site config. Scheduled in `hooks.py`."""
	run_spawner(CONFIG_KEY, LOCK_NAME, validate_config, build_cluster)


def validate_config(config: dict) -> None:
	if not isinstance(config, dict):
		frappe.throw(_("{0} must be an object.").format(CONFIG_KEY))
	count = config.get("node_count")
	if not isinstance(count, int) or isinstance(count, bool) or count < 1:
		frappe.throw(_("node_count must be a whole number of at least 1."))
	validate_node_size(config.get(NODE), NODE)
	if not isinstance(config.get("acme_contact_email"), str) or "@" not in config["acme_contact_email"]:
		frappe.throw(_("acme_contact_email must be an address the certificate authority can write to."))
	if config.get("certificate_management") not in (None, "ACME", "Manual"):
		frappe.throw(_("certificate_management must be ACME or Manual."))


def missing_prerequisite(config: dict) -> str | None:
	"""Why the region cannot hold a mail cluster yet, or None. Stores first: a cluster
	rented before them would be a RocksDB one that can never grow."""
	if not frappe.db.exists("Object Storage Cluster", {"status": "Active"}):
		return "no object storage cluster serves yet"
	postgres = frappe.get_single("Postgres Server")
	if postgres.status != "Active":
		return "the Postgres server does not serve yet"
	if frappe.get_single("Valkey Server").status != "Active":
		return "the Valkey server does not serve yet"
	needed = POOL_MAX_CONNECTIONS * config["node_count"] + RESERVED_CONNECTIONS
	if needed > (postgres.max_connections or 0):
		return f"{config['node_count']} nodes need {needed} Postgres connections, the server allows {postgres.max_connections}"
	zone = frappe.db.get_single_value("Cargo Settings", "dns_zone")
	if not zone or not frappe.db.get_value("DNS Zone", zone, "enabled"):
		return "no enabled DNS Zone is named on Cargo Settings"
	return None


def build_cluster(config: dict) -> None:
	"""One step towards the region having a mail cluster that serves."""
	name = frappe.db.exists("Stalwart Cluster", {"auto_spawn": 1})
	if not name:
		# A cluster added by hand is never joined by a second; a region whose stores do not
		# serve yet simply waits, and their own records say why.
		if not frappe.db.count("Stalwart Cluster") and not missing_prerequisite(config):
			create_cluster(config)
		return

	cluster: StalwartCluster = frappe.get_doc("Stalwart Cluster", name)
	if cluster.status == "Bootstrapping" or frappe.db.exists(
		"Stalwart Node", {"cluster": name, "status": "Provisioning"}
	):
		return  # a run owns it until it ends

	if fill_nodes(cluster, config):
		advance(cluster)


def create_stores() -> dict:
	"""The cluster's database, Valkey user and bucket, made on the services in-process."""
	if not frappe.db.exists("Postgres Database", STORE_NAME):
		frappe.get_doc({"doctype": "Postgres Database", "database_name": STORE_NAME}).insert(
			ignore_permissions=True
		)
	if not frappe.db.exists("Valkey Credential", STORE_NAME):
		frappe.get_doc({"doctype": "Valkey Credential", "username": STORE_NAME}).insert(
			ignore_permissions=True
		)
	if not frappe.db.exists("Bucket", BLOB_BUCKET):
		storage = frappe.db.get_value("Object Storage Cluster", {"status": "Active"})
		frappe.get_doc({"doctype": "Bucket", "bucket_name": BLOB_BUCKET, "cluster": storage}).insert(
			ignore_permissions=True
		)
	return {"data_store": STORE_NAME, "in_memory_store": STORE_NAME, "blob_bucket": BLOB_BUCKET}


def create_cluster(config: dict) -> StalwartCluster:
	"""One cluster on the region's stores. Nodes are asked for on the next run, so a failure
	here leaves a record to carry on from rather than a rented machine with no owner."""
	stores = create_stores()
	cluster = frappe.get_doc(
		{
			"doctype": "Stalwart Cluster",
			"title": frappe.db.get_single_value("Cargo Settings", "region") or "mail",
			"auto_spawn": 1,
			"acme_contact_email": config["acme_contact_email"],
			"certificate_management": config.get("certificate_management") or "ACME",
			"stalwart_version": config.get("stalwart_version"),
			**stores,
		}
	)
	cluster.insert(ignore_permissions=True)
	for doctype, name in (("Postgres Database", STORE_NAME), ("Valkey Credential", STORE_NAME)):
		frappe.db.set_value(
			doctype,
			name,
			{"reference_doctype": "Stalwart Cluster", "reference_name": cluster.name},
			update_modified=False,
		)
	return cluster


def fill_nodes(cluster: StalwartCluster, config: dict) -> bool:
	"""Ask Atlas for the nodes the cluster is short of: the first alone, since it brings the
	store up, and the rest one a run once the cluster serves. True when none is wanted now."""
	nodes = frappe.get_all("Stalwart Node", {"cluster": cluster.name}, ["name", "machine", "status"])
	if report_dead_machines(cluster, [node.machine for node in nodes if node.machine]):
		return False
	if not nodes:
		add_node(cluster, config)
		return False
	if cluster.status != "Active":
		return True
	if len(nodes) < config["node_count"]:
		add_node(cluster, config)
		return False
	return True


def add_node(cluster: StalwartCluster, config: dict) -> None:
	size = config[NODE]
	try:
		node = frappe.get_doc({"doctype": "Stalwart Node", "cluster": cluster.name, "role": "full"}).insert(
			ignore_permissions=True
		)
		node.request_machine(
			cpu_millicores=size["cpu_millicores"], ram_gb=size["ram_gb"], disk_gb=size["disk_gb"]
		)
	except Exception:
		frappe.log_error(title=f"{cluster.name} could not add a node")
		return
	if not frappe.flags.in_test:
		frappe.db.commit()  # nosemgrep: each machine in a transaction of its own, as object storage does


def advance(cluster: StalwartCluster) -> None:
	"""Provision a failed node again, counted on the cluster, until the attempts are spent."""
	failed = frappe.get_all(
		"Stalwart Node", {"cluster": cluster.name, "status": "Failed", "enabled": 1}, ["name", "machine"]
	)
	for row in failed:
		if cluster.auto_setup_attempts >= MAX_SETUP_ATTEMPTS:
			return
		if frappe.db.get_value("Machine", row.machine, "status") != "Running":
			continue
		cluster.db_set("auto_setup_attempts", cluster.auto_setup_attempts + 1, update_modified=False)
		node = frappe.get_doc("Stalwart Node", row.name)
		node.set_status("Pending")
		node.start_provisioning()


__all__ = ["CONFIG_KEY", "LOCK_NAME", "MAX_SETUP_ATTEMPTS", "ensure_mail", "validate_config"]
