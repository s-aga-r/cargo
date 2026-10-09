"""Node lifecycle: what the provisioning scripts are given, bootstrap completion, health,
draining and upgrades."""

from typing import TYPE_CHECKING

import frappe
from frappe import _
from frappe.utils import add_to_date, get_datetime, now

from cargo.cloud_mail.cluster import dns, plan
from cargo.cloud_mail.stalwart import forget_sessions, get_client, has_credentials
from cargo.cloud_mail.stalwart.credentials import Credential
from cargo.cloud_mail.stalwart.errors import StalwartError, StalwartUnauthorizedError
from cargo.cloud_mail.utils import log_exception
from cargo.service import MESH_NETWORK
from cargo.ssh import OutputLog, run_over_ssh, script

if TYPE_CHECKING:
	from frappe.model.document import Document

BOOTSTRAP_DEADLINE_MINUTES = 45
SCRIPTS = ("cloud_mail", "conf", "stalwart")
INSTALL_TIMEOUT = 20 * 60
BOOTSTRAP_TIMEOUT = 20 * 60


# --- what the scripts are given ---------------------------------------------------------------


def install_environment(server: Document) -> dict:
	"""What install.sh needs on a node or a gateway."""
	cluster = server.get_cluster()
	settings = frappe.get_cached_doc("Mail Settings")
	return {
		"STALWART_VERSION": server.get("stalwart_version") or cluster.stalwart_version,
		"STALWART_URL_TEMPLATE": plan.STALWART_URL_TEMPLATE,
		"STALWART_CLI_VERSION": plan.STALWART_CLI_VERSION,
		"STALWART_CLI_URL_TEMPLATE": plan.STALWART_CLI_URL_TEMPLATE,
		"SYSTEMD_UNIT": plan.systemd_unit(),
		"RECOVERY_PORT": plan.BOOTSTRAP_PORT,
		"FIREWALL_PORTS": " ".join(str(port) for port in plan.FIREWALL_PORTS),
		"MESH_NETWORK": MESH_NETWORK,
		"USE_UFW": int(bool(settings.host_firewall)),
	}


def wait_ports(node: Document) -> str:
	return "25 443" if serves_clients(node) else ""


def bootstrap_environment(node: Document) -> tuple[dict, list[str]]:
	"""What bootstrap.sh needs, and every secret in it, for the masker."""
	cluster = node.get_cluster()
	bootstrap_plan = plan.bootstrap_plan(cluster)
	recovery_plan = plan.recovery_plan(cluster)
	environment = {
		"RECOVERY_PORT": plan.BOOTSTRAP_PORT,
		"ADMIN_USER": cluster.admin_username,
		"ADMIN_PASSWORD": cluster.get_password("admin_password"),
		"PLAN_MARKER": plan.marker(recovery_plan),
		"CONFIG_VERSION": cluster.config_version or 0,
		"ENV_NORMAL": plan.render_env(plan.node_env(node, "normal")),
		"ENV_BOOTSTRAP": plan.render_env(plan.node_env(node, "bootstrap")),
		"ENV_RECOVERY": plan.render_env(plan.node_env(node, "recovery")),
		"CONFIG_JSON": frappe.as_json(plan.node_config(cluster)),
		"BOOTSTRAP_NDJSON": plan.to_ndjson(bootstrap_plan),
		"DEFAULTS_NDJSON": plan.to_ndjson(plan.defaults_plan()),
		"CLUSTER_NDJSON": plan.to_ndjson(recovery_plan),
		"WAIT_PORTS": wait_ports(node),
	}
	secrets = [
		environment["ADMIN_PASSWORD"],
		*plan.secret_strings(bootstrap_plan),
		*plan.secret_strings(recovery_plan),
	]
	return environment, secrets


def configure_environment(node: Document) -> tuple[dict, list[str]]:
	"""What configure.sh needs: the store connection and the runtime environment."""
	cluster = node.get_cluster()
	config = plan.node_config(cluster)
	environment = {
		"CONFIG_JSON": frappe.as_json(config),
		"ENV_NORMAL": plan.render_env(plan.node_env(node, "normal")),
		"WAIT_PORTS": wait_ports(node),
	}
	return environment, plan.secret_strings([{"value": config}])


def needs_bootstrap(node: Document) -> bool:
	"""Whether this node brings the data store up, or joins a cluster that is already up.

	The first node of a Pending or Failed cluster bootstraps and becomes the bootstrap node;
	a cluster whose bootstrap node is being provisioned again bootstraps again, which skips
	whatever is already in place. A node joining an Active cluster is configured."""
	cluster = node.get_cluster()
	first = cluster.status in ("Pending", "Failed") and (
		not cluster.bootstrap_node or cluster.bootstrap_node == node.name
	)
	if first or (cluster.status == "Bootstrapping" and node.is_bootstrap_node):
		if not serves_clients(node):
			frappe.throw(_("The first node must serve clients; pick the full or frontend role."))
		return True
	if cluster.status == "Active":
		return False
	frappe.throw(_("The cluster is still bootstrapping; provision more nodes once it is active."))


def start_bootstrap(node: Document) -> None:
	"""Record that this node brings the cluster up, and freeze the plan it does it with."""
	cluster = node.get_cluster()
	node.db_set("is_bootstrap_node", 1, update_modified=False)
	cluster.db_set({"status": "Bootstrapping", "bootstrap_node": node.name}, update_modified=False)
	cluster.bump_config_version(plan.cluster_plan(cluster))


def run_script(
	server: Document, name: str, environment: dict, secrets: list[str], timeout: int
) -> str | None:
	"""Run one of the Stalwart scripts on a server's machine, streaming into its setup log.
	Returns the output, or None once the failure is on the record."""
	machine = frappe.get_doc("Machine", server.machine)
	with OutputLog(server, "setup_log", append=True) as log:
		try:
			return run_over_ssh(
				machine.address,
				script(*SCRIPTS, name, environment=environment),
				machine.get_password("ssh_private_key"),
				timeout=timeout,
				on_output=log.write,
				pin=machine.host_key_pin(),
				secrets=secrets,
			)
		except Exception:
			frappe.log_error(
				title=f"{server.name}: {name} failed", message=frappe.get_traceback(with_context=False)
			)
			server.set_status("Failed", _("{0} failed. See the Setup Log.").format(name))
			return None


# --- provisioning ---------------------------------------------------------------------------


def serves_clients(node: Document) -> bool:
	"""Outbound-only nodes run no listeners: nothing to wait for, nothing to put in ingress DNS."""

	return (node.role or "full") != "outbound"


# --- callbacks --------------------------------------------------------------------------------


def after_provision(node: Document) -> None:
	"""The node is installed and answering locally."""

	cluster = node.get_cluster()
	if node.is_bootstrap_node and cluster.status == "Failed" and cluster.bootstrap_node == node.name:
		cluster.db_set("status", "Bootstrapping", update_modified=False)  # a retried bootstrap succeeded
	node.db_set(
		{
			"status": "Provisioned",
			"provisioned_at": now(),
			"installed_version": node.get_cluster().stalwart_version,
		},
		update_modified=False,
	)
	if node.is_bootstrap_node:
		# The certificate check goes through the cluster hostname, so it must resolve to us.
		dns.sync_node_records(node, include_ingress=True)
	else:
		dns.sync_node_records(node, include_ingress=False)
	dns.sync_spf_record(node.get_cluster())
	check_node(node)


def installed_version_from(output: str) -> str | None:
	"""The version `stalwart --version` printed as the last line of upgrade.sh and rollback.sh."""
	lines = output.strip().splitlines()
	if not lines or not lines[-1].split():
		return None
	version = lines[-1].split()[-1]
	return version if version.startswith("v") else f"v{version}"


def after_upgrade(node: Document, version: str | None = None) -> None:
	"""The node restarted on another version: its deadline starts afresh and it is checked again."""
	node.db_set(
		{"installed_version": version or node.get_cluster().stalwart_version, "provisioned_at": now()},
		update_modified=False,
	)
	node.set_status("Provisioned")
	check_node(node)


# --- health ------------------------------------------------------------------------------------


def check_node(node: Document) -> bool:
	"""Promotes a Provisioned node to Active once Stalwart confirms it; fails it after a deadline."""

	cluster = node.get_cluster()
	if cluster.status == "Bootstrapping" and node.is_bootstrap_node:
		return finish_bootstrap(cluster)

	if cluster.status != "Active":
		return False

	try:
		registry = cluster.get_client().cluster_nodes.find_by_hostname(node.hostname)
	except StalwartError as e:
		return _not_ready(node, str(e))

	if problem := _registry_problem(cluster, registry):
		return _not_ready(node, problem)

	node.db_set(
		{
			"node_id": (registry or {}).get("nodeId") or 0,
			"last_health_at": now(),
			"last_error": None,
		},
		update_modified=False,
	)
	if node.status == "Provisioned":  # Draining stays put until an operator restores the node
		activate_node(node)
	return True


def activate_node(node: Document) -> None:
	"""Into ingress: a node serving is held out by nobody."""
	if not node.enabled:
		frappe.throw(_("Enable the node first."))
	node.db_set("drained_by", None, update_modified=False)
	node.set_status("Active")
	dns.sync_node_records(node, include_ingress=serves_clients(node))
	dns.sync_spf_record(node.get_cluster())


def report_cluster_status(cluster: Document, status: str, **values) -> None:
	"""Active and Failed reach Central through the cluster's webhook, which only a save fires."""
	from cargo.cloud_mail.doctype.stalwart_cluster.stalwart_cluster import (
		configure_mail_webhook,
		webhook_name_for,
	)

	cluster.reload()
	if status == "Active":
		values = {
			"auto_setup_attempts": 0,
			"error": None,
			**values,
		}  # serving arms the spawner's budget again
	if not frappe.db.exists("Webhook", webhook_name_for(cluster.name)):
		configure_mail_webhook(cluster)
	cluster.update({"status": status, **values})
	cluster.save(ignore_permissions=True)


def _adopt_platform_domain(cluster: Document) -> None:
	"""The zone becomes a Mail Domain with its records published; sites get their addresses
	once it verifies."""
	from cargo.cloud_mail.tenancy import platform

	try:
		platform.adopt_platform_domain(cluster)
	except Exception:
		log_exception(f"Could not adopt the platform domain of {cluster.name}", cluster)


def _push_initial_config(cluster: Document) -> None:
	"""Objects the recovery stage cannot create (the disabled-accounts role) land here."""

	try:
		cluster.push_config()
	except StalwartError as e:
		cluster.db_set("drift_report", frappe.as_json({"error": str(e)}), update_modified=False)
		frappe.log_error(title=f"Initial config push failed for {cluster.name}", message=str(e))


def _registry_problem(cluster: Document, registry: dict | None) -> str | None:
	"""A single node without a coordinator may not hold a lease; the cluster answering is enough."""

	if registry is None and cluster.coordinator != "Disabled":
		return "Node registry entry absent"
	if registry and registry.get("status") != "active":
		return f"Node registry status: {registry.get('status')}"
	return None


def _not_ready(node: Document, detail: str) -> bool:
	"""Records the problem; only a node still waiting to come up is failed after the deadline."""

	started = get_datetime(node.provisioned_at or now())
	expired = get_datetime(now()) > add_to_date(started, minutes=BOOTSTRAP_DEADLINE_MINUTES)
	if expired and node.status == "Provisioned":
		node.set_status("Failed", f"Not healthy after {BOOTSTRAP_DEADLINE_MINUTES} minutes: {detail}")
	else:
		node.db_set("last_error", detail[:1000], update_modified=False)
	return False


def finish_bootstrap(cluster: Document) -> bool:
	"""Turns a bootstrapping cluster active once its first node serves a valid certificate.

	Reached repeatedly (button or cron) until it succeeds: the ACME DNS-01 issuance runs on the
	node after the playbook ends, so the HTTPS endpoint is not usable straight away.
	"""

	node = frappe.get_doc("Stalwart Node", cluster.bootstrap_node) if cluster.bootstrap_node else None
	if not node or node.status != "Provisioned":
		return False

	try:
		admin = cluster.get_admin_client()
		ensure_api_key(cluster, admin)
		_set_default_certificate(cluster, admin)
		registry = admin.cluster_nodes.find_by_hostname(node.hostname)
		problem = _registry_problem(cluster, registry)
	except StalwartError as e:
		problem = str(e)

	if problem:
		_not_ready(node, problem)
		if node.status == "Failed":
			report_cluster_status(cluster, "Failed")
		return False

	report_cluster_status(cluster, "Active", last_config_sync_at=now())
	_push_initial_config(cluster)
	_adopt_platform_domain(cluster)
	node.db_set(
		{
			"node_id": (registry or {}).get("nodeId") or 0,
			"last_health_at": now(),
			"last_error": None,
		},
		update_modified=False,
	)
	activate_node(node)
	return True


def poll_pending() -> None:
	"""Every minute: finish a bootstrap whose certificate or lease was not ready when the node
	came up, and promote Provisioned nodes of Active clusters once their lease is active."""
	for name in frappe.get_all("Stalwart Cluster", {"status": "Bootstrapping"}, pluck="name"):
		try:
			finish_bootstrap(frappe.get_doc("Stalwart Cluster", name))
		except Exception:
			log_exception(f"Could not finish bootstrapping {name}", frappe.get_doc("Stalwart Cluster", name))
	active = frappe.get_all("Stalwart Cluster", {"status": "Active"}, pluck="name")
	if not active:
		return
	for name in frappe.get_all(
		"Stalwart Node", {"status": "Provisioned", "enabled": 1, "cluster": ("in", active)}, pluck="name"
	):
		node = frappe.get_doc("Stalwart Node", name)
		try:
			check_node(node)
		except Exception:
			log_exception(f"Could not check {name}", node)


def ensure_api_key(target: Document, admin) -> None:
	"""Mints the management key for a cluster or gateway unless the stored one still works.

	A stored key can be dead: a re-bootstrapped data store never saw it, and a re-run of the
	recovery-stage plan replaces the admin's credentials, keys included.

	No other key is deleted here. The cron, the job callback and the form buttons can run this
	at once, and one run could delete the key another has just minted and is about to store.
	Minting only adds keys, so whichever is stored works; the extras have secrets nobody kept.
	"""

	if _api_key_works(target):
		return
	_, secret = admin.api_keys.create_secret(
		Credential(description=plan.API_KEY_DESCRIPTION, permissions=plan.api_key_permissions())
	)
	target.api_key = secret
	target.save(ignore_permissions=True)
	forget_sessions(target)


def _api_key_works(target: Document) -> bool:
	if not target.get_password("api_key", raise_exception=False):
		return False
	forget_sessions(target)  # a session cached for an earlier data store would hide a dead key
	try:
		get_client(target)
	except StalwartUnauthorizedError:
		return False
	return True


def _set_default_certificate(cluster: Document, client) -> None:
	"""Non-SNI clients (SMTP) need a default certificate; pick the issued one for the hostname."""

	try:
		for certificate in client.objects("Certificate").get_all(
			properties=["id", "subjectAlternativeNames"]
		):
			names = certificate.get("subjectAlternativeNames") or []
			names = list(names.keys()) if isinstance(names, dict) else list(names)
			if cluster.hostname in names:
				client.singleton("SystemSettings").write({"defaultCertificateId": certificate["id"]})
				return
	except StalwartError:
		log_exception(f"Could not pick a default certificate for {cluster.name}", cluster)


# --- draining / removal -----------------------------------------------------------------------


def drain_node(node: Document, drained_by: str = "Operator") -> None:
	"""Takes the node out of the ingress round-robin; Stalwart keeps running on it. Who drained
	it decides who may put it back: Health restores only its own drains."""

	dns.sync_node_records(node, include_ingress=False)
	node.db_set("drained_by", drained_by, update_modified=False)
	node.set_status("Draining" if node.status == "Active" else "Disabled")
	dns.sync_spf_record(node.get_cluster())


def restore_node(node: Document) -> None:
	if not node.enabled:
		frappe.throw(_("Enable the node first."))
	# The health deadline starts afresh, and nobody holds the node out any more.
	node.db_set({"provisioned_at": now(), "drained_by": None}, update_modified=False)
	node.set_status("Provisioned")
	check_node(node)


def fail_dead_node(node: Document, machine_status: str) -> None:
	"""A node whose machine Atlas reports gone: out of ingress and SPF at once, since its
	address can be reissued to anyone, and its lease released. Replacing it is the operator's."""
	dns.sync_node_records(node, include_ingress=False)
	node.set_status("Failed", f"{node.machine} is {machine_status}")
	dns.sync_spf_record(node.get_cluster())
	forget_node(node)


def forget_node(node: Document) -> None:
	"""Removes the node's registry lease when the cluster is reachable (best effort)."""

	cluster = node.get_cluster()
	if cluster.status != "Active" or not has_credentials(cluster):
		return
	try:
		client = cluster.get_client()
		if registry := client.cluster_nodes.find_by_hostname(node.hostname):
			client.cluster_nodes.delete(registry["id"])
	except StalwartError:
		log_exception(f"Could not remove {node.hostname} from the cluster registry", node)
