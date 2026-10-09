"""A rolling upgrade: one node at a time, out of ingress, back in on its lease, soaked."""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.cloud_mail.doctype.stalwart_cluster.stalwart_cluster import (
	StalwartCluster,
	running_flows,
	upgrade_order,
)
from cargo.cloud_mail.stalwart import forget_sessions
from cargo.cloud_mail.tests.fake_stalwart import FakeStalwart
from cargo.cloud_mail.tests.fixtures import (
	activate_cluster,
	clear_request_cache,
	configure_settings,
	make_cluster,
	make_node,
)
from cargo.testing import use_test_settings
from cargo.workflow_engine.utils import called_methods_in_order

RUN = "cargo.cloud_mail.cluster.bootstrap.run_over_ssh"


class TestRollingUpgrade(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.flags.do_not_enqueue = True
		use_test_settings()
		configure_settings()
		self.cluster = make_cluster()
		self.nodes = [make_node(self.cluster, f"203.0.113.{n}") for n in (10, 11)]
		for node in self.nodes:
			machine = frappe.get_doc(
				{
					"doctype": "Machine",
					"reference_doctype": "Stalwart Node",
					"reference_name": node.name,
					"role": "mail",
					"disk_size_gb": 40,
					"vm_id": f"vm-{frappe.generate_hash(length=6)}",
					"address": f"fdaa:1::{node.ipv4_address.split('.')[-1]}",
					"public_ipv4": node.ipv4_address,
					"status": "Running",
				}
			).insert()
			node.db_set({"status": "Active", "installed_version": "v0.16.19", "machine": machine.name})
		self.cluster.reload()
		activate_cluster(self.cluster)
		self.cluster.db_set(
			{"bootstrap_node": self.nodes[0].name, "health": "Healthy", "stalwart_version": "v0.16.21"}
		)
		self.fake = FakeStalwart(base_url=self.cluster.base_url)
		self.fake.add_token("test-token")
		for number, node in enumerate(self.nodes, start=1):
			self.fake.add_cluster_node(node.hostname, node_id=number)
		self._install = self.fake.install()
		self._install.__enter__()
		forget_sessions(self.cluster)
		clear_request_cache()

	def tearDown(self) -> None:
		self._install.__exit__(None, None, None)
		frappe.flags.do_not_enqueue = False

	def test_the_steps_run_in_order_for_each_node(self) -> None:
		flow = frappe.new_doc("Stalwart Cluster")._upgrade_nodes._wrapped
		names = [n for n, _ in called_methods_in_order(StalwartCluster, flow)]
		self.assertEqual(names, ["upgrade_node", "wait_until_serving", "soak"])

	def test_the_bootstrap_node_goes_last(self) -> None:
		self.assertEqual(upgrade_order(self.cluster), [self.nodes[1].name, self.nodes[0].name])

	def test_every_node_is_drained_upgraded_and_restored_in_turn(self) -> None:
		with patch(RUN, return_value="stalwart 0.16.21\n") as run:
			frappe.get_doc("Stalwart Cluster", self.cluster.name)._upgrade_nodes()

		hosts = [call.args[0] for call in run.call_args_list]
		# install then upgrade on the second node, then the same on the bootstrap node
		self.assertEqual(hosts, ["fdaa:1::11", "fdaa:1::11", "fdaa:1::10", "fdaa:1::10"])
		for node in self.nodes:
			node.reload()
			self.assertEqual(
				(node.status, node.installed_version, node.drained_by), ("Active", "v0.16.21", None)
			)
			self.assertTrue(frappe.db.exists("DNS Record", {"managed_by": node.name, "host": "mx"}))

	def test_a_node_that_fails_to_upgrade_stops_the_flow_out_of_ingress(self) -> None:
		with patch(RUN, side_effect=RuntimeError("boom")) as run:
			frappe.get_doc("Stalwart Cluster", self.cluster.name)._upgrade_nodes()
		self.assertEqual(run.call_count, 1)
		second = frappe.get_doc("Stalwart Node", self.nodes[1].name)
		first = frappe.get_doc("Stalwart Node", self.nodes[0].name)
		self.assertEqual((second.status, second.drained_by), ("Failed", "Upgrade"))
		self.assertEqual((first.status, first.installed_version), ("Active", "v0.16.19"))

	def test_an_upgrade_is_refused_while_another_flow_runs_or_the_cluster_is_not_active(self) -> None:
		cluster = frappe.get_doc("Stalwart Cluster", self.cluster.name)
		module = "cargo.cloud_mail.doctype.stalwart_cluster.stalwart_cluster"
		with patch(f"{module}.running_flows", return_value=["wf1"]):
			self.assertRaisesRegex(frappe.ValidationError, "Another upgrade", cluster.upgrade_nodes)
		self.assertEqual(running_flows(cluster), [])
		cluster.db_set("status", "Failed")
		cluster.reload()
		self.assertRaisesRegex(frappe.ValidationError, "Active cluster", cluster.upgrade_nodes)
