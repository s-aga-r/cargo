"""Bringing a region its datum host without an operator."""

from __future__ import annotations

import typing

import frappe
from frappe import _

from cargo.client_models import TELEMETRY
from cargo.spawn import (
	MAX_SETUP_ATTEMPTS,
	machine_status,
	report_dead_machines,
	retry_setup,
	run_spawner,
	validate_node_size,
)

if typing.TYPE_CHECKING:
	from cargo.telemetry.doctype.datum_server.datum_server import DatumServer

CONFIG_KEY = "default_telemetry_config"
LOCK_NAME = "telemetry-spawn"
DATUM_FIELDS = ("repository", "version")


def ensure_telemetry() -> None:
	"""Give this region one datum host and keep it moving.

	Off until `default_telemetry_config` is in site config. Scheduled in `hooks.py`."""
	run_spawner(CONFIG_KEY, LOCK_NAME, validate_config, build_server)


def validate_config(config: dict) -> None:
	"""A shape Cargo can build a datum host from. Throws, naming what is wrong.

	Everything a Datum Server needs that has no default of its own, plus the machine to run
	it on: a host cannot be inserted without the first, or rented without the second."""
	if not isinstance(config, dict):
		frappe.throw(_("{0} must be an object.").format(CONFIG_KEY))

	for field in DATUM_FIELDS:
		if not isinstance(config.get(field), str) or not config[field].strip():
			frappe.throw(_("{0} must be set.").format(field))

	validate_node_size(config.get(TELEMETRY), TELEMETRY)


def build_server(config: dict) -> None:
	"""One step towards the region having a datum host that serves."""
	server: DatumServer = frappe.get_single("Datum Server")

	# The Single always exists; an empty one was never filled in.
	if not server.repository:
		provision(server, config)
		return

	# Filled in by hand: left alone.
	if not server.auto_spawn:
		return

	# A setup run is already under way, and it owns the host until it ends.
	if server.status == "Setting Up":
		return

	if fill_machine(server, config):
		advance(server)


def provision(server: DatumServer, config: dict) -> None:
	"""Fill the host in from site config; the machine comes on the next run."""
	server.update(
		{
			"auto_spawn": 1,
			**{field: config[field] for field in DATUM_FIELDS},
		}
	).save(ignore_permissions=True)


def fill_machine(server: DatumServer, config: dict) -> bool:
	"""Ask Atlas for the machine this host runs on. True once it has one."""
	if server.machine:
		return not report_dead_machines(server, [server.machine])

	size = config[TELEMETRY]
	try:
		server.create_telemetry_node(
			cpu_millicores=size["cpu_millicores"],
			ram_gb=size["ram_gb"],
			disk_gb=size["disk_gb"],
		)
	except Exception:
		frappe.log_error(title=f"{server.name} could not add its machine")
		return False

	return True


def advance(server: DatumServer) -> None:
	"""Set the host up once its machine is up, and try again if a run failed."""
	if machine_status(server.machine) != "Running":
		return

	if server.status == "Draft":
		server.setup()
		return

	if server.status != "Failed":
		return

	retry_setup(server)


__all__ = ["CONFIG_KEY", "DATUM_FIELDS", "MAX_SETUP_ATTEMPTS", "ensure_telemetry", "validate_config"]
