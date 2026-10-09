"""The scripts Cargo renders, against a real Stalwart. Called by `tools/stalwart-compat/run.sh`
through `bench execute`; prints `key=value` lines for the shell."""

from __future__ import annotations

from pathlib import Path

import frappe

from cargo.mail.cluster import bootstrap, plan
from cargo.mail.cluster.bootstrap import SCRIPTS
from cargo.ssh import script
from cargo.testing import make_dns_zone

ZONE = "compat.test"
POSTGRES_SECRET = "compat-secret"


def _postgres_database():
	"""The container's Postgres, as a Postgres Database already holding the role run.sh made."""
	server = frappe.get_single("Postgres Server")
	if not server.machine:
		server.machine = (
			frappe.get_doc(
				{
					"doctype": "Machine",
					"reference_doctype": "Postgres Server",
					"reference_name": "Postgres Server",
					"role": "postgres",
					"disk_size_gb": 10,
					"vm_id": "compat-postgres",
					"address": "127.0.0.1",
					"status": "Running",
				}
			)
			.insert()
			.name
		)
		server.save()
	server.db_set("status", "Active")
	frappe.clear_document_cache("Postgres Server", "Postgres Server")
	if frappe.db.exists("Postgres Database", "stalwart"):
		return frappe.get_doc("Postgres Database", "stalwart")
	database = frappe.get_doc(
		{"doctype": "Postgres Database", "database_name": "stalwart", "password": POSTGRES_SECRET}
	)
	database.flags.adopting = True
	return database.insert()


def render(directory: str) -> None:
	"""Write install.sh and bootstrap.sh for a Postgres-backed cluster to `directory`.

	The container they run in has Postgres on localhost. The files carry the cluster's
	secrets, so the directory is the caller's to remove."""
	settings = frappe.get_single("Mail Settings")
	settings.update({"verify_stalwart_tls": 0, "host_firewall": 0, "skip_domain_verification": 1})
	settings.save()
	zone = make_dns_zone(ZONE, default=False)
	data = _postgres_database()

	name = frappe.db.exists("Stalwart Cluster", {"dns_zone": zone.name})
	cluster = (
		frappe.get_doc("Stalwart Cluster", name)
		if name
		else frappe.get_doc(
			{
				"doctype": "Stalwart Cluster",
				"title": "compat",
				"dns_zone": zone.name,
				"acme_contact_email": "ops@compat.test",
				"certificate_management": "Manual",
				"data_store": data.name,
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
	# bench execute runs outside a request
	frappe.db.commit()  # nosemgrep
	print(f"cluster={cluster.name}")
	print(f"hostname={cluster.hostname}")
	print(f"marker={plan.marker(plan.recovery_plan(cluster))}")


def verify() -> None:
	"""Read back, through Cargo's own client, what the scripts were meant to leave behind."""
	name = frappe.db.get_value("Stalwart Cluster", {"dns_zone": ZONE})
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
