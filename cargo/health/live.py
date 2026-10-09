"""What every service's live health shares: a verdict in four words, written onto the record
the moment it changes, and a local log to read after the fact when datum was unreachable.

A service subclasses `LiveHealth` with what it reads and what counts as wrong; this says how
the findings are judged, recorded and logged."""

from __future__ import annotations

import json
import typing
from dataclasses import dataclass
from itertools import dropwhile
from pathlib import Path

import frappe
from frappe.utils.synchronization import filelock

if typing.TYPE_CHECKING:
	from frappe.model.document import Document

UNKNOWN, HEALTHY, DEGRADED, CRITICAL = "Unknown", "Healthy", "Degraded", "Critical"
SEVERITY = (UNKNOWN, HEALTHY, DEGRADED, CRITICAL)
HISTORY_LOCK = "health_log"


@dataclass(frozen=True)
class Finding:
	"""One thing wrong with a service, in words an operator can act on."""

	severity: str
	reason: str


class LiveHealth:
	"""The worst thing true about a service right now. Subclasses give `findings` and what
	one log line should carry; `history_file` names their log."""

	history_file = "health.json.log"

	def __init__(self, doc: Document) -> None:
		self.doc = doc

	def check(self) -> Finding:
		"""The worst thing true about this service right now, and why."""
		if self.doc.status == "Failed":
			return Finding(CRITICAL, "the build failed, so nothing is serving")

		# A service still being built is not judged: it has not promised anything yet.
		if not self.is_live():
			return Finding(UNKNOWN, "")

		return max(
			self.findings(),
			key=lambda finding: SEVERITY.index(finding.severity),
			default=Finding(HEALTHY, ""),
		)

	def is_live(self) -> bool:
		"""Whether the service has served once. Past that, a fault is a real failure."""
		return bool(self.doc.get("activated_on"))

	def findings(self) -> list[Finding]:
		raise NotImplementedError

	def log_entry(self, finding: Finding) -> dict:
		"""What one line of the log carries besides its timestamp."""
		return {"record": self.doc.name, "severity": finding.severity, "reason": finding.reason}

	def record(self) -> Finding:
		"""Write the verdict onto the record, and say so loudly when it is Critical."""
		finding = self.check()
		if (self.doc.health, self.doc.health_reason) != (finding.severity, finding.reason):
			self.doc.db_set({"health": finding.severity, "health_reason": finding.reason}, notify=True)
			if finding.severity == CRITICAL:
				frappe.log_error(title=f"{self.doc.name} is critical", message=finding.reason)

		self.dump(finding)

		return finding

	def dump(self, finding: Finding) -> None:
		"""Append this reading to the log. Kept only so there is something to read after the
		fact when datum was unreachable; `prune_history` drops it once it is old."""
		try:
			# Stamped under the lock, so the log stays ordered and `prune_history` can stop
			# at the first line still inside the window.
			with filelock(HISTORY_LOCK, is_global=True, timeout=5):
				entry = {"timestamp": frappe.utils.now_datetime().isoformat(), **self.log_entry(finding)}
				with history_path(self.history_file).open("a") as log:
					log.write(json.dumps(entry) + "\n")
		except Exception:
			# The verdict on the record matters more than the copy of it.
			frappe.log_error(title=f"Could not write {self.history_file}")


def history_path(name: str) -> Path:
	return Path(frappe.utils.get_bench_path()) / "logs" / name


def prune_history(name: str, hours: int) -> None:
	"""Drop readings past the window from one service's log."""
	path = history_path(name)
	if not path.exists():
		return

	cutoff = frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-hours).isoformat()
	try:
		with filelock(HISTORY_LOCK, is_global=True, timeout=30):
			# Appended in order, so everything expired is at the front.
			kept = list(
				dropwhile(lambda line: json.loads(line)["timestamp"] < cutoff, path.read_text().splitlines())
			)
			# Written whole and renamed over, so a crash leaves the old log rather than a torn line.
			scratch = path.with_suffix(".tmp")
			scratch.write_text("\n".join(kept) + "\n" if kept else "")
			scratch.replace(path)
	except Exception:
		frappe.log_error(title=f"Could not prune {name}")
