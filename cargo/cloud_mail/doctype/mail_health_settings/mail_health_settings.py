# Copyright (c) 2026, Frappe Technologies Pvt Ltd and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document


class MailHealthSettings(Document):
	"""When a mail cluster counts as degraded. Region-wide: one cluster to a region."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		certificate_warn_days: DF.Int
		history_hours: DF.Int
		node_offline_seconds: DF.Int
		read_timeout_seconds: DF.Int
	# end: auto-generated types

	def validate(self) -> None:
		"""Zero is not "no wait" here: every probe times out at once, every blink is an outage."""
		for fieldname in (
			"node_offline_seconds",
			"certificate_warn_days",
			"read_timeout_seconds",
			"history_hours",
		):
			if self.get(fieldname) < 1:
				frappe.throw(
					_("{0} must be at least 1.").format(_(self.meta.get_label(fieldname))),
					frappe.ValidationError,
				)
