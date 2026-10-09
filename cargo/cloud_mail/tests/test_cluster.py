import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.cloud_mail.cluster import dns, plan
from cargo.cloud_mail.doctype.stalwart_node.stalwart_node import StalwartNode
from cargo.cloud_mail.stalwart.directory import dkim_selector, dkim_selector_template
from cargo.cloud_mail.tests.fixtures import (
	ROOT_DOMAIN,
	configure_settings,
	make_cluster,
	make_node,
	make_zone,
	no_dns_provider,
	remove_cluster,
)
from cargo.testing import make_dns_zone


class TestStalwartCluster(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.flags.do_not_enqueue = True
		configure_settings()

	def tearDown(self) -> None:
		frappe.flags.do_not_enqueue = False

	def test_cluster_derives_zone_url_and_coordinator(self) -> None:
		cluster = make_cluster()

		self.assertEqual(cluster.default_domain, ROOT_DOMAIN)
		self.assertEqual(cluster.base_url, f"https://mx.{ROOT_DOMAIN}")
		self.assertEqual(cluster.coordinator, "Default")
		self.assertEqual(cluster.status, "Pending")
		self.assertEqual(len(cluster.get_password("admin_password")), 32)
		self.assertEqual(cluster.stalwart_version, plan.STALWART_VERSION)

	def test_the_zone_names_the_cluster_and_holds_one(self) -> None:
		cluster = make_cluster()
		self.assertEqual(
			(cluster.name, cluster.hostname, cluster.default_domain, cluster.dns_zone),
			(f"mx.{ROOT_DOMAIN}", f"mx.{ROOT_DOMAIN}", ROOT_DOMAIN, ROOT_DOMAIN),
		)
		second = frappe.get_doc(
			{"doctype": "Stalwart Cluster", "title": "second", "acme_contact_email": "ops@example.test"}
		)
		self.assertRaisesRegex(frappe.ValidationError, "already has the cluster", second.insert)

	def test_a_blob_bucket_must_hold_exactly_one_credential(self) -> None:
		cluster = make_cluster()
		bucket = frappe.get_doc("Bucket", cluster.blob_bucket)
		bucket.append("bucket_credentials", {"access_key": "AK2", "secret_access_key": "SK2"})
		bucket.flags.ignore_validate = True
		bucket.db_update()
		for row in bucket.bucket_credentials:
			row.db_update() if row.name else row.db_insert()
		self.assertRaisesRegex(frappe.ValidationError, "exactly one credential", cluster.save)
		frappe.db.delete("Bucket Credential", {"parent": "mail", "access_key": "AK2"})

	def test_embedded_stores_pin_the_cluster_to_one_full_node(self) -> None:
		cluster = make_cluster(name="solo", multi_node=False)
		self.assertEqual(cluster.single_node, 1)
		self.assertEqual(cluster.coordinator, "Disabled")
		self.assertRaisesRegex(
			frappe.ValidationError, "full role", make_node, cluster, "203.0.113.1", role="frontend"
		)
		make_node(cluster, "203.0.113.1")

		self.assertRaisesRegex(frappe.ValidationError, "one node", make_node, cluster, "203.0.113.2")

		# Valkey on a single node is allowed but coordinates nothing.
		from cargo.cloud_mail.tests.fixtures import make_stores

		stores = make_stores()
		cluster.reload()  # the node insert re-saved it
		cluster.in_memory_store = stores["in_memory_store"]
		cluster.save()
		self.assertEqual(cluster.coordinator, "Disabled")

	def test_a_node_rents_its_machine_and_takes_its_public_address(self) -> None:
		cluster = make_cluster()
		node = frappe.get_doc({"doctype": "Stalwart Node", "cluster": cluster.name}).insert()
		self.assertIsNone(node.ipv4_address)
		self.assertFalse(frappe.db.exists("DNS Record", {"managed_by": node.name}))

		module = "cargo.cloud_mail.doctype.stalwart_node.stalwart_node"
		with (
			patch(f"{module}.base_image_id", return_value="img-ubuntu"),
			patch("cargo.atlas_client.AtlasClient") as atlas,
		):
			atlas.from_settings.return_value.create_vm.return_value = {"id": "vm-mail-1"}
			node.request_machine(2000, 4, 40)
		asked = atlas.from_settings.return_value.create_vm.call_args.kwargs
		self.assertTrue(asked["public_ipv4"])
		rules = asked["firewall"]["inbound"]
		self.assertIn({"protocol": "any", "cidrs": ["fdaa::/16"]}, rules)
		self.assertIn({"protocol": "tcp", "ports": "25", "cidrs": ["0.0.0.0/0", "::/0"]}, rules)
		self.assertFalse(any(rule.get("ports") == "22" for rule in rules))

		node.reload()
		machine = frappe.get_doc("Machine", node.machine)
		self.assertEqual(
			(machine.role, machine.status, machine.reference_name), ("mail", "Pending", node.name)
		)

		# Running without a public address is a fault, not a delay.
		machine.db_set({"status": "Running", "address": "fdaa:1::7"})
		node.sync_machines()
		self.assertEqual(frappe.db.get_value("Stalwart Node", node.name, "status"), "Failed")
		self.assertIn("no public address", frappe.db.get_value("Stalwart Node", node.name, "last_error"))

		node.set_status("Pending")
		machine.db_set("public_ipv4", "203.0.113.7")
		with patch.object(StalwartNode, "start_provisioning"):  # provisioning has its own tests
			node.sync_machines()
		node.reload()
		self.assertEqual(node.ipv4_address, "203.0.113.7")
		self.assertEqual(
			frappe.db.get_value("DNS Record", {"managed_by": node.name, "type": "A"}, "value"), "203.0.113.7"
		)

		machine.db_set("status", "Terminated")
		node.sync_machines()
		self.assertEqual(frappe.db.get_value("Stalwart Node", node.name, "status"), "Failed")

	def test_node_hostnames_are_handed_out_in_order(self) -> None:
		cluster = make_cluster()
		first = make_node(cluster, "203.0.113.1")
		second = make_node(cluster, "203.0.113.2")
		self.assertEqual(
			(first.name, second.hostname), (f"n1.{cluster.default_domain}", f"n2.{cluster.default_domain}")
		)

		# A typed hostname is ignored, and numbers are never handed out twice while a node holds them.
		frappe.delete_doc(
			"Stalwart Node", first.name, force=True, ignore_permissions=True, ignore_on_trash=True
		)
		third = make_node(cluster, "203.0.113.3", hostname=f"custom.{cluster.default_domain}")
		self.assertEqual(third.hostname, f"n3.{cluster.default_domain}")

	def test_node_creates_its_dns_records_and_spf_tracks_ips(self) -> None:
		cluster = make_cluster()
		node = make_node(cluster, "203.0.113.10", ipv6_address="2001:db8::10")

		records = frappe.get_all(
			"DNS Record", {"managed_by": node.name}, ["host", "type", "value", "category"], order_by="type"
		)
		self.assertEqual(
			[(r.host, r.type, r.value, r.category) for r in records],
			[("n1", "A", "203.0.113.10", "Node"), ("n1", "AAAA", "2001:db8::10", "Node")],
		)
		# Not yet in ingress: the cluster hostname does not point at a pending node.
		self.assertFalse(
			frappe.db.exists("DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "mx", "managed_by": node.name})
		)

		node.db_set("status", "Active")
		dns.sync_node_records(node, include_ingress=True)
		dns.sync_spf_record(cluster)
		ingress = frappe.get_all(
			"DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "mx"}, pluck="value", order_by="type"
		)
		self.assertEqual(ingress, ["203.0.113.10", "2001:db8::10"])
		spf = frappe.db.get_value(
			"DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "spf", "type": "TXT"}, "value"
		)
		self.assertEqual(spf, "v=spf1 ip4:203.0.113.10 ip6:2001:db8::10 -all")
		zone_spf = frappe.db.get_value(
			"DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "@", "type": "TXT"}, "value"
		)
		self.assertEqual(zone_spf, f"v=spf1 include:spf.{ROOT_DOMAIN} -all")

		dns.sync_node_records(node, include_ingress=False)
		self.assertFalse(frappe.db.exists("DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "mx"}))
		self.assertEqual(frappe.db.get_value("Stalwart Node", node.name, "in_ingress_dns"), 0)

	def test_removing_a_node_updates_spf_and_frees_a_failed_bootstrap(self) -> None:
		from cargo.cloud_mail.cluster import bootstrap

		cluster = make_cluster()
		node = make_node(cluster, "203.0.113.10")
		node.db_set({"status": "Active", "is_bootstrap_node": 1})
		dns.sync_spf_record(cluster)
		self.assertIn(
			"ip4:203.0.113.10",
			frappe.db.get_value("DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "spf"}, "value"),
		)

		bootstrap.drain_node(node)  # Draining nodes still send
		self.assertIn(
			"ip4:203.0.113.10",
			frappe.db.get_value("DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "spf"}, "value"),
		)
		node.db_set("status", "Failed")
		cluster.db_set({"status": "Failed", "bootstrap_node": node.name})

		frappe.get_doc("Stalwart Node", node.name).delete()

		self.assertEqual(
			frappe.db.get_value("DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "spf"}, "value"),
			"v=spf1 -all",
		)
		self.assertEqual(
			frappe.db.get_value("Stalwart Cluster", cluster.name, ["status", "bootstrap_node"]),
			("Pending", None),
		)

	def test_active_nodes_are_not_failed_by_transient_checks(self) -> None:
		from cargo.cloud_mail.cluster import bootstrap

		cluster = make_cluster()
		node = make_node(cluster, "203.0.113.10")
		node.db_set({"status": "Active", "provisioned_at": frappe.utils.add_to_date(None, hours=-5)})
		self.assertFalse(bootstrap._not_ready(node, "registry hiccup"))
		self.assertEqual(
			frappe.db.get_value("Stalwart Node", node.name, ["status", "last_error"]),
			("Active", "registry hiccup"),
		)

		node.db_set("status", "Provisioned")
		self.assertFalse(bootstrap._not_ready(node, "still down"))
		self.assertEqual(frappe.db.get_value("Stalwart Node", node.name, "status"), "Failed")

	def test_dns_records_may_be_wanted_by_two_owners(self) -> None:
		from cargo.cargo.doctype.dns_record.dns_record import reconcile_managed_records

		cluster = make_cluster()
		node = make_node(cluster, "203.0.113.10")
		wanted = [
			{
				"dns_zone": ROOT_DOMAIN,
				"host": "n1",
				"type": "A",
				"value": "203.0.113.10",
				"category": "Egress",
			}
		]
		reconcile_managed_records("Stalwart Cluster", cluster.name, wanted)
		wanted_filters = {"dns_zone": ROOT_DOMAIN, "host": "n1", "type": "A"}
		self.assertEqual(frappe.db.count("DNS Record", wanted_filters), 2)

		with patch("cargo.cargo.doctype.dns_record.dns_record.get_dns_provider") as provider:
			provider.return_value.delete_dns_record.return_value = True
			reconcile_managed_records("Stalwart Cluster", cluster.name, [])
			provider.return_value.delete_dns_record.assert_not_called()  # the node still wants it
			frappe.get_doc("Stalwart Node", node.name).delete()  # Pending nodes delete normally
		self.assertFalse(frappe.db.exists("DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "n1"}))

	def test_finish_bootstrap_and_key_rotation_through_the_fake(self) -> None:
		from cargo.cloud_mail.cluster import bootstrap
		from cargo.cloud_mail.stalwart import forget_sessions
		from cargo.cloud_mail.tests.fake_stalwart import FakeStalwart
		from cargo.cloud_mail.tests.fixtures import clear_request_cache

		cluster = make_cluster()
		node = make_node(cluster, "203.0.113.10")
		fake = FakeStalwart(base_url=cluster.base_url, admin_password=cluster.get_password("admin_password"))
		fake.objects["Certificate"]["cert1"] = {
			"id": "cert1",
			"subjectAlternativeNames": {cluster.hostname: True},
		}
		with fake.install():
			forget_sessions(cluster)
			clear_request_cache()
			node.db_set("is_bootstrap_node", 1)
			cluster.db_set({"status": "Bootstrapping", "bootstrap_node": node.name})
			cluster.bump_config_version(plan.cluster_plan(cluster))
			node.reload()

			# No registry lease yet: a Redis-coordinated cluster keeps waiting (nothing fails).
			bootstrap.after_provision(node)
			self.assertEqual(frappe.db.get_value("Stalwart Cluster", cluster.name, "status"), "Bootstrapping")

			fake.add_cluster_node(node.hostname, node_id=7)
			# The first attempt minted a key. A re-bootstrapped data store forgets it, and the stored
			# key is then replaced rather than trusted.
			first_key = frappe.get_doc("Stalwart Cluster", cluster.name).get_password("api_key")
			self.assertIn(first_key, fake.tokens)
			fake.objects["ApiKey:" + fake.admin_id].clear()
			fake.tokens.clear()
			self.assertTrue(bootstrap.finish_bootstrap(frappe.get_doc("Stalwart Cluster", cluster.name)))

			cluster.reload()
			node.reload()
			self.assertEqual((cluster.status, node.status, node.node_id), ("Active", "Active", 7))
			self.assertIn(cluster.get_password("api_key"), fake.tokens)
			# Activation pushes the full plan: the disabled-accounts role appears only now.
			self.assertTrue(fake.find("Role", description="suite-disabled"))
			# Refs resolve to ids and secrets are never echoed: neither may count as drift.
			self.assertEqual(
				frappe.get_doc("Stalwart Cluster", cluster.name).check_drift()["differences"], []
			)
			# The default domain's keys can be replaced after a leak, under the same selector.
			default = fake.find("Domain", name=cluster.default_domain)
			before = fake.find("DkimSignature", domainId=default["id"])
			frappe.get_doc("Stalwart Cluster", cluster.name).replace_dkim_keys()
			after = fake.find("DkimSignature", domainId=default["id"])
			self.assertNotEqual(before["id"], after["id"])
			self.assertEqual(after["selector"], dkim_selector("rsa"))
			self.assertEqual(
				fake.find("Domain", name=cluster.default_domain)["dkimManagement"]["@type"], "Automatic"
			)
			self.assertTrue(
				plan.same_value(
					{"acmeProviderId": "x", "secret": "hidden"}, {"acmeProviderId": "#acme", "secret": "s"}
				)
			)
			self.assertFalse(plan.same_value({"a": {"0": {"x": 1}}}, {"a": {"0": {"x": 2}}}))
			self.assertTrue(
				frappe.db.exists(
					"DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "mx", "managed_by": node.name}
				)
			)
			self.assertIn(cluster.get_password("api_key"), fake.tokens)
			self.assertNotEqual(cluster.get_password("api_key"), first_key)
			self.assertEqual(fake.singletons["SystemSettings"]["defaultCertificateId"], "cert1")
			self.assertEqual(len(fake.all("ApiKey:" + fake.admin_id)), 1)

			# The minted key works for management calls and rotation revokes the old one.
			old_key = cluster.get_password("api_key")
			clear_request_cache()
			self.assertEqual(cluster.get_client().cluster_nodes.find_by_hostname(node.hostname)["nodeId"], 7)
			cluster.rotate_api_key()
			self.assertNotEqual(cluster.get_password("api_key"), old_key)
			self.assertEqual(len(fake.all("ApiKey:" + fake.admin_id)), 1)

	def test_client_falls_back_to_the_admin_password_without_an_api_key(self) -> None:
		from cargo.cloud_mail.stalwart import forget_sessions, has_credentials
		from cargo.cloud_mail.tests.fake_stalwart import FakeStalwart
		from cargo.cloud_mail.tests.fixtures import clear_request_cache

		cluster = make_cluster()
		cluster.db_set("status", "Active")
		fake = FakeStalwart(base_url=cluster.base_url, admin_password=cluster.get_password("admin_password"))
		fake.add_cluster_node("n1.example.test", node_id=7)
		with fake.install():
			forget_sessions(cluster)
			clear_request_cache()
			self.assertIsNone(cluster.get_password("api_key", raise_exception=False))
			self.assertTrue(has_credentials(cluster))
			self.assertEqual(
				cluster.get_client().cluster_nodes.find_by_hostname("n1.example.test")["nodeId"], 7
			)

	def test_outbound_nodes_stay_out_of_ingress(self) -> None:
		from cargo.cloud_mail.cluster import bootstrap

		cluster = make_cluster()
		node = make_node(cluster, "203.0.113.20", role="outbound")
		self.assertFalse(bootstrap.serves_clients(node))
		bootstrap.activate_node(node)
		self.assertFalse(
			frappe.db.exists("DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "mx", "managed_by": node.name})
		)
		self.assertIn(
			"ip4:203.0.113.20",
			frappe.db.get_value("DNS Record", {"dns_zone": ROOT_DOMAIN, "host": "spf"}, "value"),
		)

	def test_a_manual_certificate_leaves_acme_out_of_the_plan(self) -> None:
		cluster = make_cluster(certificate_management="Manual")
		operations = {op["object"]: op for op in plan.cluster_plan(cluster)}
		self.assertNotIn("AcmeProvider", operations)
		self.assertEqual(
			operations["Domain"]["value"]["default"]["certificateManagement"], {"@type": "Manual"}
		)

	def test_bootstrap_and_cluster_plans(self) -> None:
		cluster = make_cluster()

		bootstrap = plan.bootstrap_plan(cluster)[0]
		self.assertEqual(bootstrap["object"], "Bootstrap")
		value = bootstrap["value"]
		self.assertEqual(value["serverHostname"], f"mx.{ROOT_DOMAIN}")
		self.assertEqual(value["defaultDomain"], ROOT_DOMAIN)
		self.assertEqual(value["dataStore"]["@type"], "PostgreSql")
		self.assertEqual(value["dataStore"]["authSecret"], {"@type": "Value", "secret": "pg-secret"})
		self.assertEqual(
			(value["dataStore"]["host"], value["dataStore"]["database"]), ("fdaa:1::20", "stalwart")
		)
		self.assertEqual(value["blobStore"]["@type"], "S3")
		self.assertEqual((value["blobStore"]["bucket"], value["blobStore"]["accessKey"]), ("mail", "AK"))
		self.assertEqual(value["blobStore"]["secretKey"], {"@type": "Value", "secret": "SK"})
		self.assertEqual(value["inMemoryStore"]["@type"], "Redis")
		self.assertEqual(value["inMemoryStore"]["url"], "redis://stalwart:vk-secret@[fdaa:1::30]:6379/0")
		# The Valkey password travels inside a URL: masked like any secret, never in the stored plan.
		self.assertIn("vk-secret", plan.secret_strings(plan.bootstrap_plan(cluster)))
		self.assertNotIn("vk-secret", plan.redacted(plan.bootstrap_plan(cluster)))
		self.assertIn("redis://stalwart:", plan.redacted(plan.bootstrap_plan(cluster)))
		self.assertEqual(value["searchStore"], {"@type": "Default"})
		self.assertFalse(value["requestTlsCertificate"])
		self.assertEqual((value["tracer"]["@type"], value["tracer"]["path"]), ("Log", "/var/log/stalwart"))

		operations = {op["object"]: op for op in plan.cluster_plan(cluster)}
		# The two settable stores are re-pushed by the plan, so a rotated credential reaches the nodes.
		self.assertEqual(operations["BlobStore"]["value"]["@type"], "S3")
		self.assertEqual(operations["InMemoryStore"]["value"]["@type"], "Redis")
		# Tracers have no name: matched by kind, so bootstrap's journal is turned down, not doubled.
		self.assertEqual(operations["Tracer"]["matchOn"], ["@type"])
		self.assertEqual(operations["Tracer"]["value"]["log"]["rotate"], "daily")
		self.assertEqual(operations["Tracer"]["value"]["journal"]["level"], "warn")
		self.assertEqual(operations["Coordinator"]["value"], {"@type": "Default"})
		self.assertEqual(set(operations["ClusterRole"]["value"]), {"full", "frontend", "outbound"})
		self.assertNotIn("DnsServer", operations)  # no DNS provider configured in tests
		self.assertEqual(
			operations["ClusterRole"]["value"]["frontend"]["tasks"]["taskTypes"], {"outboundMta": True}
		)
		self.assertEqual(operations["AcmeProvider"]["matchOn"], ["directory"])
		self.assertNotIn("description", operations["AcmeProvider"]["value"]["acme"])
		domain = operations["Domain"]["value"]["default"]
		self.assertEqual(
			domain["certificateManagement"]["subjectAlternativeNames"],
			{f"*.{cluster.default_domain}": True},
		)
		self.assertEqual(domain["dnsManagement"], {"@type": "Manual"})
		self.assertEqual(domain["dkimManagement"]["@type"], "Automatic")
		self.assertEqual(domain["dkimManagement"]["algorithms"], {"Dkim1RsaSha256": True})
		self.assertEqual(domain["dkimManagement"]["selectorTemplate"], dkim_selector_template())
		self.assertEqual(
			operations["DnsResolver"]["value"],
			{
				"@type": "Custom",
				"servers": {
					"0": {"address": "127.0.0.1", "port": 53, "protocol": "udp"},
					"1": {"address": "127.0.0.1", "port": 53, "protocol": "tcp"},
				},
			},
		)
		self.assertEqual(
			operations["SpamSettings"]["value"]["spamFilterRulesUrl"],
			"https://github.com/stalwartlabs/spam-filter/releases/download/v3.0.1/spam-filter-rules.json.gz",
		)
		# The first normal start names a cluster role and checks its resolver for DNSSEC, so both
		# come before it with the pin.
		self.assertEqual(
			plan.defaults_plan(),
			[operations["ClusterRole"], operations["DnsResolver"], operations["SpamSettings"]],
		)

		configure_settings(sign_with_ed25519=1)
		self.addCleanup(configure_settings, sign_with_ed25519=0)
		operations = {op["object"]: op for op in plan.cluster_plan(cluster)}
		self.assertEqual(
			operations["Domain"]["value"]["default"]["dkimManagement"]["algorithms"],
			{"Dkim1Ed25519Sha256": True, "Dkim1RsaSha256": True},
		)
		self.assertEqual(
			operations["SystemSettings"]["value"]["mailExchangers"],
			{"0": {"hostname": cluster.hostname, "priority": 10}},
		)
		self.assertEqual(operations["AcmeProvider"]["value"]["acme"]["contact"], {"ops@example.test": True})
		self.assertEqual(
			operations["Role"]["value"]["disabled"]["enabledPermissions"], {"emailReceive": True}
		)

		self.assertRaisesRegex(
			ValueError,
			"label #x",
			plan.to_ndjson,
			[
				{"@type": "upsert", "object": "Domain", "value": {"x": {}}},
				{"@type": "upsert", "object": "Role", "value": {"x": {}}},
			],
		)
		redacted = plan.redacted(plan.bootstrap_plan(cluster))
		self.assertNotIn("pg-secret", redacted)
		self.assertIn(plan.SECRET_MARKER, redacted)
		self.assertEqual(
			plan.to_ndjson(plan.cluster_plan(cluster)).count("\n"), len(plan.cluster_plan(cluster))
		)

	def test_node_env_and_config(self) -> None:
		cluster = make_cluster()
		node = make_node(cluster, role="frontend")

		self.assertEqual(
			plan.node_env(node),
			{
				"STALWART_HOSTNAME": node.hostname,
				"STALWART_PUBLIC_URL": cluster.base_url,
				"STALWART_ROLE": "frontend",
			},
		)
		recovery = plan.node_env(node, "recovery")
		self.assertEqual(recovery["STALWART_RECOVERY_MODE"], "1")
		self.assertTrue(recovery["STALWART_RECOVERY_ADMIN"].startswith("admin:"))
		self.assertNotIn("STALWART_RECOVERY_MODE", plan.node_env(node, "bootstrap"))
		self.assertEqual(json.loads(frappe.as_json(plan.node_config(cluster)))["@type"], "PostgreSql")
		self.assertIn("EnvironmentFile=/etc/stalwart/stalwart.env", plan.systemd_unit())

	def test_dns_server_object_maps_the_zone_provider(self) -> None:
		make_zone(dns_provider="Cloudflare", dns_provider_token="cf-token")
		with no_dns_provider():
			cluster = make_cluster()
		self.assertEqual(cluster.dns_zone, ROOT_DOMAIN)
		self.assertEqual(plan.dns_server_object(cluster)["@type"], "Cloudflare")
		self.assertEqual(plan.dns_server_object(cluster)["secret"], {"@type": "Value", "secret": "cf-token"})

		domain = {op["object"]: op for op in plan.cluster_plan(cluster)}["Domain"]["value"]["default"]
		self.assertEqual(domain["dnsManagement"]["@type"], "Automatic")
		self.assertEqual(domain["dnsManagement"]["origin"], ROOT_DOMAIN)
		self.assertEqual(domain["dnsManagement"]["publishRecords"], {"mx": True, "dkim": True, "dmarc": True})
