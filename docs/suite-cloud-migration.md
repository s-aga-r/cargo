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

## Names, addresses, zone and certificates

**Two names.** `Machine.name` (`SC-0001-mail-0001`) is the Atlas and operating-system hostname and is never published. `Stalwart Node.hostname` (`n1.mail.<wildcard domain>`) and `Egress Gateway.hostname` (`g1.mail.<wildcard domain>`) come from `naming.next_hostname`, are the reverse-DNS targets and `STALWART_HOSTNAME`, and stay the document names. `install.sh` does not run `hostnamectl`. Numbers are never reused.

**Two address kinds.** Cargo reaches a node at `Machine.address`, the mesh address, for SSH and, when `management_url` is set, for the management API. DNS, SPF and reverse DNS use `Machine.public_ipv4`, a new field that `Machine.sync` fills from `network.public_ipv4`. A running mail machine without one is Broken. `ipv6_address` stays empty unless Atlas was asked for a public IPv6; the mesh address is never published in an AAAA or SPF record. `docs/atlas-contract.md` changes accordingly; see [contract changes](#contract-changes).

**Zone.** Each region's mail lives in its own delegated zone, `mail.<wildcard domain>`, never in the zone that carries Atlas's proxy wildcard. Three things force that: `relative_host` requires every record to sit under the zone apex; Lexicon treats `domain_name` as the provider's zone; and the provider credential is pushed into Stalwart's Postgres as a `DnsServer` object on every node and gateway, so its reach is the region's blast radius. Route53 and Cloudflare can scope a token to one zone; DigitalOcean, Hetzner and Linode tokens are account-wide, so there the zone sits in an account that holds nothing else. The cluster's `default_domain` is the zone apex and its `hostname` is `mx.mail.<wildcard domain>`. `*.<wildcard domain>` resolves to the Proxy, so every mail name is an explicit record Cargo writes, and no mail traffic goes through the Proxy.

**Zone provenance.** `cargo/install.py` `ENROLMENT_VARS` gains `MAIL_DNS_ZONE`, `MAIL_DNS_PROVIDER`, the provider credential variables and `MAIL_DNS_PROVIDER_ZONE_ID`. `record_upstreams` inserts the DNS Zone (its Password fields encrypt on save) and sets `Cargo Settings.dns_zone`. `DNSZone.validate_provider` keeps its live read and adds a write-then-delete probe of a `_cargo-probe` TXT record, so a read-only token fails at insert rather than after bootstrap. Rotation is editing the zone row, after which `sync_config` re-upserts the `DnsServer` object. Drift checks cannot see a stale secret there, because the field is write-only.

**Certificates.** ACME DNS-01 through that `DnsServer`, for the wildcard `*.mail.<wildcard domain>`. `certificate_management` on the cluster is `ACME` (default) or `Manual`. `Manual` omits the `AcmeProvider` object, so a phase 3a node serves Stalwart's self-signed default certificate. `finish_bootstrap` diagnoses issuance: when the management client cannot connect, it reads `/var/log/stalwart/stalwart*` for `acme` lines since the last restart into `last_error`, and fails at once on a definitive error (authorization or propagation failed) instead of waiting out the 45-minute deadline. `acme_contact_email` is required in the spawn config. The first real region runs against the Let's Encrypt staging directory with `verify_stalwart_tls` off, then production.

## Authentication and site identity

| Claim | Value |
|---|---|
| `iss`, `sub` | `central`. Only a token with `iss` `central` may carry `site` or any `mail` scope. An Atlas-signed token (`iss` `atlas:<region-id>`) satisfies `bucket:*` and nothing else. |
| `aud` | `atlas-cargo:<region-id>` |
| `site` | Central's `Site.name`. It exists before the site is reachable, is unique per machine, and does not change when the bench renames the site. Absent on Central's own calls. The same string is `Mail Site.name`, the `site` argument of `create_site`, and the source of the platform address's local part. |
| `scope` | A space-separated set, matched as exact strings, never as globs. `mail` on a site token; `mail:*` on Central's lifecycle calls; `bucket:*` on the bucket calls and `cargo.api.webhooks.configure`, which is what `mint_cargo_token` already presents; `mail:domain` on a domain grant. A token carrying `site` is bound to that site whatever its scope says. `mail` without `site`, and `mail:*` or `bucket:*` with `site`, are refused. `*` satisfies nothing. |
| `exp` | One hour after `iat`. A grant lives ten minutes. |

`verify_token(scopes)` takes the scopes an endpoint requires. The check lives in `authenticate_request`, so a test that patches `authenticate_request` cannot pass a handler a token a real request would not. A wrong scope is `PermissionError` (403); an unverifiable token is `AuthenticationError` (401). Requests run as Guest, so any `frappe.session.user` test left in the copied code is a bug. `ownership.required()` is true exactly when the claims carry `site` and verification is not skipped. The per-minute throttle keys on the `site` claim; Central's own calls key on `sub`.

Today `verify_token` checks no scope, `cargo.api.webhooks.configure` sits behind it, and Central's bucket token already carries `bucket:*` unread. Once sites hold tokens for the same audience, that would let a site re-point Cargo's status reports and replace the webhook secret. Phase 2 closes it before any site token exists.

**Token channel.** Pilot fetches the datum token once, at setup, and never refreshes it; Central hides that with a seven-day lifetime. A one-hour mail token cannot ride that path. What exists is Pilot's per-site Central proxy: `GET {pilot_endpoint}/api/v1/sites/<name>/central/<method>` with `Authorization: Bearer <pilot_auth_token>`, which Frappe's own `PilotClient` already uses and which Pilot gates with `_ALLOWED_EXACT`. Pilot adds `central.api.pilot.mail_token` to that list. Central resolves the Site from `pilot_credential.server`, refuses when there is none or it is not registered with Cargo, mints with `_mint_regional_token(..., "mail", {"site": name}, ttl=3600)`, and answers `{token, expires_in, site, endpoint, jmap_url, mail_hostname}`. The site caches the token until `exp - 60 s` and fetches once more on a 401 from Cargo. Decision 14 holds: Pilot forwards with its `X-Pilot-Token`; only the trigger moves to the site.

**Test harness.** `cargo/testing.py` gains an Ed25519 test keypair, `signed_token(scope, site=None, aud=None, issuer="central", kid="central:test", expires_in=300, **extra)`, `trusted_test_keys()`, which patches only the key-set fetch so `match_kid` and `token_claims` run for real, and `as_request(token, request_ip)`. `test_bucket.py` and `api/test_webhooks.py` convert to it, removing both `patch("cargo.auth.authenticate_request")` calls. `test_bucket.py` passes an audience the real verifier refuses today, so the conversion is not cosmetic.

## Tenancy changes the copied code needs

Suite Cloud's tenancy code assumes every domain has a site and every site is a Suite site. Decisions 8 to 11 break both assumptions, so the code marked "copied" above still changes in these places.

| Need | Change |
|---|---|
| Owner-less domains (decision 8) | `Mail Domain.site` loses `reqd` and `set_only_once`. `cluster` resolves to the region's cluster when `site` is unset. The ownership proof, `assert_domain_available` and `assert_can_add_domain` run only when there is a site. `assert_addresses_deliverable` treats a domain with no owner as foreign (`ifnull(site, '') != site`), so no site can point a catch-all, alias or list at another site's platform address. |
| The platform address (decision 9) | When the cluster goes Active, its `default_domain` is adopted as a site-less Mail Domain, with SPF, DKIM and DMARC (`p=reject`, reports to `postmaster@`) rows published in Cargo's own zone, so the hourly job turns it live. `MailAccount.validate` sets `site = domain.site or self.site` and requires one. `create_site` creates `<site label>@mail.<wildcard domain>` with `disable_receiving=1`, `is_platform_address=1` and a small fixed quota, excluded from `max_accounts` and `max_disk_gb`, linked as `Mail Site.send_only_account`. The site API allows only password rotation on it. A `postmaster@` operator account exists for report ingestion. `From` is always the platform address (`mustMatchSender` stays on); the human goes in `Reply-To`. Bounces are refused by default and documented as lost; `Mail Site.bounces_enabled` keeps `emailReceive` with a small quota for sites that need delivery reports. `Mail Site.max_messages_per_day` is a Stalwart outbound limiter keyed on the authenticated account, with a lower per-domain limiter on the platform domain. The limiters are new cluster-plan work, because Stalwart's quotas cover storage only; their semantics are confirmed on the pinned version in phase 3b. |
| Entitlement | `Mail Site.mailboxes_allowed`, set by Central or an operator, and `Mail Domain.holds_mailboxes`. `assert_receiving_allowed(site, domain)` runs in the controllers: `MailAccount` forces `disable_receiving=1`; groups and lists are refused; `catch_all_address` and `sub_addressing` are refused; `allow_relaying` is on for a domain whose mailboxes another region holds. Plain sites take their limits from Mail Settings. |
| Suspend and archive stop mail | `suspend_site` locks every enabled account that has a `stalwart_id` onto the `suite-disabled` role, one `accounts.update_many` call per cluster. `Mail Account.enabled` is left alone, so `resume_site` unlocks exactly what suspension locked and an account the owner had disabled stays disabled. `push_enabled` and `after_insert` consult the site's status, so toggling or creating an account during a suspension cannot unlock it. Domains are not disabled on suspend, because disabling drops verification. `archive_site(delete_data=False)` locks accounts the same way, then sets every domain `enabled=0` with `disabled_at` and `disabled_reason`, rotates the verification token and records `archived_at`. A status check stops the directory API; the role lock stops mail. |
| The 90-day hold (decision 10) | `disabled_at` is set whenever `enabled` flips to 0. A daily `purge_disabled_domains` deletes a domain's objects, then the domain, one committed transaction each, once `disabled_domain_retention_days` have passed, and reports `purged` to Central. `assert_domain_available` lets another site claim a disabled domain of an Archived site after that site's own TXT proof; a disabled domain of an Active site stays unavailable. |
| Ownership proof and re-verification | The verification token is owned by Central per team and arrives with `create_site`, so a team publishes one TXT record for every region. The ownership record becomes a mandatory row in `rebuild_dns_records` for site-owned domains, `compute_is_verified` requires it, and verified domains are re-resolved daily. `ownership_miss_limit` consecutive misses (default 7) set `enabled=0`. When a caller's record resolves and the holder's does not: an Archived holder is purged and the caller attached in one request; an Active holder's domain is disabled, marked `contested_by` and `contested_at`, the holder notified, and the caller answered 409 until `contest_grace_days` (default 7) pass. `check_domain` stays neutral. |
| The domain registry (decision 11) | Central owns `Mail Domain Registry` (`domain` unique, `team`, `mx_region`, `regions`, `status` Pending, Held or Orphaned, `pending_until`). A site first asks Central for a grant through the Pilot proxy (`central.api.pilot.mail_domain_grant`). The first insert wins, so two regions cannot both become the MX; a Pending row past `pending_until` is reclaimable. The grant is a ten-minute `mail:domain` token carrying `site`, `domain` and `holds_mailboxes`. `create_domain(domain, grant)` verifies it, requires `grant.site == token.site`, runs the TXT proof and stores `holds_mailboxes`. Cargo reports `kind: "domain"` events (`registered`, `verified`, `unverified`, `disabled`, `enabled`, `purged`) through one more Frappe Webhook, on Mail Domain, built by the shared helper, so Cargo still holds no Central credential. The MX moves only when the old region reports zero mailbox accounts on the domain. A domain disabled in its MX region while others hold it goes Orphaned, with the 90-day deadline. DMARC and TLS reports reach only the MX region; the registry links the team to that region's site for them. |
| SPF in several regions | Each region's stored row keeps its value, `v=spf1 include:spf.mail.<wildcard domain> -all`, but SPF rows are verified by mechanism: exactly one `v=spf1` record at the apex, containing `include:<host>`, ending in `-all` or `~all`. `to_api()` adds the instruction to merge the include. The RFC 7208 lookup budget costs one per region plus `spfN` chunks. A platform-wide include is deferred to Central. |
| DKIM | Selectors carry the region, `cargo-<region>-rsa` and `cargo-<region>-ed25519`, for every domain. Keys live in Stalwart's Postgres. |

## Stores Cargo runs

Mail fixes this contract before the Postgres and Valkey services are started, without designing them.

| Service | Shape | What mail reads |
|---|---|---|
| `Postgres Server` | A single-machine `WorkflowBuilder` in the Datum Server shape: a `Machine`, Draft, Setting Up, Active or Failed, `health` and `health_reason`, an install script over `run_over_ssh`, a `service: "postgres"` webhook, an `ensure_postgres` spawner. Listens on the mesh address only, plain TCP, because the mesh is WireGuard. Nightly `pg_dump` of every database it owns to a service-owned bucket, 30 days kept, and a `restore.sh` that brings an empty server back from the latest dump. | — |
| `Postgres Database` | `before_insert` creates a database and an owning role; Stalwart creates its own schema. `rotate_credentials()`. `on_trash` drops both. One per consumer. | `host` (the server's mesh address), `port`, `database`, `user`, `password`, `use_tls` (false) |
| `Valkey Server`, `Valkey Credential` | The same shape; the credential adds an ACL user. Not backed up: it holds coordinator pub/sub, rate-limit counters, greylist and Bayes state, so its loss costs no mail or directory data. Counters and learning restart; nodes reconnect on restart. | `host`, `port`, `user`, `password`, assembled into `redis://user:pass@[mesh address]:6379/0` |
| `Bucket` | Existing. | `bucket_name`, the Object Storage Cluster's `service_endpoint` and `region`, the sole credential row. Validation refuses a `blob_bucket` with more than one row. |

`stores.py` carries Suite Cloud's defaults (database `stalwart`, 15, 30 and 10 second timeouts, ten pool connections, three retries, RocksDb `blobSize` 16834 and `bufferSize` 134217728). `single_node = not data_store`; `coordinator = "Default" if in_memory_store and not single_node else "Disabled"`. Only the data store is baked into `/etc/stalwart/config.json`. The blob and in-memory stores are re-pushed by `sync_config` once `cluster_plan` gains `update` operations on the `BlobStore` and `InMemoryStore` objects, which phase 4 verifies live. A Postgres credential or address change runs a `Reconfigure Nodes` flow: drain, `configure.sh`, restore, one node at a time. `plan.marker` already hashes secrets, so a rotated value re-applies. `Postgres Server` refuses a machine change while any node is Active unless that flow is queued. The mail spawner refuses a config whose `pool_max_connections × (nodes + gateways) + 10` exceeds Postgres's `max_connections`.

The Stalwart unit on a node carries `After=network-online.target` and `Restart=on-failure`, so a reboot that beats Postgres or the mesh retries rather than fails. Path-style S3 addressing against Garage behind nginx is confirmed live in phase 4.

## Network exposure

Mail machines are created with Atlas's `firewall` option, the one Central already sends for tenant machines, passed through a new argument on `AtlasClient.create_vm`: default deny inbound, allow all outbound. If Atlas cannot change a running machine's rules, `install.sh` also installs `ufw` with the same set, so a gateway's relay ports can change later.

| Role | Inbound |
|---|---|
| Stalwart node | TCP 25, 465, 587, 143, 993, 110, 995, 443 and 4190 from anywhere; anything from the mesh prefix `fdaa::/16` |
| Egress gateway | TCP 443 from anywhere; each pool's relay port only from the cluster's node addresses, re-applied by a `sync_firewall` task whenever a node is added, removed, disabled or dies; anything from the mesh |
| SSH | Never on the public interface. Port 22 from `fdaa::/16` only |
| Recovery port 8080 | No rule. `bootstrap.sh` reaches it on `127.0.0.1` |
| Postgres, Valkey, Garage | No public address and no firewall option; mesh addresses in the store configuration; their own authentication regardless, pending Atlas's answer on whether tenant machines can reach tenant `0` machines over the mesh |

Relay routes keep `MtaRoute.address = pool.hostname` over the public address with the source restriction, because the relay uses STARTTLS against the gateway's public certificate. Port 443 stays public (decision 12) with the 64-character admin password, Stalwart's authentication fail2ban confirmed on, and the management API key as the day-to-day credential. Before a new node's `public_ipv4` enters SPF or the ingress record, it is checked against the common blocklists, because Atlas reissues addresses and a replacement machine can inherit a listed one.

Cargo's `TRUSTED_PROXIES` uses `fd00::/8` in two places while Atlas's mesh is `fdaa::/16`. That is fixed Cargo-wide in phase 2b.

## Secrets through the workflow engine

The engine persists the `args` and `kwargs` of every flow and task (`Data` fields, with larger values pickled into Press Workflow Object), each task's `stdout`, and pickled exceptions with their traceback. `SshError` carries the last 3000 characters of the node's output, `OutputLog.store` writes the whole script output into `setup_log`, and the Garage pattern sends the tail to Error Log too. Suite Cloud masks every spelling of a secret in job output; Cargo masks nothing today. Three rules follow, each with a test.

1. Tasks take document names, `Machine` documents or plain values. Every secret is read inside the task with `get_password` and reaches the node only through `script(..., environment=)`, as Garage's and datum's `install_environment` already do. Plans (`bootstrap_ndjson`, `defaults_ndjson`, `cluster_ndjson`, the `env_*` files, `config_json`) are environment values, never arguments or return values.
2. `run_over_ssh(..., secrets=())` masks each line before `on_output`, the cache, the realtime event, the stored field and the `SshError` tail, trying every spelling (raw, JSON-escaped, JSON non-ASCII, `shlex.quote`d), longest first. `script()` returns the secret values it exported, so a caller cannot export one the masker does not know about. Inputs are the admin password, `plan.secret_strings()` of the bootstrap and recovery plans, the store credentials and the DNS provider token. Garage and datum pass their tokens too.
3. Scripts run under `set -euo pipefail`, never `-x`. Plan and environment files are created with `install -m 600 /dev/null <path>` and filled from the exported variable through a redirected heredoc; nothing secret appears on a command line; `trap ... EXIT` removes the plan files and rewrites the normal environment.

`test_provisioning.py` drives a flow under `foreground_enqueue_workflow` with `run_over_ssh` replaced by a fake that echoes its environment and exits non-zero, then asserts that no spelling of any secret appears in any Press Workflow, Press Workflow Task, Press Workflow Object or Error Log row, or in `setup_log`.

The one secret that leaves Cargo's control is the send-only SMTP password, which Pilot writes into the site's `site_config.json` on the tenant machine, readable by the bench user. That is accepted: it is the credential of that site alone, and revocation is the Stalwart role lock.

## State and recovery

Postgres is not the only state that cannot be rebuilt. There are four holders.

| State holder | Holds | Loss costs | Protection |
|---|---|---|---|
| Cargo's MariaDB and the site's `encryption_key` | Stalwart admin, API and relay credentials; DNS provider secrets; Mail Site ownership, limits and verification tokens; the Mail Domain, Account, Group and List mirror with its `stalwart_id`s; and already today Garage's `rpc_secret` and `admin_token`, bucket credentials and Machine SSH keys | Ownership cannot be recovered from Stalwart (decision 7), and `Adopt Directory` refuses a cluster that serves more than one site | A daily job runs Frappe's `BackupGenerator` and uploads the dump and `*-site_config_backup.json` to a `cargo-backups` bucket on the region's Garage. The bucket key and the `encryption_key` are held outside the region, by Central or in the operator vault; without them the backup cannot be fetched or read. |
| Postgres | Directory objects, the mailbox index, the queue, applied configuration, ACME certificates, DKIM private keys | Mail data; every DKIM key regenerates | Nightly `pg_dump` to a service-owned bucket, 30 days, owned by track B |
| Garage blobs | Every message body; the Postgres and Cargo backups (decision 13) | One Garage loss takes bodies and backups together | `replication_factor`, until the out-of-region copy |
| Node-local `/etc/stalwart` | `config.json`, the environment file, the plan marker | Nothing; rebuildable | `bootstrap.sh` keeps the "already been initialized" branch, which with Postgres is the normal re-bootstrap path |

Restore-point rule: Postgres older than the blobs is safe, it leaves orphan blobs; blobs older than Postgres loses bodies. Two runbooks go in `docs/mail.md`:

- **Cargo's database lost, Postgres intact.** Restore the dump and key. Regain root on the nodes through Atlas. Set a new admin password with the recovery-stage `admin_account_operation`, run `ensure_api_key`, `forget_sessions` and `reconcile_directory`, re-own `orphans_on_stalwart` from Central's registry, rotate the DNS and Garage credentials.
- **Postgres lost, Cargo's database intact.** Full re-bootstrap, then a `recreate` pass that pushes every mirrored object and rewrites its `stalwart_id`; the normal `sync` updates by id and cannot. Every domain is marked unverified so owners republish DKIM values under the unchanged selectors.

## Health and telemetry

`cargo/object_storage/health/` is split into `cargo/health/` (`Finding`, `Reading`, an abstract `LiveHealth` with `check`, `record`, `dump` and `prune_history` parameterised by service and settings; `shipping.py` with `get_metrics_info`, `parse_metrics(prefix=)`, `send` and a `Shipper`) and a Garage remainder. The existing tests move unchanged. Postgres and Valkey are built on the same split.

| Phase | Mail health |
|---|---|
| 3a | `MailHealth(LiveHealth)` every minute on Active clusters: `GET https://<node>/healthz/ready` per Active or Draining node, and one `cluster_nodes.get_all()`. Management API unreachable or no node answering is Critical, and the only finding. A node not answering, or a lease not `active`, past `node_offline_seconds` is Degraded. A default certificate within `certificate_warn_days` of expiry is Degraded. A non-empty cached `drift_report` or `directory_report` is Degraded. Failed is Critical; a cluster that has not served is Unknown. Settings in a `Mail Health Settings` Single. |
| 4 | Inherits the region's Object Storage Cluster, Postgres and Valkey verdicts: Garage or Postgres Critical is Critical; Valkey Critical is Degraded when the coordinator is in use. Stalwart's Prometheus exporter is enabled in the plan with a `metrics_token`; `ship_metrics` scrapes each node over the mesh every five minutes, prefix `stalwart_`, labels cluster, region, machine and role. Logs stay on the node (the `Log` tracer, the journal at `warn`, a 14-day prune); datum speaks no OTLP and no Cargo machine ships logs today, so shipping is deferred for all services together. |
| 6 | Auto-drain at `consecutive_failures >= 3` with `drained_by = "Health"`, never the last healthy node; auto-restore only of Health drains, after three consecutive successes; operator drains stay. Failover latency is the ingress record's TTL plus the threshold. Gateways get the same fields. |

A Critical verdict reaches only Error Log today, for mail as for Garage; nothing pages anyone for a region without mail. The smallest fix is a `kind: "health"` delivery to Central through the same webhook helper. It is deferred, and listed first among the things to pick up after the out-of-region copy.

## Operating the cluster

**A dead node.** `sync_pending_machines` watches Pending machines only, so death is detected by the health poll: a lease not active for `node_offline_seconds`, or the Machine Broken or Terminated. Then `sync_node_records(include_ingress=False)`, `set_status("Failed")` so `sending_ips` drops it, `sync_spf_record`, and a best-effort `forget_node`. A Terminated machine's IPv4 goes back to Atlas and can be reissued to someone else, so the SPF resync is a correctness requirement, not housekeeping. The cluster stays Active while one node remains in the ingress record. Cargo never replaces a machine unattended, for the reason object storage gives: the operator releases the Machine, requests a new one for the same node record (hostname, number and DNS names stay), waits for reverse DNS, and provisions (`configure.sh` when the cluster is Active, `bootstrap.sh` when it is Pending or Failed). `forget_node` runs before the join so the lease is fresh. Whether Stalwart keeps a stale `ClusterNode` of the same hostname is settled on the first real region.

**The Cargo host down.** Keeps working: mail flow, JMAP and IMAP, ACME renewal, the cluster domain's MX, DKIM and DMARC. Stops: the directory API (the Suite app shows its unavailable error), lifecycle calls, customer DNS changes and verification, node, ingress and SPF records, report fetching, health, spawn. On return the engine re-enqueues Queued and Running flows itself; the operator runs `check_drift` and `reconcile_directory` and waits for one Healthy pass.

**Upgrade and rollback.** `install.sh` installs `/usr/local/bin/stalwart-<version>` behind a `stalwart` symlink and keeps two versions; `upgrade.sh` flips and restarts; `rollback.sh` flips back. `upgrade_nodes` becomes one flow per cluster that serialises nodes: drain, upgrade, wait for the lease, restore, soak for `soak_minutes` (Mail Settings, default ten) with health Healthy, then the next. Any failure stops the flow and leaves the node Draining. It refuses to start while another upgrade or drain flow runs. A version step is rolled one node at a time only when the release notes allow mixed versions on one data store; otherwise stop all, upgrade all, start the bootstrap node first. `force_fail` takes effect at the next task boundary; a running SSH session ends at its timeout.

**Valkey loss.** Phase 6 stops Valkey on a live three-node cluster, records what Stalwart does with inbound and outbound mail, restarts it, and confirms leases reappear without a Stalwart restart. What is observed is written into `docs/mail.md`. A node joining during the outage is expected to fail after the bootstrap deadline.

**Garage Critical.** Blob writes fail, so inbound mail is deferred by senders and body reads fail. Mail health inherits Critical; the runbook points at `docs/health.md`.
