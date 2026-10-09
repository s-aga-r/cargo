"""Cargo's own database, dumped daily to the region's object storage.

What Cargo holds cannot be rebuilt from the services it runs: who owns which domain, every
site's limits and tokens, the mirror of the mail directory with its Stalwart ids, and the
credentials of every service. The bucket's key and the site's encryption key are kept out of
the region as well, by the operator; without them a dump can neither be fetched nor read."""

from __future__ import annotations

import typing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import boto3
import frappe
from botocore.config import Config
from frappe.utils.backups import new_backup

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.bucket.bucket import Bucket

BUCKET_NAME = "cargo-backups"
RETENTION_DAYS = 30


def backup_bucket() -> Bucket | None:
	"""The bucket the dumps go to, made on the region's object storage once that serves."""
	name = frappe.db.get_value("Bucket", {"bucket_name": BUCKET_NAME})
	if name:
		return frappe.get_doc("Bucket", name)
	cluster = frappe.db.get_value("Object Storage Cluster", {"status": "Active"})
	if not cluster:
		return None
	return frappe.get_doc({"doctype": "Bucket", "bucket_name": BUCKET_NAME, "cluster": cluster}).insert(
		ignore_permissions=True
	)


def s3_client(bucket: Bucket):
	cluster = frappe.get_cached_doc("Object Storage Cluster", bucket.cluster)
	credential = bucket.bucket_credentials[0]
	return boto3.client(
		"s3",
		endpoint_url=cluster.service_endpoint,
		region_name=cluster.region,
		aws_access_key_id=credential.access_key,
		aws_secret_access_key=credential.get_password("secret_access_key"),
		config=Config(s3={"addressing_style": "path"}),
	)


def backup_database() -> None:
	"""Daily: dump the database and site config, upload them, drop dumps past the window.
	A region whose storage does not serve yet has nowhere to put a dump, and waits."""
	bucket = backup_bucket()
	if not bucket:
		return

	if not frappe.flags.in_test:
		# the bucket is on Garage now; a failure below must not forget it
		frappe.db.commit()  # nosemgrep

	# The database only. site_config.json carries the encryption key that reads the dump's
	# secrets, and that key is the operator's to keep out of the region.
	generated = new_backup(ignore_files=True, ignore_conf=True, compress=True, force=True)
	client = s3_client(bucket)
	prefix = f"{frappe.local.site}/"
	stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
	path = Path(generated.backup_path_db)
	client.upload_file(str(path), BUCKET_NAME, f"{prefix}{stamp}/{path.name}")

	prune_dumps(client, prefix)


def prune_dumps(
	client, prefix: str, retention_days: int = RETENTION_DAYS, bucket: str = BUCKET_NAME
) -> list[str]:
	"""Delete every object under `prefix` older than the window; returns what went."""
	cutoff = datetime.now(UTC) - timedelta(days=retention_days)
	expired = []
	token = None
	while True:
		page = client.list_objects_v2(
			Bucket=bucket, Prefix=prefix, **({"ContinuationToken": token} if token else {})
		)
		expired += [item["Key"] for item in page.get("Contents", []) if item["LastModified"] < cutoff]
		token = page.get("NextContinuationToken")
		if not token:
			break
	for start in range(0, len(expired), 1000):
		chunk = expired[start : start + 1000]
		client.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": key} for key in chunk]})
	return expired
