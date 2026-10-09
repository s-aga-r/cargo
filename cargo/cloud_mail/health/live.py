"""The worst thing true about a mail cluster right now, read from its nodes rather than from
what Cargo last told them."""

from __future__ import annotations

import json
import socket
import ssl
import typing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cached_property

import frappe
import requests
from cryptography import x509
from frappe.utils import add_to_date, get_datetime, now_datetime

from cargo.cargo.doctype.machine.machine import DEAD_MACHINE_STATES
from cargo.cloud_mail.cluster import bootstrap
from cargo.cloud_mail.stalwart.errors import StalwartError
from cargo.health.live import CRITICAL, DEGRADED, Finding
from cargo.health.live import LiveHealth as ServiceHealth
from cargo.health.live import prune_history as prune_log

if typing.TYPE_CHECKING:
	from frappe.model.document import Document

HISTORY_FILE = "mail_health.json.log"
HEALTH = "Health"
SERVING_STATUSES = ("Active", "Draining")
READY_PATH = "/healthz/ready"


@dataclass(frozen=True)
class Reading:
	"""What the cluster said about itself, or why it said nothing."""

	leases: dict[str, dict] = field(default_factory=dict)  # by hostname
	problems: dict[str, str] = field(default_factory=dict)  # serving nodes that are not well, and why
	certificate_days: int | None = None
	certificate_error: str = ""
	error: str = ""


class LiveHealth(ServiceHealth):
	"""One readiness probe per serving node, one registry read, one look at the certificate."""

	history_file = HISTORY_FILE

	def __init__(self, cluster: Document) -> None:
		super().__init__(cluster)
		self.settings = frappe.get_cached_doc("Mail Health Settings")
		self.verify_tls = bool(frappe.get_cached_doc("Mail Settings").verify_stalwart_tls)

	def is_live(self) -> bool:
		return self.doc.status == "Active"

	@cached_property
	def nodes(self) -> list[frappe._dict]:
		return frappe.get_all(
			"Stalwart Node",
			filters={"cluster": self.doc.name, "status": ("in", SERVING_STATUSES)},
			fields=[
				"name",
				"hostname",
				"status",
				"machine",
				"last_health_at",
				"consecutive_failures",
				"consecutive_successes",
				"drained_by",
			],
			order_by="name",
		)

	@cached_property
	def reading(self) -> Reading:
		"""One read of the cluster, shared by every check and by the log line."""
		try:
			leases = {
				lease.get("hostname"): lease
				for lease in self.doc.get_client().cluster_nodes.get_all(
					properties=["hostname", "status", "lastRenewal"]
				)
			}
		except StalwartError as error:
			return Reading(error=str(error))

		problems = {}
		for node in self.nodes:
			if problem := self.lease_problem(leases.get(node.hostname)) or probe_ready(
				node.hostname, self.settings.read_timeout_seconds, self.verify_tls
			):
				problems[node.hostname] = problem
		self.record_nodes(problems)

		try:
			days = certificate_days_left(self.doc.hostname, self.settings.read_timeout_seconds)
			return Reading(leases=leases, problems=problems, certificate_days=days)
		except (OSError, ssl.SSLError, ValueError) as error:
			return Reading(leases=leases, problems=problems, certificate_error=str(error))

	def lease_problem(self, lease: dict | None) -> str:
		"""A single node without a coordinator holds no lease; the cluster answering is enough."""
		if lease is None:
			return "" if self.doc.coordinator == "Disabled" else "holds no registry lease"
		if lease.get("status") != "active":
			return f"registry lease is {lease.get('status')}"
		return ""

	def record_nodes(self, problems: dict[str, str]) -> None:
		"""Each node remembers when it last answered and how many checks in a row it passed or
		failed; enough failures take an Active node out of ingress, never the last one still
		answering, and enough passes put a node Health drained back. A node whose machine is
		gone is failed outright."""
		dead = dead_machines([node.machine for node in self.nodes if node.machine])
		answering = [
			node
			for node in self.nodes
			if node.hostname not in problems and node.status == "Active" and node.machine not in dead
		]
		for node in self.nodes:
			if node.machine in dead:
				bootstrap.fail_dead_node(frappe.get_doc("Stalwart Node", node.name), dead[node.machine])
			elif node.hostname in problems:
				self.record_failure(node, problems[node.hostname], others_answer=bool(answering))
			else:
				self.record_success(node)

	def record_failure(self, node: frappe._dict, problem: str, others_answer: bool) -> None:
		failures = (node.consecutive_failures or 0) + 1
		frappe.db.set_value(
			"Stalwart Node",
			node.name,
			{"consecutive_failures": failures, "consecutive_successes": 0, "last_error": problem[:1000]},
			update_modified=False,
		)
		if node.status == "Active" and others_answer and failures >= self.settings.auto_drain_failures:
			bootstrap.drain_node(frappe.get_doc("Stalwart Node", node.name), drained_by=HEALTH)

	def record_success(self, node: frappe._dict) -> None:
		successes = (node.consecutive_successes or 0) + 1
		frappe.db.set_value(
			"Stalwart Node",
			node.name,
			{
				"consecutive_failures": 0,
				"consecutive_successes": successes,
				"last_health_at": now_datetime(),
				"last_error": None,
			},
			update_modified=False,
		)
		if (
			node.status == "Draining"
			and node.drained_by == HEALTH
			and successes >= self.settings.auto_restore_successes
		):
			bootstrap.restore_node(frappe.get_doc("Stalwart Node", node.name))

	def findings(self) -> list[Finding]:
		"""An unreachable management API, or no node answering, is the only finding: the rest
		would be invented from data we do not have."""
		reading = self.reading
		if reading.error:
			return [Finding(CRITICAL, f"the management API could not be reached: {reading.error}")]
		if self.nodes and len(reading.problems) == len(self.nodes):
			return [Finding(CRITICAL, "no node answers: " + "; ".join(reading.problems.values()))]

		return [
			*self.store_findings(),
			*self.node_findings(reading),
			*self.certificate_findings(reading),
			*self.drift_findings(),
		]

	def store_findings(self) -> list[Finding]:
		"""Mail is only as well as the stores it runs on. Garage or Postgres critical is mail
		critical: bodies or the directory cannot be reached. Valkey critical costs coordination
		and rate limits, which degrades rather than stops."""
		findings = []
		if self.doc.data_store:
			postgres = frappe.get_single("Postgres Server")
			if postgres.health == CRITICAL:
				findings.append(
					Finding(CRITICAL, f"the Postgres server is critical: {postgres.health_reason}")
				)
		if self.doc.blob_bucket:
			storage = frappe.get_cached_value("Bucket", self.doc.blob_bucket, "cluster")
			health, reason = frappe.db.get_value(
				"Object Storage Cluster", storage, ["health", "health_reason"]
			)
			if health == CRITICAL:
				findings.append(Finding(CRITICAL, f"object storage {storage} is critical: {reason}"))
		if self.doc.in_memory_store and self.doc.coordinator == "Default":
			valkey = frappe.get_single("Valkey Server")
			if valkey.health == CRITICAL:
				findings.append(Finding(DEGRADED, f"the Valkey server is critical: {valkey.health_reason}"))
		return findings

	def node_findings(self, reading: Reading) -> list[Finding]:
		"""A node failing for less than `node_offline_seconds` is a blip, not a finding."""
		cutoff = add_to_date(now_datetime(), seconds=-self.settings.node_offline_seconds)
		findings = []
		for node in self.nodes:
			if node.hostname not in reading.problems:
				continue
			last = get_datetime(node.last_health_at) if node.last_health_at else None
			if last is None or last < cutoff:
				findings.append(Finding(DEGRADED, f"{node.hostname} {reading.problems[node.hostname]}"))
		return findings

	def certificate_findings(self, reading: Reading) -> list[Finding]:
		if reading.certificate_error:
			return [Finding(DEGRADED, f"the certificate could not be read: {reading.certificate_error}")]
		if (
			reading.certificate_days is not None
			and reading.certificate_days < self.settings.certificate_warn_days
		):
			return [Finding(DEGRADED, f"the certificate expires in {reading.certificate_days} days")]
		return []

	def drift_findings(self) -> list[Finding]:
		"""Whatever the last drift check left behind, until the next one clears it."""
		if drift_recorded(self.doc.drift_report):
			return [Finding(DEGRADED, f"configuration drift recorded at {self.doc.last_config_sync_at}")]
		return []

	def log_entry(self, finding: Finding) -> dict:
		reading = self.reading
		return {
			"cluster": self.doc.name,
			"severity": finding.severity,
			"reason": finding.reason,
			"error": reading.error,
			"leases": list(reading.leases.values()),
			"problems": reading.problems,
			"certificate_days": reading.certificate_days,
		}


def dead_machines(names: list[str]) -> dict[str, str]:
	"""The machines Atlas reports gone, by name, with how."""
	if not names:
		return {}
	rows = frappe.get_all(
		"Machine", {"name": ("in", names), "status": ("in", list(DEAD_MACHINE_STATES))}, ["name", "status"]
	)
	return {row.name: row.status for row in rows}


def drift_recorded(report: str | None) -> bool:
	"""`check_drift` stores `{"checked_at", "differences"}`; a failed push stores `{"error"}`."""
	try:
		parsed = json.loads(report or "null")
	except ValueError:
		return True
	if not isinstance(parsed, dict):
		return bool(parsed)
	return bool(parsed.get("differences") or parsed.get("error"))


def probe_ready(hostname: str, timeout: int, verify: bool) -> str:
	"""Empty when the node says it is ready; otherwise why not, in one line."""
	try:
		answer = requests.get(f"https://{hostname}{READY_PATH}", timeout=timeout, verify=verify)
	except requests.RequestException as error:
		return f"did not answer: {error.__class__.__name__}"
	return "" if answer.ok else f"answered {answer.status_code} to {READY_PATH}"


def certificate_days_left(hostname: str, timeout: int) -> int:
	"""Days until the certificate served on 443 expires, read off the wire so a self-signed
	default is reported like any other."""
	context = ssl.create_default_context()
	context.check_hostname = False
	context.verify_mode = ssl.CERT_NONE
	with (
		socket.create_connection((hostname, 443), timeout=timeout) as raw,
		context.wrap_socket(raw, server_hostname=hostname) as tls,
	):
		der = tls.getpeercert(binary_form=True)
	if not der:
		raise ValueError("no certificate presented")
	not_after = x509.load_der_x509_certificate(der).not_valid_after_utc
	return (not_after - datetime.now(UTC)).days


def prune_history() -> None:
	"""Drop readings past the window. Scheduled hourly."""
	prune_log(HISTORY_FILE, frappe.get_cached_doc("Mail Health Settings").history_hours)
