"""Relaying every serving node's Stalwart metrics to datum."""

from __future__ import annotations

from functools import cached_property

import frappe
import requests

from cargo.health import shipping
from cargo.health.shipping import MetricsInfo, get_metrics_info

SCRAPE_TIMEOUT = 15
# `datum.samples` is shared with every other Frappe service, where a bare `queue_count`
# would collide with anyone's.
PREFIX = "stalwart_"
METRICS_PATH = "/metrics/prometheus"
SERVING_STATUSES = ("Active", "Draining")


def parse_metrics(text: str, labels: dict[str, str], timestamp: str) -> list[dict]:
	return shipping.parse_metrics(text, labels, timestamp, PREFIX)


class Telemetry:
	"""Scrape every serving node over its public address and relay it. Each node ships on its
	own: one unreachable node must not cost the cluster its whole tick."""

	def __init__(self, cluster) -> None:
		self.cluster = cluster
		self.verify_tls = bool(frappe.get_cached_doc("Mail Settings").verify_stalwart_tls)

	@cached_property
	def api_key(self) -> str:
		return self.cluster.get_password("api_key")

	@cached_property
	def nodes(self) -> list[frappe._dict]:
		return frappe.get_all(
			"Stalwart Node",
			filters={"cluster": self.cluster.name, "status": ("in", SERVING_STATUSES)},
			fields=["name", "hostname", "role", "machine"],
			order_by="name",
		)

	def ship(self) -> None:
		info = get_metrics_info()
		timestamp = frappe.utils.now_datetime().isoformat()
		for node in self.nodes:
			try:
				samples = self.samples_for(node, timestamp)
			except Exception:
				frappe.log_error(title=f"Could not scrape {node.name}")
				continue
			if samples:
				self.send(info, samples, node)

	def samples_for(self, node: frappe._dict, timestamp: str) -> list[dict]:
		text = self.scrape(node)
		if text is None:
			return []
		return parse_metrics(
			text,
			labels={
				"cluster": self.cluster.name,
				"region": frappe.db.get_single_value("Cargo Settings", "region") or "",
				"role": node.role or "full",
				"machine": node.machine or "",
				"node": node.name,
			},
			timestamp=timestamp,
		)

	def scrape(self, node: frappe._dict) -> str | None:
		"""Stalwart serves Prometheus text to a key with the metrics permission. A node whose
		exporter is off answers 404, which is nothing to ship rather than a fault."""
		response = requests.get(
			f"https://{node.hostname}{METRICS_PATH}",
			headers={"Authorization": f"Bearer {self.api_key}"},
			timeout=SCRAPE_TIMEOUT,
			verify=self.verify_tls,
		)
		if response.status_code == 404:
			return None
		response.raise_for_status()
		return response.text

	def send(self, info: MetricsInfo, samples: list[dict], node: frappe._dict) -> None:
		shipping.send(info, samples, node.name)
