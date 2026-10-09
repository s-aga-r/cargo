from __future__ import annotations

import frappe

from cargo.valkey.health.live import LiveHealth, prune_history

__all__ = ["LiveHealth", "prune_history", "refresh_health"]


def refresh_health() -> None:
	server = frappe.get_single("Valkey Server")
	if server.status not in ("Active", "Failed"):
		return
	try:
		LiveHealth(server).record()
	except Exception:
		frappe.log_error(title="Could not read Valkey Server health")
