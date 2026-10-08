# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import ipaddress

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

from cargo.dns.resolver import verify_ptr_record
from cargo.mail.cluster import bootstrap, dns, naming
from cargo.mail.utils import log_exception

REMOVABLE_STATUSES = ("Pending", "Failed", "Disabled")


class StalwartNode(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		cluster: DF.Link
		enabled: DF.Check
		hostname: DF.Data
		in_ingress_dns: DF.Check
		installed_version: DF.Data | None
		ipv4_address: DF.Data
		ipv6_address: DF.Data | None
		is_bootstrap_node: DF.Check
		last_error: DF.SmallText | None
		last_health_at: DF.Datetime | None
		node_id: DF.Int
		provisioned_at: DF.Datetime | None
		ptr_verified: DF.Check
		role: DF.Literal["full", "frontend", "outbound"]
		location: DF.Data | None
		title: DF.Data | None
		status: DF.Literal[
			"Pending", "Provisioning", "Provisioned", "Active", "Draining", "Failed", "Disabled"
		]
	# end: auto-generated types

	# --- lifecycle ------------------------------------------------------------

	def autoname(self) -> None:
		self.hostname = naming.next_hostname("Stalwart Node", self.cluster, naming.NODE_PREFIX)
		self.name = self.hostname

	def validate(self) -> None:
		cluster = self.get_cluster()
		self.title = (self.title or "").strip() or self.hostname
		self.hostname = (self.hostname or "").strip().lower().rstrip(".")
		suffix = f".{cluster.default_domain}"
		if not self.hostname.endswith(suffix) or "." in self.hostname[: -len(suffix)]:
			frappe.throw(_("Hostname must be a single label under {0}.").format(cluster.default_domain))

		self.validate_single_node(cluster)
		self.ipv4_address = validate_ip(self.ipv4_address, 4)
		self.ipv6_address = validate_ip(self.ipv6_address, 6) if self.ipv6_address else None
		if self.is_new():
			self.status = "Pending"

	def validate_single_node(self, cluster: Document) -> None:
		"""Embedded stores live on one VPS: no second node, and roles that split work make no sense."""

		if not cluster.single_node:
			return
		other = frappe.db.get_value("Stalwart Node", {"cluster": cluster.name, "name": ["!=", self.name]})
		if other:
			frappe.throw(
				_("Cluster {0} uses an embedded store and can only have one node ({1}).").format(
					cluster.name, other
				)
			)
		if self.role != "full":
			frappe.throw(_("The only node of a cluster must have the full role."))

	def after_insert(self) -> None:
		dns.sync_node_records(self)
		self.get_cluster().save(ignore_permissions=True)  # re-validates stores for the new node count

	def on_update(self) -> None:
		before = self.get_doc_before_save()
		if not before:
			return
		if (before.ipv4_address, before.ipv6_address) != (self.ipv4_address, self.ipv6_address):
			dns.sync_node_records(self)
			dns.sync_spf_record(self.get_cluster())

		if before.enabled and not self.enabled and self.status in ("Active", "Provisioned"):
			bootstrap.drain_node(self)

	def on_trash(self) -> None:
		if self.status not in REMOVABLE_STATUSES:
			frappe.throw(_("Drain and disable the node before deleting it."))
		cluster = self.get_cluster()
		if self.is_bootstrap_node and cluster.status == "Active":
			frappe.throw(_("The bootstrap node of an active cluster cannot be deleted."))
		if cluster.bootstrap_node == self.name:
			# A failed first attempt: let another node bootstrap the cluster.
			cluster.db_set({"bootstrap_node": None, "status": "Pending"}, update_modified=False)

		bootstrap.forget_node(self)
		dns.delete_node_records(self)

	def after_delete(self) -> None:
		dns.sync_spf_record(self.get_cluster())

	# --- helpers --------------------------------------------------------------

	def get_cluster(self) -> Document:
		return frappe.get_cached_doc("Stalwart Cluster", self.cluster)

	def set_status(self, status: str, error: str | None = None) -> None:
		values = {"status": status}
		if error is not None:
			values["last_error"] = error[:1000]
		self.db_set(values, update_modified=False, notify=True)

	# --- actions --------------------------------------------------------------

	@frappe.whitelist()
	def drain(self) -> None:
		frappe.only_for("System Manager")
		bootstrap.drain_node(self)

	@frappe.whitelist()
	def restore(self) -> None:
		frappe.only_for("System Manager")
		bootstrap.restore_node(self)

	@frappe.whitelist()
	def check_health(self) -> bool:
		frappe.only_for("System Manager")
		return bootstrap.check_node(self)

	@frappe.whitelist()
	def verify_ptr(self) -> bool:
		frappe.only_for("System Manager")
		ok = verify_ptr_record(self.ipv4_address, self.hostname)
		if ok is None:
			return bool(self.ptr_verified)  # the lookup failed; the last known state stands
		self.db_set("ptr_verified", cint(ok), update_modified=False)
		return ok


def validate_ip(value: str | None, version: int) -> str:
	try:
		address = ipaddress.ip_address((value or "").strip())
	except ValueError:
		frappe.throw(_("{0} is not a valid IP address.").format(value))
	if address.version != version:
		frappe.throw(_("{0} is not an IPv{1} address.").format(value, version))
	return str(address)


def verify_all_ptr_records() -> None:
	for name in frappe.get_all("Stalwart Node", {"enabled": 1}, pluck="name"):
		node = frappe.get_doc("Stalwart Node", name)
		ok = verify_ptr_record(node.ipv4_address, node.hostname)
		if ok is not None:
			node.db_set("ptr_verified", cint(ok), update_modified=False)
