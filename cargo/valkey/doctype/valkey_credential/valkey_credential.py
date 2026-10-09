# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document

from cargo.valkey.client import commands

NAME = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
SECRET_LENGTH = 32
# Everything on every key and channel: the services that use Valkey share it by trust, not
# by key prefix, and a rate limiter needs the same commands a coordinator does.
RULES = ("~*", "&*", "+@all")


class ValkeyCredential(Document):
	"""One service's ACL user on the region's Valkey."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		created_on_server: DF.Check
		password: DF.Password | None
		reference_doctype: DF.Link | None
		reference_name: DF.DynamicLink | None
		username: DF.Data
	# end: auto-generated types

	def validate(self) -> None:
		self.set_names()

	def set_names(self) -> None:
		self.username = (self.username or "").strip().lower()
		if not NAME.match(self.username) or self.username == "default":
			frappe.throw(
				_("Username must be lowercase letters, digits, dashes and underscores, and not default.")
			)

	def before_insert(self) -> None:
		self.set_names()
		if self.flags.adopting:  # the server already holds it, password and all
			return
		server = self.get_server()
		if server.status != "Active":
			frappe.throw(_("The Valkey server is not active."))
		self.password = frappe.generate_hash(length=SECRET_LENGTH)
		commands(
			server,
			# resetpass first: a user left behind by a rolled-back run keeps no password of its own
			[
				("ACL", "SETUSER", self.username, "on", "resetpass", f">{self.password}", *RULES),
				("ACL", "SAVE"),
			],
		)
		self.created_on_server = 1

	def on_trash(self) -> None:
		if not self.created_on_server:
			return
		commands(self.get_server(), [("ACL", "DELUSER", self.username), ("ACL", "SAVE")])

	@frappe.whitelist()
	def rotate_credentials(self) -> None:
		"""A new password; the old one stops working at once, so the consumer is told first."""
		frappe.only_for("System Manager")
		password = frappe.generate_hash(length=SECRET_LENGTH)
		commands(
			self.get_server(),
			[("ACL", "SETUSER", self.username, "resetpass", f">{password}"), ("ACL", "SAVE")],
		)
		self.password = password
		self.save(ignore_permissions=True)

	def get_server(self):
		return frappe.get_cached_doc("Valkey Server")

	def connection(self) -> dict:
		server = self.get_server()
		password = self.get_password("password")
		return {
			"host": server.address,
			"port": server.port,
			"user": self.username,
			"password": password,
			"url": f"redis://{self.username}:{password}@[{server.address}]:{server.port}/0",
		}
