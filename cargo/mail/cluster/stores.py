"""Stalwart's store objects, rendered from the records of the services Cargo runs.

A cluster links a Postgres Database, a Bucket and a Valkey Credential; nothing about them is
copied into mail's own records, so a rotated credential reaches the next plan by itself. A
cluster with no Postgres Database runs on a RocksDb store on its one node."""

from __future__ import annotations

import frappe
from frappe.model.document import Document

DATABASE_TIMEOUT_MS = 15_000
S3_TIMEOUT_MS = 30_000
POOL_TIMEOUT_MS = 10_000
POOL_MAX_CONNECTIONS = 10
MAX_RETRIES = 3
ROCKSDB_PATH = "/var/lib/stalwart"
ROCKSDB_BLOB_SIZE = 16834
ROCKSDB_BUFFER_SIZE = 134217728
DEFAULT = {"@type": "Default"}


def secret(value: str) -> dict:
	"""Stalwart's SecretKey union: a literal value."""
	return {"@type": "Value", "secret": value}


def rocksdb_store(path: str = ROCKSDB_PATH) -> dict:
	return {
		"@type": "RocksDb",
		"path": path,
		"blobSize": ROCKSDB_BLOB_SIZE,
		"bufferSize": ROCKSDB_BUFFER_SIZE,
	}


def postgres_store(database: Document) -> dict:
	connection = database.connection()
	return {
		"@type": "PostgreSql",
		"host": connection["host"],
		"port": connection["port"],
		"database": connection["database"],
		"authUsername": connection["user"],
		"authSecret": secret(connection["password"]),
		"timeout": DATABASE_TIMEOUT_MS,
		"useTls": connection["use_tls"],
		"allowInvalidCerts": False,
		"poolMaxConnections": POOL_MAX_CONNECTIONS,
		"poolRecyclingMethod": "fast",
	}


def s3_store(bucket: Document) -> dict:
	"""Garage behind the region's gateway, path-style, with the bucket's one credential."""
	cluster = frappe.get_cached_doc("Object Storage Cluster", bucket.cluster)
	credential = bucket.bucket_credentials[0]
	return {
		"@type": "S3",
		"region": cluster.region,
		"bucket": bucket.bucket_name,
		"endpoint": cluster.service_endpoint,
		"accessKey": credential.access_key,
		"secretKey": secret(credential.get_password("secret_access_key")),
		"timeout": S3_TIMEOUT_MS,
		"maxRetries": MAX_RETRIES,
		"verifyAfterWrite": False,
	}


def redis_store(credential: Document) -> dict:
	return {
		"@type": "Redis",
		"url": credential.connection()["url"],
		"timeout": POOL_TIMEOUT_MS,
		"poolMaxConnections": POOL_MAX_CONNECTIONS,
		"poolTimeoutCreate": POOL_TIMEOUT_MS,
		"poolTimeoutWait": POOL_TIMEOUT_MS,
		"poolTimeoutRecycle": POOL_TIMEOUT_MS,
	}


def data_store(cluster: Document) -> dict:
	if cluster.data_store:
		return postgres_store(frappe.get_cached_doc("Postgres Database", cluster.data_store))
	return rocksdb_store()


def blob_store(cluster: Document) -> dict:
	if cluster.blob_bucket:
		return s3_store(frappe.get_cached_doc("Bucket", cluster.blob_bucket))
	return DEFAULT


def in_memory_store(cluster: Document) -> dict:
	if cluster.in_memory_store:
		return redis_store(frappe.get_cached_doc("Valkey Credential", cluster.in_memory_store))
	return DEFAULT
