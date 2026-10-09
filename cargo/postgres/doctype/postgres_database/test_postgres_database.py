# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.postgres.doctype.postgres_server.test_postgres_server import make_machine, reset_postgres_server
from cargo.testing import use_test_settings


class IntegrationTestPostgresDatabase(IntegrationTestCase):
	"""A database and its owning role, made and unmade with quoted names."""

	def setUp(self) -> None:
		frappe.set_user("Administrator")
		use_test_settings()
		reset_postgres_server()
		frappe.db.delete("Postgres Database")
		server = frappe.get_single("Postgres Server")
		server.machine = make_machine("Postgres Server", "Postgres Server", "postgres").name
		server.save()
		server.db_set("status", "Active")
		frappe.clear_document_cache("Postgres Server", "Postgres Server")

	def tearDown(self) -> None:
		frappe.db.delete("Postgres Database")
		reset_postgres_server()

	def statements(self, run: MagicMock) -> list[str]:
		return list(run.call_args.args[1])

	def test_creating_one_makes_the_role_then_the_database_it_owns(self) -> None:
		with patch("cargo.postgres.doctype.postgres_database.postgres_database.run") as run:
			database = frappe.get_doc({"doctype": "Postgres Database", "database_name": "Stalwart"}).insert()

		self.assertEqual(
			(database.name, database.username, database.created_on_server), ("stalwart", "stalwart", 1)
		)
		password = database.get_password("password")
		self.assertEqual(len(password), 32)
		statements = self.statements(run)
		self.assertEqual(statements[0], f"CREATE ROLE \"stalwart\" LOGIN PASSWORD '{password}'")
		self.assertEqual(statements[1], 'CREATE DATABASE "stalwart" OWNER "stalwart"')
		connection = database.connection()
		self.assertEqual(
			(connection["host"], connection["port"], connection["user"]), ("fdaa:1::20", 5432, "stalwart")
		)
		self.assertEqual((connection["password"], connection["use_tls"]), (password, False))

	def test_a_name_postgres_would_choke_on_is_refused(self) -> None:
		for name in ("1st", "has-dash", "x" * 64, "Mixed Case"):
			with self.subTest(name=name):
				doc = frappe.get_doc({"doctype": "Postgres Database", "database_name": name})
				self.assertRaises(frappe.ValidationError, doc.insert)

	def test_rotating_changes_the_role_s_password_and_the_record(self) -> None:
		with patch("cargo.postgres.doctype.postgres_database.postgres_database.run"):
			database = frappe.get_doc({"doctype": "Postgres Database", "database_name": "mail"}).insert()
		before = database.get_password("password")
		with patch("cargo.postgres.doctype.postgres_database.postgres_database.run") as run:
			database.rotate_credentials()
		after = frappe.get_doc("Postgres Database", "mail").get_password("password")
		self.assertNotEqual(before, after)
		self.assertEqual(self.statements(run), [f"ALTER ROLE \"mail\" PASSWORD '{after}'"])

	def test_deleting_ends_its_connections_then_drops_both(self) -> None:
		with patch("cargo.postgres.doctype.postgres_database.postgres_database.run"):
			database = frappe.get_doc({"doctype": "Postgres Database", "database_name": "gone"}).insert()
		with patch("cargo.postgres.doctype.postgres_database.postgres_database.run") as run:
			database.delete()
		statements = self.statements(run)
		self.assertIn("pg_terminate_backend", statements[0])
		self.assertEqual(statements[1:], ['DROP DATABASE IF EXISTS "gone"', 'DROP ROLE IF EXISTS "gone"'])

	def test_nothing_is_made_while_the_server_is_not_active(self) -> None:
		frappe.db.set_single_value("Postgres Server", "status", "Draft")
		frappe.clear_document_cache("Postgres Server", "Postgres Server")
		with patch("cargo.postgres.doctype.postgres_database.postgres_database.run") as run:
			doc = frappe.get_doc({"doctype": "Postgres Database", "database_name": "early"})
			self.assertRaisesRegex(frappe.ValidationError, "not active", doc.insert)
		run.assert_not_called()
