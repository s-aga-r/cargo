# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.postgres.doctype.postgres_server.postgres_server import WEBHOOK_NAME, PostgresServer
from cargo.testing import use_test_settings

MODULE = "cargo.postgres.doctype.postgres_server.postgres_server"


def reset_postgres_server() -> None:
	frappe.db.delete("Singles", {"doctype": "Postgres Server"})
	frappe.db.delete("Webhook", {"name": WEBHOOK_NAME})
	frappe.clear_document_cache("Postgres Server", "Postgres Server")


def make_machine(reference_doctype: str, reference_name: str, role: str, address: str = "fdaa:1::20"):
	return frappe.get_doc(
		{
			"doctype": "Machine",
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
			"role": role,
			"disk_size_gb": 50,
			"vm_id": f"vm-{frappe.generate_hash(length=6)}",
			"address": address,
			"status": "Running",
		}
	).insert()


class IntegrationTestPostgresServer(IntegrationTestCase):
	"""One Postgres for the region, on the mesh, set up by one script."""

	def setUp(self) -> None:
		frappe.set_user("Administrator")
		use_test_settings()
		reset_postgres_server()
		self.server: PostgresServer = frappe.get_single("Postgres Server")
		self.machine = make_machine("Postgres Server", "Postgres Server", "postgres")
		self.server.machine = self.machine.name
		self.server.save()

	def tearDown(self) -> None:
		reset_postgres_server()

	def test_the_admin_password_is_made_once_and_handed_to_the_install(self) -> None:
		password = self.server.get_password("admin_password")
		self.assertEqual(len(password), 32)
		environment = self.server.install_environment()
		self.assertEqual(environment["ADMIN_PASSWORD"], password)
		self.assertEqual(environment["LISTEN_ADDRESS"], "fdaa:1::20")
		self.assertEqual((environment["POSTGRES_VERSION"], environment["PORT"]), ("16", 5432))
		self.assertEqual(environment["MESH_NETWORK"], "fdaa::/16")
		self.server.save()
		self.assertEqual(self.server.get_password("admin_password"), password)

	def test_a_nonsense_shape_is_refused(self) -> None:
		for field, value in (("port", 0), ("max_connections", 5), ("version", "sixteen")):
			with self.subTest(field=field):
				self.server.set(field, value)
				self.assertRaises(frappe.ValidationError, self.server.save)
				self.server.reload()

	def test_the_install_runs_the_script_with_the_secret_masked(self) -> None:
		with patch(f"{MODULE}.run_over_ssh", return_value="ok") as ran:
			self.server._setup()

		self.assertIn('apt-get install -y -qq "postgresql-$POSTGRES_VERSION"', ran.call_args.args[1])
		self.assertIn("export ADMIN_ROLE=cargo", ran.call_args.args[1])
		self.assertEqual(ran.call_args.kwargs["secrets"], [self.server.get_password("admin_password")])
		self.server.reload()
		self.assertEqual(self.server.status, "Active")

	def test_a_failed_install_leaves_the_server_failed_with_a_reason(self) -> None:
		with patch(f"{MODULE}.run_over_ssh", side_effect=RuntimeError("boom")):
			self.server._setup()
		self.server.reload()
		self.assertEqual(self.server.status, "Failed")
		self.assertIn("Setup Log", self.server.error)

	def test_a_server_with_a_machine_gets_a_webhook_naming_postgres(self) -> None:
		webhook = frappe.get_doc("Webhook", WEBHOOK_NAME)
		self.assertIn('"service": "postgres"', webhook.webhook_json)
		self.assertIn("postgres://[fdaa:1::20]:5432", webhook.webhook_json)

	def test_a_server_whose_machine_died_is_failed(self) -> None:
		self.server.mark("Active")
		self.machine.db_set("status", "Terminated")
		self.server.sync_machines()
		self.server.reload()
		self.assertEqual(self.server.status, "Failed")
