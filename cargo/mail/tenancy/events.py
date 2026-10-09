"""What Central hears about domains: one delivery per lifecycle moment, carrying the domain's
state rather than a diff, so Central's registry can be rebuilt from the last one."""

from __future__ import annotations

import frappe

from cargo.service import configure_central_webhook

STATE = {
	"kind": "domain",
	"domain": "{{ doc.domain_name }}",
	"site": "{{ doc.site or '' }}",
	"enabled": "{{ 1 if doc.enabled else 0 }}",
	"verified": "{{ 1 if doc.is_verified else 0 }}",
	"holds_mailboxes": "{{ 1 if doc.holds_mailboxes else 0 }}",
}
CHANGED = (
	'doc.site and (doc.has_value_changed("enabled") or doc.has_value_changed("is_verified") '
	'or doc.has_value_changed("holds_mailboxes"))'
)
WEBHOOKS = (
	("mail_domain-registered", "after_insert", "doc.site", "registered"),
	("mail_domain-changed", "on_update", CHANGED, "changed"),
	("mail_domain-purged", "on_trash", "doc.site", "purged"),
)


def configure_domain_webhooks() -> None:
	"""Three deliveries on Mail Domain. Only a site's domains are reported: the platform domain
	and Central's own are nobody's to register."""
	for name, docevent, condition, event in WEBHOOKS:
		configure_central_webhook(name, "Mail Domain", docevent, condition, {**STATE, "event": event})


def webhooks_configured() -> bool:
	return all(frappe.db.exists("Webhook", name) for name, *_ in WEBHOOKS)
