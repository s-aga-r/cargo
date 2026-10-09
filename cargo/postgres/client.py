"""Cargo's own connection to the region's Postgres, over the mesh."""

from __future__ import annotations

import typing

import frappe
import psycopg2
from frappe import _
from psycopg2.extensions import adapt

if typing.TYPE_CHECKING:
	from cargo.postgres.doctype.postgres_server.postgres_server import PostgresServer

ADMIN_ROLE = "cargo"
CONNECT_TIMEOUT = 5
Error = psycopg2.Error


def server_address(server: PostgresServer) -> str:
	address = frappe.db.get_value("Machine", server.machine, "address") if server.machine else None
	if not address:
		frappe.throw(_("The Postgres server has no machine yet."))
	return address


def connect(
	server: PostgresServer, dbname: str = "postgres", user: str | None = None, password: str | None = None
):
	"""A connection as Cargo's admin role, autocommitting: CREATE DATABASE refuses a transaction."""
	connection = psycopg2.connect(
		host=server_address(server),
		port=server.port,
		dbname=dbname,
		user=user or ADMIN_ROLE,
		password=password or server.get_password("admin_password"),
		connect_timeout=CONNECT_TIMEOUT,
		sslmode="disable",  # the mesh is WireGuard
	)
	connection.autocommit = True
	return connection


def run(server: PostgresServer, statements: list[str], dbname: str = "postgres") -> None:
	with connect(server, dbname) as connection, connection.cursor() as cursor:
		for statement in statements:
			cursor.execute(statement)


def query(server: PostgresServer, statement: str, params: tuple = ()) -> list[tuple]:
	with connect(server) as connection, connection.cursor() as cursor:
		cursor.execute(statement, params)
		return cursor.fetchall()


def identifier(name: str) -> str:
	"""A quoted identifier. Names are validated to lowercase word characters before they get
	here, so quoting is belt and braces."""
	return '"' + name.replace('"', '""') + '"'


def literal(value: str) -> str:
	"""A quoted string literal, rendered without a connection."""
	return adapt(value).getquoted().decode()
