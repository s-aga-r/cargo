"""Relaying a service's Prometheus metrics to the region's datum."""

from __future__ import annotations

import tomllib
import typing
from pathlib import Path

import frappe
import requests

INGEST_PATH = "/v1/ingest"
SHIP_TIMEOUT = 30


class MetricsInfo(typing.TypedDict):
	"""The metrics endpoint and token for a cluster"""

	endpoint: str
	token: str


def get_metrics_info() -> MetricsInfo:
	"""Steal metrics endpoint and token from `common_config.toml`"""
	bench_path = Path(frappe.utils.get_bench_path())
	common_bench_config = bench_path.parent / "common_config.toml"

	if not common_bench_config.exists():
		raise RuntimeError(f"Cannot find {common_bench_config}")

	parsed_common_bench_config = tomllib.loads(common_bench_config.read_text())
	# Pilot writes the metrics destination as [datum]; [logs] is the same host, other path.
	datum = parsed_common_bench_config.get("datum") or {}
	metrics_endpoint, metrics_token = datum.get("endpoint"), datum.get("token")

	if not metrics_endpoint or not metrics_token:
		raise RuntimeError(f"Cannot find metrics endpoint or token in {common_bench_config}")

	return MetricsInfo(endpoint=metrics_endpoint, token=metrics_token)


def parse_metrics(text: str, labels: dict[str, str], timestamp: str, prefix: str) -> list[dict]:
	"""Prometheus text to datum samples.

	Histogram buckets are dropped: they are most of what an idle node exports, and
	`datum.samples` has no TTL. `_sum` and `_count` survive, so averages still work. Every
	series carries `prefix`, because `datum.samples` is shared with every other Frappe
	service, where a bare name would collide with anyone's. Anything unparseable is skipped
	rather than trusted -- stderr is merged into this stream."""
	samples = []
	for line in text.splitlines():
		line = line.strip()
		if not line or line.startswith("#"):
			continue

		name, _, rest = line.partition("{")
		if rest:
			series_labels, _, value = rest.partition("}")
			pairs = dict(_split_label(pair) for pair in _split_labels(series_labels))
		else:
			name, _, value = line.partition(" ")
			pairs = {}

		name = name.strip()
		if not name or name.endswith("_bucket"):
			continue

		try:
			numeric = float(value.strip().split()[0])
		except (ValueError, IndexError):
			continue

		samples.append(
			{
				"metric": name if name.startswith(prefix) else f"{prefix}{name}",
				"value": numeric,
				"ts": timestamp,
				"labels": {**pairs, **labels},
			}
		)

	return samples


def _split_labels(text: str) -> list[str]:
	"""Split on commas outside quotes: label values carry commas of their own."""
	parts, current, quoted = [], "", False
	for character in text:
		if character == '"':
			quoted = not quoted
		if character == "," and not quoted:
			parts.append(current)
			current = ""
			continue
		current += character

	if current:
		parts.append(current)

	return parts


def _split_label(pair: str) -> tuple[str, str]:
	key, _, value = pair.partition("=")
	return key.strip(), value.strip().strip('"')


def send(info: MetricsInfo, samples: list[dict], source: str) -> None:
	"""Datum drops writes by design rather than buffering, so nothing is retried here:
	a gap in a chart beats a stalled cron."""
	try:
		response = requests.post(
			f"{info['endpoint'].rstrip('/')}{INGEST_PATH}",
			headers={"Authorization": f"Bearer {info['token']}"},
			json={"samples": samples},
			timeout=SHIP_TIMEOUT,
		)
	except requests.RequestException as error:
		frappe.log_error(title=f"Could not ship {source} metrics", message=str(error))
		return

	if not response.ok:
		frappe.log_error(
			title=f"Datum refused {source} metrics ({response.status_code})",
			message=response.text[:500],
		)
