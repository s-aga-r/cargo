from __future__ import annotations

import typing
from functools import cached_property

import frappe
import requests

from cargo.atlas_client import host_port
from cargo.health import shipping
from cargo.health.shipping import MetricsInfo, get_metrics_info
from cargo.object_storage.doctype.object_storage_cluster.setup import Setup

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)
	from cargo.object_storage.doctype.object_storage_cluster.setup import MachineRow

SCRAPE_TIMEOUT = 15
# Garage names some series `garage_*` and some not; `datum.samples` is shared with every
# other Frappe service, where a bare `cluster_healthy` would collide with anyone's.
PREFIX = "garage_"

__all__ = [
	"PREFIX",
	"SCRAPE_TIMEOUT",
	"MetricsInfo",
	"Telemetry",
	"get_metrics_info",
	"parse_metrics",
	"requests",
]


def parse_metrics(text: str, labels: dict[str, str], timestamp: str) -> list[dict]:
	return shipping.parse_metrics(text, labels, timestamp, PREFIX)


class Telemetry:
	"""Get all possible telemetry from a clusters metric endpoint and ship it to datum"""

	def __init__(self, cluster: ObjectStorageCluster) -> None:
		self.cluster = cluster
		self.garage = Setup(self.cluster)

	@cached_property
	def metrics_token(self) -> str:
		return self.cluster.get_password("metrics_token")

	def check(self) -> None:
		"""Run the telemetry checks"""
		self.ship()

	def ship(self) -> None:
		"""Scrape every node and relay it. Each machine ships on its own: one unreachable
		node must not cost the cluster its whole tick."""
		info = get_metrics_info()
		timestamp = frappe.utils.now_datetime().isoformat()

		for machine in self.garage.machines:
			try:
				samples = self.samples_for(machine, timestamp)
			except Exception:
				frappe.log_error(title=f"Could not scrape {machine['name']}")
				continue

			if samples:
				self.send(info, samples, machine)

	def samples_for(self, machine: MachineRow, timestamp: str) -> list[dict]:
		"""One scrape of a node, labelled with where it came from. Garage's own labels
		(`volume`, `id`, `rpc_endpoint`) pass through untouched."""
		return parse_metrics(
			self.scrape(machine),
			labels={
				"cluster": self.cluster.name,
				"region": self.cluster.region,
				"role": machine["role"],
				"machine": machine["name"],
			},
			timestamp=timestamp,
		)

	def scrape(self, machine: MachineRow) -> str:
		"""Every node serves its own metrics on the admin port, reached over the mesh."""
		response = requests.get(
			f"http://{host_port(machine['address'], self.cluster.admin_port)}/metrics",
			headers={"Authorization": f"Bearer {self.metrics_token}"},
			timeout=SCRAPE_TIMEOUT,
		)
		response.raise_for_status()

		return response.text

	def send(self, info: MetricsInfo, samples: list[dict], machine: MachineRow) -> None:
		shipping.send(info, samples, machine["name"])
