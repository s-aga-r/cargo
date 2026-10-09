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
| D3. Central | Central | A `MailClient` beside `ObjectStorageClient` calling `cargo.cloud_mail.api.site.*`; the `mail-credential` push to Pilot; `Team Service.add_on_service` gains `mail`; `Mail Domain Registry` | Phase 5 (lifecycle), phase 7 (registry) | After D1 |
| E. Pilot | Pilot | Two entries in `_ALLOWED_EXACT`; a `mail-credential` site action that writes `site_config.json` | Phase 5 | After D2 |
| F. Suite app | Suite | Client on `cargo.cloud_mail.api.*` and `X-Cargo-Access-Token`; a read-only connection panel in Suite Settings | Phase 7 | After phase 2 fixes paths and exception names |
| G. DNS delegation | Whoever runs the base domain | NS delegation of `mail.<wildcard domain>` per region to a provider account or zone that holds nothing else; one credential per region | Phase 3b | Phase 0 |

Tracks A, B, C, D1 and G start in phase 0 together. C and G are the critical path to the first real region; D2, D3 and E to the first unattended site. Phase 2 ends on signed test tokens by design; the first real Central round trip is phase 5.

## Where each part lands

| Suite Cloud | Cargo | Change |
|---|---|---|
| `cloud_mail/` (module Cloud Mail) | `cargo/cloud_mail/` (module Cloud Mail) | Copied. The module keeps its name: Frappe resolves module names across every app on a bench, and the Suite app already owns one called Mail. |
| `cloud_mail/stalwart/` (JMAP management client) | `cargo/cloud_mail/stalwart/` | Unchanged. |
| `cloud_mail/tenancy/` | `cargo/cloud_mail/tenancy/` | Changed for owner-less domains, the platform address, entitlement and re-verification. See [tenancy](#tenancy-changes-the-copied-code-needs). |
| `cloud_mail/cluster/` | `cargo/cloud_mail/cluster/` | `bootstrap.py` is rewritten as flows. `plan.py` gains the pinned version, `certificate_management`, a `DnsServer` object built from the zone, store `update` operations, the Prometheus exporter and outbound limiters. `stores.py` is new. `naming.py` keeps `next_hostname`, `next_pool_name` and `assign_ehlo_hostnames`, loses `next_cluster_label`. |
| Mail Domain, Mail Account, Mail Group, Mailing List, Mail Quota, DMARC Report, TLS Report and their child tables | `cargo/cloud_mail/doctype/` | Mail Domain: `site` optional, `holds_mailboxes`, `disabled_at`, `disabled_reason`, an ownership record re-verified daily. Mail Account: `is_platform_address`. The others unchanged. |
| Stalwart Cluster | `cargo/cloud_mail/doctype/` | Loses the regions table, `is_default`, the SSH keypair, `label` and the four Store links. Gains `blob_bucket`, `data_store`, `in_memory_store`, `management_url`, `certificate_management`, `health`, `health_reason`, `metrics_token`, `auto_spawn`, `auto_setup_attempts`. Extends `WorkflowBuilder`. |
| Stalwart Node, Egress Gateway | `cargo/cloud_mail/doctype/` | Linked to a `Machine`. `ipv4_address` is read from `Machine.public_ipv4`; `ipv6_address` is set only from a public IPv6, never the mesh address. SSH fields, `verify_ssh`, the host-key reset, `validate_single_node` and `_holds_the_only_data_store` are dropped. Node gains `consecutive_failures` and `drained_by`. |
| Stalwart Store | Copied for phases 1 to 3, removed in phase 4 | Until Cargo runs the stores there is nothing else to point a cluster at. Phase 4 replaces it with `cargo/cloud_mail/cluster/stores.py`, holding `rocksdb_store`, `postgres_store`, `s3_store` and `redis_store` lifted from the `_config_*` methods with their defaults. |
| `api/mail/`, `api/site/` | `cargo/cloud_mail/api/mail/`, `cargo/cloud_mail/api/site/` | Authentication changes. Method names, response shapes and exception names do not, and `test_api_contract.py` pins them. |
| `api/fc.py` | `cargo/cloud_mail/api/central.py` | Called by Central with `mail:*`. `create_site` also creates the platform account and returns no password. |
| Suite Site | Mail Site, in `cargo/cloud_mail/doctype/` | Renamed. Loses `api_key`, `api_secret`, `user`, `allowed_ips`. Gains `mailboxes_allowed`, `send_only_account`, `max_messages_per_day`, `bounces_enabled`. Named by Central's `Site.name`. |
| DNS Zone, DNS Record, `dns/` | `cargo/cargo/doctype/`, `cargo/dns/` | Moved to the core module and generalised. Nothing under `cargo/cargo/` or `cargo/dns/` imports `cargo/cloud_mail/`. |
| Suite Cloud Settings | Mail Settings, a Single in `cargo/cloud_mail/` | Runtime knobs only: `skip_domain_verification`, report retention, `verify_stalwart_tls`, `disabled_domain_retention_days`, `ownership_miss_limit`, `sign_with_ed25519`, and later `contest_grace_days` and default limits for plain sites. Build-time values (versions, download URLs, ACME directory) become cluster fields with defaults. |
| `cloud_mail/tests/`, `fake_stalwart.py` | `cargo/cloud_mail/tests/` | `test_ansible.py` and `test_server_job.py` are replaced by `test_provisioning.py`. `test_site_api.py` is rewritten on `cargo.testing.signed_token`. |
| `workspace_sidebar/suite_cloud.json` | `cargo/workspace_sidebar/mail.json` | Sites becomes Mail Site, Settings becomes Mail Settings; the Stores and Server Jobs entries go. |

## What is not copied, in favour of Cargo's own

| Suite Cloud | Cargo's equivalent |
|---|---|
| `provisioning/ansible.py`, the five playbooks, `provisioning/ssh.py` | Scripts under `cargo/cloud_mail/conf/stalwart/`, run with `cargo.ssh.script()` and `run_over_ssh()`; `cargo.ssh.create_keypair` |
| `Server Job`, `Server Job Task`, `retry_failed_jobs`, `max_retries` | `@flow` and `@task`. The engine re-enqueues a workflow whose worker was lost (`retry_workflows`); it never re-runs a failed task. A failed bring-up is retried by running the flow again, with `auto_setup_attempts` capped at `MAX_SETUP_ATTEMPTS`. That is safe because every script is a no-op once its gate says the step is done. |
| `PlaybookRun.mask`, `__secret_values__` | `run_over_ssh(..., secrets=)` in `cargo/ssh.py`, for every service |
| The cluster SSH keypair, `ssh_user`, `ssh_port`, pinned host keys | The `Machine` keypair over the mesh as root, with the host key recorded on first contact |
| Operator-typed `ipv4_address` | `Machine.public_ipv4`, reported by Atlas |
| `Suite Cloud Settings.public_url`, `utils.get_config`, `CONFIG_KEYS` | `Cargo Settings.cargo_url`; cluster fields with defaults; `default_mail_cluster_config` at spawn; `frappe.get_cached_doc("Mail Settings")` |
| `utils.log_error`, `enqueue_job`, `reconnect_on_failure`, `user_context` | `frappe.log_error`, `frappe.enqueue`, the workflow engine. `log_exception` stays: it logs a traceback without local variables, which Cargo has no equivalent for and which keeps store secrets out of Error Log. |
| The roles, the service user, `install.py`, the API key, the IP allow-list, `rotate_site_secret` | `cargo.auth.verify_token(scopes)`; System Manager for the desk |
| `pick_cluster`, `Stalwart Cluster Region`, `is_default`, `resolve_label` | The region's one Active cluster |
| `poll_pending_nodes`, `check_node` promotion, `last_health_at` as a signal | The provision flow's last task waits for the lease; `cargo/cloud_mail/health/` covers everything after |
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
| The 90-day hold (decision 10) | `disabled_at` is set whenever `enabled` flips to 0. A daily `purge_disabled_domains` deletes a domain's objects, then the domain, one committed transaction each, once `disabled_domain_retention_days` have passed, and reports `purged` to Central. Only an archived site's domains are purged: a domain its owner disabled on a living site is theirs to bring back, and a domain nobody owns is Central's to delete. `assert_domain_available` lets another site claim a disabled domain of an Archived site after that site's own TXT proof, purging the old copy as it does; a disabled domain of an Active site stays unavailable. |
| Ownership proof and re-verification | The verification token is owned by Central per team and arrives with `create_site`, so a team publishes one TXT record for every region. The ownership record becomes a mandatory row in `rebuild_dns_records` for site-owned domains, `compute_is_verified` requires it, and verified domains are re-resolved daily. `ownership_miss_limit` consecutive misses (default 7) set `enabled=0` with the reason recorded; a hit resets the count and an inconclusive lookup counts for nothing. When a caller's record resolves and the holder's does not, an Archived holder is purged and the caller attached in one request. Contesting a living holder (disable, `contested_by`, `contested_at`, 409 until `contest_grace_days` pass) waits for phase 7, where Central's registry decides who holds a domain. `check_domain` stays neutral. |
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

## Phases

Each phase ends with the whole Cargo test suite passing and names its rollback. For the copy phases, rollback means reverting the commits; nothing outside this repository changes before phase 3b.

### Phase 0: Prepare

1. Relicensing. Record the copyright holder's approval in `docs/licensing.md`: origin, pinned commit, licence, approver, date. Fill the `[year] [fullname]` placeholders in `license.txt`. `cargo/workflow_engine`, vendored from frappe/press under AGPL, has the same gap; it is raised with the maintainer, not folded in here.
2. Merge `feat/send-only-accounts` in Suite Cloud. Record the commit here and in `docs/licensing.md` as `last_forwarded_commit`.
3. Agree track C with Atlas and track G with whoever runs the base domain. Apply the [contract changes](#contract-changes) to `docs/atlas-contract.md`.
4. Agree the site identifier (Central's `Site.name`) and the claim table with Central. File the two Pilot allow-list entries now; phase 5 is blocked on them.
5. Decide the hooks. Suite Cloud sets `use_json_request_body` and `require_type_annotated_api_methods`; both are per app, and every Cargo whitelisted method is already annotated, so both go into `cargo/hooks.py`.

**Fix forwarding.** Every Suite Cloud change under `cloud_mail/`, `dns/`, the DNS doctypes, `api/` or `Suite Site` gets a matching Cargo PR or a one-line "not ported, because" note. Before each Cargo mail PR merges, `git log <last_forwarded_commit>..HEAD -- suite_cloud/cloud_mail suite_cloud/dns suite_cloud/api` is walked in Suite Cloud, each commit ported or rejected, and the marker bumped. The mapping is the "where each part lands" table. Forwarding ends when Suite Cloud's data is migrated and the repository archived.

### Phase 1: Copy the mail code

1. Copy to the paths above. Rename `Suite Site` to `Mail Site`. Keep the `frappe-suite-verification` TXT prefix, because domain owners have published it.
2. Leave out `provisioning/`, `Server Job`, `Server Job Task`, `install.py` and `patches/`.
3. Rename imports from `suite_cloud.` to `cargo.`, reformat to tabs, replace the dropped helpers, create Mail Settings with the split above.
4. DNS to core. `DNS Record.managed_by_doctype` becomes a Link to DocType instead of a Select of mail doctypes. `default_ttl` reads the zone only, with the zone's `default_ttl` required and defaulting to 300. `frappe.only_for("System Manager")` replaces the dropped role. `enqueue_verify_all_dns_records` goes through `frappe.enqueue(..., deduplicate=True)`. `stalwart_dns_server` moves to `plan.dns_server_object`; `is_default` goes. Tests land at `cargo/dns/test_resolver.py` and under `cargo/cargo/doctype/dns_zone/`.
5. The tenancy data-model changes from the table above, with tests in `test_tenancy.py`: an owner-less domain inserts; a site adding a name under the zone is still refused; site A's platform account is visible to A and not found for B; `owned("Mail Domain", <shared>)` is not found for every site; an address on the shared domain is refused as a recipient; counts exclude the platform account; archive disables domains; purge after retention; takeover of an archived site's domain.
6. Hooks: hourly domain refresh and verification, hourly DMARC and TLS fetch; daily DNS record verification, reverse DNS verification, report pruning, drift check, ownership re-check and `purge_disabled_domains`. Not copied: `poll_pending_nodes`, `retry_failed_jobs`.

Done on 2026-10-09. Ends when the directory, tenancy, DNS, DMARC and TLS tests pass against the fake Stalwart. Nodes cannot be provisioned yet.

### Phase 2: Authentication

1. The signing helpers in `cargo/testing.py`; `test_bucket.py` and `api/test_webhooks.py` converted to them.
2. `verify_token(scopes)` with the rules above; `bucket:*` required on the bucket endpoints and on `configure`.
3. The lifecycle API behind `mail:*`, creating Mail Site rows named by Central's `Site.name` and accepting `mailboxes_allowed`, the limits, the team's ownership token and `delete_data`. `suspend_site` and `archive_site` lock accounts as described.
4. The directory API behind `mail` with `site`. `create_domain(domain, grant)` waits for phase 7, with the registry that issues grants; until then `create_domain(domain)` relies on the TXT proof alone.
5. `docs/central-contract.md` rewritten against Central's code first (see [contract changes](#contract-changes)), then the claim table, `mail_token`, `mail_domain_grant`, the lifecycle calls and the domain events added.
6. `test_api_contract.py`: a frozen list of whitelisted paths equal to what the Suite app's `fake_suite_cloud.py` dispatches; each refuses a request without a token; the exception names the Suite client switches on are kept (`SiteSuspendedError`, `StalwartRejected`, `ClusterMisconfiguredError`, Frappe's `DoesNotExistError`, `DuplicateEntryError`, `TooManyRequestsError`); `owned()` still raises `DoesNotExistError` for another site's object.

Done on 2026-10-09. Ends when the rewritten `test_site_api.py` and `test_tenancy.py` pass with the real `token_claims` through `trusted_test_keys()`, covering: a site reaches its own objects and gets not-found for others; Central acts on any site and on unowned domains; `mailboxes_allowed` off forces send-only and refuses groups, lists and catch-all; `bucket:*` is refused by the mail API and `mail` by the bucket API with 403; a wrong `aud`, `iss`, `kid` or an expired token is 401 with no Mail Site lookup; Suspended answers `SiteSuspendedError`, Archived or unknown answers 401; a background call is refused without fetching keys; the throttle keys on `site`; an Atlas-signed token carrying `site` or `mail` is refused; scope `*` satisfies nothing; suspend, resume and archive leave the expected roles and domain flags on the fake Stalwart.

### Phase 2b: Extract the regional-service helpers

Finished before Stalwart Cluster, Postgres, Valkey or SFU is written. The two existing services change here, so the maintainer is told before this phase starts.

`cargo/service.py` holds plain functions, not a mixin: `SelfCallVisitor` reads `cls.__dict__`, so an inherited `@task` runs but is never recorded as a Press Workflow Step. The functions: the header and status constants, `wildcard_domain()`, `service_domain`, `service_endpoint`, `publish_routes`, `mark`, `configure_service_webhook(doc, service, name, endpoint)` replacing the two near-identical webhook builders, and `single_machine_sync`. `cargo/spawn.py` gains `MAX_SETUP_ATTEMPTS`, `run_spawner`, `report_dead_machines` and `retry_setup`. `Role` gains `MAIL`, `POSTGRES` and `VALKEY`. `TRUSTED_PROXIES` becomes `fdaa::/16`. `cargo/ssh.py` records a Machine's host key on first contact (`Machine.ssh_host_key`) and pins it afterwards. `health/` is split into `cargo/health/`. Object Storage Cluster, its `setup.py` and Datum Server are ported onto all of it.

Porting Datum Server fixes a bug it has today: `sync_machines` compares `Machine.status` (`Broken`, `Terminated`) against Atlas's `DEAD_STATES` (`failed`), so a dead datum machine is never marked Failed. `single_machine_sync` uses `DEAD_MACHINE_STATES`.

Done on 2026-10-09; the two assertions that pinned `fd00::/8` now pin `fdaa::/16`. Ends when `test_spawn.py`, `test_object_storage_cluster.py`, `test_datum_server.py`, `api/test_webhooks.py` and the health tests pass unchanged, and `docs/service.md` describes the pattern. Rollback: revert; the two services behave as before.

### Phase 3a: Provisioning, locally

1. `Machine.public_ipv4` and the `firewall` argument on `create_vm`. Stalwart Node and Egress Gateway linked to a Machine; node validation accepts a missing public address.
2. Scripts: `install.sh` (packages, Unbound with the DNSSEC `ad` check, the `stalwart` user, versioned binaries behind a symlink, the unit, optional `ufw`), `bootstrap.sh`, `configure.sh`, `upgrade.sh`, `rollback.sh`. `bootstrap.sh` keeps every gate the playbook has as its own step: skip when `config.json` exists; delete stale plan markers before a fresh bootstrap; tolerate "already been initialized" and rewrite `config.json`; bootstrap, recovery for the defaults, one normal start, stop, recovery for the cluster plan and admin, normal, each wait bounded at 120 seconds; `trap EXIT` removes the plans and rewrites the normal environment; the marker is written only after a successful apply; `grep STALWART_RECOVERY` must fail before the final restart; a `registry.(validation-error|build-error)` line since `ActiveEnterTimestamp` fails the run. The Ubuntu 20.04 workarounds go: Atlas boots 24.04.
3. Flows: `provision` (rent, wait for Running through `defer_current_task`, `install.sh`, `bootstrap.sh` or `configure.sh`, publish DNS, wait for the lease and promote, with the 45-minute deadline counted from `provisioned_at`), `upgrade` (serialised), `rollback_node`, `drain`, `restore`, `reconfigure_nodes`, `sync_firewall`. Each ordering the playbooks rely on (`check_dnssec_resolution` before `install_stalwart`, `forget_stale_plan_markers` before `check_cluster_plan_applied`, `apply_defaults_plan` before `start_normally`, `check_start_for_config_errors` last) is a task boundary asserted with `called_methods_in_order`.
4. Secret masking in `cargo/ssh.py` and the three rules above.
5. `management_url`, `certificate_management`, `verify_stalwart_tls`; the pinned `STALWART_VERSION` and the spam-rules version in `plan.py`, with the cluster fields defaulting from them.
6. `MailHealth`.
7. `tools/e2e/mail.sh` against `fake_atlas --systemd`: fake_atlas publishes port 443 for role `mail`, `/etc/hosts` gains the cluster hostname, a provider-less DNS Zone and `certificate_management: Manual` are used, and a RocksDb single-node cluster is provisioned through the real flow. It asserts the cluster and node Active, `check_drift()` empty, the `suite-disabled` role present, no `STALWART_RECOVERY` in the environment, no `*.ndjson` under `/etc/stalwart`, exactly one marker, the registry grep clean, and a second `provision` a no-op.
8. `tools/stalwart-compat/run.sh`, a CI job with path filters: it renders the exact script texts for a Postgres and Redis cluster with no blob store and no provider, pipes `install.sh` then `bootstrap.sh` into a systemd container running apt Postgres and Redis, then reads `Domain`, `Role`, `ClusterRole` and `SpamSettings` back through Cargo's own client. That proves the wire format the fake Stalwart accepts without checking. It also confirms a failed ACME order is not logged as a `registry.*` event.
9. `test_provisioning.py`: the orderings; the secret test; script texts carry the expected exports, no `set -x`, and `env_normal` has no `STALWART_RECOVERY`; retry through `advance()` up to `MAX_SETUP_ATTEMPTS` and never on an Active cluster; `sync_firewall` takes typed ports and refuses anything else; `bash -n` over every script.

Code done on 2026-10-09; the exit check is still owed, since the machine the work was done on cannot use Docker: `tools/e2e/mail.sh` and `tools/stalwart-compat/run.sh` are written but have not run, and the ten-minute Healthy soak and the lone-node replacement are untested. Deviations from the steps above: provisioning is three tasks, `install` (install.sh), `bring_up` (bootstrap.sh on the first node of a Pending or Failed cluster, which becomes the bootstrap node, configure.sh on a node joining an Active one) and `record_provisioned`, and the orderings the playbooks relied on live inside the scripts as named steps, so `called_methods_in_order` asserts the task order, not the step order. Renting is the `request_machine` desk action, and `sync_machines` starts provisioning when the machine runs; the flow does not wait with `defer_current_task`. `upgrade`, `rollback` and `reconfigure` are per-node flows that drain an Active node first and record the version the restarted binary reports; `drain` and `restore` stay plain actions; the cluster-wide serialised `upgrade_nodes` is phase 6; `sync_firewall` waits for Atlas to change a running machine's rules (track C), with `Mail Settings.host_firewall` driving `ufw` until then. `management_url` was not added, `base_url` serves. `MailHealth` reads the certificate off the wire rather than through Stalwart, counts only `drift_report` (`directory_report` is not cached), and keeps `last_health_at` and `consecutive_failures` per node; its thresholds are in `Mail Health Settings`. `test_provisioning.py` covers the task orderings, the environments and secrets each script is given, a failed script leaving the node Failed with its log masked, `bash -n`, no `set -x` and no `STALWART_RECOVERY` in the normal environment; the `advance()` retry belongs to the phase 4 spawner and the typed `sync_firewall` ports to track C. `tools/fake_atlas` now reports `public_ipv4: 127.0.0.1` for a machine that asked for one and publishes port 443 for role `mail`.

Ends when `tools/e2e/mail.sh` and the compat job are green, Health reads Healthy for ten minutes on fake_atlas, and the lone node can be replaced (release, re-request, `bootstrap.sh`) with the cluster returning to Active. Rollback: revert; no region touched.

### Phase 3b: The first real region

Before starting: track C delivered (`public_ipv4` in the payload, reverse DNS, egress, inbound ports, firewall); track G delivered and the DNS Zone inserted through the enrolment environment with the write probe passing; stores entered by hand, since the Postgres and Valkey services may not exist yet (a RocksDb data store is acceptable here); `certificate_management: ACME` against the staging directory first.

Ends when `tools/mail-smoke/check.sh <cluster> --phase 3` is green: `check_drift` and `reconcile_directory` empty; `verify_ptr` true; the platform domain verified; `defaultCertificateId` set with names covering the hostname; A, MX, SPF chain, DMARC and DKIM answered by a public resolver; TLS on 443, 465, 993, 587 and 25 presenting that certificate; JMAP 200 with a token and 401 without; one message from a send-only account to an external mailbox arriving with `spf=pass dkim=pass dmarc=pass`; an unknown local part rejected; and one plain site, configured by hand in `site_config.json`, sending from its platform address. Rollback: release the machine, delete the node and cluster records; the zone holds only records Cargo wrote, and `delete_managed_records` removes them.

### Phase 4: One cluster per region, built by Cargo

Entry gate: the Postgres and Valkey services Active in the 3b region, `docs/postgres.md` documenting a restore drill executed there once, and D1 live in Central. Until D1 lands, `accept_cargo_report` ignores `mail`.

1. Link `blob_bucket`, `data_store` and `in_memory_store`, and remove `Stalwart Store` in favour of `stores.py`. A `create_stores` task inserts the `Bucket`, `Postgres Database` and `Valkey Credential` in-process. Derive `single_node` and `coordinator`. Verify the store `update` operations live.
2. Remove the regions table, `is_default`, `pick_cluster` and the label. Refuse a second Active cluster, as object storage does.
3. `cargo/cloud_mail/spawn.py` with `ensure_mail`, driven by `default_mail_cluster_config` (`node_count`, `node: {cpu_millicores, ram_gb, disk_gb}`, `acme_contact_email`, version overrides), validated through `spawn_config`. It waits for Garage, Postgres and Valkey to be Active and for an enabled DNS Zone whose probe passes; otherwise it rents nothing. It adopts the platform domain and creates `postmaster@` when the cluster goes Active.
4. The platform address, the limiters, entitlement enforcement, the `kind: "domain"` webhook. `service: "mail"` reported with `service_endpoint` set to the public HTTPS base URL.
5. Health inheritance, metrics shipping, the Cargo database backup job and the `cargo-backups` bucket.
6. `docs/mail.md` (records, requirements, auto spawn, health, runbooks, validation); `docs/bootstrapping.md` gains the config key and the DNS enrolment variables.

Code done on 2026-10-09, with track B built alongside: `cargo/postgres/` (Postgres Server, Postgres Database, nightly dumps, `restore.sh`) and `cargo/valkey/` (Valkey Server, Valkey Credential), each in the Datum Server shape with its own spawner and health. Deviations from the steps above: `Stalwart Store` is gone and `stores.py` renders from the three records, with the blob and in-memory stores re-pushed by the cluster plan as `update` operations that the first real region still has to confirm live; the regions table, `is_default` and the label went in their own commit, and the cluster is now `mx.<zone>` with the zone as its default domain, so one cluster per zone replaces "one Active cluster"; the spawner checks that the DNS Zone is enabled, not that its probe passes, because the probe belongs to the zone's own validation (track G); `postmaster@` is not created as an account, since Stalwart's report analysis intercepts mail to it; the outbound limiters, `Mail Site.max_messages_per_day` and `bounces_enabled` wait for their semantics to be confirmed on the pinned Stalwart (3b), and the `kind: "domain"` webhook waits for Central to accept it (phase 7); turning on Stalwart's Prometheus exporter in the plan waits for its object name to be confirmed, so `ship_metrics` treats a 404 as nothing to ship; the Cargo database dump went to `cargo/backup.py` as a plain daily job. The fake_atlas exit check and everything in the 3b region are still owed.

Ends when, on fake_atlas, a host with the config key builds its own cluster and the webhook payload is asserted; and in the 3b region, `Service Detail <region>-mail` reads Available, datum shows `stalwart_*` series, Health is Healthy, and the restore drill passes: on fresh machines, Postgres and Cargo's database are restored from Garage with only the out-of-band credentials, the cluster returns to Active through `configure.sh` alone, and `check_drift` and `reconcile_directory` are empty. Rollback: `auto_spawn` off; the drill itself is the rollback rehearsal.

### Phase 5: Callers for every site

Send-only mail for every site needs one node and Central, Pilot and nothing else, so it lands before several nodes do.

1. Central: a `MailClient` beside `ObjectStorageClient`. `create_site` is called from `Site.create_once_addressable`; `archive_site` from wherever Central observes a site's end. No `Site.on_trash` or termination hook was found that fits, so that anchor is Central's to name before this phase starts. `Team Service.add_on_service` gains `mail`, carrying `mailboxes_allowed` and the limits, with `update_site` on change; a Suite signup seeds it.
2. The send-only credential. Cargo keeps the app password encrypted on the Mail Account and never returns it from `create_site`. Central's `mail-credential` action, pushed with `_post_to_pilot` and `mint_bench_login` as `rename_site` is, carries a password obtained once from `rotate_app_password` with its `mail:*` token. Pilot writes `mail_server`, `mail_port`, `use_tls`, `mail_login`, `mail_password` and `auto_email_id` into that site's `site_config.json` through `Site.set_config_values`. Frappe reads these natively, so a plain site needs no app. Every fetch is a rotation; Central stores no mail secret; revocation is the role lock. Frappe uses these keys only for the default outgoing account, so a site with its own Email Account keeps using it.
3. Central adds `mint_mail_token` and `central.api.pilot.mail_token`; Pilot adds the allow-list entry and the action, with cases in its Central client tests.

Ends when a plain site on Central staging sends its first mail from `site_config.json` alone, with nothing configured by hand. Rollback: the `Team Service` row removed and `archive_site` called.

### Phase 6: Several nodes, upgrades, failure drills, sending pools

Suite Cloud's README lists these as never run on a live server: several nodes with a Redis coordinator, node upgrades and the egress gateway.

1. Add nodes; each joins the ingress record once its lease is active; auto-drain and auto-restore.
2. A serialised rolling upgrade, then one node rolled back to the previous version and restored to ingress.
3. Kill one node's machine and replace it, with mail flowing throughout. Stop Valkey, record, restart.
4. Egress Gateway on a Machine with several public addresses (track C2), a local RocksDb store, `sync_firewall`. Plain sites send through a pool separate from Suite sites.

Cargo's part done in code on 2026-10-09: auto-drain and auto-restore in `MailHealth` with `auto_drain_failures` and `auto_restore_successes` in Mail Health Settings, a dead machine failing its node out of ingress and SPF, and the serialised `upgrade_nodes` flow on the cluster with `soak_minutes` in Mail Settings. Not done: `sync_firewall` and a gateway on a multi-address machine, which wait for Atlas (track C2); the drills themselves, which need a region. Plain sites send through whatever pool the platform Mail Domain names, which an operator sets on that domain.

Ends when `check.sh --phase 6` is green across the upgrade, the rollback, the node replacement and the Valkey restart on a three-node cluster, counting sent messages at the receiving mailbox.

### Phase 7: Suite sites, customer domains, several regions

1. The Suite app's client calls `/api/method/cargo.cloud_mail.api.*` with `X-Cargo-Access-Token` from the proxied token. Suite Settings drops `suite_cloud_url`, `site_api_key` and `site_api_secret` for a read-only connection panel fed by `mail_token` and `ping`; `is_suite_cloud_configured` becomes "site config has `pilot_endpoint` and `pilot_auth_token` and the last token fetch succeeded", with its call sites unchanged. The client tests pin the new prefix, header and exception map.
2. Coexistence. A Suite site uses Cargo when that condition holds and the old Suite Cloud client otherwise, until its data is migrated. A Suite Cloud customer domain is not registered in Central until then, so it cannot collide with the registry.
3. Central adds `mail_domain_grant`, `Mail Domain Registry` and the `kind: "domain"` handling; Pilot adds the second allow-list entry.
4. Second-region domains follow the registry rules; DKIM selectors carry the region.

Cargo's part done in code on 2026-10-09: `create_domain(domain, grant)` with `require_domain_grant` in Mail Settings, the three `kind: "domain"` deliveries on Mail Domain carrying the domain's state, and `cargo-<region>-<algorithm>` selectors. The Suite client, Central's registry and Pilot's allow-list entry are theirs.

Ends when `check.sh --phase 7` is green: a Suite site adds a domain through a grant and a mailbox end to end, and no new domain carries a `frappemail-*` selector. Rollback: the Suite app keeps its old client until Suite Cloud is retired.

## SFU

SFU is a fourth consumer of phase 2b: one `Machine` with `public_ipv4`, the `cargo/service.py` helpers without proxy routes, an `install.sh` over SSH, `service: "sfu"` through `configure_service_webhook`, `health` and `health_reason` on `cargo/health/`, and its secret delivered to sites the way the mail credential is, through a Pilot site action. Its Atlas asks are the generic ones below, with TCP 80 and 443 and a UDP range for media. Nothing here is mail-specific, and D1's open service list means it costs Central no further change.

## Contract changes

`docs/atlas-contract.md`: replace "no public address is asked for" and "`network.public_ipv4` is never used" with the role-based asks. A tenant `0` machine may request a public IPv4, reported as `network.public_ipv4`; reverse DNS to a hostname Cargo names; egress from its own address; a `firewall` with inbound TCP and UDP ranges; several addresses (track C2); a public IPv6 on request. The per-role ports are mail's and SFU's. `tools/fake_atlas` answers `public_ipv4: 127.0.0.1` once a machine that asked for one runs, and publishes port 443 for role `mail`; it answers `null` otherwise.

`docs/central-contract.md` is stale and is rewritten before mail is added. Central's receiver is `central.api.state_delivery.receive`, backed by `central/integrations/state_delivery.py`, with `X-FC-Source`, `X-FC-Region` and `X-Frappe-Webhook-Signature`; the client is `central/integrations/cargo.py` beside `object_storage.py`; `Cargo Instance` became `Region.cargo_*` and `Service Backend` became `Service Detail`; the accepted statuses are `Available` and `Not Available`, which Cargo already sends; and Central already mints the `atlas-cargo:<region-id>` audience with scope `bucket:*` (`mint_cargo_token`) for `configure_webhooks` and the bucket calls, so the "Central does not mint it yet" line goes. Then the mail additions: `service: "mail"`, the token route and claim table, the lifecycle calls, the `kind: "domain"` deliveries, the grant, the ownership rules, and the scope row in the check table.

## Deferred

| Item | Why |
|---|---|
| An out-of-region copy of the Postgres, Cargo and blob backups | First to pick up: one Garage loss takes bodies and backups together. |
| Health verdicts delivered to Central | Critical reaches Error Log only, for every service. One `kind: "health"` delivery through the shared helper. |
| Log shipping from Cargo-managed machines to datum | One shipper for Garage, Postgres, Valkey and Stalwart, decided together; datum speaks no OTLP. |
| Postgres and Valkey high availability | Postgres down stops all mail; Valkey down stops coordination and rate limiting. A stable address in front of Valkey needs no mail change. |
| A platform-wide SPF include | Central- or operator-owned DNS; per-mechanism verification accepts either shape. |
| Unifying the `_frappe-verification` and `frappe-suite-verification` records | Two proofs for one customer domain; a Central follow-up. |
| Moving records from the running Suite Cloud site; retiring `frappe/suite_cloud` and the old Suite client | Follows the data migration. Fix forwarding ends here. |
| A search store | Stalwart indexes into the data store until one is needed. |
| `shellcheck` in pre-commit | Repo-wide; offered to the maintainer separately. |

## Risks

| Risk | Mitigation |
|---|---|
| The playbooks carry lessons from a real bootstrap, and a port can lose them | Each gate is a named task asserted in order; `tools/stalwart-compat` runs the real scripts against the pinned binary in CI; the real machine then proves only networking, reverse DNS and certificates. |
| Four state holders, not one; Cargo's database and Garage are as irreplaceable as Postgres | Phase 4 ends with the restore drill; keys held out of region; the out-of-region copy is the first deferred item. |
| Four teams on the critical path to the first real region | Phase 3a proves Cargo's part before any of them deliver; the checklist gates 3b. |
| A dead node's IPv4 left in SPF is reissued to someone who then sends as the region's domains | Health detects death and resyncs SPF; `forget_node` before replacement; blocklist check before a new address is published. |
| The port loses Ansible's `no_log` | Masking at the SSH boundary and the secret-leak test; a secret in a workflow record fails the suite. |
| Stalwart's Prometheus exporter, its `/healthz/ready` endpoint, store `update` operations, path-style S3, the outbound limiters, multi-node leases and stale `ClusterNode` rows are unverified on the pinned version. Suite Cloud reads a node's state from the registry lease only | Each is confirmed on the 3b region before a phase depends on it. |
| Mixed Stalwart versions on one Postgres during an upgrade | A per-release decision from the notes; the stop-all path otherwise. |
| The platform domain's reputation is shared by every tenant | Per-site and per-domain outbound limiters; plain sites on their own egress pool. |
| Account-wide DNS provider tokens | A delegated zone in an account holding nothing else. Suite Cloud's live run used DigitalOcean, so this differs from what was tested. |
| Whether tenant machines reach tenant `0` machines over the mesh | An Atlas question in track C; Postgres and Valkey authenticate regardless. |
| Suite Cloud's hooks `use_json_request_body` and `require_type_annotated_api_methods` change request parsing for every Cargo endpoint | Decided in phase 0 with the existing tests as the check. |

## Review findings and where they land

The earlier draft was reviewed from seven angles; 46 findings stood after each was checked against the code, and three fell. Where each landed:

| Finding | Where | Note |
|---|---|---|
| Scope check underspecified; `configure` left open | Authentication, phase 2 | Adopted: issuer binding, exact matching, `site` binding. |
| Suspend and archive do not stop mail | Tenancy | Adopted: role lock; groups untouched; domains disabled only on archive. |
| Secrets persist in the engine; `no_log` lost | Secrets, phase 3a | Adopted with the leak test. |
| No firewall for public machines | Network exposure | Adopted: Atlas firewall first, `ufw` as fallback. |
| Postgres is not the only irreplaceable state | State and recovery | Adopted: four holders, Cargo backup job, keys out of region. |
| No health, auto-drain, metrics, alerting | Health and telemetry | Adopted; alerting to Central and log shipping deferred and named. |
| ACME and DNS zone provenance | Names, zone and certificates | Adopted: delegated zone, enrolment variables, write probe. |
| Dead node has no path | Operating the cluster | Adopted; replacement stays an operator's call. |
| Extract the service pattern first | Phase 2b | Adopted as plain functions; `cargo/spawn.py` extended. |
| Drop Stalwart Store | Stores Cargo runs | Adopted; RocksDb survives as a builder for gateways and tests. |
| Phase gates contradict; prerequisites hidden; one linear list; token channel unbuilt | Environments and tracks, phases 3a and 3b, phase 5 | Adopted: per-phase environments, tracks table, callers moved before several nodes. |
| fake_atlas can run bootstrap; plans unvalidated against the binary; dropped Server Job tests; phase 2 exit and test signing; contract and smoke tests | Phases 2, 3a, 3b | Adopted. |
| Domain registry has no arbiter; SPF cannot pass in two regions; platform address unrepresentable; archive and retention; ownership reclaim | Tenancy | Adopted with the grant shape and per-mechanism SPF. |
| Owner-less domains unrepresentable; unattended bootstrap inputs; Postgres and Valkey contract; Valkey failure mode; RocksDb for gateways | Tenancy, zone provenance, stores | Adopted; the plain `Redis` type is kept, Sentinel is not added now. |
| Retry and idempotency; Cargo down; rolling upgrade and rollback | Not copied table, operating the cluster | Adopted; the engine resumes flows itself. |
| DNS doctypes to core; Machine to node; Mail Settings split; hooks and sidebar | Phase 1, names and addresses, where each part lands | Adopted. |
| Entitlement on the row, not the scope; Suite UX; stale contract documents; licensing mechanics; SFU | Decisions, phase 7, contract changes, phase 0, SFU | Adopted. |
| A service-agnostic Site record in Cargo's core | — | Refuted: Central has no single Site per site either, and nothing but mail needs a per-site row today. |
| Valkey not needed for a single node, so ship phase 4 without it | — | Refuted: the premise holds, but it would mean a second cutover and contradicts the start order in decision 6. |
| CI shape, semgrep and pip-audit | — | Refuted: three of five claims contradicted by the configuration; the rest is a workflow tweak. |
