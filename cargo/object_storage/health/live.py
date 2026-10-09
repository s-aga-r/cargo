from __future__ import annotations

import typing
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

import frappe

from cargo.health.live import CRITICAL, DEGRADED, HEALTHY, UNKNOWN, Finding, history_path
from cargo.health.live import LiveHealth as ServiceHealth
from cargo.health.live import prune_history as prune_log
from cargo.object_storage.client import Client, Error

if typing.TYPE_CHECKING:
	from cargo.object_storage.doctype.object_storage_cluster.object_storage_cluster import (
		ObjectStorageCluster,
	)
	from cargo.object_storage.doctype.object_storage_health_settings.object_storage_health_settings import (
		ObjectStorageHealthSettings,
	)

HISTORY_FILE = "cluster_health.json.log"

__all__ = [
	"CRITICAL",
	"DEGRADED",
	"HEALTHY",
	"UNKNOWN",
	"Finding",
	"LiveHealth",
	"Path",
	"history_file",
	"prune_history",
]


@dataclass(frozen=True)
class Reading:
	"""What the gateway said, or why it said nothing."""

	health: dict
	nodes: list[dict]
	error: str = ""


class LiveHealth(ServiceHealth, Client):
	"""Check live critical metrics of a cluster, by talking to the admin api
	In case of any error, fire off webhooks to alert someone. This is a safety in case
	the metrics/telemetry go down, also metrics and telemetry might be only used for postmortem

	Tracking the following:
		- Is node up.
		- Node disk space.
		- Node lastSeenSecAgo
		- Quorum satisfied
	"""

	history_file = HISTORY_FILE

	def __init__(self, cluster: ObjectStorageCluster) -> None:
		Client.__init__(self, cluster)
		ServiceHealth.__init__(self, cluster)
		self.settings: ObjectStorageHealthSettings = frappe.get_cached_doc("Object Storage Health Settings")
		# Health runs every minute, so a hung gateway must not still be waiting on the next tick.
		self.timeout = self.settings.admin_timeout_seconds

	def is_live(self) -> bool:
		return self.cluster.is_live

	@cached_property
	def reading(self) -> Reading:
		"""One read of the gateway, shared by every check and by the log line."""
		try:
			return Reading(health=self.health(), nodes=self.status().get("nodes") or [])
		except Error as error:
			return Reading(health={}, nodes=[], error=str(error))

	def findings(self) -> list[Finding]:
		"""Everything the cluster has against it. An unreachable gateway is the only
		finding: the rest would be invented from data we do not have."""
		reading = self.reading
		if reading.error:
			return [Finding(CRITICAL, f"the gateway's admin API could not be reached: {reading.error}")]

		return [*self.quorum_findings(reading.health), *self.node_findings(reading.nodes)]

	def quorum_findings(self, health: dict) -> list[Finding]:
		"""Partitions are the keyspace, not the nodes. Losing quorum on some means the objects
		living there cannot be written, which is why it outranks a node being down."""
		partitions = health.get("partitions") or 0
		if not partitions:
			return []

		if (quorum := health.get("partitionsQuorum") or 0) < partitions:
			return [Finding(CRITICAL, f"{partitions - quorum} of {partitions} partitions cannot be written")]

		if (all_ok := health.get("partitionsAllOk") or 0) < partitions:
			return [Finding(DEGRADED, f"{partitions - all_ok} of {partitions} partitions are missing a copy")]

		return []

	def node_findings(self, nodes: list[dict]) -> list[Finding]:
		"""Named by machine, not Garage node id: the tag setup wrote is what an operator can
		look up. A node with no role is not in the layout yet."""
		findings = []
		for node in nodes:
			tags = (node.get("role") or {}).get("tags") or []
			if not tags:
				continue

			machine = tags[0]
			if not node.get("isUp"):
				seen = node.get("lastSeenSecsAgo")
				if seen is None or seen >= self.settings.node_offline_seconds:
					findings.append(Finding(DEGRADED, f"{machine} has been unreachable for {seen or 0}s"))
				continue

			findings.extend(self.disk_findings(machine, node))

		return findings

	def disk_findings(self, machine: str, node: dict) -> list[Finding]:
		"""Both volumes matter: Garage stops accepting writes when either fills."""
		findings = []
		for volume, key in (("data", "dataPartition"), ("metadata", "metadataPartition")):
			partition = node.get(key) or {}
			total, available = partition.get("total") or 0, partition.get("available") or 0
			if not total:
				continue

			free = available * 100 // total
			if free < self.settings.disk_critical_percent:
				findings.append(Finding(CRITICAL, f"{machine} has {free}% free on its {volume} volume"))
			elif free < self.settings.disk_degraded_percent:
				findings.append(Finding(DEGRADED, f"{machine} has {free}% free on its {volume} volume"))

		return findings

	def log_entry(self, finding: Finding) -> dict:
		"""`health` and `nodes` are Garage's raw replies, kept as-is."""
		return {
			"cluster": self.cluster.name,
			"severity": finding.severity,
			"reason": finding.reason,
			"error": self.reading.error,
			"health": self.reading.health,
			"nodes": self.reading.nodes,
		}


def history_file() -> Path:
	return history_path(HISTORY_FILE)


def prune_history() -> None:
	"""Drop readings past the window. Scheduled hourly."""
	prune_log(HISTORY_FILE, frappe.get_cached_doc("Object Storage Health Settings").history_hours)
