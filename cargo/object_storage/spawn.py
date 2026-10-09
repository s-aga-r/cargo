"""Bringing a region its object storage cluster without an operator."""

from __future__ import annotations

import typing

import frappe
from frappe import _

from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES
from cargo.client_models import GATEWAY, STORAGE, Role
from cargo.spawn import (
	MAX_SETUP_ATTEMPTS,
	machine_status,
	report_dead_machines,
	retry_setup,
	run_spawner,
	validate_node_size,
)

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)

CONFIG_KEY = "default_storage_cluster_config"
LOCK_NAME = "object-storage-spawn"


def ensure_cluster() -> None:
	"""Give this region one object storage cluster and keep it moving.

	Off until `default_storage_cluster_config` is in site config. Scheduled in `hooks.py`."""
	run_spawner(CONFIG_KEY, LOCK_NAME, validate_config, build_cluster)


def build_cluster(config: dict) -> None:
	"""One step towards the region having a cluster that serves."""
	name = frappe.db.exists("Object Storage Cluster", {"auto_spawn": 1})
	if not name:
		if not frappe.db.count("Object Storage Cluster"):
			create_cluster(config)

		return

	cluster: ObjectStorageCluster = frappe.get_doc("Object Storage Cluster", name)

	# A setup run is already under way, and it owns the cluster until it ends.
	if cluster.status == "Setting Up":
		return

	if fill_machines(cluster, config):
		advance(cluster)


def validate_config(config: dict) -> None:
	"""A shape Cargo can ask Atlas for. Throws, naming what is wrong."""
	if not isinstance(config, dict):
		frappe.throw(_("{0} must be an object.").format(CONFIG_KEY))

	for count in ("storage_node_count", "replication_factor"):
		if not isinstance(config.get(count), int) or config[count] < 1:
			frappe.throw(_("{0} must be a whole number of at least 1.").format(count))

	# A cluster needs a full copy's worth of storage nodes before it can be set up. Fewer and
	# the machines would be rented, and every setup run would then refuse them.
	if config["storage_node_count"] < config["replication_factor"]:
		frappe.throw(
			_("storage_node_count must be at least the replication_factor of {0}.").format(
				config["replication_factor"]
			)
		)

	for role in (GATEWAY, STORAGE):
		validate_node_size(config.get(role), role)


def create_cluster(config: dict) -> ObjectStorageCluster:
	"""One cluster, on its defaults. Machines are asked for on the next run, so a failure
	here leaves a record to carry on from rather than a rented machine with no owner.

	The replication factor comes from the same config as the node count, so the two can
	never disagree about how many storage nodes the cluster needs."""
	return frappe.get_doc(
		{
			"doctype": "Object Storage Cluster",
			"auto_spawn": 1,
			"replication_factor": config["replication_factor"],
		}
	).insert()


def missing_slots(cluster: ObjectStorageCluster, config: dict) -> list[Role]:
	"""What this cluster still needs, gateway first: every other node joins through it."""
	alive = [row.role for row in cluster.machines if machine_status(row.machine) not in DEAD_MACHINE_STATES]
	gateways = 1 - alive.count(GATEWAY)
	storage = config["storage_node_count"] - alive.count(STORAGE)

	return [GATEWAY] * max(gateways, 0) + [STORAGE] * max(storage, 0)


def fill_machines(cluster: ObjectStorageCluster, config: dict) -> bool:
	"""Ask Atlas for the machines this cluster is short of. True once it has them all."""
	if report_dead_machines(cluster, [row.machine for row in cluster.machines]):
		return False

	for role in missing_slots(cluster, config):
		size = config[role]
		try:
			cluster.add_node(
				role,
				cpu_millicores=size["cpu_millicores"],
				ram_gb=size["ram_gb"],
				disk_gb=size["disk_gb"],
			)
		except Exception:
			frappe.log_error(title=f"{cluster.name} could not add a {role} machine")
			return False

		# Each machine in a transaction of its own: `Machine.request` rolls back the row it
		# failed on, and an uncommitted sibling would take a machine Atlas already built.
		if not frappe.flags.in_test:
			frappe.db.commit()  # nosemgrep

	return not missing_slots(cluster, config)


def advance(cluster: ObjectStorageCluster) -> None:
	"""Set the cluster up once its machines are up, and try again if a run failed."""
	if cluster.status == "Draft":
		if all(machine.status == "Running" for machine in cluster.all_nodes):
			cluster.setup()

		return

	# Only bring-up retries. A live cluster fails because its machines died, and setting up
	# again cannot raise the dead.
	if cluster.status != "Failed" or cluster.is_live:
		return

	retry_setup(cluster)


__all__ = ["CONFIG_KEY", "LOCK_NAME", "MAX_SETUP_ATTEMPTS", "ensure_cluster", "validate_config"]
