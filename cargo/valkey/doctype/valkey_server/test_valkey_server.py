# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.postgres.doctype.postgres_server.test_postgres_server import make_machine
from cargo.testing import use_test_settings
from cargo.valkey.doctype.valkey_server.valkey_server import WEBHOOK_NAME, ValkeyServer

MODULE = "cargo.valkey.doctype.valkey_server.valkey_server"


def reset_valkey_server() -> None:
	frappe.db.delete("Singles", {"doctype": "Valkey Server"})
	frappe.db.delete("Webhook", {"name": WEBHOOK_NAME})
	frappe.clear_document_cache("Valkey Server", "Valkey Server")


class IntegrationTestValkeyServer(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.set_user("Administrator")
		use_test_settings()
		reset_valkey_server()
		self.server: ValkeyServer = frappe.get_single("Valkey Server")
		self.machine = make_machine("Valkey Server", "Valkey Server", "valkey", address="fdaa:1::30")
		self.server.machine = self.machine.name
		self.server.save()

	def tearDown(self) -> None:
		reset_valkey_server()

	def test_the_install_is_told_the_release_the_address_and_the_default_password(self) -> None:
		environment = self.server.install_environment()
		self.assertEqual(
			(environment["VALKEY_VERSION"], environment["LISTEN_ADDRESS"], environment["PORT"]),
			("8.1.3", "fdaa:1::30", 6379),
		)
		self.assertEqual(environment["ADMIN_PASSWORD"], self.server.get_password("admin_password"))
		self.assertIn("{version}-noble-{arch}", environment["VALKEY_URL_TEMPLATE"])

	def test_a_nonsense_shape_is_refused(self) -> None:
		for field, value in (("port", 70000), ("max_memory_mb", 8), ("version", "8.1")):
			with self.subTest(field=field):
				self.server.set(field, value)
				self.assertRaises(frappe.ValidationError, self.server.save)
				self.server.reload()

	def test_the_install_runs_the_script_and_the_server_goes_active(self) -> None:
		with patch(f"{MODULE}.run_over_ssh", return_value="ok") as ran:
			self.server._setup()
		text = ran.call_args.args[1]
		self.assertIn("aclfile /etc/valkey/users.acl", text)
		self.assertIn("bind $LISTEN_ADDRESS", text)
		self.assertEqual(ran.call_args.kwargs["secrets"], [self.server.get_password("admin_password")])
		self.server.reload()
		self.assertEqual(self.server.status, "Active")

	def test_a_failed_install_leaves_the_server_failed(self) -> None:
		with patch(f"{MODULE}.run_over_ssh", side_effect=RuntimeError("boom")):
			self.server._setup()
		self.server.reload()
		self.assertEqual(self.server.status, "Failed")

	def test_the_webhook_names_valkey(self) -> None:
		webhook = frappe.get_doc("Webhook", WEBHOOK_NAME)
		self.assertIn('"service": "valkey"', webhook.webhook_json)
		self.assertIn("redis://[fdaa:1::30]:6379", webhook.webhook_json)
