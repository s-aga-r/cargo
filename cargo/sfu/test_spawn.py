# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.client_models import SFU
from cargo.sfu.doctype.sfu_server.sfu_server import SFUServer
from cargo.sfu.doctype.sfu_server.test_sfu_server import make_machine, reset_sfu_server
from cargo.sfu.spawn import CONFIG_KEY, ensure_sfu, validate_config
from cargo.testing import make_dns_zone, use_test_settings

CONFIG = {SFU: {"cpu_millicores": 4000, "ram_gb": 8, "disk_gb": 40}, "ssl_email": "ops@example.test"}


class IntegrationTestSFUSpawn(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.set_user("Administrator")
		use_test_settings()
		make_dns_zone()
		reset_sfu_server()

	def tearDown(self) -> None:
		reset_sfu_server()

	def test_the_config_is_checked(self) -> None:
		validate_config(CONFIG)
		for bad in ("text", {SFU: CONFIG[SFU]}, {**CONFIG, "workers": 0}):
			with self.subTest(bad=bad):
				self.assertRaises(frappe.ValidationError, validate_config, bad)

	def test_the_first_run_claims_the_record_and_asks_for_a_machine(self) -> None:
		with (
			patch.dict(frappe.local.conf, {CONFIG_KEY: {**CONFIG, "workers": 8}}),
			patch.object(SFUServer, "create_sfu_node") as made,
		):
			ensure_sfu()
		made.assert_called_once_with(cpu_millicores=4000, ram_gb=8, disk_gb=40)
		server = frappe.get_single("SFU Server")
		self.assertEqual((server.auto_spawn, server.workers, server.ssl_email), (1, 8, "ops@example.test"))

	def test_setup_waits_for_the_public_address(self) -> None:
		server = frappe.get_single("SFU Server")
		server.update(
			{"auto_spawn": 1, "ssl_email": "ops@example.test", "machine": make_machine().name}
		).save()
		with patch.dict(frappe.local.conf, {CONFIG_KEY: CONFIG}), patch.object(SFUServer, "setup") as setup:
			ensure_sfu()
		setup.assert_not_called()
		server.sync_machines()
		with patch.dict(frappe.local.conf, {CONFIG_KEY: CONFIG}), patch.object(SFUServer, "setup") as setup:
			ensure_sfu()
		setup.assert_called_once()
