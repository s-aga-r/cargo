# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from cargo.cloud_mail.reports import DEFAULT_RETENTION_DAYS

DEFAULT_DOMAIN_RETENTION_DAYS = 90
DEFAULT_OWNERSHIP_MISS_LIMIT = 7


class MailSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		disabled_domain_retention_days: DF.Int
		dmarc_report_retention_days: DF.Int
		host_firewall: DF.Check
		ownership_miss_limit: DF.Int
		require_domain_grant: DF.Check
		sign_with_ed25519: DF.Check
		skip_domain_verification: DF.Check
		tls_report_retention_days: DF.Int
		verify_stalwart_tls: DF.Check
	# end: auto-generated types

	def validate(self) -> None:
		self.validate_report_retention()
		self.validate_retention("disabled_domain_retention_days", DEFAULT_DOMAIN_RETENTION_DAYS)
		self.validate_retention("ownership_miss_limit", DEFAULT_OWNERSHIP_MISS_LIMIT)

	def on_update(self) -> None:
		# Frappe drops the cached copy only after this hook, and the recheck below reads it.
		frappe.clear_document_cache(self.doctype, self.name)
		before = self.get_doc_before_save()
		if before and cint(before.skip_domain_verification) and not cint(self.skip_domain_verification):
			from cargo.cloud_mail.doctype.mail_domain.mail_domain import (
				enqueue_recheck_of_vouched_domains,
			)

			enqueue_recheck_of_vouched_domains()

	def validate_report_retention(self) -> None:
		for field in ("dmarc_report_retention_days", "tls_report_retention_days"):
			self.validate_retention(field, DEFAULT_RETENTION_DAYS)

	def validate_retention(self, field: str, default: int) -> None:
		# A site set up before a field existed has it empty: the default applies rather than a
		# refusal to save anything else. An explicit value under one is still a mistake.
		if self.get(field) in (None, ""):
			self.set(field, default)
		elif cint(self.get(field)) < 1:
			frappe.throw(_("{0} must be at least 1.").format(_(self.meta.get_label(field))))
