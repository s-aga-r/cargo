"""The smoke check's Cargo half runs against a fixture cluster and says what it found."""

import contextlib
import io
from unittest.mock import patch

import frappe

from cargo.mail import smoke
from cargo.mail.tests.test_tenancy import TenancyTestCase


class TestSmokeChecks(TenancyTestCase):
	def run_checks(self, phase: int = 3) -> str:
		out = io.StringIO()
		with (
			contextlib.redirect_stdout(out),
			patch("cargo.mail.health.live.probe_ready", return_value=""),
			patch("cargo.mail.health.live.certificate_days_left", return_value=60),
			patch(
				"cargo.mail.doctype.stalwart_node.stalwart_node.StalwartNode.verify_ptr",
				return_value=True,
			),
		):
			smoke.checks(self.cluster.name, phase)
		return out.getvalue()

	def test_every_check_reports_and_the_count_closes_the_report(self) -> None:
		report = self.run_checks()
		lines = report.strip().splitlines()
		self.assertTrue(lines[-1].startswith("failed="))
		stray = [line for line in lines[:-1] if not line.startswith(("ok   ", "FAIL "))]
		self.assertEqual(stray, [])
		self.assertIn("ok    the cluster is active", report)
		self.assertIn("FAIL  the platform domain is adopted and verified", report)  # nothing adopted it here
		self.assertIn("FAIL  three or more nodes serve", self.run_checks(phase=6))

	def test_facts_name_what_the_network_is_asked_about(self) -> None:
		out = io.StringIO()
		with contextlib.redirect_stdout(out):
			smoke.facts(self.cluster.name)
		self.assertIn(f"hostname={self.cluster.hostname}", out.getvalue())
		self.assertIn(f"spf_include=spf.{self.cluster.default_domain}", out.getvalue())

	def test_an_unknown_cluster_is_refused(self) -> None:
		self.assertRaises(frappe.ValidationError, smoke.checks, "nope")
