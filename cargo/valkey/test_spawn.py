# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.client_models import VALKEY
from cargo.postgres.doctype.postgres_server.test_postgres_server import make_machine
from cargo.testing import use_test_settings
from cargo.valkey.doctype.valkey_server.test_valkey_server import reset_valkey_server
from cargo.valkey.doctype.valkey_server.valkey_server import ValkeyServer
from cargo.valkey.spawn import CONFIG_KEY, ensure_valkey, validate_config

CONFIG = {VALKEY: {"cpu_millicores": 1000, "ram_gb": 2, "disk_gb": 10}}


class IntegrationTestValkeySpawn(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.set_user("Administrator")
		use_test_settings()
		reset_valkey_server()

	def tearDown(self) -> None:
		reset_valkey_server()

	def test_the_config_is_checked(self) -> None:
		validate_config(CONFIG)
		for bad in ("text", {}, {**CONFIG, "max_memory_mb": 8}):
			with self.subTest(bad=bad):
				self.assertRaises(frappe.ValidationError, validate_config, bad)

	def test_the_first_run_claims_the_record_and_asks_for_a_machine(self) -> None:
		with (
			patch.dict(frappe.local.conf, {CONFIG_KEY: {**CONFIG, "max_memory_mb": 2048}}),
			patch.object(ValkeyServer, "create_valkey_node") as made,
		):
			ensure_valkey()
		made.assert_called_once_with(cpu_millicores=1000, ram_gb=2, disk_gb=10)
		server = frappe.get_single("Valkey Server")
		self.assertEqual((server.auto_spawn, server.max_memory_mb), (1, 2048))

	def test_a_running_machine_starts_the_setup(self) -> None:
		server = frappe.get_single("Valkey Server")
		server.update(
			{"auto_spawn": 1, "machine": make_machine("Valkey Server", "Valkey Server", "valkey").name}
		).save()
		with (
			patch.dict(frappe.local.conf, {CONFIG_KEY: CONFIG}),
			patch.object(ValkeyServer, "setup") as setup,
		):
			ensure_valkey()
		setup.assert_called_once()
