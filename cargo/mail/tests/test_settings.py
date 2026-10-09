import frappe
from frappe.tests import IntegrationTestCase

from cargo.mail.reports import DEFAULT_RETENTION_DAYS


class TestMailSettings(IntegrationTestCase):
	def setUp(self) -> None:
		self.settings = frappe.get_single("Mail Settings")

	def test_empty_retention_takes_the_default(self) -> None:
		self.settings.dmarc_report_retention_days = None
		self.settings.save()

		self.assertEqual(self.settings.dmarc_report_retention_days, DEFAULT_RETENTION_DAYS)

	def test_retention_under_a_day_is_refused(self) -> None:
		self.settings.tls_report_retention_days = 0
		self.assertRaises(frappe.ValidationError, self.settings.save)
