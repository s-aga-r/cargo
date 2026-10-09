# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.health.live import CRITICAL, DEGRADED, HEALTHY, UNKNOWN
from cargo.postgres.doctype.postgres_server.test_postgres_server import make_machine, reset_postgres_server
from cargo.postgres.health.live import LiveHealth
from cargo.testing import use_test_settings


class IntegrationTestPostgresHealth(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.set_user("Administrator")
		use_test_settings()
		reset_postgres_server()
		self.server = frappe.get_single("Postgres Server")
		self.server.machine = make_machine("Postgres Server", "Postgres Server", "postgres").name
		self.server.save()
		self.server.db_set("status", "Active")

	def tearDown(self) -> None:
		reset_postgres_server()

	def verdict(self, **query):
		with patch("cargo.postgres.health.live.client.query", **query):
			return LiveHealth(frappe.get_single("Postgres Server")).record()

	def test_a_server_that_answers_with_room_is_healthy(self) -> None:
		finding = self.verdict(return_value=[(12, 200)])
		self.assertEqual(finding.severity, HEALTHY)
		self.assertEqual(frappe.db.get_single_value("Postgres Server", "health"), HEALTHY)

	def test_a_server_near_its_connection_limit_is_degraded(self) -> None:
		finding = self.verdict(return_value=[(185, 200)])
		self.assertEqual((finding.severity, finding.reason), (DEGRADED, "185 of 200 connections in use"))

	def test_a_server_that_cannot_be_reached_is_critical(self) -> None:
		import psycopg2

		finding = self.verdict(side_effect=psycopg2.OperationalError("connection refused"))
		self.assertEqual(finding.severity, CRITICAL)
		self.assertIn("connection refused", finding.reason)

	def test_a_server_that_has_not_served_is_not_judged(self) -> None:
		self.server.db_set("status", "Draft")
		self.assertEqual(self.verdict(return_value=[(0, 200)]).severity, UNKNOWN)
