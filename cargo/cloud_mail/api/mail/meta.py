import frappe

from cargo.cloud_mail.api.site import current_site, site_api
from cargo.cloud_mail.stalwart import get_client
from cargo.cloud_mail.stalwart.directory import DISK_QUOTA

SCHEMA_CACHE_TTL = 3600


# nosemgrep: guest-whitelisted-method -- site_api verifies the caller's token.
@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@site_api
def get_account_options() -> dict:
	"""Locales and time zones the cluster accepts for accounts (from Stalwart's schema)."""

	cluster = current_site().get_cluster()
	key = f"suite_cloud:schema-enums:{cluster.name}"
	cached = frappe.cache.get_value(key)
	if cached:
		return cached

	client = get_client(cluster)
	schema = client.connection.request("GET", f"{cluster.base_url}/api/schema")
	enums = schema.get("enums") or {}
	options = {
		"locales": [_enum_entry(e) for e in enums.get("Locale") or []],
		"time_zones": [_enum_entry(e) for e in enums.get("TimeZone") or []],
		# Disk space has its own field and validation; the rest can be set as quota rows.
		"quotas": [
			_enum_entry(e)
			for e in enums.get("StorageQuota") or []
			if (e.get("name") if isinstance(e, dict) else e) != DISK_QUOTA
		],
	}
	frappe.cache.set_value(key, options, expires_in_sec=SCHEMA_CACHE_TTL)
	return options


def _enum_entry(entry) -> dict:
	"""Stalwart lists enum members as ``{name, label}``; a bare string is its own label."""

	if isinstance(entry, dict):
		value = entry.get("name") or entry.get("id")
		return {"value": value, "label": entry.get("label") or entry.get("description") or value}
	return {"value": entry, "label": entry}
