"""What a mail cluster in a real region must look like from Cargo's side. Called by
`tools/mail-smoke/check.sh` through `bench execute`; prints `ok`/`FAIL` lines, facts the shell
needs as `key=value`, and `failed=<n>` last. Nothing here changes the cluster."""

from __future__ import annotations

import frappe

from cargo.cloud_mail.cluster import dns
from cargo.cloud_mail.cluster.reconcile import directory_report
from cargo.cloud_mail.health.live import LiveHealth
from cargo.cloud_mail.stalwart.errors import StalwartError


class Checks:
	def __init__(self) -> None:
		self.failed = 0

	def check(self, label: str, passed: bool, detail: str = "") -> None:
		detail = " ".join(str(detail).split())  # one line per check, whatever the detail was
		print(f"{'ok   ' if passed else 'FAIL '} {label}" + (f": {detail}" if detail and not passed else ""))
		self.failed += not passed

	def finish(self) -> None:
		print(f"failed={self.failed}")


def _cluster(name: str):
	if not frappe.db.exists("Stalwart Cluster", name):
		frappe.throw(f"No Stalwart Cluster named {name}.")
	return frappe.get_doc("Stalwart Cluster", name)


def checks(cluster: str, phase: int = 3) -> None:
	"""The Cargo-side checks for a phase; the shell does the network's."""
	doc = _cluster(cluster)
	checks = Checks()
	nodes = frappe.get_all(
		"Stalwart Node",
		{"cluster": doc.name, "enabled": 1},
		["name", "hostname", "status", "ipv4_address", "installed_version", "ptr_verified", "role"],
		order_by="name",
	)
	active = [n for n in nodes if n.status == "Active"]

	checks.check("the cluster is active", doc.status == "Active", doc.status)
	finding = LiveHealth(doc).check()
	checks.check("health is Healthy", finding.severity == "Healthy", f"{finding.severity}: {finding.reason}")
	checks.check("at least one node is active", bool(active), f"{len(nodes)} nodes, none active")
	try:
		differences = doc.check_drift().get("differences") or []
		checks.check("nothing drifted from the plan", not differences, frappe.as_json(differences)[:400])
		report = directory_report(doc)
		gaps = {k: v for k, v in report.items() if k != "checked_at" and v}
		checks.check("the directory matches Stalwart", not gaps, frappe.as_json(gaps)[:400])
		admin = doc.get_admin_client()
		settings = admin.singleton("SystemSettings").read()
		certificate_id = settings.get("defaultCertificateId")
		names: list[str] = []
		if certificate_id:
			certificate = admin.objects("Certificate").get(certificate_id) or {}
			sans = certificate.get("subjectAlternativeNames") or []
			names = list(sans.keys()) if isinstance(sans, dict) else list(sans)
		covered = doc.hostname in names or f"*.{doc.default_domain}" in names
		checks.check(
			"the default certificate covers the hostname",
			bool(certificate_id) and covered,
			f"{certificate_id} {names}",
		)
		checks.check(
			"jmap answers 200 with Cargo's key", bool(doc.get_client().connection.session.get("apiUrl"))
		)
	except StalwartError as error:
		checks.check("the management api answers", False, str(error))

	for node in active:
		doc_node = frappe.get_doc("Stalwart Node", node.name)
		checks.check(
			f"{node.hostname} has reverse dns", bool(doc_node.verify_ptr()), node.ipv4_address or "no address"
		)
		in_ingress = frappe.db.exists(
			"DNS Record", {"managed_by": node.name, "host": dns.relative_host(doc.hostname, doc.dns_zone)}
		)
		checks.check(f"{node.hostname} is in the ingress record", bool(in_ingress) or node.role == "outbound")

	platform = frappe.db.get_value(
		"Mail Domain", doc.default_domain, ["is_verified", "enabled"], as_dict=True
	)
	checks.check(
		"the platform domain is adopted and verified",
		bool(platform and platform.is_verified and platform.enabled),
		str(platform),
	)
	addresses = frappe.db.count("Mail Account", {"is_platform_address": 1, "cluster": doc.name})
	checks.check("sites hold platform addresses", addresses > 0, "none yet")

	if phase >= 6:
		checks.check("three or more nodes serve", len(active) >= 3, str(len(active)))
		versions = {n.installed_version for n in active}
		checks.check(
			"every node runs the cluster's version", versions == {doc.stalwart_version}, str(versions)
		)
		upgraded = frappe.db.exists(
			"Press Workflow",
			{
				"linked_doctype": "Stalwart Cluster",
				"linked_docname": doc.name,
				"main_method_name": "_upgrade_nodes",
				"status": "Success",
			},
		)
		checks.check("a rolling upgrade has completed", bool(upgraded))
		gateways = frappe.db.count("Egress Gateway", {"cluster": doc.name, "status": "Active"})
		checks.check("an egress gateway serves", gateways > 0, str(gateways))

	if phase >= 7:
		checks.check("grants are required", bool(frappe.get_cached_doc("Mail Settings").require_domain_grant))
		granted = frappe.db.exists(
			"Mail Domain", {"cluster": doc.name, "site": ("is", "set"), "is_verified": 1}
		)
		checks.check("a site holds a verified domain", bool(granted))
		legacy = frappe.db.count("Mail Domain DNS Record", {"host": ("like", "frappemail-%")})
		checks.check("no domain carries a frappemail selector", legacy == 0, str(legacy))

	checks.finish()


def facts(cluster: str) -> None:
	"""What the shell needs to ask the network about."""
	doc = _cluster(cluster)
	print(f"hostname={doc.hostname}")
	print(f"zone={doc.default_domain}")
	print(f"spf_include={dns.spf_include(doc)}")
	rows = frappe.get_all(
		"Mail Domain DNS Record",
		{"parent": doc.default_domain, "parenttype": "Mail Domain", "category": "DKIM"},
		["host", "value"],
	)
	print("dkim_hosts=" + " ".join(f"{row.host}.{doc.default_domain}" for row in rows))
	nodes = frappe.get_all(
		"Stalwart Node", {"cluster": doc.name, "status": "Active"}, ["hostname", "ipv4_address"]
	)
	print("nodes=" + " ".join(f"{n.hostname}={n.ipv4_address}" for n in nodes))
