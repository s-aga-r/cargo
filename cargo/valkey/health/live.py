"""The worst thing true about the region's Valkey right now, read over the mesh."""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property

import frappe

from cargo.health.live import CRITICAL, DEGRADED, Finding
from cargo.health.live import LiveHealth as ServiceHealth
from cargo.health.live import prune_history as prune_log
from cargo.valkey import client

HISTORY_FILE = "valkey_health.json.log"
HISTORY_HOURS = 6
MEMORY_DEGRADED_PERCENT = 90


@dataclass(frozen=True)
class Reading:
	used_memory: int = 0
	max_memory: int = 0
	error: str = ""


class LiveHealth(ServiceHealth):
	history_file = HISTORY_FILE

	def is_live(self) -> bool:
		return self.doc.status == "Active"

	@cached_property
	def reading(self) -> Reading:
		try:
			info = client.command(self.doc, "INFO", "memory")
		except (client.Error, frappe.ValidationError) as error:
			return Reading(error=str(error).strip())
		values = dict(line.split(":", 1) for line in str(info).splitlines() if ":" in line)
		return Reading(
			used_memory=int(values.get("used_memory", 0)), max_memory=int(values.get("maxmemory", 0))
		)

	def findings(self) -> list[Finding]:
		reading = self.reading
		if reading.error:
			return [Finding(CRITICAL, f"valkey could not be reached: {reading.error}")]
		if reading.max_memory and reading.used_memory * 100 // reading.max_memory >= MEMORY_DEGRADED_PERCENT:
			used, maximum = reading.used_memory // 2**20, reading.max_memory // 2**20
			return [
				Finding(DEGRADED, f"{used} of {maximum} MB in use; keys with a lifetime are being evicted")
			]
		return []

	def log_entry(self, finding: Finding) -> dict:
		return {
			"record": self.doc.name,
			"severity": finding.severity,
			"reason": finding.reason,
			"used_memory": self.reading.used_memory,
			"max_memory": self.reading.max_memory,
			"error": self.reading.error,
		}


def prune_history() -> None:
	prune_log(HISTORY_FILE, HISTORY_HOURS)
