# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.sfu.api import get_credential
from cargo.sfu.doctype.sfu_server.sfu_server import WEBHOOK_NAME, SFUServer
from cargo.testing import (
	TEST_ZONE,
	as_request,
	make_dns_zone,
	signed_token,
	trusted_test_keys,
	use_test_settings,
)

MODULE = "cargo.sfu.doctype.sfu_server.sfu_server"


def reset_sfu_server() -> None:
	frappe.db.delete("Singles", {"doctype": "SFU Server"})
	frappe.db.delete("Webhook", {"name": WEBHOOK_NAME})
	frappe.db.delete("DNS Record", {"managed_by_doctype": "SFU Server"})
	frappe.clear_document_cache("SFU Server", "SFU Server")


def make_machine(
	address: str = "fdaa:1::40", public_ipv4: str | None = "203.0.113.40", status: str = "Running"
):
	return frappe.get_doc(
		{
			"doctype": "Machine",
			"reference_doctype": "SFU Server",
			"reference_name": "SFU Server",
			"role": "sfu",
			"disk_size_gb": 40,
			"vm_id": f"vm-{frappe.generate_hash(length=6)}",
			"address": address,
			"public_ipv4": public_ipv4,
			"status": status,
		}
	).insert()


class IntegrationTestSFUServer(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.set_user("Administrator")
		use_test_settings()
		make_dns_zone()
		reset_sfu_server()
		self.server: SFUServer = frappe.get_single("SFU Server")
		self.server.ssl_email = "ops@example.test"
		self.server.save()

	def tearDown(self) -> None:
		reset_sfu_server()

	def attach_machine(self, **fields):
		machine = make_machine(**fields)
		self.server.machine = machine.name
		self.server.save()
		return machine

	def test_the_hostname_hangs_off_the_zone_and_the_secrets_are_minted_once(self) -> None:
		self.assertEqual(self.server.hostname, f"sfu.{TEST_ZONE}")
		secret = self.server.get_password("jwt_secret")
		self.assertEqual(len(secret), 32)
		self.server.save()
		self.assertEqual(self.server.get_password("jwt_secret"), secret)

	def test_the_firewall_opens_the_web_and_one_udp_port_per_worker(self) -> None:
		rules = {(r["protocol"], r.get("ports")) for r in self.server.firewall()["inbound"]}
		self.assertIn(("tcp", "443"), rules)
		self.assertIn(("tcp", "80"), rules)
		self.assertIn(("udp", "40000-40003"), rules)
		self.assertIn(("any", None), rules)  # the mesh

	def test_a_running_machine_s_public_address_is_published_as_the_hostname(self) -> None:
		self.attach_machine()
		self.server.sync_machines()
		self.server.reload()
		self.assertEqual(self.server.ipv4_address, "203.0.113.40")
		record = frappe.get_value(
			"DNS Record",
			{"managed_by": "SFU Server", "type": "A"},
			["host", "value", "category"],
			as_dict=True,
		)
		self.assertEqual((record.host, record.value, record.category), ("sfu", "203.0.113.40", "Service"))

	def test_a_machine_without_a_public_address_or_dead_fails_the_server(self) -> None:
		self.attach_machine(public_ipv4=None)
		self.server.sync_machines()
		self.assertEqual(frappe.get_single("SFU Server").status, "Failed")

	def test_the_install_is_told_the_deployment_and_its_secrets_are_masked(self) -> None:
		self.attach_machine()
		self.server.sync_machines()
		self.server.reload()
		with patch(f"{MODULE}.run_over_ssh", return_value="ok") as ran:
			self.server._setup()
		text = ran.call_args.args[1]
		self.assertIn(f"export DOMAIN=sfu.{TEST_ZONE}", text)
		self.assertIn("export WEBRTC_ANNOUNCED_IP=203.0.113.40", text)
		self.assertIn("export SUITE_REF=develop", text)
		self.assertIn("./deploy.sh setup", text)
		self.assertEqual(
			set(ran.call_args.kwargs["secrets"]),
			{self.server.get_password("jwt_secret"), self.server.get_password("metrics_token")},
		)
		self.server.reload()
		self.assertEqual(self.server.status, "Active")

	def test_the_webhook_names_sfu_and_its_public_url(self) -> None:
		self.attach_machine()
		webhook = frappe.get_doc("Webhook", WEBHOOK_NAME)
		self.assertIn('"service": "sfu"', webhook.webhook_json)
		self.assertIn(f"https://sfu.{TEST_ZONE}", webhook.webhook_json)

	def test_central_fetches_the_credential_with_its_own_scope_only(self) -> None:
		self.attach_machine()
		self.server.db_set("status", "Active")
		frappe.clear_document_cache("SFU Server", "SFU Server")
		with trusted_test_keys(), as_request(signed_token("sfu:*")):
			credential = get_credential()
		self.assertEqual(
			credential,
			{
				"sfu_server_url": f"https://sfu.{TEST_ZONE}",
				"sfu_secret": self.server.get_password("jwt_secret"),
			},
		)
		with trusted_test_keys(), as_request(signed_token("mail:*")):
			self.assertRaises(frappe.PermissionError, get_credential)
		with trusted_test_keys(), as_request(signed_token("sfu:*", site="acme.frappe.test")):
			self.assertRaises(frappe.AuthenticationError, get_credential)  # a site never holds it
