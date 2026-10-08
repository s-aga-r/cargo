# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now

from cargo.cloud_mail.cluster import dns, egress, naming, plan
from cargo.cloud_mail.doctype.stalwart_node.stalwart_node import validate_ip
from cargo.cloud_mail.stalwart import get_admin_client, get_client
from cargo.cloud_mail.utils import dkim_algorithms, log_exception, validate_version


class EgressGateway(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		admin_password: DF.Password | None
		admin_username: DF.Data | None
		api_key: DF.Password | None
		base_url: DF.Data | None
		cluster: DF.Link
		config_plan: DF.Code | None
		config_version: DF.Int
		data_store: DF.Link | None
		enabled: DF.Check
		hostname: DF.Data
		installed_version: DF.Data | None
		ipv4_address: DF.Data
		last_config_sync_at: DF.Datetime | None
		last_error: DF.SmallText | None
		provisioned_at: DF.Datetime | None
		stalwart_version: DF.Data | None
		location: DF.Data | None
		title: DF.Data | None
		status: DF.Literal["Pending", "Provisioning", "Provisioned", "Active", "Failed", "Disabled"]
	# end: auto-generated types

	# --- lifecycle --------------------------------------------------------------

	def autoname(self) -> None:
		self.hostname = naming.next_hostname("Egress Gateway", self.cluster, naming.GATEWAY_PREFIX)
		self.name = self.hostname

	def before_insert(self) -> None:
		self.status = "Pending"
		self.admin_username = self.admin_username or "admin"
		if not self.admin_password:
			self.admin_password = frappe.generate_hash(length=32)
		if not self.data_store:
			self.data_store = self.create_local_store().name

	def validate(self) -> None:
		cluster = self.get_cluster()
		self.title = (self.title or "").strip() or self.hostname
		self.hostname = (self.hostname or "").strip().lower().rstrip(".")
		suffix = f".{cluster.default_domain}"
		if not self.hostname.endswith(suffix) or "." in self.hostname[: -len(suffix)]:
			frappe.throw(_("Hostname must be a single label under {0}.").format(cluster.default_domain))

		self.base_url = f"https://{self.hostname}"
		self.ipv4_address = validate_ip(self.ipv4_address, 4)
		self.stalwart_version = validate_version(
			self.stalwart_version or cluster.stalwart_version,
			_("Stalwart Version"),
		)

	def after_insert(self) -> None:
		dns.sync_gateway_records(self)

	def on_update(self) -> None:
		before = self.get_doc_before_save()
		if before and before.ipv4_address != self.ipv4_address:
			dns.sync_gateway_records(self)
			for pool in self.pools():
				dns.sync_pool_records(pool)
		if before and before.enabled and not self.enabled and self.status in ("Provisioned", "Active"):
			self.set_status("Disabled")

	def on_trash(self) -> None:
		if self.status in ("Provisioning", "Provisioned", "Active"):
			frappe.throw(_("Disable the gateway before deleting it."))
		if frappe.db.exists("Egress IP Pool Address", {"gateway": self.name}):
			frappe.throw(_("Remove this gateway's addresses from every pool first."))
		dns.delete_gateway_records(self)

	# --- helpers ------------------------------------------------------------------

	def create_local_store(self) -> Document:
		store = frappe.get_doc(
			{
				"doctype": "Stalwart Store",
				"title": f"{self.hostname} local data",
				"kind": "Data",
				"type": "RocksDb",
				"path": "/var/lib/stalwart",
			}
		)
		store.insert(ignore_permissions=True)
		return store

	def get_cluster(self) -> Document:
		return frappe.get_cached_doc("Stalwart Cluster", self.cluster)

	def get_store(self, field: str = "data_store") -> Document | None:
		return frappe.get_cached_doc("Stalwart Store", self.get(field)) if self.get(field) else None

	def pools(self) -> list[Document]:
		names = frappe.get_all(
			"Egress IP Pool Address", {"gateway": self.name}, pluck="parent", distinct=True
		)
		return [frappe.get_doc("Egress IP Pool", name) for name in sorted(set(names))]

	def set_status(self, status: str, error: str | None = None) -> None:
		serving_before = self.status == "Active"
		values = {"status": status}
		if error is not None:
			values["last_error"] = error[:1000]
		self.db_set(values, update_modified=False, notify=True)
		if serving_before != (status == "Active"):
			self.resync_pool_records()

	def resync_pool_records(self) -> None:
		"""The pool hostnames list only serving gateways, so they follow this one's status."""

		for pool in self.pools():
			try:
				dns.sync_pool_records(pool)
			except Exception:
				log_exception(f"Pool DNS for {pool.name} could not follow {self.name}", self)

	def get_client(self):
		return get_client(self)

	def get_admin_client(self):
		return get_admin_client(self)

	def bump_config_version(self, rendered_plan: list[dict]) -> None:
		self.db_set(
			{"config_version": (self.config_version or 0) + 1, "config_plan": plan.redacted(rendered_plan)},
			update_modified=False,
		)

	# --- actions --------------------------------------------------------------------

	@frappe.whitelist()
	def sync_config(self) -> dict:
		frappe.only_for("System Manager")
		if self.status != "Active":
			frappe.throw(_("Only an active gateway can be synced."))
		return self.push_config()

	def push_config(self) -> dict:
		rendered = egress.gateway_plan(self)
		result = self.get_client().apply(rendered)
		self.get_client().reload_settings()
		self.bump_config_version(rendered)
		self.db_set("last_config_sync_at", now(), update_modified=False)
		return {"created": result.created, "updated": result.updated, "unchanged": result.unchanged}

	@frappe.whitelist()
	def preview_plan(self) -> str:
		frappe.only_for("System Manager")
		return plan.redacted(egress.gateway_plan(self))

	@frappe.whitelist()
	def show_admin_password(self) -> str:
		frappe.only_for("Administrator")
		return self.get_password("admin_password")

	@frappe.whitelist()
	def check_health(self) -> bool:
		frappe.only_for("System Manager")
		return egress.check_gateway(self)

	@frappe.whitelist()
	def replace_dkim_keys(self) -> None:
		"""Emergency replacement of the gateway domain's keys after a leak, same selectors."""

		frappe.only_for("System Manager")
		client = self.get_client()
		domain = client.domains.find_by_name(self.hostname)
		if not domain:
			frappe.throw(_("The gateway does not hold its domain {0} yet.").format(self.hostname))
		client.domains.replace_dkim_keys(domain["id"], dkim_algorithms())
