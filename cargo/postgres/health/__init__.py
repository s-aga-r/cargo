from __future__ import annotations

import frappe

from cargo.postgres.health.live import LiveHealth, prune_history

__all__ = ["LiveHealth", "prune_history", "refresh_health"]


def refresh_health() -> None:
	"""Re-read the server's health over the mesh. Scheduled in `hooks.py`."""
	server = frappe.get_single("Postgres Server")
	if server.status not in ("Active", "Failed"):
		return
	try:
		LiveHealth(server).record()
	except Exception:
		frappe.log_error(title="Could not read Postgres Server health")
