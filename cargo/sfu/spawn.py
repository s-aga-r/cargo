"""Bringing a region its SFU without an operator."""

from __future__ import annotations

import typing

import frappe
from frappe import _

from cargo.cargo.doctype.dns_zone.dns_zone import settings_zone
from cargo.client_models import SFU
from cargo.spawn import (
	MAX_SETUP_ATTEMPTS,
	machine_status,
	report_dead_machines,
	retry_setup,
	run_spawner,
	validate_node_size,
)

if typing.TYPE_CHECKING:
	from cargo.sfu.doctype.sfu_server.sfu_server import SFUServer

CONFIG_KEY = "default_sfu_config"
LOCK_NAME = "sfu-spawn"
OPTIONAL_FIELDS = ("suite_ref", "image", "workers", "media_port")


def ensure_sfu() -> None:
	"""Give this region one SFU and keep it moving. Off until `default_sfu_config` is in site
	config. Scheduled in `hooks.py`."""
	run_spawner(CONFIG_KEY, LOCK_NAME, validate_config, build_server)


def validate_config(config: dict) -> None:
	if not isinstance(config, dict):
		frappe.throw(_("{0} must be an object.").format(CONFIG_KEY))
	validate_node_size(config.get(SFU), SFU)
	if not isinstance(config.get("ssl_email"), str) or "@" not in config["ssl_email"]:
		frappe.throw(_("ssl_email must be an address the certificate authority can write to."))
	if "workers" in config and (not isinstance(config["workers"], int) or not 1 <= config["workers"] <= 64):
		frappe.throw(_("workers must be a whole number between 1 and 64."))


def build_server(config: dict) -> None:
	server: SFUServer = frappe.get_single("SFU Server")

	if not server.machine and not server.auto_spawn:
		if frappe.db.exists("Webhook", "sfu_server") or server.setup_log:
			return  # filled in by hand at some point: left alone
		zone = settings_zone()
		if not zone or not frappe.db.get_value("DNS Zone", zone, "enabled"):
			return  # reached by name, so there must be a zone to put the name in
		server.update(
			{
				"auto_spawn": 1,
				"ssl_email": config["ssl_email"],
				**{f: config[f] for f in OPTIONAL_FIELDS if f in config},
			}
		).save(ignore_permissions=True)

	if not server.auto_spawn or server.status == "Setting Up":
		return

	if fill_machine(server, config):
		advance(server)


def fill_machine(server: SFUServer, config: dict) -> bool:
	if server.machine:
		return not report_dead_machines(server, [server.machine])
	size = config[SFU]
	try:
		server.create_sfu_node(
			cpu_millicores=size["cpu_millicores"], ram_gb=size["ram_gb"], disk_gb=size["disk_gb"]
		)
	except Exception:
		frappe.log_error(title="SFU Server could not add its machine")
		return False
	return True


def advance(server: SFUServer) -> None:
	if machine_status(server.machine) != "Running" or not server.ipv4_address:
		return
	if server.status == "Draft":
		server.setup()
		return
	if server.status == "Failed":
		retry_setup(server)


__all__ = ["CONFIG_KEY", "MAX_SETUP_ATTEMPTS", "ensure_sfu", "validate_config"]
