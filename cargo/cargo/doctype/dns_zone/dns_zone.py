# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from cargo.dns.provider import DNSProvider
from cargo.mail.utils import password_or_none

# A change to any of these means the stored provider access may no longer work.
PROVIDER_FIELDS = (
	"dns_provider",
	"dns_provider_access_key",
	"dns_provider_access_secret",
	"dns_provider_token",
	"dns_provider_username",
	"dns_provider_client_ip",
	"dns_provider_key",
	"dns_provider_secret",
	"dns_provider_zone_id",
)


DEFAULT_TTL = 300


class DNSZone(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		default_ttl: DF.Int
		dns_provider: DF.Literal[
			"", "AmazonRoute53", "DigitalOcean", "Cloudflare", "Hetzner", "Linode", "Namecheap", "GoDaddy"
		]
		dns_provider_access_key: DF.Data | None
		dns_provider_access_secret: DF.Password | None
		dns_provider_client_ip: DF.Data | None
		dns_provider_key: DF.Data | None
		dns_provider_private_zone: DF.Check
		dns_provider_secret: DF.Password | None
		dns_provider_token: DF.Password | None
		dns_provider_username: DF.Data | None
		dns_provider_zone_id: DF.Data | None
		domain_name: DF.Data
		enabled: DF.Check
	# end: auto-generated types

	def before_insert(self) -> None:
		self.normalise_domain_name()  # the name is taken from the field before validate runs

	def validate(self) -> None:
		self.normalise_domain_name()
		self.validate_provider()

	def normalise_domain_name(self) -> None:
		self.domain_name = (self.domain_name or "").strip().lower().rstrip(".")
		if not self.domain_name or "." not in self.domain_name:
			frappe.throw(_("Domain Name must be a registrable domain, e.g. frappemail.com."))

	def validate_provider(self) -> None:
		"""Checks the provider credentials are complete and, when they changed, that they work."""

		if not self.dns_provider:
			return
		self.validate_credentials()
		if self.flags.skip_dns_provider_verification:
			return
		if any(self.has_value_changed(field) for field in PROVIDER_FIELDS):
			self.get_provider().read_dns_records("MX")

	def validate_credentials(self) -> None:
		match self.dns_provider:
			case "AmazonRoute53":
				if not self.dns_provider_access_key or not self.dns_provider_access_secret:
					frappe.throw(_("Please set the DNS Provider Access Key and Secret."))

			case "DigitalOcean" | "Cloudflare" | "Hetzner" | "Linode" | "Namecheap":
				if not self.dns_provider_token:
					frappe.throw(_("Please set the DNS Provider Token."))
				elif self.dns_provider == "Namecheap":
					if not self.dns_provider_username or not self.dns_provider_client_ip:
						frappe.throw(_("Please set the DNS Provider Username and Client IP."))

			case "GoDaddy":
				if not self.dns_provider_key or not self.dns_provider_secret:
					frappe.throw(_("Please set the DNS Provider Key and Secret."))

	# --- provider access ------------------------------------------------------

	def get_provider(self) -> DNSProvider | None:
		"""The Lexicon client for this zone, or None when records are published by hand."""

		if not self.dns_provider:
			return None

		return DNSProvider(
			provider=self.dns_provider,
			domain=self.domain_name,
			access_key=self.dns_provider_access_key,
			access_secret=password_or_none(self, "dns_provider_access_secret"),
			auth_key=self.dns_provider_key,
			auth_secret=password_or_none(self, "dns_provider_secret"),
			username=self.dns_provider_username,
			token=password_or_none(self, "dns_provider_token"),
			client_ip=self.dns_provider_client_ip,
			zone_id=self.dns_provider_zone_id,
			private_zone=bool(self.dns_provider_private_zone),
		)


def settings_zone() -> str | None:
	"""The zone Cargo publishes its service records in, named on Cargo Settings."""

	return frappe.db.get_single_value("Cargo Settings", "dns_zone")


APEX = "@"


def relative_host(fqdn: str, zone: str) -> str:
	"""``n1.mail.blr.example`` -> ``n1`` in zone ``mail.blr.example``; the zone itself is ``@``."""

	if fqdn == zone:
		return APEX
	suffix = f".{zone}"
	if not fqdn.endswith(suffix):
		frappe.throw(_("{0} is not under the DNS zone {1}.").format(fqdn, zone))
	return fqdn[: -len(suffix)]
