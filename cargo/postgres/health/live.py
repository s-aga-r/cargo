"""The worst thing true about the region's Postgres right now, read over the mesh."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import frappe

from cargo.health.live import CRITICAL, DEGRADED, Finding
from cargo.health.live import LiveHealth as ServiceHealth
from cargo.health.live import prune_history as prune_log
from cargo.postgres import client

HISTORY_FILE = "postgres_health.json.log"
HISTORY_HOURS = 6
CONNECTIONS_DEGRADED_PERCENT = 90


@dataclass(frozen=True)
class Reading:
	connections: int = 0
	max_connections: int = 0
	error: str = ""


class LiveHealth(ServiceHealth):
	history_file = HISTORY_FILE

	def is_live(self) -> bool:
		return self.doc.status == "Active"

	@cached_property
	def reading(self) -> Reading:
		try:
			rows = client.query(
				self.doc,
				"SELECT (SELECT count(*) FROM pg_stat_activity), current_setting('max_connections')::int",
			)
		except (client.Error, frappe.ValidationError) as error:
			return Reading(error=str(error).strip())
		connections, maximum = rows[0]
		return Reading(connections=int(connections), max_connections=int(maximum))

	def findings(self) -> list[Finding]:
		reading = self.reading
		if reading.error:
			return [Finding(CRITICAL, f"postgres could not be reached: {reading.error}")]
		if (
			reading.max_connections
			and reading.connections * 100 // reading.max_connections >= CONNECTIONS_DEGRADED_PERCENT
		):
			return [
				Finding(DEGRADED, f"{reading.connections} of {reading.max_connections} connections in use")
			]
		return []

	def log_entry(self, finding: Finding) -> dict:
		return {
			"record": self.doc.name,
			"severity": finding.severity,
			"reason": finding.reason,
			"connections": self.reading.connections,
			"max_connections": self.reading.max_connections,
			"error": self.reading.error,
		}


def prune_history() -> None:
	prune_log(HISTORY_FILE, HISTORY_HOURS)
