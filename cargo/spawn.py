"""What every spawner needs: whether this host may build anything, and what to build."""

from __future__ import annotations

import typing

import frappe
from frappe import _
from frappe.utils.file_lock import LockTimeoutError
from frappe.utils.synchronization import filelock

from cargo.atlas_client import MAXIMUM_CPU_MILLICORES, MINIMUM_CPU_MILLICORES
from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES

if typing.TYPE_CHECKING:
	from collections.abc import Callable, Iterable
	from contextlib import AbstractContextManager

	from frappe.model.document import Document

# Without these nothing can be built, set up, or reported to Central. The same for every
# service: they all rent from Atlas, answer under the region's domain, and report in.
REQUIRED_SETTINGS = ("region", "wildcard_domain", "atlas_url", "central_url", "proxy_url")
REQUIRED_SECRETS = ("atlas_token", "central_webhook_secret", "proxy_token")
# Setting up again rents no machine, so a transient fault is worth another run. Three is
# where saying so beats trying again.
MAX_SETUP_ATTEMPTS = 3


def has_required_settings() -> bool:
	"""A half provisioned host is quiet rather than noisy: it is still being installed.

	A Password field reads back empty from the document, so the secrets are asked for by
	name rather than counted with the rest."""
	settings = frappe.get_cached_doc("Cargo Settings")
	if not all(settings.get(field) for field in REQUIRED_SETTINGS):
		return False

	return all(settings.get_password(field, raise_exception=False) for field in REQUIRED_SECRETS)


def spawn_config(config_key: str, validate: Callable[[dict], None]) -> dict | None:
	"""What to build, from site config. Absent means this region builds none, and a config
	that cannot be used is logged once and treated the same way."""
	config = frappe.conf.get(config_key)
	if not config:
		return None

	try:
		validate(config)
	except frappe.ValidationError as error:
		frappe.log_error(title=f"{config_key} is not usable", message=str(error))
		return None

	return config


def spawn_lock(name: str) -> AbstractContextManager:
	"""One run of a spawner at a time for this site."""
	return filelock(name, timeout=0)


def report(doc: Document, reason: str) -> None:
	"""Say why a spawn is stuck, once rather than on every run."""
	if doc.error != reason:
		doc.db_set("error", reason)


def machine_status(name: str) -> str:
	return frappe.db.get_value("Machine", name, "status")


def validate_node_size(size: object, role: str) -> None:
	"""A machine shape Atlas will accept. Throws, naming what is wrong."""
	if not isinstance(size, dict):
		frappe.throw(_("{0} must hold cpu_millicores, ram_gb and disk_gb.").format(role))

	for field in ("cpu_millicores", "ram_gb", "disk_gb"):
		if not isinstance(size.get(field), int) or isinstance(size[field], bool) or size[field] < 1:
			frappe.throw(_("{0}.{1} must be a whole number of at least 1.").format(role, field))

	if not MINIMUM_CPU_MILLICORES <= size["cpu_millicores"] <= MAXIMUM_CPU_MILLICORES:
		frappe.throw(
			_("{0}.cpu_millicores must be between {1} and {2}.").format(
				role, MINIMUM_CPU_MILLICORES, MAXIMUM_CPU_MILLICORES
			)
		)


def run_spawner(
	config_key: str, lock_name: str, validate: Callable[[dict], None], build: Callable[[dict], None]
) -> None:
	"""One run of a spawner: off until its config is in site config, quiet until the host is
	enrolled, and one at a time for this site."""
	config = spawn_config(config_key, validate)
	if not config or not has_required_settings():
		return

	try:
		with spawn_lock(lock_name):
			build(config)
	except LockTimeoutError:
		# Another run holds it and is already doing this work. Nothing here is urgent enough
		# to wait for: the next run picks up wherever that one leaves the region.
		return


def report_dead_machines(doc: Document, machines: Iterable[str]) -> bool:
	"""Say which machines never came up, once. True when there are any: a spawner stops
	there, because replacing a machine unattended is how it runs away with money, and one
	that would not boot is worth a look."""
	dead = sorted(name for name in machines if machine_status(name) in DEAD_MACHINE_STATES)
	if dead:
		report(
			doc,
			_("{0} did not come up. Release it, and Cargo asks Atlas for another.").format(", ".join(dead)),
		)

	return bool(dead)


def retry_setup(doc: Document) -> None:
	"""Run a failed bring-up again, counted on the record, until the attempts are spent. From
	there the record says how many runs it took and why the last one failed: it waits for a
	person."""
	if doc.auto_setup_attempts >= MAX_SETUP_ATTEMPTS:
		return

	doc.db_set("auto_setup_attempts", doc.auto_setup_attempts + 1)
	doc.setup()
