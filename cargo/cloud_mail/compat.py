"""The scripts Cargo renders, against a real Stalwart. Called by `tools/stalwart-compat/run.sh`
through `bench execute`; prints `key=value` lines for the shell."""

from __future__ import annotations

from pathlib import Path

import frappe

from cargo.cloud_mail.cluster import bootstrap, plan
from cargo.cloud_mail.cluster.bootstrap import SCRIPTS
from cargo.ssh import script
from cargo.testing import make_dns_zone

LABEL = "compat"
ZONE = "compat.test"
POSTGRES_SECRET = "compat-secret"


def _store(title: str, **fields):
	name = frappe.db.exists("Stalwart Store", {"title": title})
	if name:
		return frappe.get_doc("Stalwart Store", name)
	return frappe.get_doc({"doctype": "Stalwart Store", "title": title, **fields}).insert()


def render(directory: str) -> None:
	"""Write install.sh and bootstrap.sh for a Postgres and Redis cluster to `directory`.

	The container they run in has both stores on localhost. The files carry the cluster's
	secrets, so the directory is the caller's to remove."""
	settings = frappe.get_single("Mail Settings")
	settings.update({"verify_stalwart_tls": 0, "host_firewall": 0, "skip_domain_verification": 1})
	settings.save()
	zone = make_dns_zone(ZONE, default=False)
	data = _store(
		"compat Postgres",
		kind="Data",
		type="PostgreSql",
		host="127.0.0.1",
		port=5432,
		database="stalwart",
		auth_username="stalwart",
		auth_secret=POSTGRES_SECRET,
	)
	memory = _store("compat Redis", kind="In-Memory", type="Redis", url="redis://127.0.0.1:6379")

	name = frappe.db.exists("Stalwart Cluster", {"label": LABEL, "dns_zone": zone.name})
	cluster = (
		frappe.get_doc("Stalwart Cluster", name)
		if name
		else frappe.get_doc(
			{
				"doctype": "Stalwart Cluster",
				"title": "compat",
				"label": LABEL,
				"dns_zone": zone.name,
				"acme_contact_email": "ops@compat.test",
				"certificate_management": "Manual",
				"data_store": data.name,
				"in_memory_store": memory.name,
				"regions": [{"region": LABEL}],
			}
		).insert()
	)
	node_name = frappe.db.get_value("Stalwart Node", {"cluster": cluster.name})
	node = (
		frappe.get_doc("Stalwart Node", node_name)
		if node_name
		else frappe.get_doc(
			{"doctype": "Stalwart Node", "cluster": cluster.name, "role": "full", "ipv4_address": "127.0.0.1"}
		).insert()
	)
	bootstrap.start_bootstrap(node)

	target = Path(directory)
	target.mkdir(parents=True, exist_ok=True)
	environment, _secrets = bootstrap.bootstrap_environment(node)
	for filename, env in (
		("install.sh", bootstrap.install_environment(node)),
		("bootstrap.sh", environment),
	):
		path = target / filename
		path.touch(mode=0o600)
		path.write_text(script(*SCRIPTS, filename, environment=env))
	frappe.db.commit()  # nosemgrep: bench execute runs outside a request
	print(f"cluster={cluster.name}")
	print(f"hostname={cluster.hostname}")
	print(f"marker={plan.marker(plan.recovery_plan(cluster))}")


def verify() -> None:
	"""Read back, through Cargo's own client, what the scripts were meant to leave behind."""
	name = frappe.db.get_value("Stalwart Cluster", {"label": LABEL})
	cluster = frappe.get_doc("Stalwart Cluster", name)
	node = frappe.get_doc("Stalwart Node", cluster.bootstrap_node)
	node.db_set("status", "Provisioned")
	finished = bootstrap.finish_bootstrap(cluster)
	cluster.reload()
	node.reload()
	checks = {f"the bootstrap finished ({node.last_error or 'no error'})": finished}
	if finished:
		client = cluster.get_client()
		settings = client.singleton("SystemSettings").read()
		checks["the default domain was created"] = bool(client.domains.find_by_name(cluster.default_domain))
		checks["the disabled-accounts role was created"] = bool(
			client.roles.find_by_description(plan.DISABLED_ROLE_DESCRIPTION)
		)
		checks["the hostname is the system default"] = settings.get("defaultHostname") == cluster.hostname
		checks["nothing drifted from the plan"] = not cluster.check_drift().get("differences")

	failed = 0
	for label, passed in checks.items():
		print(f"{'ok   ' if passed else 'FAIL '} {label}")
		failed += not passed
	frappe.db.commit()  # nosemgrep
	print(f"failed={failed}")
