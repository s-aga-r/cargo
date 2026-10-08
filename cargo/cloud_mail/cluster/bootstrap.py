"""Node lifecycle: bootstrap completion, health, draining and upgrades."""

from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now

from cargo.cloud_mail.cluster import dns, plan
from cargo.cloud_mail.stalwart import forget_sessions, get_client, has_credentials
from cargo.cloud_mail.stalwart.credentials import Credential
from cargo.cloud_mail.stalwart.errors import StalwartError, StalwartUnauthorizedError
from cargo.cloud_mail.utils import log_exception

if TYPE_CHECKING:
	from frappe.model.document import Document

BOOTSTRAP_DEADLINE_MINUTES = 45


# --- provisioning ---------------------------------------------------------------------------


def serves_clients(node: Document) -> bool:
	"""Outbound-only nodes run no listeners: nothing to wait for, nothing to put in ingress DNS."""

	return (node.role or "full") != "outbound"


# --- callbacks --------------------------------------------------------------------------------


def after_provision(node: Document) -> None:
	"""The node is installed and answering locally."""

	cluster = node.get_cluster()
	if node.is_bootstrap_node and cluster.status == "Failed" and cluster.bootstrap_node == node.name:
		cluster.db_set("status", "Bootstrapping", update_modified=False)  # a retried bootstrap succeeded
	node.db_set(
		{
			"status": "Provisioned",
			"provisioned_at": now(),
			"installed_version": node.get_cluster().stalwart_version,
		},
		update_modified=False,
	)
	if node.is_bootstrap_node:
		# The certificate check goes through the cluster hostname, so it must resolve to us.
		dns.sync_node_records(node, include_ingress=True)
	else:
		dns.sync_node_records(node, include_ingress=False)
	dns.sync_spf_record(node.get_cluster())
	check_node(node)


def after_upgrade(node: Document) -> None:
	node.db_set(
		{"installed_version": node.get_cluster().stalwart_version, "provisioned_at": now()},
		update_modified=False,
	)
	node.set_status("Provisioned")
	check_node(node)


# --- health ------------------------------------------------------------------------------------


def check_node(node: Document) -> bool:
	"""Promotes a Provisioned node to Active once Stalwart confirms it; fails it after a deadline."""

	cluster = node.get_cluster()
	if cluster.status == "Bootstrapping" and node.is_bootstrap_node:
		return finish_bootstrap(cluster)

	if cluster.status != "Active":
		return False

	try:
		registry = cluster.get_client().cluster_nodes.find_by_hostname(node.hostname)
	except StalwartError as e:
		return _not_ready(node, str(e))

	if problem := _registry_problem(cluster, registry):
		return _not_ready(node, problem)

	node.db_set(
		{
			"node_id": (registry or {}).get("nodeId") or 0,
			"last_health_at": now(),
			"last_error": None,
		},
		update_modified=False,
	)
	if node.status == "Provisioned":  # Draining stays put until an operator restores the node
		activate_node(node)
	return True


def activate_node(node: Document) -> None:
	if not node.enabled:
		frappe.throw(_("Enable the node first."))
	node.set_status("Active")
	dns.sync_node_records(node, include_ingress=serves_clients(node))
	dns.sync_spf_record(node.get_cluster())


def _push_initial_config(cluster: Document) -> None:
	"""Objects the recovery stage cannot create (the disabled-accounts role) land here."""

	try:
		cluster.push_config()
	except StalwartError as e:
		cluster.db_set("drift_report", frappe.as_json({"error": str(e)}), update_modified=False)
		frappe.log_error(title=f"Initial config push failed for {cluster.name}", message=str(e))


def _registry_problem(cluster: Document, registry: dict | None) -> str | None:
	"""A single node without a coordinator may not hold a lease; the cluster answering is enough."""

	if registry is None and cluster.coordinator != "Disabled":
		return "Node registry entry absent"
	if registry and registry.get("status") != "active":
		return f"Node registry status: {registry.get('status')}"
	return None


def _not_ready(node: Document, detail: str) -> bool:
	"""Records the problem; only a node still waiting to come up is failed after the deadline."""

	started = get_datetime(node.provisioned_at or now())
	expired = get_datetime(now()) > add_to_date(started, minutes=BOOTSTRAP_DEADLINE_MINUTES)
	if expired and node.status == "Provisioned":
		node.set_status("Failed", f"Not healthy after {BOOTSTRAP_DEADLINE_MINUTES} minutes: {detail}")
	else:
		node.db_set("last_error", detail[:1000], update_modified=False)
	return False


def finish_bootstrap(cluster: Document) -> bool:
	"""Turns a bootstrapping cluster active once its first node serves a valid certificate.

	Reached repeatedly (button or cron) until it succeeds: the ACME DNS-01 issuance runs on the
	node after the playbook ends, so the HTTPS endpoint is not usable straight away.
	"""

	node = frappe.get_doc("Stalwart Node", cluster.bootstrap_node) if cluster.bootstrap_node else None
	if not node or node.status != "Provisioned":
		return False

	try:
		admin = cluster.get_admin_client()
		ensure_api_key(cluster, admin)
		_set_default_certificate(cluster, admin)
		registry = admin.cluster_nodes.find_by_hostname(node.hostname)
		problem = _registry_problem(cluster, registry)
	except StalwartError as e:
		problem = str(e)

	if problem:
		_not_ready(node, problem)
		if node.status == "Failed":
			cluster.db_set("status", "Failed", update_modified=False)
		return False

	cluster.db_set({"status": "Active", "last_config_sync_at": now()}, update_modified=False)
	_push_initial_config(cluster)
	node.db_set(
		{
			"node_id": (registry or {}).get("nodeId") or 0,
			"last_health_at": now(),
			"last_error": None,
		},
		update_modified=False,
	)
	activate_node(node)
	return True


def ensure_api_key(target: Document, admin) -> None:
	"""Mints the management key for a cluster or gateway unless the stored one still works.

	A stored key can be dead: a re-bootstrapped data store never saw it, and a re-run of the
	recovery-stage plan replaces the admin's credentials, keys included.

	No other key is deleted here. The cron, the job callback and the form buttons can run this
	at once, and one run could delete the key another has just minted and is about to store.
	Minting only adds keys, so whichever is stored works; the extras have secrets nobody kept.
	"""

	if _api_key_works(target):
		return
	_, secret = admin.api_keys.create_secret(
		Credential(description=plan.API_KEY_DESCRIPTION, permissions=plan.api_key_permissions())
	)
	target.api_key = secret
	target.save(ignore_permissions=True)
	forget_sessions(target)


def _api_key_works(target: Document) -> bool:
	if not target.get_password("api_key", raise_exception=False):
		return False
	forget_sessions(target)  # a session cached for an earlier data store would hide a dead key
	try:
		get_client(target)
	except StalwartUnauthorizedError:
		return False
	return True


def _set_default_certificate(cluster: Document, client) -> None:
	"""Non-SNI clients (SMTP) need a default certificate; pick the issued one for the hostname."""

	try:
		for certificate in client.objects("Certificate").get_all(
			properties=["id", "subjectAlternativeNames"]
		):
			names = certificate.get("subjectAlternativeNames") or []
			names = list(names.keys()) if isinstance(names, dict) else list(names)
			if cluster.hostname in names:
				client.singleton("SystemSettings").write({"defaultCertificateId": certificate["id"]})
				return
	except StalwartError:
		log_exception(f"Could not pick a default certificate for {cluster.name}", cluster)


# --- draining / removal -----------------------------------------------------------------------


def drain_node(node: Document) -> None:
	"""Takes the node out of the ingress round-robin; Stalwart keeps running on it."""

	dns.sync_node_records(node, include_ingress=False)
	node.set_status("Draining" if node.status == "Active" else "Disabled")
	dns.sync_spf_record(node.get_cluster())


def restore_node(node: Document) -> None:
	if not node.enabled:
		frappe.throw(_("Enable the node first."))
	node.db_set("provisioned_at", now(), update_modified=False)  # the health deadline starts afresh
	node.set_status("Provisioned")
	check_node(node)


def forget_node(node: Document) -> None:
	"""Removes the node's registry lease when the cluster is reachable (best effort)."""

	cluster = node.get_cluster()
	if cluster.status != "Active" or not has_credentials(cluster):
		return
	try:
		client = cluster.get_client()
		if registry := client.cluster_nodes.find_by_hostname(node.hostname):
			client.cluster_nodes.delete(registry["id"])
	except StalwartError:
		log_exception(f"Could not remove {node.hostname} from the cluster registry", node)
