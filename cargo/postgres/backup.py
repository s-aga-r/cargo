"""Every database on the region's Postgres, dumped nightly to its object storage."""

from __future__ import annotations

import frappe

from cargo.backup import prune_dumps, s3_client
from cargo.postgres.doctype.postgres_server.postgres_server import BACKUP_BUCKET, PostgresServer
from cargo.ssh import run_over_ssh, script

CONF = ("postgres", "conf", "postgres", "backup.sh")
BACKUP_TIMEOUT = 60 * 60
LOG_LIMIT = 20_000
RETENTION_DAYS = 30


def backup_bucket(server: PostgresServer):
	"""The bucket the dumps go to, made on the region's object storage once that serves."""
	if server.backup_bucket and frappe.db.exists("Bucket", server.backup_bucket):
		return frappe.get_doc("Bucket", server.backup_bucket)
	cluster = frappe.db.get_value("Object Storage Cluster", {"status": "Active"})
	if not cluster:
		return None
	bucket = frappe.get_doc({"doctype": "Bucket", "bucket_name": BACKUP_BUCKET, "cluster": cluster}).insert(
		ignore_permissions=True
	)
	server.db_set("backup_bucket", bucket.name, update_modified=False)
	return bucket


def backup_environment(server: PostgresServer, bucket, databases: list[str]) -> dict:
	cluster = frappe.get_cached_doc("Object Storage Cluster", bucket.cluster)
	credential = bucket.bucket_credentials[0]
	return {
		"DATABASES": " ".join(databases),
		"PORT": server.port,
		"S3_ENDPOINT": cluster.service_endpoint,
		"S3_BUCKET": bucket.bucket_name,
		"S3_REGION": cluster.region,
		"S3_ACCESS_KEY": credential.access_key,
		"S3_SECRET_KEY": credential.get_password("secret_access_key"),
	}


def backup_databases() -> None:
	"""Daily: dump every Postgres Database to the backup bucket, then drop dumps past the
	window. A region whose storage does not serve yet has nowhere to put them, and waits."""
	server: PostgresServer = frappe.get_single("Postgres Server")
	if server.status != "Active":
		return
	databases = frappe.get_all("Postgres Database", pluck="database_name")  # adopted ones included
	bucket = backup_bucket(server) if databases else None
	if not bucket:
		return
	if not frappe.flags.in_test:
		# the bucket is on Garage now; a failed dump must not forget it
		frappe.db.commit()  # nosemgrep

	machine = frappe.get_doc("Machine", server.machine)
	environment = backup_environment(server, bucket, databases)
	# Its own log, written whole at the end: the setup log belongs to setup runs, which may be
	# streaming into it at the same time.
	lines: list[str] = []
	try:
		run_over_ssh(
			machine.address,
			script(*CONF, environment=environment),
			machine.get_password("ssh_private_key"),
			timeout=BACKUP_TIMEOUT,
			on_output=lines.append,
			pin=machine.host_key_pin(),
			secrets=[environment["S3_SECRET_KEY"]],
		)
	except Exception:
		lines.append(frappe.get_traceback(with_context=False))
		frappe.log_error(title="Postgres dump failed", message=frappe.get_traceback(with_context=False))
		return
	finally:
		server.db_set("backup_log", "".join(lines)[-LOG_LIMIT:], update_modified=False)
	client = s3_client(bucket)
	for database in databases:
		prune_dumps(client, f"{database}/", RETENTION_DAYS, bucket=bucket.bucket_name)
