# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
import redis
from frappe.tests import IntegrationTestCase

from cargo.health.live import CRITICAL, DEGRADED, HEALTHY
from cargo.postgres.doctype.postgres_server.test_postgres_server import make_machine
from cargo.testing import use_test_settings
from cargo.valkey.doctype.valkey_server.test_valkey_server import reset_valkey_server
from cargo.valkey.health.live import LiveHealth


class IntegrationTestValkeyHealth(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.set_user("Administrator")
		use_test_settings()
		reset_valkey_server()
		server = frappe.get_single("Valkey Server")
		server.machine = make_machine("Valkey Server", "Valkey Server", "valkey", address="fdaa:1::30").name
		server.save()
		server.db_set("status", "Active")

	def tearDown(self) -> None:
		reset_valkey_server()

	def verdict(self, **command):
		with patch("cargo.valkey.health.live.client.command", **command):
			return LiveHealth(frappe.get_single("Valkey Server")).record()

	def test_a_server_with_room_is_healthy(self) -> None:
		# redis-py hands INFO back parsed, as a dict
		self.assertEqual(
			self.verdict(return_value={"used_memory": 100 * 2**20, "maxmemory": 1024 * 2**20}).severity,
			HEALTHY,
		)

	def test_a_server_evicting_is_degraded(self) -> None:
		finding = self.verdict(return_value={"used_memory": 950 * 2**20, "maxmemory": 1024 * 2**20})
		self.assertEqual(finding.severity, DEGRADED)
		self.assertIn("950 of 1024 MB", finding.reason)

	def test_an_unreachable_server_is_critical(self) -> None:
		finding = self.verdict(side_effect=redis.ConnectionError("refused"))
		self.assertEqual(finding.severity, CRITICAL)
		self.assertIn("refused", finding.reason)
