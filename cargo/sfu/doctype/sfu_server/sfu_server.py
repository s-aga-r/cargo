# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import cint

from cargo.atlas_client import base_image_id
from cargo.cargo.doctype.dns_record.dns_record import reconcile_managed_records
from cargo.cargo.doctype.dns_zone.dns_zone import settings_zone
from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES, Machine
from cargo.client_models import SFU, NodeSpec
from cargo.service import (
	ANYWHERE,
	central_enrolled,
	configure_service_webhook,
	firewall,
	firewall_rule,
	mark,
	release_machine,
)
from cargo.ssh import OutputLog, run_over_ssh, script
from cargo.workflow_engine.doctype.press_workflow.decorators import flow, task
from cargo.workflow_engine.doctype.press_workflow.workflow_builder import WorkflowBuilder

CONF = ("sfu", "conf", "sfu", "install.sh")
SETUP_TIMEOUT = 30 * 60
SECRET_LENGTH = 32
WEBHOOK_NAME = "sfu_server"
HOST_LABEL = "sfu"
WEB_PORTS = (80, 443)


class SFUServer(WorkflowBuilder):
	"""The region's one mediasoup SFU for Frappe Meet: a machine with a public address, the
	Suite project's own Docker deployment on it, and the secret every site signs its room
	tokens with."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		auto_setup_attempts: DF.Int
		auto_spawn: DF.Check
		base_image: DF.Data | None
		error: DF.LongText | None
		health: DF.Literal["Unknown", "Healthy", "Degraded", "Critical"]
		health_reason: DF.Data | None
		hostname: DF.Data | None
		image: DF.Data
		ipv4_address: DF.Data | None
		jwt_secret: DF.Password | None
		machine: DF.Link | None
		media_port: DF.Int
		metrics_token: DF.Password | None
		setup_log: DF.Code | None
		ssl_email: DF.Data | None
		status: DF.Literal["Draft", "Setting Up", "Active", "Failed"]
		suite_ref: DF.Data
		workers: DF.Int
	# end: auto-generated types

	def validate(self) -> None:
		if not 1 <= cint(self.workers) <= 64:
			frappe.throw(_("Workers must be between 1 and 64."), frappe.ValidationError)
		if not 1024 <= cint(self.media_port) <= 65535 - cint(self.workers):
			frappe.throw(_("Media Port must leave room for one UDP port per worker below 65536."))
		if not self.ssl_email:
			frappe.throw(_("SSL Email is required: the certificate authority writes to it."))
		frappe.utils.validate_email_address(self.ssl_email, throw=True)
		zone = settings_zone()
		if not zone:
			frappe.throw(_("Name a DNS Zone on Cargo Settings first: the SFU is reached by name."))
		self.hostname = f"{HOST_LABEL}.{zone}"

	def before_save(self) -> None:
		for field in ("jwt_secret", "metrics_token"):
			if not self.get(field):
				self.set(field, frappe.generate_hash(length=SECRET_LENGTH))

	def on_update(self) -> None:
		if self.machine and central_enrolled() and not frappe.db.exists("Webhook", WEBHOOK_NAME):
			configure_service_webhook(self, "sfu", WEBHOOK_NAME, self.service_endpoint)

	@property
	def service_endpoint(self) -> str:
		return f"https://{self.hostname}"

	def media_ports(self) -> str:
		return f"{self.media_port}-{self.media_port + self.workers - 1}"

	def firewall(self) -> dict:
		"""HTTPS and the ACME challenge from anywhere, one UDP port per worker for media."""
		rules = [firewall_rule("tcp", port, ANYWHERE) for port in WEB_PORTS]
		rules.append(firewall_rule("udp", self.media_ports(), ANYWHERE))
		return firewall(rules)

	@frappe.whitelist()
	def create_sfu_node(self, cpu_millicores: int, ram_gb: int, disk_gb: int) -> str:
		if self.machine:
			frappe.throw(_("This server already has a machine."), frappe.ValidationError)
		machine = Machine.request(
			self,
			NodeSpec(
				role=SFU, cpu_millicores=cint(cpu_millicores), ram_gb=cint(ram_gb), disk_gb=cint(disk_gb)
			),
			base_image=self.base_image or base_image_id(),
			public_ipv4=True,
			firewall=self.firewall(),
		)
		self.machine = machine.name
		self.save()
		return self.machine

	def sync_machines(self) -> None:
		"""A running machine hands over its public address, which the hostname then points at."""
		machine = frappe.get_doc("Machine", self.machine)
		if machine.status in DEAD_MACHINE_STATES:
			self.mark("Failed", _("{0} is {1}.").format(machine.name, machine.status))
			return
		if machine.status == "Running" and not machine.public_ipv4:
			self.mark("Failed", _("{0} runs without a public address.").format(machine.name))
			return
		if machine.public_ipv4 and self.ipv4_address != machine.public_ipv4:
			self.db_set("ipv4_address", machine.public_ipv4)
			self.publish_address()

	@frappe.whitelist()
	def release_machine(self) -> None:
		"""Let a dead or failed machine go; the hostname's record goes with its address."""
		frappe.only_for("System Manager")
		release_machine(self, ("Draft", "Failed"), ipv4_address=None)
		self.reload()
		self.publish_address()
		self.mark("Draft")

	def publish_address(self) -> None:
		"""The hostname's A record in Cargo's zone, which the certificate authority resolves."""
		rows = []
		if self.ipv4_address:
			rows.append(
				{
					"dns_zone": settings_zone(),
					"host": HOST_LABEL,
					"type": "A",
					"value": self.ipv4_address,
					"category": "Service",
				}
			)
		reconcile_managed_records(self.doctype, self.name, rows)

	@frappe.whitelist()
	def reset_auto_setup_attempts(self) -> None:
		self.check_permission("write")
		self.db_set("auto_setup_attempts", 0)

	@frappe.whitelist()
	def setup(self) -> None:
		if frappe.db.get_value("Machine", self.machine, "status") != "Running":
			frappe.throw(_("Machine must be running to set up the SFU."), frappe.ValidationError)
		if not self.ipv4_address:
			frappe.throw(_("The machine has no public address yet."), frappe.ValidationError)
		self.mark("Setting Up")
		self._setup.run_as_workflow()

	@flow
	def _setup(self) -> None:
		if self.install():
			self.mark("Active")

	@task(queue="long", timeout=3 * SETUP_TIMEOUT)
	def install(self) -> bool:
		"""Docker, the Suite project's deploy files, the environment, then its own setup."""
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
					secrets=[environment["JWT_SECRET"], environment["METRICS_TOKEN"]],
				)
			except Exception:
				frappe.log_error(
					title="SFU Server failed to set up", message=frappe.get_traceback(with_context=False)
				)
				self.mark("Failed", "The SFU did not come up. See the Setup Log.")
				return False
		return True

	def install_environment(self) -> dict[str, str]:
		return {
			"SUITE_REF": self.suite_ref,
			"SFU_IMAGE": self.image,
			"DOMAIN": self.hostname,
			"SSL_EMAIL": self.ssl_email,
			"JWT_SECRET": self.get_password("jwt_secret"),
			"METRICS_TOKEN": self.get_password("metrics_token"),
			"WEBRTC_ANNOUNCED_IP": self.ipv4_address,
			"WEBRTC_SERVER_PORT": self.media_port,
			"MEDIASOUP_NUM_WORKERS": self.workers,
		}

	def credential(self) -> dict:
		"""What a site puts in its config to use this SFU."""
		return {"sfu_server_url": self.service_endpoint, "sfu_secret": self.get_password("jwt_secret")}

	def mark(self, status: str, error: str | None = None) -> None:
		mark(self, status, error)
