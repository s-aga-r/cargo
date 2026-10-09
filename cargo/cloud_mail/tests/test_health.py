"""The verdict a mail cluster gets from one read of its nodes, and the log it leaves behind."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from cargo.cloud_mail.health import live as live_module
from cargo.cloud_mail.health import refresh_health
from cargo.cloud_mail.health.live import HISTORY_FILE, LiveHealth, drift_recorded
from cargo.cloud_mail.stalwart.errors import StalwartUnavailableError
from cargo.cloud_mail.tests.fake_stalwart import FakeStalwart
from cargo.cloud_mail.tests.fixtures import activate_cluster, configure_settings, make_cluster, make_node
from cargo.health.live import CRITICAL, DEGRADED, HEALTHY, UNKNOWN, history_path
from cargo.testing import use_test_settings

PROBE = "cargo.cloud_mail.health.live.probe_ready"
CERTIFICATE = "cargo.cloud_mail.health.live.certificate_days_left"


class MailClusterTestCase(IntegrationTestCase):
	"""An active two-node cluster with a fake Stalwart behind it."""

	def setUp(self) -> None:
		frappe.flags.do_not_enqueue = True
		use_test_settings()
		configure_settings()
		self.cluster = make_cluster()
		self.nodes = [make_node(self.cluster, "203.0.113.10"), make_node(self.cluster, "203.0.113.11")]
		for node in self.nodes:
			node.db_set({"status": "Active", "last_health_at": now_datetime()})
		self.cluster.reload()  # adding nodes touched it
		activate_cluster(self.cluster)
		self.fake = FakeStalwart(base_url=self.cluster.base_url)
		self.fake.add_token("test-token")
		for number, node in enumerate(self.nodes, start=1):
			self.fake.add_cluster_node(node.hostname, node_id=number)
		history_path(HISTORY_FILE).unlink(missing_ok=True)

	def tearDown(self) -> None:
		frappe.flags.do_not_enqueue = False
		history_path(HISTORY_FILE).unlink(missing_ok=True)


class TestMailHealth(MailClusterTestCase):
	def verdict(self, probe=None, days: int = 60):
		with (
			self.fake.install(),
			patch(PROBE, side_effect=probe or (lambda *args: "")),
			patch(CERTIFICATE, return_value=days),
		):
			return LiveHealth(frappe.get_doc("Stalwart Cluster", self.cluster.name)).record()

	def failing(self, *hostnames):
		return lambda hostname, *args: "did not answer: ConnectTimeout" if hostname in hostnames else ""

	def test_a_cluster_whose_nodes_all_answer_is_healthy(self) -> None:
		finding = self.verdict()
		self.assertEqual((finding.severity, finding.reason), (HEALTHY, ""))
		self.assertEqual(frappe.db.get_value("Stalwart Cluster", self.cluster.name, "health"), HEALTHY)
		for node in self.nodes:
			self.assertEqual(frappe.db.get_value("Stalwart Node", node.name, "consecutive_failures"), 0)

	def test_a_node_that_just_stopped_answering_is_a_blip(self) -> None:
		finding = self.verdict(self.failing(self.nodes[1].hostname))
		self.assertEqual(finding.severity, HEALTHY)
		name = self.nodes[1].name
		self.assertEqual(frappe.db.get_value("Stalwart Node", name, "consecutive_failures"), 1)
		self.assertIn("did not answer", frappe.db.get_value("Stalwart Node", name, "last_error"))

	def test_a_node_silent_past_the_window_degrades_the_cluster(self) -> None:
		self.nodes[1].db_set("last_health_at", add_to_date(now_datetime(), seconds=-600))
		finding = self.verdict(self.failing(self.nodes[1].hostname))
		self.assertEqual(finding.severity, DEGRADED)
		self.assertIn(self.nodes[1].hostname, finding.reason)
		self.assertIn("did not answer", finding.reason)

	def test_a_lease_that_is_not_active_counts_like_a_silent_node(self) -> None:
		self.nodes[0].db_set("last_health_at", None)
		lease = self.fake.find("ClusterNode", hostname=self.nodes[0].hostname)
		lease["status"] = "expired"
		finding = self.verdict()
		self.assertEqual(finding.severity, DEGRADED)
		self.assertIn("registry lease is expired", finding.reason)

	def test_no_node_answering_is_critical_whatever_else_is_true(self) -> None:
		finding = self.verdict(self.failing(*(node.hostname for node in self.nodes)))
		self.assertEqual(finding.severity, CRITICAL)
		self.assertIn("no node answers", finding.reason)
		self.assertTrue(frappe.db.exists("Error Log", {"method": f"{self.cluster.name} is critical"}))

	def test_an_unreachable_management_api_is_critical_and_the_only_finding(self) -> None:
		with (
			patch(
				"cargo.cloud_mail.stalwart.config.ClusterNodeService.get_all",
				side_effect=StalwartUnavailableError("503 from the gateway"),
			),
			patch(PROBE) as probe,
		):
			finding = self.verdict()
		self.assertEqual(finding.severity, CRITICAL)
		self.assertIn("503 from the gateway", finding.reason)
		probe.assert_not_called()

	def test_a_certificate_about_to_expire_degrades_the_cluster(self) -> None:
		finding = self.verdict(days=3)
		self.assertEqual((finding.severity, finding.reason), (DEGRADED, "the certificate expires in 3 days"))

	def test_recorded_drift_degrades_the_cluster_until_the_next_check_clears_it(self) -> None:
		self.cluster.db_set(
			"drift_report", frappe.as_json({"checked_at": "2026-10-09", "differences": [{"id": "x"}]})
		)
		self.assertEqual(self.verdict().severity, DEGRADED)
		self.cluster.db_set("drift_report", frappe.as_json({"checked_at": "2026-10-09", "differences": []}))
		self.assertEqual(self.verdict().severity, HEALTHY)
		self.assertTrue(drift_recorded(frappe.as_json({"error": "push failed"})))
		self.assertFalse(drift_recorded(None))

	def test_the_stores_verdicts_are_inherited(self) -> None:
		frappe.db.set_single_value("Postgres Server", "health", CRITICAL)
		frappe.db.set_single_value("Postgres Server", "health_reason", "postgres could not be reached")
		frappe.clear_document_cache("Postgres Server", "Postgres Server")
		finding = self.verdict()
		self.assertEqual(finding.severity, CRITICAL)
		self.assertIn("Postgres server is critical", finding.reason)
		frappe.db.set_single_value("Postgres Server", "health", HEALTHY)
		frappe.clear_document_cache("Postgres Server", "Postgres Server")

		frappe.db.set_single_value("Valkey Server", "health", CRITICAL)
		frappe.clear_document_cache("Valkey Server", "Valkey Server")
		finding = self.verdict()
		self.assertEqual(finding.severity, DEGRADED)  # coordination lost, mail still flows
		self.assertIn("Valkey server is critical", finding.reason)
		frappe.db.set_single_value("Valkey Server", "health", HEALTHY)
		frappe.clear_document_cache("Valkey Server", "Valkey Server")

	def test_a_cluster_that_has_not_served_is_not_judged(self) -> None:
		self.cluster.db_set("status", "Bootstrapping")
		self.assertEqual(self.verdict().severity, UNKNOWN)
		self.cluster.db_set("status", "Failed")
		self.assertEqual(self.verdict().severity, CRITICAL)

	def test_every_reading_lands_in_the_local_log(self) -> None:
		self.verdict()
		lines = history_path(HISTORY_FILE).read_text().splitlines()
		self.assertEqual(len(lines), 1)
		self.assertIn(self.cluster.name, lines[0])
		self.assertIn('"severity": "Healthy"', lines[0])

	def test_the_scheduled_refresh_reads_only_active_clusters(self) -> None:
		with patch.object(live_module.LiveHealth, "record") as record:
			refresh_health()
		record.assert_called_once()
		self.cluster.db_set("status", "Failed")
		with patch.object(live_module.LiveHealth, "record") as record:
			refresh_health()
		record.assert_not_called()


METRICS = """\
# HELP queue_count Messages in the queue
queue_count 3
stalwart_smtp_connections{listener="smtp"} 2
http_request_duration_bucket{le="0.5"} 9
http_request_duration_sum 1.5
"""
INFO = {"endpoint": "https://datum.test", "token": "datum-jwt"}


class TestMailTelemetry(MailClusterTestCase):
	"""Every serving node's Prometheus text, relabelled and relayed."""

	def answer(self, status: int = 200, text: str = METRICS):
		response = MagicMock(status_code=status, text=text)
		response.raise_for_status.return_value = None
		return patch("cargo.cloud_mail.health.telemetry.requests.get", return_value=response)

	def test_every_node_is_scraped_and_its_samples_prefixed_and_labelled(self) -> None:
		from cargo.cloud_mail.health.telemetry import Telemetry

		with (
			self.answer() as get,
			patch("cargo.cloud_mail.health.telemetry.get_metrics_info", return_value=INFO),
			patch("cargo.cloud_mail.health.telemetry.shipping.send") as send,
		):
			Telemetry(frappe.get_doc("Stalwart Cluster", self.cluster.name)).ship()

		self.assertEqual(
			sorted(call.args[0] for call in get.call_args_list),
			sorted(f"https://{node.hostname}/metrics/prometheus" for node in self.nodes),
		)
		self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer test-token")
		self.assertEqual(send.call_count, 2)
		samples = send.call_args_list[0].args[1]
		names = {sample["metric"] for sample in samples}
		self.assertEqual(
			names, {"stalwart_queue_count", "stalwart_smtp_connections", "stalwart_http_request_duration_sum"}
		)
		labels = next(s for s in samples if s["metric"] == "stalwart_smtp_connections")["labels"]
		self.assertEqual(labels["listener"], "smtp")
		self.assertEqual((labels["cluster"], labels["node"]), (self.cluster.name, self.nodes[0].name))

	def test_a_node_without_the_exporter_ships_nothing_and_is_no_fault(self) -> None:
		from cargo.cloud_mail.health.telemetry import Telemetry

		with (
			self.answer(status=404, text="not found"),
			patch("cargo.cloud_mail.health.telemetry.get_metrics_info", return_value=INFO),
			patch("cargo.cloud_mail.health.telemetry.shipping.send") as send,
		):
			Telemetry(frappe.get_doc("Stalwart Cluster", self.cluster.name)).ship()
		send.assert_not_called()
		self.assertFalse(frappe.db.exists("Error Log", {"method": ["like", "Could not scrape%"]}))

	def test_a_host_without_datum_credentials_ships_nothing(self) -> None:
		from cargo.cloud_mail.health import ship_metrics

		with (
			patch("cargo.cloud_mail.health.get_metrics_info", side_effect=RuntimeError("no datum")),
			patch("cargo.cloud_mail.health.Telemetry") as telemetry,
		):
			ship_metrics()
		telemetry.assert_not_called()
