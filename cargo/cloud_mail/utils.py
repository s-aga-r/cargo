import re

import frappe
from frappe import _
from frappe.utils import cint


def dkim_algorithms() -> tuple[str, ...]:
	"""The key types Stalwart generates for a domain registered now: RSA always, Ed25519 by choice.

	Read at registration time only; Stalwart keeps the algorithms a domain was created with.
	"""

	from cargo.cloud_mail.stalwart.directory import DKIM_ED25519, DKIM_RSA

	settings = frappe.get_cached_doc("Mail Settings")
	return (DKIM_ED25519, DKIM_RSA) if cint(settings.sign_with_ed25519) else (DKIM_RSA,)


def utc_iso(value) -> str | None:
	"""A stored datetime as an aware UTC string (``2026-09-09T10:00:00Z``) for API payloads.

	Frappe stores naive system-time values; handing them out naive lets a site in another time
	zone read them as its own local time and show them hours off.
	"""

	if not value:
		return None
	from datetime import UTC
	from zoneinfo import ZoneInfo

	moment = frappe.utils.get_datetime(value)
	if moment.tzinfo is None:
		moment = moment.replace(tzinfo=ZoneInfo(frappe.utils.get_system_timezone()))
	return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def password_or_none(doc, field: str) -> str | None:
	"""Returns the decrypted password if the field is set, otherwise None."""

	return doc.get_password(field) if doc.get(field) else None


def log_exception(title: str, doc=None) -> None:
	"""The current exception's traceback, without local variables.

	Frappe's default adds every frame's locals and masks only names that look like secrets; a
	connection or plan object holding a password would go in whole. The plain traceback carries
	what is needed to find the fault.
	"""

	frappe.log_error(
		title=title,
		message=frappe.get_traceback(),
		reference_doctype=doc.doctype if doc is not None else None,
		reference_name=doc.name if doc is not None else None,
	)


def child_rows(doctype: str, parenttype: str, parents: list[str], fields: list[str]) -> dict[str, list]:
	"""``{parent: [rows]}`` for a child table of many parents at once, rows in their saved order.

	A page of documents would otherwise cost one query per document per child table.
	"""

	rows: dict[str, list] = {parent: [] for parent in parents}
	if not parents:
		return rows
	for row in frappe.get_all(
		doctype,
		filters={"parenttype": parenttype, "parent": ["in", parents]},
		fields=["parent", *fields],
		order_by="parent asc, idx asc",
	):
		rows[row.parent].append(row)
	return rows


def alias_payloads(rows: list) -> list[dict]:
	return [{"email": r.alias_email, "enabled": bool(r.enabled), "description": r.description} for r in rows]


VERSION = re.compile(r"^v?\d+\.\d+\.\d+$")


def validate_version(value: str | None, label: str) -> str | None:
	"""A release tag such as v0.16.20; it is interpolated into a download URL, so nothing else."""

	value = (value or "").strip()
	if not value:
		return None
	if not VERSION.match(value):
		frappe.throw(_("{0} must be a version such as v0.16.20.").format(label))
	return value
