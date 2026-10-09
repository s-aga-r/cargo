import frappe
from frappe.tests import IntegrationTestCase

from cargo.cloud_mail.tests.fixtures import (
	ROOT_DOMAIN,
	clear_request_cache,
	configure_settings,
	make_cluster,
	make_node,
	make_zone,
	remove_cluster,
)

OTHER_ZONE = "other.test"


class TestDNSZone(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.flags.do_not_enqueue = True
		configure_settings()

	def tearDown(self) -> None:
		frappe.flags.do_not_enqueue = False

	def test_cluster_records_live_in_the_cluster_zone(self) -> None:
		cluster = make_cluster("eu-1", zone=OTHER_ZONE)
		node = make_node(cluster, "203.0.113.50")
		clear_request_cache()

		zones = frappe.get_all(
			"DNS Record", {"managed_by": ["in", [cluster.name, node.name]]}, pluck="dns_zone"
		)
		self.assertTrue(zones)
		self.assertEqual(set(zones), {OTHER_ZONE})
		self.assertTrue(frappe.db.exists("DNS Record", {"dns_zone": OTHER_ZONE, "host": "spf"}))
		self.assertTrue(frappe.db.exists("DNS Record", {"dns_zone": OTHER_ZONE, "host": "@", "type": "TXT"}))
		remove_cluster(cluster.name)

	def test_cluster_hostname_follows_its_zone(self) -> None:
		cluster = make_cluster("eu-1", zone=OTHER_ZONE)
		self.assertEqual((cluster.name, cluster.default_domain), (f"mx.{OTHER_ZONE}", OTHER_ZONE))
		remove_cluster(cluster.name)

	def test_every_zone_is_reserved_for_mail_domains(self) -> None:
		from cargo.cloud_mail.tenancy.addresses import assert_domain_available

		make_zone(OTHER_ZONE, default=False)
		self.assertRaisesRegex(
			frappe.ValidationError, "reserved", assert_domain_available, f"customer.{OTHER_ZONE}", "any-site"
		)
