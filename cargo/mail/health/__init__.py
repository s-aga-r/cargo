from __future__ import annotations

import frappe

from cargo.health.shipping import get_metrics_info
from cargo.mail.health.live import LiveHealth, prune_history
from cargo.mail.health.telemetry import Telemetry

__all__ = ["LiveHealth", "Telemetry", "prune_history", "refresh_health", "ship_metrics"]


def live_clusters() -> list[str]:
	return frappe.get_all("Stalwart Cluster", filters={"status": "Active", "enabled": 1}, pluck="name")


def refresh_health() -> None:
	"""Re-read every active cluster's health from its nodes. Scheduled in `hooks.py`."""
	for name in live_clusters():
		try:
			LiveHealth(frappe.get_doc("Stalwart Cluster", name)).record()
		except Exception:
			frappe.log_error(title=f"Could not read {name} health")


def ship_metrics() -> None:
	"""Relay every active cluster's Stalwart metrics to datum. Scheduled in `hooks.py`.

	A host whose bench has no datum credentials yet has nothing to ship, which is a state
	to wait out rather than report every five minutes."""
	try:
		get_metrics_info()
	except RuntimeError:
		return

	for name in live_clusters():
		try:
			Telemetry(frappe.get_doc("Stalwart Cluster", name)).ship()
		except Exception:
			frappe.log_error(title=f"Could not ship {name} metrics")
