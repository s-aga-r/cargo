import frappe
from frappe.tests import IntegrationTestCase

from cargo.testing import TEST_ZONE, make_dns_zone

# Unique to the tests so a site's own records never collide with them.
TEST_HOST = "cargo-test"
OTHER_ZONE = "other.test"


class TestDNSZone(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.flags.do_not_enqueue = True
		frappe.db.delete("DNS Record", {"host": TEST_HOST})
		make_dns_zone()

	def tearDown(self) -> None:
		frappe.flags.do_not_enqueue = False

	def test_domain_name_is_normalised(self) -> None:
		zone = make_dns_zone("Zone.TEST.", default=False)
		self.assertEqual(zone.name, "zone.test")

	def test_provider_requires_credentials(self) -> None:
		zone = make_dns_zone()
		zone.dns_provider = "Cloudflare"
		zone.dns_provider_token = ""

		self.assertRaisesRegex(frappe.ValidationError, "Token", zone.save)

	def test_cargo_settings_name_the_default_zone(self) -> None:
		make_dns_zone(OTHER_ZONE)
		self.assertEqual(frappe.db.get_single_value("Cargo Settings", "dns_zone"), OTHER_ZONE)
		self.assertEqual(make_dns_record().dns_zone, OTHER_ZONE)

	def test_dns_record_defaults_to_the_settings_zone(self) -> None:
		record = make_dns_record()
		self.assertEqual(record.dns_zone, TEST_ZONE)
		self.assertEqual(record.fqdn, f"{TEST_HOST}.{TEST_ZONE}")

	def test_zone_ttl_is_the_record_default(self) -> None:
		make_dns_zone(default_ttl=60)
		self.assertEqual(make_dns_record().ttl, 60)

	def test_same_record_may_exist_in_two_zones(self) -> None:
		make_dns_zone(OTHER_ZONE, default=False)
		make_dns_record()
		other = make_dns_record(zone=OTHER_ZONE)
		self.assertEqual(other.fqdn, f"{TEST_HOST}.{OTHER_ZONE}")
		self.assertEqual(frappe.db.count("DNS Record", {"host": TEST_HOST}), 2)


def make_dns_record(zone: str | None = None):
	"""Inserts a DNS Record without a provider, which leaves it unverified and enqueues nothing."""

	record = frappe.new_doc("DNS Record")
	record.dns_zone = zone
	record.host = TEST_HOST
	record.type = "A"
	record.value = "203.0.113.10"
	record.category = "Other"
	return record.insert()
