from __future__ import annotations

import frappe

from cargo.cloud_mail.health.live import LiveHealth, prune_history

__all__ = ["LiveHealth", "prune_history", "refresh_health"]


def live_clusters() -> list[str]:
	return frappe.get_all("Stalwart Cluster", filters={"status": "Active", "enabled": 1}, pluck="name")


def refresh_health() -> None:
	"""Re-read every active cluster's health from its nodes. Scheduled in `hooks.py`."""
	for name in live_clusters():
		try:
			LiveHealth(frappe.get_doc("Stalwart Cluster", name)).record()
		except Exception:
			frappe.log_error(title=f"Could not read {name} health")
