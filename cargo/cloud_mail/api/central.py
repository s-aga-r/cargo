"""Central's calls: a Mail Site for every site Central places in this region, its limits and
entitlement, and its end. Central authenticates with a token carrying `mail:*`; a site's own
token is refused here."""

import frappe
from frappe import _
from frappe.utils import sbool

from cargo.auth import verify_token
from cargo.cloud_mail.doctype.stalwart_cluster.stalwart_cluster import region_cluster

SITE_FIELDS = (
	"title",
	"contact_email",
	"max_domains",
	"max_accounts",
	"max_groups",
	"max_mailing_lists",
	"max_disk_gb",
	"default_disk_quota_gb",
)


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@verify_token("mail:*")
def create_site(
	site: str,
	mailboxes_allowed: bool = True,
	ownership_token: str | None = None,
	title: str | None = None,
	contact_email: str | None = None,
	max_domains: int | None = None,
	max_accounts: int | None = None,
	max_groups: int | None = None,
	max_mailing_lists: int | None = None,
	max_disk_gb: float | None = None,
	default_disk_quota_gb: float | None = None,
) -> dict:
	"""`site` is Central's name for the site, the string its tokens carry. `ownership_token` is
	the team's, so one TXT record proves its domains in every region. A site that only sends
	has `mailboxes_allowed` off."""

	site = (site or "").strip().lower()
	if frappe.db.exists("Mail Site", site):
		frappe.throw(_("Site {0} already exists.").format(site), frappe.DuplicateEntryError)

	doc = frappe.get_doc(
		{
			"doctype": "Mail Site",
			"site_name": site,
			"cluster": region_cluster(),
			"mailboxes_allowed": int(sbool(mailboxes_allowed)),
			"domain_verification_token": ownership_token or None,
		}
	)
	apply(doc, locals())
	doc.insert(ignore_permissions=True)

	frappe.local.response["http_status_code"] = 201
	return doc.to_api()


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@verify_token("mail:*")
def get_site(site: str) -> dict:
	return load(site).to_api()


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST", "PUT"])
@verify_token("mail:*")
def update_site(
	site: str,
	mailboxes_allowed: bool | None = None,
	title: str | None = None,
	contact_email: str | None = None,
	max_domains: int | None = None,
	max_accounts: int | None = None,
	max_groups: int | None = None,
	max_mailing_lists: int | None = None,
	max_disk_gb: float | None = None,
	default_disk_quota_gb: float | None = None,
) -> dict:
	"""Changes the site's display name, contact address, limits or entitlement; omitted fields
	stay as they are."""

	doc = load(site)
	if mailboxes_allowed is not None:
		doc.mailboxes_allowed = int(sbool(mailboxes_allowed))
	apply(doc, locals())
	doc.save(ignore_permissions=True)
	return doc.to_api()


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@verify_token("mail:*")
def suspend_site(site: str) -> dict:
	doc = load(site)
	doc.stop()
	return doc.to_api()


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@verify_token("mail:*")
def resume_site(site: str) -> dict:
	doc = load(site)
	doc.restart()
	return doc.to_api()


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@verify_token("mail:*")
def archive_site(site: str, delete_data: bool = False) -> dict:
	doc = load(site)
	doc.retire(delete_data=bool(sbool(delete_data)))
	return doc.to_api()


def apply(doc, values: dict) -> None:
	for field in SITE_FIELDS:
		if values.get(field) is not None:
			doc.set(field, values[field])


def load(site: str):
	site = (site or "").strip().lower()
	if not frappe.db.exists("Mail Site", site):
		frappe.throw(_("Site {0} not found.").format(site), frappe.DoesNotExistError)
	return frappe.get_doc("Mail Site", site)
