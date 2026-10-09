"""Fixtures for the Mail tests; the generic ones are re-exported from the core tests."""

from unittest.mock import patch

import frappe

from cargo.mail.tests.core_fixtures import (
	ROOT_DOMAIN,
	clear_request_cache,
	configure_settings,
	make_zone,
	no_dns_provider,
)
from cargo.object_storage.doctype.bucket.bucket import Bucket
from cargo.postgres.doctype.postgres_server.test_postgres_server import make_machine
from cargo.testing import make_dns_zone, use_test_settings


def make_stores() -> dict:
	"""The three service records a multi-node cluster links, as the services would have
	left them: nothing is asked of a Postgres, a Valkey or a Garage here."""
	use_test_settings()
	for doctype, name, address in (
		("Postgres Server", "Postgres Server", "fdaa:1::20"),
		("Valkey Server", "Valkey Server", "fdaa:1::30"),
	):
		server = frappe.get_single(doctype)
		if not server.machine:
			server.machine = make_machine(doctype, name, doctype.split()[0].lower(), address).name
			server.save()
		server.db_set("status", "Active")
		frappe.clear_document_cache(doctype, name)
	if not frappe.db.exists("Postgres Database", "stalwart"):
		database = frappe.get_doc(
			{"doctype": "Postgres Database", "database_name": "stalwart", "password": "pg-secret"}
		)
		database.flags.adopting = True
		database.insert()
	if not frappe.db.exists("Valkey Credential", "stalwart"):
		credential = frappe.get_doc(
			{"doctype": "Valkey Credential", "username": "stalwart", "password": "vk-secret"}
		)
		credential.flags.adopting = True
		credential.insert()
	if not frappe.db.exists("Bucket", "mail"):
		cluster = (
			frappe.db.get_value("Object Storage Cluster", {}, "name")
			or frappe.get_doc({"doctype": "Object Storage Cluster"}).insert().name
		)
		with patch.object(Bucket, "provision"):
			bucket = frappe.get_doc({"doctype": "Bucket", "bucket_name": "mail", "cluster": cluster})
			bucket.append("bucket_credentials", {"access_key": "AK", "secret_access_key": "SK"})
			bucket.insert()
	return {"data_store": "stalwart", "in_memory_store": "stalwart", "blob_bucket": "mail"}


def make_cluster(name: str = "blr-1", zone: str = ROOT_DOMAIN, multi_node: bool = True, **fields):
	"""``name`` becomes the title; the document is named ``mx.<zone>``, and a zone other than
	the fixture one is created on the way."""
	stores = make_stores() if multi_node else {}
	if not frappe.db.exists("DNS Zone", zone):
		make_dns_zone(zone, default=False)
	remove_cluster(f"mx.{zone}")
	cluster = frappe.get_doc(
		{
			"doctype": "Stalwart Cluster",
			"title": name,
			"acme_contact_email": "ops@example.test",
			"dns_zone": zone,
			**stores,
			**fields,
		}
	)
	cluster.insert()
	return cluster


def remove_cluster(name: str) -> None:
	"""Tests share one transaction per class, so a cluster left by an earlier test is torn down here."""

	if not frappe.db.exists("Stalwart Cluster", name):
		return

	nodes = frappe.get_all("Stalwart Node", {"cluster": name}, pluck="name")
	if nodes:
		# A node's machine links back to it; nothing else holds the row, so it goes first.
		frappe.db.delete("Machine", {"reference_doctype": "Stalwart Node", "reference_name": ["in", nodes]})
	frappe.db.delete("DNS Record", {"managed_by": ["in", [*nodes, name]]})
	for workflow in frappe.get_all(
		"Press Workflow",
		{
			"linked_doctype": ["in", ["Stalwart Node", "Stalwart Cluster"]],
			"linked_docname": ["in", [*nodes, name]],
		},
		pluck="name",
	):
		frappe.delete_doc("Press Workflow", workflow, force=True, ignore_permissions=True)
	for node in nodes:
		frappe.delete_doc("Stalwart Node", node, force=True, ignore_permissions=True, ignore_on_trash=True)
	frappe.delete_doc("Stalwart Cluster", name, force=True, ignore_permissions=True, ignore_on_trash=True)


def make_node(cluster, ipv4: str = "203.0.113.10", **fields):
	"""Nodes name themselves n1, n2, ... in creation order."""

	node = frappe.get_doc(
		{"doctype": "Stalwart Node", "cluster": cluster.name, "ipv4_address": ipv4, **fields}
	)
	node.insert()
	return node


def verified_ownership():
	"""Every domain's ownership record resolves, so tests may add domains without publishing one."""

	return patch("cargo.mail.tenancy.ownership.verify_dns_record", return_value=True)


def activate_cluster(cluster, token: str = "test-token"):
	"""Marks a cluster active with a known API key so a FakeStalwart can serve it."""

	cluster.api_key = token
	cluster.save()
	cluster.db_set("status", "Active")
	cluster.reload()
	return cluster


def make_site(cluster, name: str = "acme.frappe.test", **fields):
	frappe.db.delete("Mail Site", {"name": name})
	site = frappe.get_doc({"doctype": "Mail Site", "site_name": name, "cluster": cluster.name, **fields})
	site.insert()
	return site
