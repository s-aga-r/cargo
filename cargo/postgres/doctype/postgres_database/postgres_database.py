# Copyright (c) 2026, Aradhya-Tripathi and contributors
# For license information, please see license.txt

from __future__ import annotations

import re

import frappe
from frappe import _
from frappe.model.document import Document

from cargo.postgres.client import identifier, literal, run

NAME = re.compile(r"^[a-z][a-z0-9_]{0,62}$")
SECRET_LENGTH = 32


class PostgresDatabase(Document):
	"""One service's database on the region's Postgres, with the role that owns it."""

	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		created_on_server: DF.Check
		database_name: DF.Data
		password: DF.Password | None
		reference_doctype: DF.Link | None
		reference_name: DF.DynamicLink | None
		username: DF.Data | None
	# end: auto-generated types

	def validate(self) -> None:
		self.set_names()

	def set_names(self) -> None:
		"""Checked here and in `before_insert`, which Frappe runs before `validate`."""
		self.database_name = (self.database_name or "").strip().lower()
		if not NAME.match(self.database_name):
			frappe.throw(
				_("Database Name must be lowercase letters, digits and underscores, starting with a letter.")
			)
		self.username = self.username or self.database_name

	def before_insert(self) -> None:
		"""The role and the database come first; the consumer creates its own schema."""
		self.set_names()
		if self.flags.adopting:  # the server already holds it, password and all
			return
		server = self.get_server()
		if server.status != "Active":
			frappe.throw(_("The Postgres server is not active."))
		self.password = frappe.generate_hash(length=SECRET_LENGTH)
		run(
			server,
			[
				f"CREATE ROLE {identifier(self.username)} LOGIN PASSWORD {literal(self.password)}",
				f"CREATE DATABASE {identifier(self.database_name)} OWNER {identifier(self.username)}",
			],
		)
		self.created_on_server = 1

	def on_trash(self) -> None:
		"""Drop both. Open connections are ended first; the consumer is gone or going."""
		if not self.created_on_server:
			return
		run(
			self.get_server(),
			[
				"SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
				f"WHERE datname = {literal(self.database_name)} AND pid <> pg_backend_pid()",
				f"DROP DATABASE IF EXISTS {identifier(self.database_name)}",
				f"DROP ROLE IF EXISTS {identifier(self.username)}",
			],
		)

	@frappe.whitelist()
	def rotate_credentials(self) -> None:
		"""A new password for the owning role; the consumer is told through its own reconfigure."""
		frappe.only_for("System Manager")
		password = frappe.generate_hash(length=SECRET_LENGTH)
		run(self.get_server(), [f"ALTER ROLE {identifier(self.username)} PASSWORD {literal(password)}"])
		self.password = password
		self.save(ignore_permissions=True)

	def get_server(self):
		return frappe.get_cached_doc("Postgres Server")

	def connection(self) -> dict:
		"""What a consumer connects with."""
		server = self.get_server()
		return {
			"host": server.address,
			"port": server.port,
			"database": self.database_name,
			"user": self.username,
			"password": self.get_password("password"),
			"use_tls": False,
		}
