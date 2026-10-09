# Mail

## Purpose

Each region runs one Stalwart cluster, and Cargo owns it. Every site in the region gets a send-only address on the region's mail zone; sites entitled to mailboxes add their own domains, accounts, groups and lists through the site API. This document covers the cluster's records, what it needs, how a region builds it by itself, and what to look at when it is unwell. The plan this grew from is [suite-cloud-migration.md](suite-cloud-migration.md).

## Records

| Record | What it is |
|---|---|
| Stalwart Cluster | The region's one cluster, named `mx.<zone>` after the region's mail zone. Links the Postgres Database, Bucket and Valkey Credential it runs on. Status is the bring-up: Pending, Bootstrapping, Active, Failed, Disabled. Health is how it is doing now. |
| Stalwart Node | One Stalwart on one Machine, `n1.<zone>`, `n2.<zone>` and so on, with a public IPv4 from Atlas. The first node bootstraps the store; the rest join once the cluster is Active. |
| Egress Gateway, Egress IP Pool | Dedicated sending addresses and the relays that use them, on machines of their own with a local RocksDB store. |
| Mail Site, Mail Domain, Mail Account, Mail Group, Mailing List | The tenancy mirror: who owns what, pushed to Stalwart as it changes. |
| Mail Settings, Mail Health Settings | Runtime knobs and health thresholds, region-wide. |

The region's zone, `mail.<wildcard domain>`, is a DNS Zone named on Cargo Settings. The cluster's own records, the nodes' A records, the ingress round-robin at `mx`, SPF at the apex and `spf`, and the platform domain's MX, DKIM, DMARC and TLS-RPT rows, are DNS Records in that zone, written through its provider.

## Requirements

Before a cluster can be built the region needs an Active Object Storage Cluster, an Active Postgres Server with `max_connections` of at least ten per node plus ten, an Active Valkey Server, and an enabled DNS Zone on Cargo Settings. Read [postgres](postgres.md), [valkey](valkey.md) and [object storage](object-storage.md). Atlas must give mail machines a public IPv4 and apply the firewall Cargo sends; read [atlas-contract.md](atlas-contract.md).

## Auto spawn

```json
"default_mail_cluster_config": {
  "node_count": 2,
  "node": {"cpu_millicores": 2000, "ram_gb": 4, "disk_gb": 40},
  "acme_contact_email": "ops@example.com",
  "certificate_management": "ACME",
  "stalwart_version": "v0.16.20"
}
```

`node_count`, `node` and `acme_contact_email` are required. `ensure_mail` runs every minute under a site lock. It rents nothing until the requirements above hold. Then it makes the database `stalwart`, the Valkey user `stalwart` and the bucket `mail`, inserts the cluster on them, and rents the first node. The machine sync starts provisioning when the machine runs: `install.sh`, then `bootstrap.sh`, then the lease is read back and the cluster goes Active, adopts its zone as the platform domain, and reports `Available` to Central. Only then are the remaining nodes rented, one a run, each running `configure.sh` to join. A machine that never came up is reported on the cluster's Error and stops the run; a failed node is provisioned again up to three times.

A cluster added by hand is never joined by the spawner.

## Health

Every minute Cargo probes each serving node's `/healthz/ready` over its public address, reads the registry leases, and looks at the certificate the cluster serves. An unreachable management API or no node answering is Critical and the only finding. A node silent or without an active lease past `node_offline_seconds` is Degraded, as are a certificate within `certificate_warn_days` of expiry and recorded drift. The stores' verdicts are inherited: Postgres or object storage Critical is mail Critical; Valkey Critical is Degraded while the coordinator is in use. Readings go to `logs/mail_health.json.log`.

Every five minutes each serving node's Prometheus metrics are relayed to datum under `stalwart_`, once the exporter is on.

## Runbooks

**A node is Failed.** Its Setup Log holds every line the scripts printed, secrets masked. Fix what it says and run Provision again; the machine is kept. With auto spawn on, Cargo tries three times by itself.

**A node's machine died.** Health fails the node and takes it out of ingress and SPF. On the node, Release Machine lets the machine go (Atlas terminates it if it still runs), then Request Machine asks for another on the same record and provisioning starts when it runs. The hostname and its number stay; the address and lease are new. The Postgres, Valkey and SFU servers have the same two buttons.

**Upgrading.** Set the cluster's `stalwart_version`, then Upgrade Nodes on the cluster: each Active node in turn, the bootstrap node last, is drained, gets the new binary beside the old, restarts, rejoins ingress once its lease is active, and soaks for `soak_minutes` with the cluster Healthy before the next is touched. A failure stops the flow with that node out of ingress and its log on the record. A single node can be upgraded or rolled back on its own; rollback flips the symlink back. Read the release notes about mixed versions first.

**A node Health drained.** After `auto_drain_failures` failed checks an Active node leaves ingress with `drained_by` Health, unless it is the last one answering; it returns by itself after `auto_restore_successes` passes. A node an operator drained is the operator's to restore.

**Store credentials rotated.** Rotate on the Postgres Database or Valkey Credential, then Reconfigure each node: the data store is baked into `config.json`, the rest reaches the nodes through the next config sync.

**Restoring.** Postgres from its dump with `restore.sh`, Cargo's own database from `cargo-backups`, then Provision each node again: `configure.sh` on an Active cluster rewrites the connection and restarts. The drill is owed in the first real region.

## Validation

`tools/mail-smoke/check.sh <cluster> --phase 3` is what a real region must pass: Cargo's own checks, a public resolver's answers and the certificate on every port; read [tools/mail-smoke/README.md](../tools/mail-smoke/README.md). The unit tests run against a fake Stalwart. `tools/stalwart-compat/run.sh` runs the rendered scripts against the pinned Stalwart; `tools/e2e/mail.sh` takes a single-node cluster through the real flows on fake_atlas. Neither has run yet where this was written, since that machine cannot use Docker.
