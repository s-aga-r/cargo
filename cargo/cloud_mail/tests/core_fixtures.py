"""Shared builders for tests: settings, stores, clusters and nodes that never touch a network."""

from unittest.mock import patch

import frappe

from cargo.testing import TEST_ZONE, make_dns_zone

ROOT_DOMAIN = TEST_ZONE


def configure_settings(**overrides) -> None:
	settings = frappe.get_single("Mail Settings")
	settings.sign_with_ed25519 = 0  # tests assume the default; the local site may have it on
	settings.skip_domain_verification = 0  # likewise: a development cloud may have it on
	for key, value in overrides.items():
		settings.set(key, value)
	settings.save()
	frappe.clear_document_cache("Mail Settings", "Mail Settings")
	clear_request_cache()
	make_zone()


def make_zone(domain_name: str = ROOT_DOMAIN, **fields):
	"""The default zone every fixture cluster lives under."""

	return make_dns_zone(domain_name, **fields)


def clear_request_cache() -> None:
	cache = getattr(frappe.local, "request_cache", None)
	if cache is not None:
		cache.clear()


def no_dns_provider():
	"""DNS Record pushes go nowhere: the provider is unset, so records stay unverified."""

	return patch("cargo.cargo.doctype.dns_record.dns_record.get_dns_provider", return_value=None)
