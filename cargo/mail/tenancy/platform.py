"""The platform domain: the region's zone as a Mail Domain nobody owns, where every site gets
its send-only address, and whose records Cargo publishes itself."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document

from cargo.cargo.doctype.dns_record.dns_record import reconcile_managed_records
from cargo.mail.utils import log_exception

# Enough for a queue of outgoing mail; nothing is meant to stay in it.
PLATFORM_ADDRESS_QUOTA_GB = 0.5
PUBLISHED_TYPES = ("TXT", "MX")


def adopt_platform_domain(cluster: Document) -> Document:
	"""The cluster's default domain as Cargo's own Mail Domain. The cluster plan created it on
	Stalwart; here it is recorded, and its records published in Cargo's zone."""
	name = cluster.default_domain
	if frappe.db.exists("Mail Domain", name):
		domain = frappe.get_doc("Mail Domain", name)
	else:
		held = cluster.get_client().domains.find_by_name(name)
		if not held:
			frappe.throw(_("The cluster does not hold its default domain {0} yet.").format(name))
		domain = frappe.get_doc(
			{
				"doctype": "Mail Domain",
				"domain_name": name,
				"cluster": cluster.name,
				"description": "Platform domain",
				"holds_mailboxes": 1,
				"stalwart_id": held["id"],
			}
		)
		domain.flags.adopting = True
		domain.flags.skip_push = True
		domain.insert(ignore_permissions=True)
		domain.reload()
	publish_platform_records(domain)
	return domain


def publish_platform_records(domain: Document) -> None:
	"""The platform domain is in Cargo's zone, so what a customer would publish by hand is
	written through the zone's provider here. SPF at the apex is also the cluster's own row."""
	cluster = domain.get_cluster()
	rows = [
		{
			"dns_zone": cluster.dns_zone,
			"host": row.host,
			"type": row.record_type,
			"value": row.value,
			"priority": row.priority,
			"ttl": row.ttl,
			"category": "SPF" if row.category == "SPF" else "Platform",
		}
		for row in domain.dns_rows()
		if row.record_type in PUBLISHED_TYPES and row.category != "Ownership"
	]
	reconcile_managed_records("Mail Domain", domain.name, rows)


def is_platform_domain(domain: Document) -> bool:
	return domain.domain_name == frappe.get_cached_value("Stalwart Cluster", domain.cluster, "default_domain")


def platform_domain(cluster_name: str) -> Document | None:
	default_domain = frappe.get_cached_value("Stalwart Cluster", cluster_name, "default_domain")
	if not frappe.db.exists("Mail Domain", default_domain):
		return None
	return frappe.get_cached_doc("Mail Domain", default_domain)


def platform_local_part(site_name: str) -> str:
	"""`acme` for acme.frappe.cloud; the whole name, dashed, when another site took that."""
	label = site_name.split(".", 1)[0]
	taken = frappe.db.get_value("Mail Account", {"email": ["like", f"{label}@%"], "is_platform_address": 1})
	return label if not taken else site_name.replace(".", "-")


def ensure_platform_address(site: Document) -> Document | None:
	"""The site's send-only address on the platform domain, created once the domain is live.
	Nothing lands in it; a site sends as it and replies go to the human in Reply-To."""
	if site.send_only_account and frappe.db.exists("Mail Account", site.send_only_account):
		return frappe.get_doc("Mail Account", site.send_only_account)
	domain = platform_domain(site.cluster)
	if not domain or not domain.is_live():
		return None
	account = frappe.get_doc(
		{
			"doctype": "Mail Account",
			"email": f"{platform_local_part(site.name)}@{domain.name}",
			"site": site.name,
			"display_name": site.title or site.name,
			"is_platform_address": 1,
			"disable_receiving": 1,
		}
	)
	account.set_disk_quota_gb(PLATFORM_ADDRESS_QUOTA_GB)
	account.insert(ignore_permissions=True)
	site.db_set("send_only_account", account.name, update_modified=False)
	return account


def provide_platform_addresses() -> None:
	"""Hourly: sites created before the platform domain went live get their address now."""
	for name in frappe.get_all(
		"Mail Site", {"status": "Active", "send_only_account": ["is", "not set"]}, pluck="name"
	):
		site = frappe.get_doc("Mail Site", name)
		try:
			ensure_platform_address(site)
		except Exception:
			log_exception(f"Could not create the platform address of {name}", site)
