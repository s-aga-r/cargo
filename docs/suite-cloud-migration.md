# Bringing Suite Cloud into Cargo

## Purpose

`frappe/suite_cloud` deploys Stalwart mail clusters and gives Suite sites a directory API for their domains, accounts, groups and mailing lists. This plan copies it into Cargo, so that mail becomes one more regional service beside object storage and telemetry. Suite Cloud is copied, not moved: its repository and its running site stay as they are until their data is migrated, which is out of scope here.

The plan is ordered around failure modes. The stateful pieces (Postgres, Cargo's own database, Garage) are backed up and restored before any mail depends on them; certificates, DNS and health are automated before several nodes exist; every phase names the environment it runs in, the check that ends it, and how it is rolled back.

Suite Cloud was read at `feat/send-only-accounts` (`353a8ed`), Cargo at `develop` (`4593a9a`), Central and Pilot at their `develop` heads on 2026-10-08. Every Central and Pilot behaviour this plan relies on was checked against that code; anything further is checked before the phase that needs it starts.

## Decisions this plan rests on

The first fifteen are the maintainer's and are fixed. The rest are this plan's, made where latitude was left; each names the alternative it rejects.

| Decision | Effect on the move |
|---|---|
| 1. Cargo keeps its MIT licence; Suite Cloud is AGPL-3.0 | The copied code needs the copyright holder's written approval to be relicensed before it lands. It is recorded in `docs/licensing.md`. |
| 2. The code is copied, not moved; no data migration | Files are copied from one pinned commit, without history. Suite Cloud keeps running and takes fixes only. See [fix forwarding](#phase-0-prepare). |
| 3. Provisioning is ported to Cargo's scripts | Ansible, `ansible-runner`, `PyYAML`, `Server Job` and `Server Job Task` never enter Cargo. |
| 4. Callers use Central-minted EdDSA tokens | Per-site API keys, the service user and the roles are dropped. `scope` and `site` claims are enforced. |
| 5. No code repeated between the two apps | Applied to Cargo itself as well: the regional-service helpers are extracted before mail, Postgres or Valkey are written, so none of them is a fourth copy of the pattern. |
| 6. One Stalwart cluster per region; Garage, Postgres, Valkey and datum run in-region; start order Garage, Postgres, Valkey, Stalwart | Postgres and Valkey are new Cargo services, built against the contract in [stores Cargo runs](#stores-cargo-runs). |
| 7. No Stalwart tenants; Cargo holds the only admin credential | Ownership lives only in Cargo's database, which makes that database irreplaceable state. See [state and recovery](#state-and-recovery). |
| 8. A domain's owner is a site; no owner means Central manages it | `Mail Domain.site` becomes optional. Suite Cloud has no owner-less domain today. |
| 9. Every site gets send-only mail as `<unique-site>@<subdomain>.<region domain>`; mailboxes only for Suite sites | The address is `<site label>@mail.<wildcard domain>`, where `wildcard domain` is already `<region>.<base domain>`. |
| 10. Deleting a site disables its domains unless the owner asks for deletion; disabled domains are kept 90 days; re-attaching needs a fresh DNS proof | New Cargo behaviour: Suite Cloud's `archive_site` touches no domain. |
| 11. Central holds a domain registry; a domain may be added in several regions; the first holds its MX and mailboxes; DKIM selectors carry the region | Central arbitrates with a grant token. SPF is verified by mechanism, not by string equality. |
| 12. Stalwart is reachable over public HTTPS | Mail machines are Cargo's first machines with a public address, so the firewall is specified. |
| 13. Backups go to the same Garage cluster for now | Bodies and backups share one failure domain. The out-of-region copy is the first deferred item to pick up. |
| 14. Pilot fetches site tokens from Central with its `X-Pilot-Token` | Through Pilot's existing per-site Central proxy, triggered by the site. Pilot has no refresh loop to copy: it fetches the datum token once, at setup. |
| 15. SFU is wanted as a simpler single-host service | The extracted helpers are written so SFU is a fourth consumer, not a fourth copy. |
| The zone is `mail.<wildcard domain>`; the cluster label is dropped | Rejected: keeping Suite Cloud's `c1` label. One cluster per region makes it meaningless, and it would put the label in every platform address. |
| `Stalwart Store` is not copied; store configuration is rendered from `Bucket`, `Postgres Database` and `Valkey Credential` records | Rejected: a trimmed Store doctype. It would hold a second copy of every store secret, and `Bucket.rotate_key` would silently break mail. |
| Mailbox entitlement is a field on Mail Site, set by Central | Rejected: entitlement in the token scope. Operators cannot see it and background jobs cannot consult it. Scopes collapse to `mail`, `mail:*`, `bucket:*` and `mail:domain`. |
| Callers for plain sites land before several nodes and egress | Rejected: callers last. Send-only mail for every site needs one node and is the first value this work delivers. |
| Host keys are recorded on first contact and pinned afterwards, for every service | Rejected: calling `StrictHostKeyChecking=accept-new` pinning. With `UserKnownHostsFile=/dev/null` it pins nothing. |
| Secrets are masked in `cargo/ssh.py` for every service, and never travel as task arguments | Rejected: mail-only masking. Garage and datum export secrets the same way. |
| The firewall is Atlas's, requested when the machine is created | Rejected: host `ufw`, unless Atlas cannot change a running machine's rules. Then `install.sh` adds `ufw` with the same set. |
| Health and metrics follow `cargo/object_storage/health/`, split into a shared `cargo/health/` | Rejected: a health module per service. |
| Egress gateways keep a local RocksDb store | Rejected: a second database on the region's Postgres for a transient queue. |

## Environments, tracks and dependencies

| Phase | Runs against |
|---|---|
| 0, 1, 2, 2b | The test suite, with the fake Stalwart and signed test tokens |
| 3a | `tools/fake_atlas --systemd` locally; `tools/stalwart-compat` in CI. No public address, DNS provider or certificate authority |
| 3b | The first real region |
| 4 | fake_atlas for spawn and webhook logic; the 3b region for the restore drill and Central's `Service Detail` |
| 5, 6, 7 | The 3b region, and Central staging |

The Cargo copy is the least constrained work. What gates shipping is outside this repository. Each track names its owner, what it must deliver, and the phase that cannot end without it.

| Track | Owner | Items | Needed by | Starts |
|---|---|---|---|---|
| A. Mail code in Cargo | Cargo | Phases 1, 2, 2b, 3a | — | Phase 0 |
| B. Postgres and Valkey services | Cargo | `Postgres Server` and `Valkey Server` in the Datum Server shape; `Postgres Database` and `Valkey Credential` consumer records; `health`; `pg_dump` to Garage and a drilled `restore.sh`; `ensure_*` spawners | Phase 4 | Phase 0, against `docs/service.md` from phase 2b |
| C. Atlas | Atlas | A public IPv4 on request for a tenant `0` machine, reported as `network.public_ipv4`; reverse DNS to a hostname Cargo names; egress from the machine's own address; the `firewall` create option Central already sends, with inbound rules per role; whether rules can change on a running machine; whether tenant VMs reach tenant `0` machines over the mesh | Phase 3b | Phase 0 |
| C2. Atlas | Atlas | Several public addresses on one machine, each with reverse DNS; `public_ipv6` on request | Phase 6 | After C |
| D1. Central | Central | `state_delivery.SERVICES` and `Service Detail.service` open to `mail`, `postgres`, `valkey`, `sfu` (the Select becomes a Data field or a small registry); `kind: "domain"` deliveries | Phase 4 | Phase 0 |
| D2. Central | Central | `mint_mail_token(region_id, site, scope)`; `central.api.pilot.mail_token` and `mail_domain_grant` behind `pilot_credential_auth` | Phase 5 | Phase 2, once the claim table is agreed |
| D3. Central | Central | A `MailClient` beside `ObjectStorageClient` calling `cargo.mail.api.site.*`; the `mail-credential` push to Pilot; `Team Service.add_on_service` gains `mail`; `Mail Domain Registry` | Phase 5 (lifecycle), phase 7 (registry) | After D1 |
| E. Pilot | Pilot | Two entries in `_ALLOWED_EXACT`; a `mail-credential` site action that writes `site_config.json` | Phase 5 | After D2 |
| F. Suite app | Suite | Client on `cargo.mail.api.*` and `X-Cargo-Access-Token`; a read-only connection panel in Suite Settings | Phase 7 | After phase 2 fixes paths and exception names |
| G. DNS delegation | Whoever runs the base domain | NS delegation of `mail.<wildcard domain>` per region to a provider account or zone that holds nothing else; one credential per region | Phase 3b | Phase 0 |

Tracks A, B, C, D1 and G start in phase 0 together. C and G are the critical path to the first real region; D2, D3 and E to the first unattended site. Phase 2 ends on signed test tokens by design; the first real Central round trip is phase 5.

## Where each part lands

| Suite Cloud | Cargo | Change |
|---|---|---|
| `cloud_mail/` (module Cloud Mail) | `cargo/mail/` (module Mail) | Copied. |
| `cloud_mail/stalwart/` (JMAP management client) | `cargo/mail/stalwart/` | Unchanged. |
| `cloud_mail/tenancy/` | `cargo/mail/tenancy/` | Changed for owner-less domains, the platform address, entitlement and re-verification. See [tenancy](#tenancy-changes-the-copied-code-needs). |
| `cloud_mail/cluster/` | `cargo/mail/cluster/` | `bootstrap.py` is rewritten as flows. `plan.py` gains the pinned version, `certificate_management`, a `DnsServer` object built from the zone, store `update` operations, the Prometheus exporter and outbound limiters. `stores.py` is new. `naming.py` keeps `next_hostname`, `next_pool_name` and `assign_ehlo_hostnames`, loses `next_cluster_label`. |
| Mail Domain, Mail Account, Mail Group, Mailing List, Mail Quota, DMARC Report, TLS Report and their child tables | `cargo/mail/doctype/` | Mail Domain: `site` optional, `holds_mailboxes`, `disabled_at`, `disabled_reason`, an ownership record re-verified daily. Mail Account: `is_platform_address`. The others unchanged. |
| Stalwart Cluster | `cargo/mail/doctype/` | Loses the regions table, `is_default`, the SSH keypair, `label` and the four Store links. Gains `blob_bucket`, `data_store`, `in_memory_store`, `management_url`, `certificate_management`, `health`, `health_reason`, `metrics_token`, `auto_spawn`, `auto_setup_attempts`. Extends `WorkflowBuilder`. |
| Stalwart Node, Egress Gateway | `cargo/mail/doctype/` | Linked to a `Machine`. `ipv4_address` is read from `Machine.public_ipv4`; `ipv6_address` is set only from a public IPv6, never the mesh address. SSH fields, `verify_ssh`, the host-key reset, `validate_single_node` and `_holds_the_only_data_store` are dropped. Node gains `consecutive_failures` and `drained_by`. |
| Stalwart Store | Not copied | `cargo/mail/cluster/stores.py` holds `rocksdb_store`, `postgres_store`, `s3_store` and `redis_store`, lifted from the `_config_*` methods with their defaults. |
| `api/mail/` | `cargo/mail/api/` | Authentication changes. Method names, response shapes and exception names do not, and `test_api_contract.py` pins them. |
| `api/fc.py` | `cargo/mail/api/site.py` | Called by Central with `mail:*`. `create_site` also creates the platform account and returns no password. |
| Suite Site | Mail Site, in `cargo/mail/doctype/` | Renamed. Loses `api_key`, `api_secret`, `user`, `allowed_ips`. Gains `mailboxes_allowed`, `send_only_account`, `max_messages_per_day`, `bounces_enabled`. Named by Central's `Site.name`. |
| DNS Zone, DNS Record, `dns/` | `cargo/cargo/doctype/`, `cargo/dns/` | Moved to the core module and generalised. Nothing under `cargo/cargo/` or `cargo/dns/` imports `cargo/mail/`. |
| Suite Cloud Settings | Mail Settings, a Single in `cargo/mail/` | Runtime knobs only: `skip_domain_verification`, report retention, `verify_stalwart_tls`, `disabled_domain_retention_days`, `ownership_miss_limit`, `contest_grace_days`, default limits for plain sites. Build-time values (versions, download URLs, ACME directory) become cluster fields with defaults. |
| `cloud_mail/tests/`, `fake_stalwart.py` | `cargo/mail/tests/` | `test_ansible.py` and `test_server_job.py` are replaced by `test_provisioning.py`. `test_site_api.py` is rewritten on `cargo.testing.signed_token`. |
| `workspace_sidebar/suite_cloud.json` | `cargo/workspace_sidebar/mail.json` | Sites becomes Mail Site, Settings becomes Mail Settings; the Stores and Server Jobs entries go. |

## What is not copied, in favour of Cargo's own

| Suite Cloud | Cargo's equivalent |
|---|---|
| `provisioning/ansible.py`, the five playbooks, `provisioning/ssh.py` | Scripts under `cargo/mail/conf/stalwart/`, run with `cargo.ssh.script()` and `run_over_ssh()`; `cargo.ssh.create_keypair` |
| `Server Job`, `Server Job Task`, `retry_failed_jobs`, `max_retries` | `@flow` and `@task`. The engine re-enqueues a workflow whose worker was lost (`retry_workflows`); it never re-runs a failed task. A failed bring-up is retried by running the flow again, with `auto_setup_attempts` capped at `MAX_SETUP_ATTEMPTS`. That is safe because every script is a no-op once its gate says the step is done. |
| `PlaybookRun.mask`, `__secret_values__` | `run_over_ssh(..., secrets=)` in `cargo/ssh.py`, for every service |
| The cluster SSH keypair, `ssh_user`, `ssh_port`, pinned host keys | The `Machine` keypair over the mesh as root, with the host key recorded on first contact |
| Operator-typed `ipv4_address` | `Machine.public_ipv4`, reported by Atlas |
| `Suite Cloud Settings.public_url`, `utils.get_config`, `CONFIG_KEYS` | `Cargo Settings.cargo_url`; cluster fields with defaults; `default_mail_cluster_config` at spawn; `frappe.get_cached_doc("Mail Settings")` |
| `utils.log_error`, `log_exception`, `enqueue_job`, `reconnect_on_failure`, `user_context` | `frappe.log_error`, `frappe.enqueue`, the workflow engine |
| The roles, the service user, `install.py`, the API key, the IP allow-list, `rotate_site_secret` | `cargo.auth.verify_token(scopes)`; System Manager for the desk |
| `pick_cluster`, `Stalwart Cluster Region`, `is_default`, `resolve_label` | The region's one Active cluster |
| `poll_pending_nodes`, `check_node` promotion, `last_health_at` as a signal | The provision flow's last task waits for the lease; `cargo/mail/health/` covers everything after |
| `act_as`, `frappe.set_user(site_service_user)` in tests | `cargo.testing.signed_token` and `as_request` |
| `DNSZone.stalwart_dns_server`, `is_default`, `get_default_zone` | `plan.dns_server_object(zone)`; `Cargo Settings.dns_zone` |
| `patches/v1_0/` | Nothing. No data is migrated. |

Dependencies that go with them: `ansible`, `ansible-runner`, `PyYAML`. Dependencies Cargo gains: `dnspython`, `dns-lexicon`.
