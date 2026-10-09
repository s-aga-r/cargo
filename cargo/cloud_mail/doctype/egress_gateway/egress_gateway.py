# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, now

from cargo.atlas_client import base_image_id
from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES, Machine
from cargo.client_models import MAIL, NodeSpec
from cargo.cloud_mail.cluster import bootstrap, dns, egress, naming, plan
from cargo.cloud_mail.cluster.firewall import gateway_firewall
from cargo.cloud_mail.doctype.stalwart_node.stalwart_node import validate_ip
from cargo.cloud_mail.stalwart import get_admin_client, get_client
from cargo.cloud_mail.utils import dkim_algorithms, log_exception, validate_version
from cargo.service import release_machine
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder


class EgressGateway(WorkflowBuilder):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_password: DF.Password | None
		admin_username: DF.Data | None
		api_key: DF.Password | None
		base_url: DF.Data | None
		cluster: DF.Link
		config_plan: DF.Code | None
		config_version: DF.Int
		enabled: DF.Check
		hostname: DF.Data
		installed_version: DF.Data | None
		ipv4_address: DF.Data | None
		last_config_sync_at: DF.Datetime | None
		last_error: DF.SmallText | None
		machine: DF.Link | None
		provisioned_at: DF.Datetime | None
		setup_log: DF.Code | None
		stalwart_version: DF.Data | None
		location: DF.Data | None
		title: DF.Data | None
		status: DF.Literal["Pending", "Provisioning", "Provisioned", "Active", "Failed", "Disabled"]
	# end: auto-generated types

	# --- lifecycle --------------------------------------------------------------

	def autoname(self) -> None:
		self.hostname = naming.next_hostname("Egress Gateway", self.cluster, naming.GATEWAY_PREFIX)
		self.name = self.hostname

	def before_insert(self) -> None:
		self.status = "Pending"
		self.admin_username = self.admin_username or "admin"
		if not self.admin_password:
			self.admin_password = frappe.generate_hash(length=32)

	def validate(self) -> None:
		cluster = self.get_cluster()
		self.title = (self.title or "").strip() or self.hostname
		self.hostname = (self.hostname or "").strip().lower().rstrip(".")
		suffix = f".{cluster.default_domain}"
		if not self.hostname.endswith(suffix) or "." in self.hostname[: -len(suffix)]:
			frappe.throw(_("Hostname must be a single label under {0}.").format(cluster.default_domain))

		self.base_url = f"https://{self.hostname}"
		self.ipv4_address = validate_ip(self.ipv4_address, 4) if self.ipv4_address else None
		self.stalwart_version = validate_version(
			self.stalwart_version or cluster.stalwart_version,
			_("Stalwart Version"),
		)

	def after_insert(self) -> None:
		dns.sync_gateway_records(self)

	def on_update(self) -> None:
		before = self.get_doc_before_save()
		if before and before.ipv4_address != self.ipv4_address:
			dns.sync_gateway_records(self)
			for pool in self.pools():
				dns.sync_pool_records(pool)
		if before and before.enabled and not self.enabled and self.status in ("Provisioned", "Active"):
			self.set_status("Disabled")

	def on_trash(self) -> None:
		if self.status in ("Provisioning", "Provisioned", "Active"):
			frappe.throw(_("Disable the gateway before deleting it."))
		if frappe.db.exists("Egress IP Pool Address", {"gateway": self.name}):
			frappe.throw(_("Remove this gateway's addresses from every pool first."))
		dns.delete_gateway_records(self)
		super().on_trash()

	# --- helpers ------------------------------------------------------------------

	def get_cluster(self) -> Document:
		return frappe.get_cached_doc("Stalwart Cluster", self.cluster)

	def pools(self) -> list[Document]:
		names = frappe.get_all(
			"Egress IP Pool Address", {"gateway": self.name}, pluck="parent", distinct=True
		)
		return [frappe.get_doc("Egress IP Pool", name) for name in sorted(set(names))]

	def set_status(self, status: str, error: str | None = None) -> None:
		serving_before = self.status == "Active"
		values = {"status": status}
		if error is not None:
			values["last_error"] = error[:1000]
		self.db_set(values, update_modified=False, notify=True)
		if serving_before != (status == "Active"):
			self.resync_pool_records()

	def resync_pool_records(self) -> None:
		"""The pool hostnames list only serving gateways, so they follow this one's status."""

		for pool in self.pools():
			try:
				dns.sync_pool_records(pool)
			except Exception:
				log_exception(f"Pool DNS for {pool.name} could not follow {self.name}", self)

	def get_client(self):
		return get_client(self)

	def get_admin_client(self):
		return get_admin_client(self)

	def bump_config_version(self, rendered_plan: list[dict]) -> None:
		self.db_set(
			{"config_version": (self.config_version or 0) + 1, "config_plan": plan.redacted(rendered_plan)},
			update_modified=False,
		)

	# --- the machine ---------------------------------------------------------------------

	@frappe.whitelist()
	def request_machine(self, cpu_millicores: int, ram_gb: int, disk_gb: int) -> str:
		"""Rent this gateway's machine from Atlas, with a public address and the relay firewall."""
		frappe.only_for("System Manager")
		if self.machine:
			frappe.throw(_("This gateway already has a machine."))

		cluster = self.get_cluster()
		machine = Machine.request(
			self,
			NodeSpec(
				role=MAIL, cpu_millicores=cint(cpu_millicores), ram_gb=cint(ram_gb), disk_gb=cint(disk_gb)
			),
			base_image=base_image_id(),
			public_ipv4=True,
			firewall=gateway_firewall(
				[pool.relay_port for pool in self.pools()], egress.node_addresses(cluster)
			),
		)
		self.db_set("machine", machine.name, update_modified=False)
		return machine.name

	@frappe.whitelist()
	def release_machine(self) -> None:
		frappe.only_for("System Manager")
		release_machine(self, ("Pending", "Failed", "Disabled"), ipv4_address=None)
		self.reload()
		dns.sync_gateway_records(self)
		self.set_status("Pending", "")

	def sync_machines(self) -> None:
		"""What this gateway's machine settling means for it."""
		machine: Machine = frappe.get_doc("Machine", self.machine)
		if machine.status in DEAD_MACHINE_STATES:
			self.set_status("Failed", _("{0} is {1}.").format(machine.name, machine.status))
			return
		if machine.status != "Running":
			return
		if not machine.public_ipv4:
			self.set_status("Failed", _("Atlas gave {0} no public address.").format(machine.name))
			return
		if self.ipv4_address != machine.public_ipv4:
			self.ipv4_address = machine.public_ipv4
			self.save(ignore_permissions=True)
		if self.status == "Pending" and self.enabled:
			self.start_provisioning()

	# --- provisioning -------------------------------------------------------------------

	@frappe.whitelist()
	def provision(self) -> None:
		frappe.only_for("System Manager")
		self.start_provisioning()

	def start_provisioning(self) -> None:
		if not self.enabled:
			frappe.throw(_("Enable the gateway first."))
		if not self.machine or frappe.db.get_value("Machine", self.machine, "status") != "Running":
			frappe.throw(_("The gateway's machine must be running."))
		self.bump_config_version(egress.gateway_plan(self))
		self.set_status("Provisioning")
		self._provision.run_as_workflow()

	def on_workflow_failure(self, workflow) -> None:
		if self.status == "Provisioning":
			self.set_status(
				"Failed", _("Provisioning failed in {0}. See the workflow.").format(workflow.name)
			)

	@flow
	def _provision(self) -> None:
		if not self.install():
			return
		if not self.bring_up():
			return
		self.record_provisioned()

	@task(queue="long", timeout=3 * bootstrap.INSTALL_TIMEOUT)
	def install(self) -> bool:
		environment = {
			**bootstrap.install_environment(self),
			"FIREWALL_PORTS": "443",
			"RELAY_PORTS": " ".join(str(pool.relay_port) for pool in self.pools()),
			"RELAY_SOURCES": " ".join(egress.node_addresses(self.get_cluster())),
		}
		return (
			bootstrap.run_script(self, "install.sh", environment, [], bootstrap.INSTALL_TIMEOUT) is not None
		)

	@task(queue="long", timeout=3 * bootstrap.BOOTSTRAP_TIMEOUT)
	def bring_up(self) -> bool:
		environment, secrets = egress.bootstrap_environment(self)
		return (
			bootstrap.run_script(self, "bootstrap.sh", environment, secrets, bootstrap.BOOTSTRAP_TIMEOUT)
			is not None
		)

	@task
	def record_provisioned(self) -> None:
		egress.after_gateway_provision(self)

	# --- actions --------------------------------------------------------------------

	@frappe.whitelist()
	def sync_config(self) -> dict:
		frappe.only_for("System Manager")
		if self.status != "Active":
			frappe.throw(_("Only an active gateway can be synced."))
		return self.push_config()

	def push_config(self) -> dict:
		rendered = egress.gateway_plan(self)
		result = self.get_client().apply(rendered)
		self.get_client().reload_settings()
		self.bump_config_version(rendered)
		self.db_set("last_config_sync_at", now(), update_modified=False)
		return {"created": result.created, "updated": result.updated, "unchanged": result.unchanged}

	@frappe.whitelist()
	def preview_plan(self) -> str:
		frappe.only_for("System Manager")
		return plan.redacted(egress.gateway_plan(self))

	@frappe.whitelist()
	def show_admin_password(self) -> str:
		frappe.only_for("Administrator")
		return self.get_password("admin_password")

	@frappe.whitelist()
	def check_health(self) -> bool:
		frappe.only_for("System Manager")
		return egress.check_gateway(self)

	@frappe.whitelist()
	def replace_dkim_keys(self) -> None:
		"""Emergency replacement of the gateway domain's keys after a leak, same selectors."""

		frappe.only_for("System Manager")
		client = self.get_client()
		domain = client.domains.find_by_name(self.hostname)
		if not domain:
			frappe.throw(_("The gateway does not hold its domain {0} yet.").format(self.hostname))
		client.domains.replace_dkim_keys(domain["id"], dkim_algorithms())
