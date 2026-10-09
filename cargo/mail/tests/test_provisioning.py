"""How a node is brought up: which script runs, in what order, with what, and what reaches
the record when it fails. The scripts themselves run against a real Stalwart in tools/."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.mail.cluster import bootstrap, plan
from cargo.mail.doctype.stalwart_node.stalwart_node import StalwartNode
from cargo.mail.tests.fixtures import configure_settings, make_cluster, make_node
from cargo.ssh import MASK, SshError
from cargo.testing import use_test_settings
from cargo.workflow_engine.utils import called_methods_in_order

SCRIPTS = Path(frappe.get_app_path("cargo", "mail", "conf", "stalwart"))
RUN = "cargo.mail.cluster.bootstrap.run_over_ssh"


class TestScripts(IntegrationTestCase):
	def test_every_script_parses_and_none_traces_its_commands(self) -> None:
		for path in sorted(SCRIPTS.glob("*.sh")):
			with self.subTest(path.name):
				subprocess.run(["bash", "-n", str(path)], check=True)
				text = path.read_text()
				self.assertIn("set -euo pipefail", text)
				self.assertNotIn("set -x", text)

	def test_the_version_a_restart_reports_is_what_is_recorded(self) -> None:
		self.assertEqual(bootstrap.installed_version_from("...\nstalwart 0.16.21\n"), "v0.16.21")
		self.assertEqual(bootstrap.installed_version_from("stalwart v0.16.20"), "v0.16.20")
		self.assertIsNone(bootstrap.installed_version_from("  \n"))

	def test_the_steps_run_in_the_order_the_playbooks_relied_on(self) -> None:
		node = frappe.new_doc("Stalwart Node")
		names = [name for name, _ in called_methods_in_order(StalwartNode, node._provision._wrapped)]
		self.assertEqual(names, ["install", "bring_up", "record_provisioned"])
		names = [name for name, _ in called_methods_in_order(StalwartNode, node._upgrade._wrapped)]
		self.assertEqual(
			names, ["take_out_of_ingress", "install", "restart_on_installed_version", "record_upgraded"]
		)
		names = [name for name, _ in called_methods_in_order(StalwartNode, node._rollback._wrapped)]
		self.assertEqual(names, ["take_out_of_ingress", "restart_on_previous_version", "record_upgraded"])


class TestProvisioning(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.flags.do_not_enqueue = True
		use_test_settings()
		configure_settings()
		self.cluster = make_cluster()
		self.node = make_node(self.cluster, "203.0.113.10")
		self.machine = frappe.get_doc(
			{
				"doctype": "Machine",
				"reference_doctype": "Stalwart Node",
				"reference_name": self.node.name,
				"role": "mail",
				"disk_size_gb": 40,
				"vm_id": f"vm-{frappe.generate_hash(length=6)}",
				"address": "fdaa:1::10",
				"public_ipv4": "203.0.113.10",
				"status": "Running",
			}
		).insert()
		self.node.db_set("machine", self.machine.name)
		self.node.reload()

	def tearDown(self) -> None:
		frappe.flags.do_not_enqueue = False

	def sent(self, run) -> list[str]:
		return [call.args[1] for call in run.call_args_list]

	def test_the_first_node_installs_then_bootstraps_the_store(self) -> None:
		with patch(RUN, return_value="ok") as run:
			self.node._provision()

		install, bring_up = self.sent(run)
		self.assertIn(f"export STALWART_VERSION={plan.STALWART_VERSION}", install)
		self.assertIn("export USE_UFW=0", install)
		self.assertIn("install_release stalwart ", install)
		self.assertIn("export BOOTSTRAP_NDJSON=", bring_up)
		self.assertIn("export WAIT_PORTS='25 443'", bring_up)
		self.assertNotIn("STALWART_RECOVERY", bootstrap.bootstrap_environment(self.node)[0]["ENV_NORMAL"])
		# The secrets the scripts carry are the ones the masker is told about.
		secrets = run.call_args_list[1].kwargs["secrets"]
		self.assertIn(self.cluster.get_password("admin_password"), secrets)
		self.assertTrue(run.call_args_list[1].kwargs["pin"].known is None)

		self.node.reload()
		self.cluster.reload()
		self.assertTrue(self.node.is_bootstrap_node)
		self.assertEqual((self.node.status, self.cluster.status), ("Provisioned", "Bootstrapping"))
		self.assertEqual(self.cluster.bootstrap_node, self.node.name)

	def test_a_node_joining_a_live_cluster_is_configured_not_bootstrapped(self) -> None:
		self.cluster.db_set({"status": "Active", "bootstrap_node": "n0.example.test"})
		with patch(RUN, return_value="ok") as run, patch("cargo.mail.cluster.bootstrap.check_node"):
			self.node._provision()

		_, bring_up = self.sent(run)
		self.assertIn("export CONFIG_JSON=", bring_up)
		self.assertNotIn("BOOTSTRAP_NDJSON", bring_up)
		self.assertFalse(frappe.db.get_value("Stalwart Node", self.node.name, "is_bootstrap_node"))

	def test_a_script_that_fails_leaves_the_node_failed_with_its_output_masked(self) -> None:
		password = self.cluster.get_password("admin_password")

		def leaky(address, text, key, **kwargs):
			kwargs["on_output"](f"export ADMIN_PASSWORD={MASK}\n")
			raise SshError(f"{address} exited 1:\n...apply refused {MASK}")

		with patch(RUN, side_effect=leaky):
			self.node._provision()

		self.node.reload()
		self.assertEqual(self.node.status, "Failed")
		self.assertIn("install.sh failed", self.node.last_error)
		self.assertNotIn(password, self.node.setup_log or "")
		self.assertIn(MASK, self.node.setup_log)

	def test_provisioning_is_refused_until_the_machine_runs_with_an_address(self) -> None:
		self.machine.db_set("status", "Pending")
		self.assertRaisesRegex(frappe.ValidationError, "must be running", self.node.start_provisioning)
		self.machine.db_set("status", "Running")
		self.node.db_set("ipv4_address", None)
		self.node.reload()
		self.assertRaisesRegex(frappe.ValidationError, "no public address", self.node.start_provisioning)

	def test_a_running_machine_starts_provisioning_by_itself(self) -> None:
		with patch.object(StalwartNode, "start_provisioning") as start:
			self.node.sync_machines()
		start.assert_called_once()

	def test_an_outbound_node_may_not_bring_the_cluster_up(self) -> None:
		self.node.db_set("role", "outbound")
		self.node.reload()
		self.assertRaisesRegex(
			frappe.ValidationError, "must serve clients", bootstrap.needs_bootstrap, self.node
		)
