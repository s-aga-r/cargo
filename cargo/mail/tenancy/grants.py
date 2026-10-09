"""Central's say on who may add a domain: a short-lived token naming the site, the domain and
whether this region holds its mailboxes. Central's registry decides; Cargo only checks."""

from __future__ import annotations

import frappe
from frappe import _

from cargo.auth import token_claims, token_is_coherent, token_scopes

GRANT_SCOPE = "mail:domain"


def required() -> bool:
	return bool(frappe.get_cached_doc("Mail Settings").require_domain_grant)


def verified_grant(grant: str, site: str, domain: str) -> frappe._dict:
	"""The grant's claims once it verifies and names this site and this domain."""
	claims = token_claims(grant)
	if claims is None or not token_is_coherent(claims) or GRANT_SCOPE not in token_scopes(claims):
		frappe.throw(_("The domain grant is not one Cargo accepts."), frappe.PermissionError)
	if claims.get("site") != site or (claims.get("domain") or "").strip().lower() != domain:
		frappe.throw(_("The domain grant names another site or domain."), frappe.PermissionError)
	return frappe._dict(claims)
