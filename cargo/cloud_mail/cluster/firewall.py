"""What Atlas lets into a mail machine, asked for when the machine is created.

The mesh may reach anything, since that is where Cargo and the cluster's own nodes come from;
the Internet reaches only the mail ports. The recovery port is reached on the loopback alone."""

from __future__ import annotations

from cargo.cloud_mail.cluster.plan import FIREWALL_PORTS
from cargo.service import ANYWHERE, firewall, firewall_rule

GATEWAY_PORTS = (443,)


def node_firewall() -> dict:
	return firewall([firewall_rule("tcp", port, ANYWHERE) for port in FIREWALL_PORTS])


def gateway_firewall(relay_ports: list[int], node_addresses: list[str]) -> dict:
	"""A gateway takes mail from the cluster's nodes and nobody else, so each pool's relay port
	opens to those addresses alone."""
	sources = [_host(address) for address in node_addresses]
	rules = [firewall_rule("tcp", port, ANYWHERE) for port in GATEWAY_PORTS]
	rules += [firewall_rule("tcp", port, sources) for port in sorted(set(relay_ports)) if sources]
	return firewall(rules)


def _host(address: str) -> str:
	return f"{address}/128" if ":" in address else f"{address}/32"
