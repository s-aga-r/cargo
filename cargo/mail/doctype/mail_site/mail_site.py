# Copyright (c) 2026, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.functions import Count, Sum
from frappe.utils import cint, flt, now

from cargo.mail.stalwart.directory import DISK_QUOTA, GB
from cargo.mail.tenancy import sync
from cargo.mail.utils import log_exception

DIRECTORY_DOCTYPES = ("Mail Account", "Mail Group", "Mailing List", "Mail Domain")


# Documents with a mailbox of their own, hence a quota that counts against the site's total.
QUOTA_HOLDERS = ("Mail Account", "Mail Group")


class MailSite(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		archived_at: DF.Datetime | None
		cluster: DF.Link
		contact_email: DF.Data | None
		default_disk_quota_gb: DF.Float
		domain_verification_token: DF.Data | None
		egress_pool: DF.Link | None
		enabled: DF.Check
		max_accounts: DF.Int
		max_disk_gb: DF.Float
		max_domains: DF.Int
		max_groups: DF.Int
		mailboxes_allowed: DF.Check
		send_only_account: DF.Link | None
		max_mailing_lists: DF.Int
		site_name: DF.Data
		title: DF.Data | None
		status: DF.Literal["Active", "Suspended", "Archived"]
	# end: auto-generated types

	# --- lifecycle ------------------------------------------------------------

	def autoname(self) -> None:
		self.site_name = (self.site_name or "").strip().lower().rstrip("/")
		self.name = self.site_name

	def before_insert(self) -> None:
		self.status = "Active"
		self.domain_verification_token = self.domain_verification_token or frappe.generate_hash(length=32)

	def validate(self) -> None:
		self.site_name = (self.site_name or "").strip().lower().rstrip("/")
		if "/" in self.site_name or " " in self.site_name or "." not in self.site_name:
			frappe.throw(_("Site Name must be the site's domain, e.g. acme.frappe.cloud"))
		self.title = (self.title or "").strip() or self.site_name
		if self.contact_email:
			self.contact_email = self.contact_email.strip().lower()
			frappe.utils.validate_email_address(self.contact_email, throw=True)
		self.validate_disk_quotas()

		if self.is_new():
			cluster = frappe.get_cached_doc("Stalwart Cluster", self.cluster)
			if not cluster.enabled or cluster.status != "Active":
				frappe.throw(_("Cluster {0} is not active.").format(self.cluster))
		elif self.has_value_changed("cluster") and self.directory_size():
			# Ids and mailboxes live on the old cluster; moving them is a migration, not a field edit.
			frappe.throw(
				_("Site {0} has a directory on cluster {1}; it cannot be moved.").format(
					self.name, self.get_doc_before_save().cluster
				)
			)

		if (
			self.egress_pool
			and frappe.db.get_value("Egress IP Pool", self.egress_pool, "cluster") != self.cluster
		):
			frappe.throw(_("Egress pool {0} belongs to another cluster.").format(self.egress_pool))

	def after_insert(self) -> None:
		from cargo.mail.tenancy import platform

		try:
			platform.ensure_platform_address(self)
		except Exception:
			# The site exists without it; the hourly job tries again.
			log_exception(f"Could not create the platform address of {self.name}", self)

	def on_update(self) -> None:
		before = self.get_doc_before_save()
		if before and before.mailboxes_allowed and not self.mailboxes_allowed:
			self.withdraw_mailboxes()
		if before and before.egress_pool != self.egress_pool:
			from cargo.mail.cluster import egress

			egress.resync_cluster(self.get_cluster())

	def on_trash(self) -> None:
		if self.status != "Archived":
			frappe.throw(_("Archive the site before deleting it."))
		if frappe.db.exists("Mail Domain", {"site": self.name}):
			frappe.throw(_("Delete the site's mail domains first."))

	# --- actions --------------------------------------------------------------

	@frappe.whitelist()
	def adopt_directory(self) -> dict:
		"""Records the domains, accounts, groups and lists the cluster already holds for this site."""

		frappe.only_for("System Manager")
		from cargo.mail.tenancy.adopt import adopt_directory

		return adopt_directory(self.name)

	# --- state ----------------------------------------------------------------

	@frappe.whitelist()
	def suspend(self) -> None:
		frappe.only_for("System Manager")
		self.stop()

	@frappe.whitelist()
	def resume(self) -> None:
		frappe.only_for("System Manager")
		self.restart()

	@frappe.whitelist()
	def archive(self, delete_data: bool = False) -> None:
		frappe.only_for("System Manager")
		self.retire(delete_data=delete_data)

	def stop(self) -> None:
		"""Stops the site's mail as well as its API: a status check holds off the directory calls,
		the cluster role holds off every mailbox. Domains stay enabled, since disabling one drops
		its verification and a suspension is meant to be lifted."""

		if self.status == "Archived":
			frappe.throw(_("An archived site cannot be suspended."))
		self.db_set({"status": "Suspended"})
		sync.lock_site_accounts(self.name, locked=True)

	def restart(self) -> None:
		if self.status == "Archived":
			frappe.throw(_("An archived site cannot be resumed."))
		self.db_set({"enabled": 1, "status": "Active"})
		sync.lock_site_accounts(self.name, locked=False)

	def retire(self, delete_data: bool = False) -> None:
		"""Locks the site out and disables its domains, so they can be purged after their hold or
		claimed by a new site that proves control afresh. With ``delete_data`` every directory
		object is removed from Stalwart at once instead."""

		self.db_set({"enabled": 0, "status": "Archived", "archived_at": now()})
		# Locked and disabled first either way: with delete_data the purge runs later, one object a
		# transaction, and mail must not flow in the meantime.
		sync.lock_site_accounts(self.name, locked=True)
		self.disable_domains(_("The site was archived."))
		self.db_set("domain_verification_token", frappe.generate_hash(length=32))
		if not delete_data:
			return
		if frappe.flags.do_not_enqueue:
			purge_directory(self.name)
		else:
			frappe.enqueue(
				purge_directory,
				site=self.name,
				queue="long",
				job_id=f"purge-site:{self.name}",
				deduplicate=True,
				enqueue_after_commit=True,
			)

	def withdraw_mailboxes(self) -> None:
		"""An entitlement taken away reaches what the site already has: every account and group
		stops receiving, every catch-all goes. Lists are left to be deleted by their owner."""
		for doctype in ("Mail Account", "Mail Group"):
			for name in frappe.get_all(doctype, {"site": self.name, "disable_receiving": 0}, pluck="name"):
				doc = frappe.get_doc(doctype, name)
				doc.disable_receiving = 1
				doc.save(ignore_permissions=True)
		for name in frappe.get_all(
			"Mail Domain", {"site": self.name, "catch_all_address": ("is", "set")}, pluck="name"
		):
			domain = frappe.get_doc("Mail Domain", name)
			domain.catch_all_address = None
			domain.sub_addressing = 0
			domain.save(ignore_permissions=True)

	def disable_domains(self, reason: str) -> None:
		"""Every domain of the site, the hold counted from now: one disabled months ago would
		otherwise be purged the day after the archive."""
		for name in frappe.get_all("Mail Domain", {"site": self.name}, pluck="name"):
			domain = frappe.get_doc("Mail Domain", name)
			if domain.enabled:
				domain.enabled = 0
				domain.disabled_reason = reason
				domain.save(ignore_permissions=True)
			else:
				domain.db_set("disabled_at", now(), update_modified=False)

	# --- limits -----------------------------------------------------------------

	def validate_disk_quotas(self) -> None:
		for field in ("max_domains", "max_accounts", "max_groups", "max_mailing_lists"):
			if cint(self.get(field)) < 0:
				frappe.throw(
					_("{0} cannot be negative; 0 means unlimited.").format(_(self.meta.get_label(field)))
				)
		if flt(self.default_disk_quota_gb) <= 0:
			frappe.throw(_("Default Disk Quota must be above 0: every account needs a quota."))
		if flt(self.max_disk_gb) < 0:
			frappe.throw(_("Total Disk Quota cannot be negative; 0 means unlimited."))
		if flt(self.max_disk_gb) and flt(self.default_disk_quota_gb) > flt(self.max_disk_gb):
			frappe.throw(_("Default Disk Quota cannot exceed the site's Total Disk Quota."))

	def domain_count(self) -> int:
		return self.locked_count("Mail Domain")

	def account_count(self) -> int:
		"""The platform address is the site's but not of its making, so it is not counted."""
		return self.locked_count("Mail Account", own_only=True)

	def group_count(self) -> int:
		return self.locked_count("Mail Group")

	def mailing_list_count(self) -> int:
		return self.locked_count("Mailing List")

	def locked_count(self, doctype: str, own_only: bool = False) -> int:
		"""A locking read: under REPEATABLE READ a plain count sees the snapshot from before the
		site lock was taken, so two requests could both find room for the last slot."""
		table = frappe.qb.DocType(doctype)
		query = frappe.qb.from_(table).select(Count(table.name)).where(table.site == self.name)
		if own_only:
			query = query.where(table.is_platform_address == 0)
		rows = query.for_update().run()
		return cint(rows[0][0]) if rows else 0

	def assert_can_add_domain(self) -> None:
		self.lock()
		self.assert_within_limit(self.max_domains, self.domain_count(), _("domains"))

	def assert_can_add_account(self) -> None:
		self.lock()
		self.assert_within_limit(self.max_accounts, self.account_count(), _("accounts"))

	def assert_can_add_group(self) -> None:
		self.lock()
		self.assert_within_limit(self.max_groups, self.group_count(), _("groups"))

	def assert_can_add_mailing_list(self) -> None:
		self.lock()
		self.assert_within_limit(self.max_mailing_lists, self.mailing_list_count(), _("mailing lists"))

	def allocated_disk_gb(self, exclude: tuple[str, str] | None = None) -> float:
		"""Sum of the disk quotas of the site's accounts and groups, optionally leaving one out."""

		quota = frappe.qb.DocType("Mail Quota")
		total = 0
		for doctype in QUOTA_HOLDERS:
			holder = frappe.qb.DocType(doctype)
			query = (
				frappe.qb.from_(quota)
				.join(holder)
				.on((quota.parent == holder.name) & (quota.parenttype == doctype))
				.select(Sum(quota.value))
				.where((holder.site == self.name) & (quota.quota == DISK_QUOTA))
			)
			if doctype == "Mail Account":
				query = query.where(holder.is_platform_address == 0)
			query = query.for_update()
			if exclude and exclude[0] == doctype:
				query = query.where(holder.name != exclude[1])
			total += cint(query.run()[0][0])
		return round(total / GB, 6)

	def validate_quota_of(self, doc: Document) -> None:
		"""Accounts and groups: a disk quota above 0 (defaulting to the site's), within the site's total."""

		if doc.is_new() and doc.disk_row() is None:
			doc.set_disk_quota_gb(self.default_disk_quota_gb)
		if doc.disk_quota_bytes() <= 0:
			frappe.throw(_("Disk Quota must be above 0 GB."))
		if doc.flags.adopting or doc.get("is_platform_address"):
			return  # the cluster already grants it; the operator sizes the site's total afterwards
		before = doc.get_doc_before_save()
		if doc.is_new() or before.disk_quota_bytes() != doc.disk_quota_bytes():
			exclude = None if doc.is_new() else (doc.doctype, doc.name)
			self.assert_can_allocate_disk(doc.allotted_disk_gb(), exclude)

	def assert_can_allocate_disk(self, quota_gb: float, exclude: tuple[str, str] | None = None) -> None:
		"""The site's accounts and groups together may not exceed its total disk quota (0 = unlimited)."""

		if not flt(self.max_disk_gb):
			return
		self.lock()
		allocated = self.allocated_disk_gb(exclude)
		if allocated + flt(quota_gb) > flt(self.max_disk_gb):
			frappe.throw(
				_("Site {0} has {1} GB of its {2} GB total disk quota left; {3} GB requested.").format(
					self.name, round(flt(self.max_disk_gb) - allocated, 2), self.max_disk_gb, quota_gb
				)
			)

	def directory_size(self) -> int:
		return sum(frappe.db.count(doctype, {"site": self.name}) for doctype in DIRECTORY_DOCTYPES)

	def lock(self) -> None:
		"""Serialises this site's limit checks: two requests must not both see room for the last slot."""

		if frappe.db.exists("Mail Site", self.name):
			frappe.db.get_value("Mail Site", self.name, "name", for_update=True)

	def assert_within_limit(self, limit: int, current: int, what: str) -> None:
		"""A limit of 0 means unlimited."""

		if limit and current >= limit:
			frappe.throw(_("Site {0} has reached its limit of {1} {2}.").format(self.name, limit, what))

	# --- helpers ----------------------------------------------------------------

	def get_cluster(self) -> Document:
		return frappe.get_cached_doc("Stalwart Cluster", self.cluster)

	def to_api(self) -> dict:
		cluster = self.get_cluster()
		return {
			"site": self.name,
			"cluster": self.cluster,
			"status": self.status,
			"title": self.title,
			"contact_email": self.contact_email,
			"enabled": bool(self.enabled),
			"mailboxes_allowed": bool(self.mailboxes_allowed),
			"send_only_address": self.send_only_account,
			"jmap_url": cluster.base_url,
			"mail_hostname": cluster.hostname,
			"limits": {
				"max_domains": self.max_domains,
				"max_accounts": self.max_accounts,
				"max_groups": self.max_groups,
				"max_mailing_lists": self.max_mailing_lists,
				"max_disk_gb": self.max_disk_gb,
				"default_disk_quota_gb": self.default_disk_quota_gb,
			},
			"usage": {
				"domains": self.domain_count(),
				"accounts": self.account_count(),
				"groups": self.group_count(),
				"mailing_lists": self.mailing_list_count(),
				"allocated_disk_gb": self.allocated_disk_gb(),
			},
		}


def purge_directory(site: str) -> None:
	"""Deletes an archived site's directory one document at a time.

	Each delete is its own transaction: every document destroys its Stalwart object inside its
	delete, so a failure half-way must not resurrect rows whose objects are already gone.
	"""

	for doctype in DIRECTORY_DOCTYPES:
		for name in frappe.get_all(doctype, {"site": site}, pluck="name"):
			frappe.delete_doc(doctype, name, ignore_permissions=True)
			if not frappe.in_test:
				frappe.db.commit()  # nosemgrep
