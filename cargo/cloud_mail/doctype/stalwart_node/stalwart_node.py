# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import ipaddress

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from cargo.atlas_client import base_image_id
from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES, Machine
from cargo.client_models import MAIL, NodeSpec
from cargo.cloud_mail.cluster import bootstrap, dns, naming
from cargo.cloud_mail.cluster.firewall import node_firewall
from cargo.cloud_mail.utils import log_exception
from cargo.dns.resolver import verify_ptr_record
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder

REMOVABLE_STATUSES = ("Pending", "Failed", "Disabled")


class StalwartNode(WorkflowBuilder):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cluster: DF.Link
		consecutive_failures: DF.Int
		drained_by: DF.Data | None
		enabled: DF.Check
		hostname: DF.Data
		in_ingress_dns: DF.Check
		installed_version: DF.Data | None
		ipv4_address: DF.Data | None
		ipv6_address: DF.Data | None
		is_bootstrap_node: DF.Check
		last_error: DF.SmallText | None
		last_health_at: DF.Datetime | None
		machine: DF.Link | None
		node_id: DF.Int
		provisioned_at: DF.Datetime | None
		setup_log: DF.Code | None
		ptr_verified: DF.Check
		role: DF.Literal["full", "frontend", "outbound"]
		location: DF.Data | None
		title: DF.Data | None
		status: DF.Literal[
			"Pending", "Provisioning", "Provisioned", "Active", "Draining", "Failed", "Disabled"
		]
	# end: auto-generated types

	# --- lifecycle ------------------------------------------------------------

	def autoname(self) -> None:
		self.hostname = naming.next_hostname("Stalwart Node", self.cluster, naming.NODE_PREFIX)
		self.name = self.hostname

	def validate(self) -> None:
		cluster = self.get_cluster()
		self.title = (self.title or "").strip() or self.hostname
		self.hostname = (self.hostname or "").strip().lower().rstrip(".")
		suffix = f".{cluster.default_domain}"
		if not self.hostname.endswith(suffix) or "." in self.hostname[: -len(suffix)]:
			frappe.throw(_("Hostname must be a single label under {0}.").format(cluster.default_domain))

		self.validate_single_node(cluster)
		self.ipv4_address = validate_ip(self.ipv4_address, 4) if self.ipv4_address else None
		self.ipv6_address = validate_ip(self.ipv6_address, 6) if self.ipv6_address else None
		if self.is_new():
			self.status = "Pending"

	def validate_single_node(self, cluster: Document) -> None:
		"""Embedded stores live on one VPS: no second node, and roles that split work make no sense."""

		if not cluster.single_node:
			return
		other = frappe.db.get_value("Stalwart Node", {"cluster": cluster.name, "name": ["!=", self.name]})
		if other:
			frappe.throw(
				_("Cluster {0} uses an embedded store and can only have one node ({1}).").format(
					cluster.name, other
				)
			)
		if self.role != "full":
			frappe.throw(_("The only node of a cluster must have the full role."))

	def after_insert(self) -> None:
		dns.sync_node_records(self)
		self.get_cluster().save(ignore_permissions=True)  # re-validates stores for the new node count

	def on_update(self) -> None:
		before = self.get_doc_before_save()
		if not before:
			return
		if (before.ipv4_address, before.ipv6_address) != (self.ipv4_address, self.ipv6_address):
			dns.sync_node_records(self)
			dns.sync_spf_record(self.get_cluster())

		if before.enabled and not self.enabled and self.status in ("Active", "Provisioned"):
			bootstrap.drain_node(self)

	def on_trash(self) -> None:
		if self.status not in REMOVABLE_STATUSES:
			frappe.throw(_("Drain and disable the node before deleting it."))
		cluster = self.get_cluster()
		if self.is_bootstrap_node and cluster.status == "Active":
			frappe.throw(_("The bootstrap node of an active cluster cannot be deleted."))
		if cluster.bootstrap_node == self.name:
			# A failed first attempt: let another node bootstrap the cluster.
			cluster.db_set({"bootstrap_node": None, "status": "Pending"}, update_modified=False)

		bootstrap.forget_node(self)
		dns.delete_node_records(self)
		super().on_trash()

	def after_delete(self) -> None:
		dns.sync_spf_record(self.get_cluster())

	# --- helpers --------------------------------------------------------------

	def get_cluster(self) -> Document:
		return frappe.get_cached_doc("Stalwart Cluster", self.cluster)

	def set_status(self, status: str, error: str | None = None) -> None:
		values = {"status": status}
		if error is not None:
			values["last_error"] = error[:1000]
		self.db_set(values, update_modified=False, notify=True)

	# --- the machine ------------------------------------------------------------

	@frappe.whitelist()
	def request_machine(self, cpu_millicores: int, ram_gb: int, disk_gb: int) -> str:
		"""Rent this node's machine from Atlas, with a public address and the mail firewall."""
		frappe.only_for("System Manager")
		if self.machine:
			frappe.throw(_("This node already has a machine."))

		machine = Machine.request(
			self,
			NodeSpec(
				role=MAIL, cpu_millicores=cint(cpu_millicores), ram_gb=cint(ram_gb), disk_gb=cint(disk_gb)
			),
			base_image=base_image_id(),
			public_ipv4=True,
			firewall=node_firewall(),
		)
		self.db_set("machine", machine.name, update_modified=False)
		return machine.name

	def sync_machines(self) -> None:
		"""What this node's machine settling means for it. Its state is already recorded;
		`sync_pending_machines` calls this once it changes."""
		machine: Machine = frappe.get_doc("Machine", self.machine)
		if machine.status in DEAD_MACHINE_STATES:
			self.set_status("Failed", _("{0} is {1}.").format(machine.name, machine.status))
			return
		if machine.status != "Running":
			return
		if not machine.public_ipv4:
			# Atlas says it is up; an address that never came is a fault, not a delay.
			self.set_status("Failed", _("Atlas gave {0} no public address.").format(machine.name))
			return
		if self.ipv4_address != machine.public_ipv4:
			self.ipv4_address = machine.public_ipv4
			self.save(ignore_permissions=True)
		if self.status == "Pending" and self.enabled:
			self.start_provisioning()

	# --- provisioning -------------------------------------------------------------

	@frappe.whitelist()
	def provision(self) -> None:
		frappe.only_for("System Manager")
		self.start_provisioning()

	def start_provisioning(self) -> None:
		"""Install Stalwart on this node's machine and bring it into the cluster."""
		if not self.enabled:
			frappe.throw(_("Enable the node first."))
		if not self.machine or frappe.db.get_value("Machine", self.machine, "status") != "Running":
			frappe.throw(_("The node's machine must be running."))
		if not self.ipv4_address:
			frappe.throw(_("The node has no public address yet."))
		bootstrap.needs_bootstrap(self)  # refused now rather than half-way through
		self.set_status("Provisioning")
		self._provision.run_as_workflow()

	@flow
	def _provision(self) -> None:
		if not self.install():
			return
		if not self.bring_up():
			return
		self.record_provisioned()

	@task(queue="long", timeout=3 * bootstrap.INSTALL_TIMEOUT)
	def install(self) -> bool:
		return (
			bootstrap.run_script(
				self, "install.sh", bootstrap.install_environment(self), [], bootstrap.INSTALL_TIMEOUT
			)
			is not None
		)

	@task(queue="long", timeout=3 * bootstrap.BOOTSTRAP_TIMEOUT)
	def bring_up(self) -> bool:
		"""Bootstrap the store from this node, or join a cluster that is already up."""
		if bootstrap.needs_bootstrap(self):
			bootstrap.start_bootstrap(self)
			environment, secrets = bootstrap.bootstrap_environment(self)
			name = "bootstrap.sh"
		else:
			environment, secrets = bootstrap.configure_environment(self)
			name = "configure.sh"
		return bootstrap.run_script(self, name, environment, secrets, bootstrap.BOOTSTRAP_TIMEOUT) is not None

	@task
	def record_provisioned(self) -> None:
		bootstrap.after_provision(self)

	@frappe.whitelist()
	def upgrade(self) -> None:
		frappe.only_for("System Manager")
		if not self.enabled:
			frappe.throw(_("Enable the node first."))
		self._upgrade.run_as_workflow()

	@flow
	def _upgrade(self) -> None:
		self.take_out_of_ingress()
		if not self.install():
			return
		version = self.restart_on_installed_version()
		if version is None:
			return
		self.record_upgraded(version)

	@task
	def take_out_of_ingress(self) -> None:
		if self.status == "Active":
			bootstrap.drain_node(self)

	@task(queue="long", timeout=3 * bootstrap.BOOTSTRAP_TIMEOUT)
	def restart_on_installed_version(self) -> str | None:
		"""The version the node came back on, or None once the failure is on the record."""
		environment = {"WAIT_PORTS": bootstrap.wait_ports(self)}
		output = bootstrap.run_script(self, "upgrade.sh", environment, [], bootstrap.BOOTSTRAP_TIMEOUT)
		if output is None:
			return None
		return bootstrap.installed_version_from(output) or self.get_cluster().stalwart_version

	@task
	def record_upgraded(self, version: str | None = None) -> None:
		bootstrap.after_upgrade(self, version)

	@frappe.whitelist()
	def rollback(self) -> None:
		frappe.only_for("System Manager")
		self._rollback.run_as_workflow()

	@flow
	def _rollback(self) -> None:
		self.take_out_of_ingress()
		version = self.restart_on_previous_version()
		if version is None:
			return
		self.record_upgraded(version)

	@task(queue="long", timeout=3 * bootstrap.BOOTSTRAP_TIMEOUT)
	def restart_on_previous_version(self) -> str | None:
		environment = {"WAIT_PORTS": bootstrap.wait_ports(self)}
		output = bootstrap.run_script(self, "rollback.sh", environment, [], bootstrap.BOOTSTRAP_TIMEOUT)
		if output is None:
			return None
		return bootstrap.installed_version_from(output) or "unknown"

	@frappe.whitelist()
	def reconfigure(self) -> None:
		"""Rewrite the store connection and restart, after a store credential or address changed."""
		frappe.only_for("System Manager")
		self._reconfigure.run_as_workflow()

	@flow
	def _reconfigure(self) -> None:
		self.take_out_of_ingress()
		if not self.rewrite_store_connection():
			return
		self.record_upgraded(self.installed_version)

	@task(queue="long", timeout=3 * bootstrap.BOOTSTRAP_TIMEOUT)
	def rewrite_store_connection(self) -> bool:
		environment, secrets = bootstrap.configure_environment(self)
		return (
			bootstrap.run_script(self, "configure.sh", environment, secrets, bootstrap.BOOTSTRAP_TIMEOUT)
			is not None
		)

	# --- actions --------------------------------------------------------------

	@frappe.whitelist()
	def drain(self) -> None:
		frappe.only_for("System Manager")
		bootstrap.drain_node(self)

	@frappe.whitelist()
	def restore(self) -> None:
		frappe.only_for("System Manager")
		bootstrap.restore_node(self)

	@frappe.whitelist()
	def check_health(self) -> bool:
		frappe.only_for("System Manager")
		return bootstrap.check_node(self)

	@frappe.whitelist()
	def verify_ptr(self) -> bool:
		frappe.only_for("System Manager")
		ok = verify_ptr_record(self.ipv4_address, self.hostname)
		if ok is None:
			return bool(self.ptr_verified)  # the lookup failed; the last known state stands
		self.db_set("ptr_verified", cint(ok), update_modified=False)
		return ok


def validate_ip(value: str | None, version: int) -> str:
	try:
		address = ipaddress.ip_address((value or "").strip())
	except ValueError:
		frappe.throw(_("{0} is not a valid IP address.").format(value))
	if address.version != version:
		frappe.throw(_("{0} is not an IPv{1} address.").format(value, version))
	return str(address)


def verify_all_ptr_records() -> None:
	for name in frappe.get_all("Stalwart Node", {"enabled": 1}, pluck="name"):
		node = frappe.get_doc("Stalwart Node", name)
		ok = verify_ptr_record(node.ipv4_address, node.hostname)
		if ok is not None:
			node.db_set("ptr_verified", cint(ok), update_modified=False)
