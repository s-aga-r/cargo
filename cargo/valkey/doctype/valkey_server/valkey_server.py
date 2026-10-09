# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from cargo.atlas_client import base_image_id
from cargo.cargo.doctype.machine.machine import Machine
from cargo.client_models import VALKEY, NodeSpec
from cargo.service import configure_service_webhook, mark, release_machine, single_machine_sync
from cargo.ssh import OutputLog, run_over_ssh, script
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder

CONF = ("valkey", "conf", "valkey", "install.sh")
# Binary releases from valkey.io, one per Ubuntu release and architecture.
VALKEY_URL_TEMPLATE = "https://download.valkey.io/releases/valkey-{version}-noble-{arch}.tar.gz"
SETUP_TIMEOUT = 15 * 60
MAX_PORT = 65535
SECRET_LENGTH = 32
WEBHOOK_NAME = "valkey_server"


class ValkeyServer(WorkflowBuilder):
	"""The region's one Valkey: coordinator pub/sub, rate limits and greylists for the services
	that need them. Transient state, so it is not backed up."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_password: DF.Password | None
		auto_setup_attempts: DF.Int
		auto_spawn: DF.Check
		base_image: DF.Data | None
		error: DF.LongText | None
		health: DF.Literal["Unknown", "Healthy", "Degraded", "Critical"]
		health_reason: DF.Data | None
		machine: DF.Link | None
		max_memory_mb: DF.Int
		port: DF.Int
		setup_log: DF.Code | None
		status: DF.Literal["Draft", "Setting Up", "Active", "Failed"]
		version: DF.Data
	# end: auto-generated types

	def validate(self) -> None:
		if not 1 <= cint(self.port) <= MAX_PORT:
			frappe.throw(_("Port must be between 1 and {0}.").format(MAX_PORT))
		if cint(self.max_memory_mb) < 64:
			frappe.throw(_("Max Memory must be at least 64 MB."), frappe.ValidationError)
		parts = (self.version or "").strip().split(".")
		if len(parts) != 3 or not all(part.isdigit() for part in parts):
			frappe.throw(_("Version is a Valkey release, such as 8.1.3."), frappe.ValidationError)

	def before_save(self) -> None:
		if not self.admin_password:
			self.admin_password = frappe.generate_hash(length=SECRET_LENGTH)

	def on_update(self) -> None:
		if self.machine and not frappe.db.exists("Webhook", WEBHOOK_NAME):
			configure_valkey_webhook(self)

	@property
	def address(self) -> str | None:
		return frappe.db.get_value("Machine", self.machine, "address") if self.machine else None

	@property
	def service_endpoint(self) -> str:
		return f"redis://[{self.address}]:{self.port}"

	@frappe.whitelist()
	def create_valkey_node(self, cpu_millicores: int, ram_gb: int, disk_gb: int) -> str:
		if self.machine:
			frappe.throw(_("This server already has a machine."), frappe.ValidationError)
		machine = Machine.request(
			self,
			NodeSpec(
				role=VALKEY, cpu_millicores=cint(cpu_millicores), ram_gb=cint(ram_gb), disk_gb=cint(disk_gb)
			),
			base_image=self.base_image or base_image_id(),
		)
		self.machine = machine.name
		self.save()
		return self.machine

	@frappe.whitelist()
	def reset_auto_setup_attempts(self) -> None:
		self.check_permission("write")
		self.db_set("auto_setup_attempts", 0)

	@frappe.whitelist()
	def setup(self) -> None:
		if frappe.db.get_value("Machine", self.machine, "status") != "Running":
			frappe.throw(_("Machine must be running to set up Valkey."), frappe.ValidationError)
		self.mark("Setting Up")
		self._setup.run_as_workflow()

	@flow
	def _setup(self) -> None:
		if self.install():
			self.mark("Active")

	@task(queue="long", timeout=3 * SETUP_TIMEOUT)
	def install(self) -> bool:
		machine: Machine = frappe.get_doc("Machine", self.machine)
		environment = self.install_environment()
		with OutputLog(self, "setup_log", append=True) as log:
			try:
				run_over_ssh(
					machine.address,
					script(*CONF, environment=environment),
					machine.get_password("ssh_private_key"),
					timeout=SETUP_TIMEOUT,
					on_output=log.write,
					pin=machine.host_key_pin(),
					secrets=[environment["ADMIN_PASSWORD"]],
				)
			except Exception:
				frappe.log_error(
					title="Valkey Server failed to set up", message=frappe.get_traceback(with_context=False)
				)
				self.mark("Failed", "Valkey did not install. See the Setup Log.")
				return False
		return True

	def install_environment(self) -> dict[str, str]:
		return {
			"VALKEY_VERSION": self.version,
			"VALKEY_URL_TEMPLATE": VALKEY_URL_TEMPLATE,
			"LISTEN_ADDRESS": self.address,
			"PORT": self.port,
			"MAX_MEMORY_MB": self.max_memory_mb,
			"ADMIN_PASSWORD": self.get_password("admin_password"),
		}

	@frappe.whitelist()
	def release_machine(self) -> None:
		"""Let a dead or failed machine go; the next one is asked for on the same record."""
		frappe.only_for("System Manager")
		release_machine(self, ("Draft", "Failed"))
		self.reload()
		self.mark("Draft")

	def sync_machines(self) -> None:
		single_machine_sync(self)

	def mark(self, status: str, error: str | None = None) -> None:
		mark(self, status, error)


def configure_valkey_webhook(server: ValkeyServer) -> None:
	configure_service_webhook(server, "valkey", WEBHOOK_NAME, server.service_endpoint)
