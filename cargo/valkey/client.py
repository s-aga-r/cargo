"""Cargo's own connection to the region's Valkey, over the mesh."""

from __future__ import annotations

import typing

import frappe
import redis
from frappe import _

if typing.TYPE_CHECKING:
	from cargo.valkey.doctype.valkey_server.valkey_server import ValkeyServer

ADMIN_USER = "default"
CONNECT_TIMEOUT = 5
Error = redis.RedisError


def server_address(server: ValkeyServer) -> str:
	address = frappe.db.get_value("Machine", server.machine, "address") if server.machine else None
	if not address:
		frappe.throw(_("The Valkey server has no machine yet."))
	return address


def connect(server: ValkeyServer) -> redis.Redis:
	"""A connection as the default user. The mesh is WireGuard, so no TLS."""
	return redis.Redis(
		host=server_address(server),
		port=server.port,
		username=ADMIN_USER,
		password=server.get_password("admin_password"),
		socket_connect_timeout=CONNECT_TIMEOUT,
		socket_timeout=CONNECT_TIMEOUT,
		decode_responses=True,
	)


def command(server: ValkeyServer, *args: str):
	connection = connect(server)
	try:
		return connection.execute_command(*args)
	finally:
		connection.close()


def commands(server: ValkeyServer, calls: list[tuple[str, ...]]) -> None:
	connection = connect(server)
	try:
		for call in calls:
			connection.execute_command(*call)
	finally:
		connection.close()
