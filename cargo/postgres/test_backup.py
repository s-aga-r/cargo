# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.postgres import backup
from cargo.postgres.doctype.postgres_server.test_postgres_server import make_machine, reset_postgres_server
from cargo.testing import use_test_settings


class IntegrationTestPostgresBackup(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.set_user("Administrator")
		use_test_settings()
		reset_postgres_server()
		frappe.db.delete("Postgres Database")
		self.server = frappe.get_single("Postgres Server")
		self.server.machine = make_machine("Postgres Server", "Postgres Server", "postgres").name
		self.server.save()
		self.server.db_set("status", "Active")
		with (
			patch("cargo.postgres.doctype.postgres_database.postgres_database.run"),
			patch("cargo.postgres.doctype.postgres_database.postgres_database.query", return_value=[]),
		):
			frappe.get_doc({"doctype": "Postgres Database", "database_name": "stalwart"}).insert()

	def tearDown(self) -> None:
		frappe.db.delete("Postgres Database")
		reset_postgres_server()

	def bucket(self):
		credential = MagicMock(access_key="GK1")
		credential.get_password.return_value = "shh"
		return frappe._dict(
			cluster="OSC-0001",
			bucket_name="postgres-backups",
			name="postgres-backups",
			bucket_credentials=[credential],
		)

	def test_every_database_is_dumped_to_the_bucket_and_old_dumps_pruned(self) -> None:
		cluster = frappe._dict(service_endpoint="https://s3.example.test", region="blr")
		with (
			patch("cargo.postgres.backup.backup_bucket", return_value=self.bucket()),
			patch("cargo.postgres.backup.frappe.get_cached_doc", return_value=cluster),
			patch("cargo.postgres.backup.run_over_ssh", return_value="dumped stalwart") as ran,
			patch("cargo.postgres.backup.s3_client") as s3,
			patch("cargo.postgres.backup.prune_dumps") as prune,
		):
			backup.backup_databases()

		text = ran.call_args.args[1]
		self.assertIn("export DATABASES=stalwart", text)
		self.assertIn("export S3_ENDPOINT=https://s3.example.test", text)
		self.assertIn("--aws-sigv4", text)
		self.assertEqual(ran.call_args.kwargs["secrets"], ["shh"])
		prune.assert_called_once_with(
			s3.return_value, "stalwart/", backup.RETENTION_DAYS, bucket="postgres-backups"
		)

	def test_nothing_runs_while_the_region_has_no_storage(self) -> None:
		with (
			patch("cargo.postgres.backup.backup_bucket", return_value=None),
			patch("cargo.postgres.backup.run_over_ssh") as ran,
		):
			backup.backup_databases()
		ran.assert_not_called()
