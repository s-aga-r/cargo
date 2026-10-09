"""Site-facing API: what a site may do with its own slice of the directory.

A site calls with a token Central minted for it, carrying the `mail` scope and the site's
name in the `site` claim; Cargo verifies it against the key set it already trusts. Every
endpoint then touches only documents owned by that site. Objects of other sites are reported
as missing, never as forbidden.
"""

import functools
import json
from collections.abc import Callable
from typing import Any

import frappe
from frappe import _
from frappe.query_builder.functions import Count
from frappe.utils import cint

from cargo.auth import SITE_CLAIM, SITE_SCOPE, verify_token
from cargo.mail.stalwart.errors import StalwartRejectedError, StalwartUnauthorizedError

OWNED_DOCTYPES = {"Mail Domain", "Mail Account", "Mail Group", "Mailing List"}
RATE_LIMIT = 300  # requests per site per minute
ALIAS_CAP = 100  # aliases on one object; more than that is a list, not an account
MEMBERSHIP_CAP = 500  # groups or members named in one request
RECIPIENT_BATCH = 5000  # recipients added or removed in one request


class SiteAuthError(frappe.AuthenticationError):
	pass


class SiteSuspendedError(frappe.PermissionError):
	pass


class StalwartRejected(frappe.ValidationError):
	"""Stalwart refused the change; the type/description are safe to show the caller."""

	http_status_code = 422


class ClusterMisconfiguredError(frappe.ValidationError):
	"""Cargo's own credentials for the cluster are wrong: an operator problem."""

	http_status_code = 502


def current_site():
	"""The Mail Site behind this request (cached for the request)."""

	if site := getattr(frappe.local, "mail_site", None):
		return site

	site = _resolve_site()
	frappe.local.mail_site = site
	return site


def _resolve_site():
	"""The site named by the token's `site` claim. Nothing else names one: operators work on
	the desk, not through this API."""

	claims = getattr(frappe.local, "request_claims", None) or {}
	name = claims.get(SITE_CLAIM)
	if not name or not frappe.db.exists("Mail Site", name):
		raise SiteAuthError(_("Site authentication failed."))

	site = frappe.get_cached_doc("Mail Site", name)
	throttle(f"site:{site.name}")  # counted before any refusal, so refusals cannot be free
	if site.status == "Suspended":
		# Told apart from a bad token on purpose: the site learns why it is being refused.
		raise SiteSuspendedError(_("Site {0} is suspended.").format(site.name))
	if site.status != "Active" or not site.enabled:
		# Gone for good, or switched off: the token names a site this region no longer serves.
		raise SiteAuthError(_("Site authentication failed."))
	return site


def throttle(subject: str) -> None:
	"""A fixed one-minute window per subject (a site, or an address that failed to authenticate)."""

	if not getattr(frappe.local, "request", None):
		return

	window = frappe.utils.now_datetime().strftime("%Y%m%d%H%M")
	key = frappe.cache.make_key(f"cargo:mail:ratelimit:{subject}:{window}")
	count = frappe.cache.incr(key)
	if count == 1:
		frappe.cache.expire(key, 90)
	if count > RATE_LIMIT:
		raise frappe.TooManyRequestsError(
			_("Rate limit of {0} requests per minute exceeded.").format(RATE_LIMIT)
		)


def site_api(fn: Callable) -> Callable:
	"""Verifies the site's token, resolves and throttles the site, and turns Stalwart errors
	into API-shaped exceptions."""

	@functools.wraps(fn)
	def wrapper(*args, **kwargs):
		current_site()  # resolves and throttles once per request
		try:
			return fn(*args, **kwargs)
		except StalwartRejectedError as e:
			raise StalwartRejected(_("The mail server rejected the change: {0}").format(_describe(e))) from e
		except StalwartUnauthorizedError as e:
			frappe.log_error(title="Cluster credentials rejected", message=str(e))
			raise ClusterMisconfiguredError(_("The mail cluster refused Cargo's credentials.")) from e

	return verify_token(SITE_SCOPE)(wrapper)


def _describe(error: StalwartRejectedError) -> str:
	if error.error_type and error.description:
		return f"{error.error_type} ({error.description})"
	return error.error_type or error.description or "unknown error"


def normalize_name(name: str | None) -> str:
	"""A domain or address the way it is stored: lowercase, the domain part IDNA-encoded."""

	name = (name or "").strip().lower()
	local, at, domain = name.rpartition("@")
	try:
		domain = domain.encode("idna").decode()
	except UnicodeError:
		pass  # a bad domain simply does not match anything
	return f"{local}@{domain}" if at else domain


def owned(doctype: str, name: str, for_update: bool = False):
	"""Loads one of the site's documents; anything else is a 404.

	``for_update`` locks the row until the request commits, for read-modify-write changes such
	as adding one alias, so two concurrent edits cannot drop each other's rows.
	"""

	site = current_site()
	if doctype not in OWNED_DOCTYPES:
		raise ValueError(doctype)

	name = normalize_name(name)
	doc = None
	if name and frappe.db.exists(doctype, name):
		doc = frappe.get_doc(doctype, name, for_update=for_update)
	if doc is None or doc.site != site.name:
		raise frappe.DoesNotExistError(_("{0} {1} not found.").format(_(doctype), name))
	return doc


def owned_names(doctype: str, filters: dict | None = None, **kwargs) -> list[str]:
	filters = {"site": current_site().name, **(filters or {})}
	return frappe.get_all(doctype, filters=filters, pluck="name", order_by="name asc", **kwargs)


def owned_page(
	doctype: str,
	search: str | None,
	start: Any,
	limit: Any,
	cap: int,
	search_fields: tuple[str, ...] = ("name", "description"),
	filters: dict | None = None,
	order_by: str = "name asc",
) -> tuple[list[str], int]:
	"""One page of the site's document names by name, plus how many match in all."""

	filters = {"site": current_site().name, **(filters or {})}
	or_filters = None
	if search and search.strip():
		like = f"%{search.strip()}%"
		or_filters = [[field, "like", like] for field in search_fields]
	total = frappe.qb.get_query(
		doctype, filters=filters, or_filters=or_filters, fields=Count("*"), distinct=True
	).run()[0][0]
	names = frappe.get_all(
		doctype,
		filters=filters,
		or_filters=or_filters,
		pluck="name",
		order_by=order_by,
		limit_start=max(cint(start), 0),
		limit_page_length=page_size(limit, cap),
	)
	return names, cint(total)


def as_alias_rows(value: Any) -> list[dict]:
	"""Aliases arrive as addresses, or as ``{email, enabled, description}`` objects; rows come out.

	A JSON string is accepted too, so a form can post either shape.
	"""

	if isinstance(value, str) and value.strip().startswith("["):
		value = frappe.parse_json(value)
	if isinstance(value, list) and len(value) > ALIAS_CAP:
		frappe.throw(_("At most {0} aliases per request.").format(ALIAS_CAP))
	rows = []
	for item in as_list(value, ALIAS_CAP) if not isinstance(value, list) else value:
		if isinstance(item, dict):
			email = str(item.get("email") or item.get("alias_email") or "").strip()
			if not email:
				continue
			rows.append(
				{
					"alias_email": email,
					"enabled": int(bool(item.get("enabled", True))),
					"description": item.get("description") or None,
				}
			)
		elif str(item).strip():
			rows.append({"alias_email": str(item).strip(), "enabled": 1, "description": None})
	return rows


def page_size(limit: Any, cap: int) -> int:
	"""A page length between 1 and ``cap``; Frappe reads 0 as no limit, which would return every row."""

	try:
		wanted = int(limit)
	except (TypeError, ValueError):
		wanted = cap
	return max(1, min(wanted, cap))


def as_list(value: Any, cap: int | None = None) -> list[str]:
	"""Accepts a JSON list, a comma/newline separated string or None; ``cap`` bounds one request."""

	items = _as_list(value)
	if cap is not None and len(items) > cap:
		frappe.throw(_("At most {0} entries per request.").format(cap))
	return items


def _as_list(value: Any) -> list[str]:
	if value is None:
		return []
	if isinstance(value, str):
		text = value.strip()
		if text.startswith("["):
			# Form-encoded clients (FrappeClient, query strings) send lists as JSON text.
			try:
				value = json.loads(text)
			except ValueError:
				frappe.throw(_("Expected a list."))
		else:
			value = text.replace("\n", ",").split(",")
	return [str(v).strip() for v in value if str(v).strip()]


# nosemgrep: guest-whitelisted-method -- site_api verifies the caller's token.
@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@site_api
def ping() -> dict:
	"""Confirms the credentials and returns where the site's mail lives."""

	return current_site().to_api()


# nosemgrep: guest-whitelisted-method -- site_api verifies the caller's token.
@frappe.whitelist(allow_guest=True, methods=["POST"])
@site_api
def update_site_profile(title: str | None = None, contact_email: str | None = None) -> dict:
	"""What the site says about itself: its workspace name as the title, and where to reach it.

	Only the fields passed change; an empty string clears the contact and resets the title to the
	site name.
	"""

	site = current_site()
	if title is not None:
		site.title = title.strip()
	if contact_email is not None:
		contact_email = contact_email.strip().lower()
		if contact_email:
			frappe.utils.validate_email_address(contact_email, throw=True)
		site.contact_email = contact_email or None
	site.save(ignore_permissions=True)
	return site.to_api()
