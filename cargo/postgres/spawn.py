"""Bringing a region its Postgres without an operator."""

from __future__ import annotations

import typing

import frappe
from frappe import _

from cargo.client_models import POSTGRES
from cargo.spawn import (
	MAX_SETUP_ATTEMPTS,
	machine_status,
	report_dead_machines,
	retry_setup,
	run_spawner,
	validate_node_size,
)

if typing.TYPE_CHECKING:
	from cargo.postgres.doctype.postgres_server.postgres_server import PostgresServer

CONFIG_KEY = "default_postgres_config"
LOCK_NAME = "postgres-spawn"
OPTIONAL_FIELDS = ("version", "max_connections")


def ensure_postgres() -> None:
	"""Give this region one Postgres and keep it moving. Off until `default_postgres_config`
	is in site config. Scheduled in `hooks.py`."""
	run_spawner(CONFIG_KEY, LOCK_NAME, validate_config, build_server)


def validate_config(config: dict) -> None:
	if not isinstance(config, dict):
		frappe.throw(_("{0} must be an object.").format(CONFIG_KEY))
	validate_node_size(config.get(POSTGRES), POSTGRES)
	if "version" in config and not str(config["version"]).strip().isdigit():
		frappe.throw(_("version must be a PostgreSQL major version, such as 16."))
	if "max_connections" in config and (
		not isinstance(config["max_connections"], int) or config["max_connections"] < 10
	):
		frappe.throw(_("max_connections must be a whole number of at least 10."))


def build_server(config: dict) -> None:
	server: PostgresServer = frappe.get_single("Postgres Server")

	if not server.machine and not server.auto_spawn:
		if frappe.db.exists("Webhook", "postgres_server") or server.setup_log:
			return  # filled in by hand at some point: left alone
		server.update({"auto_spawn": 1, **{f: config[f] for f in OPTIONAL_FIELDS if f in config}}).save(
			ignore_permissions=True
		)

	if not server.auto_spawn or server.status == "Setting Up":
		return

	if fill_machine(server, config):
		advance(server)


def fill_machine(server: PostgresServer, config: dict) -> bool:
	if server.machine:
		return not report_dead_machines(server, [server.machine])
	size = config[POSTGRES]
	try:
		server.create_postgres_node(
			cpu_millicores=size["cpu_millicores"], ram_gb=size["ram_gb"], disk_gb=size["disk_gb"]
		)
	except Exception:
		frappe.log_error(title="Postgres Server could not add its machine")
		return False
	return True


def advance(server: PostgresServer) -> None:
	if machine_status(server.machine) != "Running":
		return
	if server.status == "Draft":
		server.setup()
		return
	if server.status == "Failed":
		retry_setup(server)


__all__ = ["CONFIG_KEY", "MAX_SETUP_ATTEMPTS", "ensure_postgres", "validate_config"]
