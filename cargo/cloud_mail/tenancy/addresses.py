"""Address rules shared by accounts, groups and lists: syntax, ownership and uniqueness."""

import re

import frappe
from frappe import _
from frappe.utils import validate_email_address as frappe_validate_email

DOMAIN_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
ADDRESS_DOCTYPES = ("Mail Account", "Mail Group", "Mailing List")


def normalize_domain(value: str | None) -> str:
	"""Lowercase and IDNA-encoded, as domains are stored, without judging validity."""

	domain = (value or "").strip().lower().rstrip(".")
	try:
		return domain.encode("idna").decode()
	except UnicodeError:
		return domain


def validate_domain_name(value: str | None) -> str:
	domain = (value or "").strip().lower().rstrip(".")
	try:
		domain = domain.encode("idna").decode()
	except UnicodeError:
		frappe.throw(_("{0} is not a valid domain name.").format(value))

	labels = domain.split(".")
	if len(labels) < 2 or not all(DOMAIN_LABEL.match(label) for label in labels) or len(domain) > 253:
		frappe.throw(_("{0} is not a valid domain name.").format(value))
	return domain


def validate_email_address(value: str | None) -> str:
	email = (value or "").strip().lower()
	if not frappe_validate_email(email) or email.count("@") != 1:
		frappe.throw(_("{0} is not a valid email address.").format(value))
	local, domain = email.split("@", 1)
	if "%" in local or "/" in local:  # '%' is Stalwart's master-user separator
		frappe.throw(_("{0} is not a valid email address.").format(value))
	return f"{local}@{validate_domain_name(domain)}"


def get_site_domain(site: str, domain_name: str):
	"""The site's Mail Domain named ``domain_name``; other sites' domains are invisible (404)."""

	domain_name = normalize_domain(domain_name)
	domain = (
		frappe.get_cached_doc("Mail Domain", domain_name)
		if frappe.db.exists("Mail Domain", domain_name)
		else None
	)
	if domain is None or domain.site != site:
		frappe.throw(
			_("Domain {0} does not belong to this site.").format(domain_name), frappe.DoesNotExistError
		)
	return domain


def resolve_domain(site: str | None, email: str):
	"""The domain an address sits on: the site's own, or one nobody owns. Another site's is
	invisible (404)."""

	domain_name = email.split("@", 1)[1]
	domain = (
		frappe.get_cached_doc("Mail Domain", domain_name)
		if frappe.db.exists("Mail Domain", domain_name)
		else None
	)
	if domain is None or (domain.site and domain.site != site):
		frappe.throw(
			_("Domain {0} does not belong to this site.").format(domain_name), frappe.DoesNotExistError
		)
	return domain


def receiving_allowed(site: str | None, domain) -> bool:
	"""Whether mail may land here: the site is entitled to mailboxes and this region holds the
	domain's. Without a site only the domain decides."""

	if not domain.holds_mailboxes:
		return False
	return not site or bool(frappe.get_cached_value("Mail Site", site, "mailboxes_allowed"))


def assert_receiving_allowed(site: str | None, domain) -> None:
	"""Groups, lists and catch-alls exist to receive, so a site that cannot gets none."""

	if not receiving_allowed(site, domain):
		frappe.throw(_("Mailboxes are not available on {0} for this site.").format(domain.domain_name))


def assert_domain_live(domain) -> None:
	"""Accounts, groups and lists are created only on a domain that is enabled and verified."""

	if not domain.is_live():
		frappe.throw(
			_("Domain {0} is not active: enable it and verify its DNS records first.").format(
				domain.domain_name
			)
		)


def assert_domain_available(domain_name: str, site: str) -> None:
	"""Free for this site to add: unclaimed, and not a zone of the mail infrastructure itself."""

	if frappe.db.exists("Mail Domain", domain_name):
		if claimable(domain_name):
			# An archived site's disabled domain, held for its retention: the caller has proved
			# control of it, so the old holder's copy goes and the name is free.
			from cargo.cloud_mail.doctype.mail_domain.mail_domain import purge_domain

			purge_domain(domain_name)
			assert_domain_not_reserved(domain_name)
			return
		if frappe.db.get_value("Mail Domain", domain_name, "site") == site:
			frappe.throw(
				_("Domain {0} is already added to this site.").format(domain_name),
				frappe.DuplicateEntryError,
			)
		# Neutral on purpose: whoever holds the name, another site or Central, is not this
		# site's business.
		frappe.throw(_("Domain {0} is not available.").format(domain_name), frappe.DuplicateEntryError)
	assert_domain_not_reserved(domain_name)


def claimable(domain_name: str) -> bool:
	"""A disabled domain whose site is archived may be taken over by a site that proves control."""

	enabled, owner = frappe.db.get_value("Mail Domain", domain_name, ["enabled", "site"])
	return not enabled and bool(owner) and frappe.db.get_value("Mail Site", owner, "status") == "Archived"


def assert_domain_not_reserved(domain_name: str) -> None:
	for zone in frappe.get_all("DNS Zone", pluck="name"):
		if domain_name == zone or domain_name.endswith(f".{zone}"):
			frappe.throw(_("{0} is reserved for the mail infrastructure.").format(domain_name))


def assert_addresses_deliverable(site: str, emails: list[str]) -> None:
	"""Addresses a site may route mail to: its own, or ones outside the platform.

	An address under a domain that another site holds is refused as if it did not exist: a list
	or catch-all pointing at it would inject mail into that site's mailboxes, unseen by it. A
	domain nobody owns is foreign to every site, and a site's domain is foreign to it.
	"""

	domains = {e.split("@", 1)[1] for e in emails if "@" in e}
	if not domains:
		return
	foreign = {
		row.name
		for row in frappe.get_all(
			"Mail Domain", filters={"name": ["in", list(domains)]}, fields=["name", "site"]
		)
		if (row.site or "") != (site or "")
	}
	if foreign:
		offending = sorted(e for e in emails if e.split("@", 1)[1] in foreign)
		frappe.throw(_("Address {0} is not available.").format(offending[0]), frappe.DoesNotExistError)


def assert_address_available(email: str, exclude: tuple[str, str] | None = None) -> None:
	"""An address may be a primary address or an alias exactly once across the whole directory."""

	for doctype in ADDRESS_DOCTYPES:
		if doctype == (exclude or (None,))[0] and exclude[1] == email:
			continue
		if frappe.db.exists(doctype, email):
			frappe.throw(
				_("{0} is already a {1}.").format(email, _(doctype)),
				frappe.DuplicateEntryError,
			)

	alias = frappe.db.get_value(
		"Mail Address Alias", {"alias_email": email}, ["parenttype", "parent"], as_dict=True
	)
	if alias and (alias.parenttype, alias.parent) != exclude:
		frappe.throw(
			_("{0} is already an alias of {1} {2}.").format(email, _(alias.parenttype), alias.parent),
			frappe.DuplicateEntryError,
		)
