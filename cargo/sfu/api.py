"""What Central asks the SFU service for: the URL and secret a site is configured with."""

from __future__ import annotations

import frappe
from frappe import _

from cargo.auth import verify_token


# nosemgrep: guest-whitelisted-method -- verify_token authenticates the caller below.
@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@verify_token("sfu:*")
def get_credential() -> dict:
	"""`sfu_server_url` and `sfu_secret` for a site's config. Only an SFU that serves has them
	to give; Central pushes them to the site the way it does the mail credential."""
	server = frappe.get_single("SFU Server")
	if server.status != "Active":
		frappe.throw(_("The region has no SFU serving yet."), frappe.DoesNotExistError)
	return server.credential()
