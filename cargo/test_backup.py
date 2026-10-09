# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo import backup
from cargo.object_storage.doctype.bucket.bucket import Bucket
from cargo.testing import use_test_settings


class IntegrationTestDatabaseBackup(IntegrationTestCase):
	"""Cargo's own database dumped to the region's storage, and only there."""

	def setUp(self) -> None:
		frappe.set_user("Administrator")
		use_test_settings()
		frappe.db.delete("Bucket", {"bucket_name": backup.BUCKET_NAME})

	def dump(self, directory: Path) -> MagicMock:
		database = directory / "x-database.sql.gz"
		database.write_text("dump")
		config = directory / "x-site_config_backup.json"
		config.write_text("{}")
		return MagicMock(backup_path_db=str(database), backup_path_conf=str(config))

	def test_nothing_is_dumped_while_the_region_has_no_storage(self) -> None:
		with (
			patch("cargo.backup.backup_bucket", return_value=None),
			patch("cargo.backup.new_backup") as new_backup,
		):
			backup.backup_database()
		new_backup.assert_not_called()

	def test_the_dump_and_the_site_config_are_uploaded_under_the_site_and_the_time(self) -> None:
		bucket = frappe._dict(cluster="OSC-0001", bucket_credentials=[])
		client = MagicMock()
		client.list_objects_v2.return_value = {"Contents": []}
		with (
			tempfile.TemporaryDirectory() as directory,
			patch("cargo.backup.backup_bucket", return_value=bucket),
			patch("cargo.backup.s3_client", return_value=client),
			patch("cargo.backup.new_backup", return_value=self.dump(Path(directory))) as new_backup,
		):
			backup.backup_database()

		self.assertTrue(new_backup.call_args.kwargs["ignore_files"])
		keys = sorted(call.args[2] for call in client.upload_file.call_args_list)
		self.assertEqual(len(keys), 2)
		self.assertTrue(all(key.startswith(f"{frappe.local.site}/") for key in keys))
		self.assertTrue(keys[0].endswith("x-database.sql.gz"))
		self.assertTrue(keys[1].endswith("x-site_config_backup.json"))
		self.assertEqual({call.args[1] for call in client.upload_file.call_args_list}, {backup.BUCKET_NAME})

	def test_dumps_past_the_window_are_deleted_and_recent_ones_kept(self) -> None:
		now = datetime.now(UTC)
		client = MagicMock()
		client.list_objects_v2.side_effect = [
			{
				"Contents": [
					{"Key": "site/old/db.sql.gz", "LastModified": now - timedelta(days=40)},
					{"Key": "site/new/db.sql.gz", "LastModified": now - timedelta(days=2)},
				],
				"NextContinuationToken": "more",
			},
			{"Contents": [{"Key": "site/older/db.sql.gz", "LastModified": now - timedelta(days=31)}]},
		]
		gone = backup.prune_dumps(client, "site/")

		self.assertEqual(gone, ["site/old/db.sql.gz", "site/older/db.sql.gz"])
		deleted = client.delete_objects.call_args.kwargs["Delete"]["Objects"]
		self.assertEqual([item["Key"] for item in deleted], gone)

	def test_the_bucket_is_made_once_the_region_s_storage_serves(self) -> None:
		if not frappe.db.exists("Object Storage Cluster", {"status": "Active"}):
			cluster = frappe.get_doc({"doctype": "Object Storage Cluster"}).insert()
			cluster.db_set("status", "Active")
		with patch.object(Bucket, "provision") as provision:
			made = backup.backup_bucket()
		provision.assert_called_once()
		self.assertEqual(made.bucket_name, backup.BUCKET_NAME)
		# The second call finds it rather than making another.
		with patch.object(Bucket, "provision") as provision:
			self.assertEqual(backup.backup_bucket().name, made.name)
		provision.assert_not_called()
