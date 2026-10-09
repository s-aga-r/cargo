"""What Atlas lets into a mail machine, asked for when the machine is created.

Default deny inbound, everything out. The mesh may reach anything, since that is where Cargo
and the cluster's own nodes come from; the Internet reaches only the mail ports. SSH never
leaves the mesh, and the recovery port is reached on the loopback alone."""

from __future__ import annotations

from cargo.cloud_mail.cluster.plan import FIREWALL_PORTS
from cargo.service import MESH_NETWORK

ANYWHERE = ["0.0.0.0/0", "::/0"]
GATEWAY_PORTS = (443,)


def node_firewall() -> dict:
	return _firewall([_rule("tcp", port, ANYWHERE) for port in FIREWALL_PORTS])


def gateway_firewall(relay_ports: list[int], node_addresses: list[str]) -> dict:
	"""A gateway takes mail from the cluster's nodes and nobody else, so each pool's relay port
	opens to those addresses alone."""
	sources = [_host(address) for address in node_addresses]
	rules = [_rule("tcp", port, ANYWHERE) for port in GATEWAY_PORTS]
	rules += [_rule("tcp", port, sources) for port in sorted(set(relay_ports)) if sources]
	return _firewall(rules)


def _firewall(public_rules: list[dict]) -> dict:
	return {
		"enabled": True,
		"inbound": [
			{"protocol": "any", "cidrs": [MESH_NETWORK]},
			{"protocol": "icmp", "cidrs": ANYWHERE},
			*public_rules,
		],
		"outbound": [{"protocol": "any", "cidrs": ANYWHERE}],
	}


def _rule(protocol: str, port: int, cidrs: list[str]) -> dict:
	return {"protocol": protocol, "ports": str(port), "cidrs": list(cidrs)}


def _host(address: str) -> str:
	return f"{address}/128" if ":" in address else f"{address}/32"
