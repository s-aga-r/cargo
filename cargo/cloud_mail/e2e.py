"""One single-node mail cluster taken through the real flows on fake_atlas.

Only `tools/e2e/mail.sh` calls this, through `bench execute`; nothing in production does.
Every function prints `key=value` lines for the shell to read."""

from __future__ import annotations

import frappe
from frappe.utils import cint
from frappe.utils.password import set_encrypted_password

from cargo.cloud_mail.cluster import plan
from cargo.cloud_mail.health.live import LiveHealth
from cargo.cloud_mail.tenancy import sync
from cargo.testing import make_dns_zone


def prepare(atlas_url: str) -> None:
	"""Point Cargo at fake_atlas and give it a provider-less zone and a RocksDb cluster."""
	settings = frappe.get_single("Cargo Settings")
	if not settings.wildcard_domain:
		frappe.throw("Cargo Settings needs a wildcard domain; the zone hangs off it.")
	zone = make_dns_zone(f"mail.{settings.wildcard_domain}", default=False)
	# Written field by field: a development site may not have every other setting filled in.
	frappe.db.set_single_value(
		"Cargo Settings",
		{"atlas_url": atlas_url, "atlas_tenant_id": cint(settings.atlas_tenant_id), "dns_zone": zone.name},
	)
	if not settings.get_password("atlas_token", raise_exception=False):
		set_encrypted_password("Cargo Settings", "Cargo Settings", "e2e-atlas-token", "atlas_token")
	frappe.clear_document_cache("Cargo Settings", "Cargo Settings")

	mail_settings = frappe.get_single("Mail Settings")
	mail_settings.update({"skip_domain_verification": 1, "verify_stalwart_tls": 0, "host_firewall": 0})
	mail_settings.save()

	cluster_name = frappe.db.exists("Stalwart Cluster", {"dns_zone": zone.name})
	cluster = (
		frappe.get_doc("Stalwart Cluster", cluster_name)
		if cluster_name
		else frappe.get_doc(
			{
				"doctype": "Stalwart Cluster",
				"title": "e2e",
				"dns_zone": zone.name,
				"acme_contact_email": "ops@e2e.invalid",
				"certificate_management": "Manual",
			}
		).insert()
	)
	frappe.db.commit()  # nosemgrep: bench execute runs outside a request
	print(f"cluster={cluster.name}")
	print(f"hosts=127.0.0.1 {cluster.hostname} n1.{cluster.default_domain}")


def _cluster():
	name = frappe.db.get_value("Stalwart Cluster", {"title": "e2e"})
	if not name:
		frappe.throw("Run prepare first.")
	return frappe.get_doc("Stalwart Cluster", name)


def _node():
	cluster = _cluster()
	name = frappe.db.get_value("Stalwart Node", {"cluster": cluster.name})
	if not name:
		frappe.throw("Run request_node first.")
	return frappe.get_doc("Stalwart Node", name)


def request_node() -> None:
	"""The cluster's one node, with a machine asked of fake_atlas."""
	cluster = _cluster()
	name = frappe.db.get_value("Stalwart Node", {"cluster": cluster.name})
	node = (
		frappe.get_doc("Stalwart Node", name)
		if name
		else frappe.get_doc({"doctype": "Stalwart Node", "cluster": cluster.name, "role": "full"}).insert()
	)
	if not node.machine:
		node.request_machine(1000, 1, 10)
	frappe.db.commit()  # nosemgrep
	print(f"node={node.name}")
	print(f"machine={node.machine}")
	print(f"vm_id={frappe.db.get_value('Machine', node.machine, 'vm_id')}")


def status() -> None:
	"""Where the bring-up stands, one line per fact. The scheduler moves it along."""
	node = _node()
	cluster = node.get_cluster()
	machine = frappe.db.get_value("Machine", node.machine, ["status", "vm_id"], as_dict=True) or {}
	print(f"cluster={cluster.status}")
	print(f"node={node.status}")
	print(f"machine={machine.get('status')}")
	print(f"container=cargo-fake-{machine.get('vm_id')}")
	print(f"marker={plan.marker(plan.recovery_plan(cluster))}")
	print(f"error={(node.last_error or '').splitlines()[0] if node.last_error else ''}")


def reprovision() -> None:
	"""Provision the node again: configure.sh on a live cluster must change nothing."""
	node = _node()
	node.set_status("Pending")
	node.start_provisioning()
	frappe.db.commit()  # nosemgrep
	print("started=1")


def verify() -> None:
	"""What a brought-up cluster must look like from Cargo's side. Prints `failed=<n>` last."""
	node = _node()
	cluster = node.get_cluster()
	checks = {
		"the cluster is active": cluster.status == "Active",
		"the node is active": node.status == "Active",
		"the node is the bootstrap node": bool(node.is_bootstrap_node),
		"the node has its public address": node.ipv4_address == "127.0.0.1",
	}
	if cluster.status == "Active":
		checks["nothing drifted from the plan"] = not cluster.check_drift().get("differences")
		client = cluster.get_client()
		checks["the disabled-accounts role exists"] = bool(
			client.roles.find_by_description(plan.DISABLED_ROLE_DESCRIPTION)
		)
		checks["the default domain exists"] = bool(client.domains.find_by_name(cluster.default_domain))
		sync.disabled_role_id(cluster)  # the role is usable, not just present
		finding = LiveHealth(cluster).record()
		checks[f"health is Healthy ({finding.severity}: {finding.reason})"] = finding.severity == "Healthy"

	failed = 0
	for label, passed in checks.items():
		print(f"{'ok   ' if passed else 'FAIL '} {label}")
		failed += not passed
	frappe.db.commit()  # nosemgrep
	print(f"failed={failed}")
