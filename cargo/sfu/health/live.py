"""Whether the SFU answers on its public name, as a browser would reach it."""

from __future__ import annotations

from functools import cached_property

import requests

from cargo.health.live import CRITICAL, Finding
from cargo.health.live import LiveHealth as ServiceHealth
from cargo.health.live import prune_history as prune_log

HISTORY_FILE = "sfu_health.json.log"
HISTORY_HOURS = 6
PROBE_TIMEOUT = 5


class LiveHealth(ServiceHealth):
	history_file = HISTORY_FILE

	def is_live(self) -> bool:
		return self.doc.status == "Active"

	@cached_property
	def problem(self) -> str:
		"""Empty when /health answers 2xx over HTTPS on the hostname; otherwise why not."""
		try:
			response = requests.get(f"{self.doc.service_endpoint}/health", timeout=PROBE_TIMEOUT)
		except requests.RequestException as error:
			return f"did not answer: {error.__class__.__name__}"
		return "" if response.ok else f"answered {response.status_code} to /health"

	def findings(self) -> list[Finding]:
		return [Finding(CRITICAL, f"the SFU {self.problem}")] if self.problem else []

	def log_entry(self, finding: Finding) -> dict:
		return {
			"record": self.doc.name,
			"severity": finding.severity,
			"reason": finding.reason,
			"problem": self.problem,
		}


def prune_history() -> None:
	prune_log(HISTORY_FILE, HISTORY_HOURS)
