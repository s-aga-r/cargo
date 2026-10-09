# Copyright (c) 2026, Aradhya-Tripathi and Contributors
# See license.txt

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from cargo.client_models import POSTGRES
from cargo.postgres.doctype.postgres_server.postgres_server import PostgresServer
from cargo.postgres.doctype.postgres_server.test_postgres_server import make_machine, reset_postgres_server
from cargo.postgres.spawn import CONFIG_KEY, ensure_postgres, validate_config
from cargo.testing import use_test_settings

CONFIG = {POSTGRES: {"cpu_millicores": 2000, "ram_gb": 4, "disk_gb": 50}}


class IntegrationTestPostgresSpawn(IntegrationTestCase):
	def setUp(self) -> None:
		frappe.set_user("Administrator")
		use_test_settings()
		reset_postgres_server()

	def tearDown(self) -> None:
		reset_postgres_server()

	def refused(self, config) -> bool:
		try:
			validate_config(config)
			return False
		except frappe.ValidationError:
			return True

	def test_the_config_this_region_would_build_from_is_accepted(self) -> None:
		self.assertFalse(self.refused(CONFIG))
		self.assertFalse(self.refused({**CONFIG, "version": "17", "max_connections": 300}))

	def test_a_bad_shape_is_refused(self) -> None:
		self.assertTrue(self.refused("text"))
		self.assertTrue(self.refused({}))
		self.assertTrue(self.refused({**CONFIG, "version": "latest"}))
		self.assertTrue(self.refused({**CONFIG, "max_connections": 2}))

	def test_a_region_told_nothing_builds_nothing(self) -> None:
		with (
			patch.dict(frappe.local.conf, {CONFIG_KEY: None}),
			patch.object(PostgresServer, "create_postgres_node") as made,
		):
			ensure_postgres()
		made.assert_not_called()

	def test_the_first_run_claims_the_record_and_asks_for_a_machine(self) -> None:
		with (
			patch.dict(frappe.local.conf, {CONFIG_KEY: {**CONFIG, "version": "17"}}),
			patch.object(PostgresServer, "create_postgres_node") as made,
		):
			ensure_postgres()
		made.assert_called_once_with(cpu_millicores=2000, ram_gb=4, disk_gb=50)
		server = frappe.get_single("Postgres Server")
		self.assertEqual((server.auto_spawn, server.version), (1, "17"))

	def test_a_running_machine_starts_the_setup_once(self) -> None:
		server = frappe.get_single("Postgres Server")
		server.update(
			{"auto_spawn": 1, "machine": make_machine("Postgres Server", "Postgres Server", "postgres").name}
		).save()
		with (
			patch.dict(frappe.local.conf, {CONFIG_KEY: CONFIG}),
			patch.object(PostgresServer, "setup") as setup,
		):
			ensure_postgres()
		setup.assert_called_once()

	def test_a_server_filled_in_by_hand_is_left_alone(self) -> None:
		server = frappe.get_single("Postgres Server")
		server.machine = make_machine("Postgres Server", "Postgres Server", "postgres").name
		server.save()
		with (
			patch.dict(frappe.local.conf, {CONFIG_KEY: CONFIG}),
			patch.object(PostgresServer, "setup") as setup,
		):
			ensure_postgres()
		setup.assert_not_called()
