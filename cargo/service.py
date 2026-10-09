"""What every regional service shares: how it is named under the region's domain, routed
through the Proxy, marked, and reported to Central.

Plain functions rather than a mixin: the workflow engine finds a class's own tasks by name,
so an inherited `@task` would run but never show as a step."""

from __future__ import annotations

import typing

import frappe
from frappe import _

from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES
from cargo.proxy_client import ProxyClient

if typing.TYPE_CHECKING:
	from collections.abc import Iterable

	from frappe.integrations.doctype.webhook.webhook import Webhook
	from frappe.model.document import Document

	from cargo.cargo.doctype.cargo_settings.cargo_settings import CargoSettings

SOURCE_HEADER = "X-FC-Source"
REGION_HEADER = "X-FC-Region"
SOURCE = "cargo"
# The two states Central is told about; everything between them is Cargo's business.
REPORTED_STATUSES = ("Active", "Failed")
# Atlas's mesh prefix: what the Proxy forwards from, and all a service's nginx may trust.
MESH_NETWORK = "fdaa::/16"
TRUSTED_PROXIES = ("127.0.0.1", "::1", MESH_NETWORK)


def wildcard_domain() -> str:
	domain = frappe.db.get_single_value("Cargo Settings", "wildcard_domain", cache=True)
	if not domain:
		frappe.throw(_("Wildcard Domain must be set in Cargo Settings."))

	return domain


def service_domain(site_name: str) -> str:
	"""Where a service answers: one label below the region's wildcard domain."""
	return f"{site_name}.{wildcard_domain()}"


def service_endpoint(site_name: str) -> str:
	return f"https://{service_domain(site_name)}"


def publish_routes(domains: Iterable[str], address: str) -> None:
	"""Point each domain at a machine's mesh address through the regional Proxy."""
	client = ProxyClient.from_settings()
	for domain in domains:
		client.map_domain(domain, address)


def mark(doc: Document, status: str, error: str | None = None) -> None:
	doc.status = status
	doc.error = error
	doc.save()


def single_machine_sync(doc: Document) -> None:
	"""What a service on one machine makes of that machine settling. Its state is already
	recorded; `sync_pending_machines` calls this once it changes."""
	status = frappe.db.get_value("Machine", doc.machine, "status")
	if status in DEAD_MACHINE_STATES:
		mark(doc, "Failed", _("{0} is {1}.").format(doc.machine, status))


def configure_service_webhook(doc: Document, service: str, name: str, endpoint: str) -> None:
	"""Point a Frappe Webhook at Central so this service reports its own status changes."""
	configure_central_webhook(
		name,
		doc.doctype,
		"on_update",
		f"doc.status in {REPORTED_STATUSES}",
		{
			"service": service,
			"status": "{{ 'Available' if doc.status == 'Active' else 'Not Available' }}",
			"service_endpoint": endpoint,
		},
	)


def configure_central_webhook(name: str, doctype: str, docevent: str, condition: str, payload: dict) -> None:
	"""A Frappe Webhook that tells Central about `doctype` on `docevent`, carrying `payload` on top
	of the region. One receiver and one secret serve every delivery; both live on Cargo Settings,
	where Central's enrolment put them."""
	settings: CargoSettings = frappe.get_cached_doc("Cargo Settings")
	if not settings.central_webhook_url:
		raise frappe.ValidationError(
			_("Central has not enrolled this Cargo yet, so there is nowhere to report to.")
		)

	secret = settings.get_password("central_webhook_secret", raise_exception=True)
	webhook: Webhook = (
		frappe.get_doc("Webhook", name) if frappe.db.exists("Webhook", name) else frappe.new_doc("Webhook")
	)
	webhook.name = name
	webhook.update(
		{
			"webhook_doctype": doctype,
			"webhook_docevent": docevent,
			"request_url": settings.central_webhook_url,
			"request_method": "POST",
			"request_structure": "JSON",
			"condition": condition,
			"webhook_json": frappe.as_json(
				{"region": settings.region, "region_id": settings.region_id, **payload}
			),
			"webhook_headers": [
				{"key": SOURCE_HEADER, "value": SOURCE},
				{"key": REGION_HEADER, "value": settings.region},
			],
			"enable_security": True,
			"webhook_secret": secret,
			"enabled": settings.central_webhook_enabled,
		}
	)
	webhook.save(ignore_permissions=True)
