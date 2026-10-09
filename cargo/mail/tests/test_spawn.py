"""How a region gets its mail cluster without an operator."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.mail.doctype.stalwart_node.stalwart_node import StalwartNode
from cargo.mail.spawn import CONFIG_KEY, MAX_SETUP_ATTEMPTS, ensure_mail, validate_config
from cargo.mail.tests.fixtures import ROOT_DOMAIN, configure_settings, make_stores, remove_cluster
from cargo.postgres.doctype.postgres_server.test_postgres_server import reset_postgres_server
from cargo.testing import use_test_settings
from cargo.valkey.doctype.valkey_server.test_valkey_server import reset_valkey_server

CONFIG = {
	"node_count": 2,
	"node": {"cpu_millicores": 2000, "ram_gb": 4, "disk_gb": 40},
	"acme_contact_email": "ops@example.test",
	"certificate_management": "Manual",
}
MODULE = "cargo.mail.doctype.stalwart_node.stalwart_node"


class TestMailSpawn(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.flags.do_not_enqueue = True
		frappe.set_user("Administrator")
		use_test_settings()
		configure_settings()
		remove_cluster(f"mx.{ROOT_DOMAIN}")
		frappe.db.delete("Press Workflow")
		# Earlier tests leave the stores serving; every test here says itself whether they do.
		reset_postgres_server()
		reset_valkey_server()
		frappe.db.sql("update `tabObject Storage Cluster` set status = 'Draft' where status = 'Active'")

	def tearDown(self) -> None:
		remove_cluster(f"mx.{ROOT_DOMAIN}")
		frappe.flags.do_not_enqueue = False

	def configured(self, config=CONFIG):
		return patch.dict(frappe.local.conf, {CONFIG_KEY: config})

	def stores_serve(self) -> None:
		make_stores()
		for name in frappe.get_all("Object Storage Cluster", pluck="name"):
			frappe.db.set_value("Object Storage Cluster", name, "status", "Active", update_modified=False)

	def atlas(self):
		client = patch("cargo.atlas_client.AtlasClient.from_settings").start()
		self.addCleanup(patch.stopall)
		client.return_value.find_system_image.return_value = "img-ubuntu"
		client.return_value.create_vm.side_effect = lambda **kwargs: {
			"id": f"vm-{frappe.generate_hash(length=8)}"
		}
		return client.return_value

	def cluster(self):
		name = frappe.db.get_value("Stalwart Cluster", {"auto_spawn": 1})
		return frappe.get_doc("Stalwart Cluster", name) if name else None

	def nodes(self) -> list[frappe._dict]:
		return frappe.get_all(
			"Stalwart Node", {"cluster": f"mx.{ROOT_DOMAIN}"}, ["name", "machine", "status"]
		)

	def test_the_config_is_checked(self) -> None:
		validate_config(CONFIG)
		for bad in (
			"text",
			{**CONFIG, "node_count": 0},
			{**CONFIG, "acme_contact_email": "nobody"},
			{**CONFIG, "certificate_management": "Letsencrypt"},
			{k: v for k, v in CONFIG.items() if k != "node"},
		):
			with self.subTest(bad=bad):
				self.assertRaises(frappe.ValidationError, validate_config, bad)

	def test_a_region_whose_stores_do_not_serve_yet_waits(self) -> None:
		with self.configured():
			ensure_mail()
		self.assertIsNone(self.cluster())

	def test_the_cluster_is_made_on_the_stores_then_its_first_node_rented(self) -> None:
		self.stores_serve()
		with self.configured():
			ensure_mail()
		cluster = self.cluster()
		self.assertEqual(
			(
				cluster.data_store,
				cluster.in_memory_store,
				cluster.blob_bucket,
				cluster.certificate_management,
			),
			("stalwart", "stalwart", "mail", "Manual"),
		)
		self.assertEqual(frappe.db.get_value("Postgres Database", "stalwart", "reference_name"), cluster.name)
		self.assertEqual(self.nodes(), [])

		atlas = self.atlas()
		with self.configured():
			ensure_mail()
		nodes = self.nodes()
		self.assertEqual(len(nodes), 1)
		self.assertTrue(nodes[0].machine)
		self.assertEqual(atlas.create_vm.call_args.kwargs["cpu_millicores"], 2000)
		self.assertTrue(atlas.create_vm.call_args.kwargs["public_ipv4"])

		# The rest wait for the cluster to serve: the first node brings the store up alone.
		with self.configured():
			ensure_mail()
		self.assertEqual(len(self.nodes()), 1)

	def test_the_rest_join_one_a_run_once_the_cluster_serves(self) -> None:
		self.stores_serve()
		self.atlas()
		with self.configured():
			ensure_mail()
			ensure_mail()
		cluster = self.cluster()
		frappe.db.set_value("Stalwart Node", {"cluster": cluster.name}, "status", "Active")
		cluster.db_set("status", "Active")
		with self.configured():
			ensure_mail()
		self.assertEqual(len(self.nodes()), 2)
		with self.configured():
			ensure_mail()
		self.assertEqual(len(self.nodes()), 2)

	def test_a_cluster_added_by_hand_is_never_joined(self) -> None:
		self.stores_serve()
		frappe.get_doc(
			{"doctype": "Stalwart Cluster", "title": "by hand", "acme_contact_email": "ops@example.test"}
		).insert()
		self.atlas()
		with self.configured():
			ensure_mail()
		self.assertIsNone(self.cluster())
		self.assertEqual(self.nodes(), [])

	def test_a_machine_that_would_not_boot_stops_the_run(self) -> None:
		self.stores_serve()
		self.atlas()
		with self.configured():
			ensure_mail()
			ensure_mail()
		node = self.nodes()[0]
		frappe.db.set_value("Machine", node.machine, "status", "Broken")
		with self.configured():
			ensure_mail()
		self.assertIn("did not come up", self.cluster().error)
		self.assertEqual(len(self.nodes()), 1)

	def test_a_failed_node_is_provisioned_again_until_the_budget_is_spent(self) -> None:
		self.stores_serve()
		self.atlas()
		with self.configured():
			ensure_mail()
			ensure_mail()
		node = self.nodes()[0]
		frappe.db.set_value("Machine", node.machine, {"status": "Running", "public_ipv4": "203.0.113.9"})
		frappe.db.set_value("Stalwart Node", node.name, {"status": "Failed", "ipv4_address": "203.0.113.9"})
		for attempt in range(1, MAX_SETUP_ATTEMPTS + 1):
			frappe.db.set_value("Stalwart Node", node.name, "status", "Failed")
			with self.configured(), patch.object(StalwartNode, "start_provisioning") as start:
				ensure_mail()
			start.assert_called_once()
			self.assertEqual(self.cluster().auto_setup_attempts, attempt)
		frappe.db.set_value("Stalwart Node", node.name, "status", "Failed")
		with self.configured(), patch.object(StalwartNode, "start_provisioning") as start:
			ensure_mail()
		start.assert_not_called()
