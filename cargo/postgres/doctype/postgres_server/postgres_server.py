# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from cargo.atlas_client import base_image_id
from cargo.cargo.doctype.machine.machine import Machine
from cargo.client_models import POSTGRES, NodeSpec
from cargo.postgres.client import ADMIN_ROLE
from cargo.service import (
	MESH_NETWORK,
	central_enrolled,
	configure_service_webhook,
	mark,
	release_machine,
	single_machine_sync,
)
from cargo.ssh import OutputLog, run_over_ssh, script
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder

CONF = ("postgres", "conf", "postgres", "install.sh")
SETUP_TIMEOUT = 20 * 60
MAX_PORT = 65535
SECRET_LENGTH = 32
WEBHOOK_NAME = "postgres_server"
BACKUP_BUCKET = "postgres-backups"


class PostgresServer(WorkflowBuilder):
	"""The region's one Postgres: a machine on the mesh that every store-backed service shares."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_password: DF.Password | None
		auto_setup_attempts: DF.Int
		auto_spawn: DF.Check
		backup_bucket: DF.Link | None
		backup_log: DF.Code | None
		base_image: DF.Data | None
		error: DF.LongText | None
		health: DF.Literal["Unknown", "Healthy", "Degraded", "Critical"]
		health_reason: DF.Data | None
		machine: DF.Link | None
		max_connections: DF.Int
		port: DF.Int
		setup_log: DF.Code | None
		status: DF.Literal["Draft", "Setting Up", "Active", "Failed"]
		version: DF.Data
	# end: auto-generated types

	def validate(self) -> None:
		if not 1 <= cint(self.port) <= MAX_PORT:
			frappe.throw(_("Port must be between 1 and {0}.").format(MAX_PORT))
		if cint(self.max_connections) < 10:
			frappe.throw(_("Max Connections must be at least 10."), frappe.ValidationError)
		if not (self.version or "").strip().isdigit():
			frappe.throw(_("Version is a PostgreSQL major version, such as 16."), frappe.ValidationError)

	def before_save(self) -> None:
		"""The admin role's password, made here rather than in `before_insert`, which a Single
		never runs."""
		if not self.admin_password:
			self.admin_password = frappe.generate_hash(length=SECRET_LENGTH)

	def on_update(self) -> None:
		"""Tell Central when the server settles. A record with no machine was never filled in."""
		if self.machine and central_enrolled() and not frappe.db.exists("Webhook", WEBHOOK_NAME):
			configure_postgres_webhook(self)

	@property
	def address(self) -> str | None:
		return frappe.db.get_value("Machine", self.machine, "address") if self.machine else None

	@property
	def service_endpoint(self) -> str:
		"""Where consumers connect; reachable on the mesh only."""
		return f"postgres://[{self.address}]:{self.port}"

	@frappe.whitelist()
	def create_postgres_node(self, cpu_millicores: int, ram_gb: int, disk_gb: int) -> str:
		if self.machine:
			frappe.throw(_("This server already has a machine."), frappe.ValidationError)

		machine = Machine.request(
			self,
			NodeSpec(
				role=POSTGRES, cpu_millicores=cint(cpu_millicores), ram_gb=cint(ram_gb), disk_gb=cint(disk_gb)
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
		"""Install Postgres on this server's machine."""
		if frappe.db.get_value("Machine", self.machine, "status") != "Running":
			frappe.throw(_("Machine must be running to set up Postgres."), frappe.ValidationError)

		self.mark("Setting Up")
		self._setup.run_as_workflow()

	@flow
	def _setup(self) -> None:
		if self.install():
			self.mark("Active")

	@task(queue="long", timeout=3 * SETUP_TIMEOUT)
	def install(self) -> bool:
		"""Install Postgres on the machine, streaming to `setup_log`. Re-run safe."""
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
					title="Postgres Server failed to set up", message=frappe.get_traceback(with_context=False)
				)
				self.mark("Failed", "Postgres did not install. See the Setup Log.")
				return False
		return True

	def install_environment(self) -> dict[str, str]:
		"""What the install script needs: which Postgres, where to listen, whom to admit."""
		return {
			"POSTGRES_VERSION": self.version,
			"LISTEN_ADDRESS": self.address,
			"PORT": self.port,
			"MAX_CONNECTIONS": self.max_connections,
			"MESH_NETWORK": MESH_NETWORK,
			"ADMIN_ROLE": ADMIN_ROLE,
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


def configure_postgres_webhook(server: PostgresServer) -> None:
	"""Point a Frappe Webhook at Central so this server reports its own status changes."""
	configure_service_webhook(server, "postgres", WEBHOOK_NAME, server.service_endpoint)
