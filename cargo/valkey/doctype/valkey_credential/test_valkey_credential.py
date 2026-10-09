# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.postgres.doctype.postgres_server.test_postgres_server import make_machine
from cargo.testing import use_test_settings
from cargo.valkey.doctype.valkey_server.test_valkey_server import reset_valkey_server

COMMANDS = "cargo.valkey.doctype.valkey_credential.valkey_credential.commands"


class IntegrationTestValkeyCredential(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.set_user("Administrator")
		use_test_settings()
		reset_valkey_server()
		frappe.db.delete("Valkey Credential")
		server = frappe.get_single("Valkey Server")
		server.machine = make_machine("Valkey Server", "Valkey Server", "valkey", address="fdaa:1::30").name
		server.save()
		server.db_set("status", "Active")
		frappe.clear_document_cache("Valkey Server", "Valkey Server")

	def tearDown(self) -> None:
		frappe.db.delete("Valkey Credential")
		reset_valkey_server()

	def test_creating_one_adds_an_acl_user_with_a_password_and_saves_the_file(self) -> None:
		with patch(COMMANDS) as commands:
			credential = frappe.get_doc({"doctype": "Valkey Credential", "username": "Mail"}).insert()
		password = credential.get_password("password")
		self.assertEqual(
			commands.call_args.args[1],
			[("ACL", "SETUSER", "mail", "on", f">{password}", "~*", "&*", "+@all"), ("ACL", "SAVE")],
		)
		connection = credential.connection()
		self.assertEqual(connection["url"], f"redis://mail:{password}@[fdaa:1::30]:6379/0")

	def test_the_default_user_and_odd_names_are_refused(self) -> None:
		for name in ("default", "1st", "Has Space"):
			with self.subTest(name=name):
				doc = frappe.get_doc({"doctype": "Valkey Credential", "username": name})
				self.assertRaises(frappe.ValidationError, doc.insert)

	def test_rotating_and_deleting_reach_the_server(self) -> None:
		with patch(COMMANDS):
			credential = frappe.get_doc({"doctype": "Valkey Credential", "username": "mail"}).insert()
		before = credential.get_password("password")
		with patch(COMMANDS) as commands:
			credential.rotate_credentials()
		after = frappe.get_doc("Valkey Credential", "mail").get_password("password")
		self.assertNotEqual(before, after)
		self.assertEqual(commands.call_args.args[1][0], ("ACL", "SETUSER", "mail", "resetpass", f">{after}"))
		with patch(COMMANDS) as commands:
			credential.delete()
		self.assertEqual(commands.call_args.args[1], [("ACL", "DELUSER", "mail"), ("ACL", "SAVE")])

	def test_nothing_is_made_while_the_server_is_not_active(self) -> None:
		frappe.db.set_single_value("Valkey Server", "status", "Draft")
		frappe.clear_document_cache("Valkey Server", "Valkey Server")
		with patch(COMMANDS) as commands:
			doc = frappe.get_doc({"doctype": "Valkey Credential", "username": "early"})
			self.assertRaisesRegex(frappe.ValidationError, "not active", doc.insert)
		commands.assert_not_called()
