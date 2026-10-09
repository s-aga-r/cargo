"""Bringing a region its Valkey without an operator."""

from __future__ import annotations

import typing

import frappe
from frappe import _

from cargo.client_models import VALKEY
from cargo.spawn import (
	MAX_SETUP_ATTEMPTS,
	machine_status,
	report_dead_machines,
	retry_setup,
	run_spawner,
	validate_node_size,
)

if typing.TYPE_CHECKING:
	from cargo.valkey.doctype.valkey_server.valkey_server import ValkeyServer

CONFIG_KEY = "default_valkey_config"
LOCK_NAME = "valkey-spawn"
OPTIONAL_FIELDS = ("version", "max_memory_mb")


def ensure_valkey() -> None:
	"""Give this region one Valkey and keep it moving. Off until `default_valkey_config` is in
	site config. Scheduled in `hooks.py`."""
	run_spawner(CONFIG_KEY, LOCK_NAME, validate_config, build_server)


def validate_config(config: dict) -> None:
	if not isinstance(config, dict):
		frappe.throw(_("{0} must be an object.").format(CONFIG_KEY))
	validate_node_size(config.get(VALKEY), VALKEY)
	if "max_memory_mb" in config and (
		not isinstance(config["max_memory_mb"], int) or config["max_memory_mb"] < 64
	):
		frappe.throw(_("max_memory_mb must be a whole number of at least 64."))


def build_server(config: dict) -> None:
	server: ValkeyServer = frappe.get_single("Valkey Server")

	if not server.machine and not server.auto_spawn:
		if frappe.db.exists("Webhook", "valkey_server") or server.setup_log:
			return  # filled in by hand at some point: left alone
		server.update({"auto_spawn": 1, **{f: config[f] for f in OPTIONAL_FIELDS if f in config}}).save(
			ignore_permissions=True
		)

	if not server.auto_spawn or server.status == "Setting Up":
		return

	if fill_machine(server, config):
		advance(server)


def fill_machine(server: ValkeyServer, config: dict) -> bool:
	if server.machine:
		return not report_dead_machines(server, [server.machine])
	size = config[VALKEY]
	try:
		server.create_valkey_node(
			cpu_millicores=size["cpu_millicores"], ram_gb=size["ram_gb"], disk_gb=size["disk_gb"]
		)
	except Exception:
		frappe.log_error(title="Valkey Server could not add its machine")
		return False
	return True


def advance(server: ValkeyServer) -> None:
	if machine_status(server.machine) != "Running":
		return
	if server.status == "Draft":
		server.setup()
		return
	if server.status == "Failed":
		retry_setup(server)


__all__ = ["CONFIG_KEY", "MAX_SETUP_ATTEMPTS", "ensure_valkey", "validate_config"]
