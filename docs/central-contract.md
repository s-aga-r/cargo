# What Cargo and Central say to each other

Three conversations.

**Cargo → Central** is one signed delivery per service, and nothing else: a service reports whether it can be used. Cargo holds no Central credential and calls no Central endpoint.

**Central → Cargo** is bucket work and mail-site work: make a bucket, drop it, add, rotate or remove its keys; register a site for mail, set what it may do, suspend, resume or archive it. Cargo owns every service's admin credential, so Central asks rather than acts.

**Site → Cargo** is the mail directory: a site adds domains, mailboxes, groups and lists on its own slice, with a token Central minted for it.

Cargo's side is `cargo/object_storage/api/bucket.py`, `cargo/cloud_mail/api/central.py`, `cargo/cloud_mail/api/mail/` and the webhooks built in each service's doctype. Central's side is `central/api/state_delivery.py` with `central/integrations/state_delivery.py` behind it, `central/integrations/cargo.py`, and `central/integrations/object_storage.py`.

## Central enrolling Cargo

Before Cargo reports anything it has to be told where, and with which secret. Central owns both, so Central sets them, once it has seen Cargo answer `/api/method/ping`:

```
POST {cargo_url}/api/method/cargo.api.webhooks.configure
X-Cargo-Access-Token: <a token for atlas-cargo:<region-id> with scope bucket:*>

{"request_url": "https://central.example.com/api/method/central.api.state_delivery.receive",
 "webhook_secret": "a-shared-secret",
 "enabled": true}
```

One receiver and one secret serve every service this region runs, so Cargo stores them on Cargo Settings and nothing else. Each delivery reads them when it is built, and knows nothing about how they got there. A repeated call refreshes a rotated secret. `enabled: false` stops the reports and keeps the receiver.

Until this call lands, `central_webhook_url` is empty and a host refuses to configure a delivery rather than pointing one at a guess.

## Cargo reporting in

A **Frappe Webhook**, created against the service's record when the record is created, firing `on_update` when its status is one worth a call.

```
POST {central_webhook_url}
X-FC-Source: cargo
X-FC-Region: <region>
X-Frappe-Webhook-Signature: <base64 HMAC-SHA256 of the body>
```

| Send | What it is |
|---|---|
| `region` | Which region this is. Central identifies a service by its region |
| `region_id` | Atlas's numeric id for that region |
| `service` | `storage`, `telemetry`, and from the mail phases `mail`, `postgres`, `valkey` |
| `status` | `Available` once the service can be used, `Not Available` when it cannot |
| `service_endpoint` | Where the service is reached: the S3 gateway, the telemetry write URL, the mail cluster's HTTPS base URL |

`X-FC-Source` picks Central's handler; the signature, checked against the region's secret, is what authorises the write. Central records only the words above: a report naming another service or status is ignored, with the reason in its reply, so a wrong delivery is readable in the sender's own log. Each region and service has one `Service Detail` row; `activated_on` marks the first report of an outage ending.

`Available` is not "the machines joined". For storage it is "this cluster can hold an object", which needs an applied layout; for mail it is "the cluster serves a valid certificate and holds a management key".

## Central calling Cargo

```
POST {cargo_url}/api/method/<method>
X-Cargo-Access-Token: <token>
```

JSON in, JSON out, unwrapped from Frappe's `{"message": ...}`. Cargo verifies the token against the merged key set at its configured `JWKS_URL` and holds no verification secret of its own. Every check below must pass, in this order:

| Check | Rule |
|---|---|
| Algorithm | The header `alg` is `EdDSA`, and so is the algorithm of the key it resolves to. Checked before the key set is fetched. |
| Key ID | `kid` starts with `central:` or `atlas:<region-id>:`, and names a key on the set. |
| Issuer | `iss` equals the issuer that `kid` namespace belongs to. |
| Audience | `aud` is exactly `atlas-cargo:<region-id>`. |
| Claims | `iss`, `sub`, `aud`, `iat` and `exp` are all present, and `exp` is in the future. |
| Coherence | Only `iss` `central` may carry a `site` claim or a `mail` scope. A token carrying `site` carries neither `mail:*` nor `bucket:*`. The `mail` scope never appears without `site`. |
| Scope | `scope` is a space-separated set, matched as exact strings; `*` names nothing. The endpoint's scope must be in it. |

A token that fails any check but the last answers `AuthenticationError` (401), with no detail. One that verifies but does not cover the call answers `PermissionError` (403).

The key set carries both planes' keys, so a valid signature alone does not say who signed. The key ID decides which issuer a token may claim to be, and `iss` is held to it. It travels in `X-Cargo-Access-Token` rather than `Authorization`, because Frappe rejects an unrecognised `Authorization` header before the endpoint is reached.

Central mints these with `_mint_regional_token` in `central/sso.py`. `mint_cargo_token` already carries scope `bucket:*` for the calls below and for `configure`. The scopes Cargo knows:

| Scope | Carried by | Opens |
|---|---|---|
| `bucket:*` | Central's `mint_cargo_token`; Atlas's own token for its bucket call | `cargo.object_storage.api.bucket.*`, `cargo.api.webhooks.configure` |
| `mail:*` | Central's mail lifecycle token | `cargo.cloud_mail.api.central.*` |
| `mail` with `site` | A site's token, minted by Central for one site | `cargo.cloud_mail.api.site.*`, `cargo.cloud_mail.api.mail.*` |

### Buckets

A Cargo host serves one region. A call naming another is refused, and so is one arriving while the region has no single serving storage cluster. Every call takes `name`, the bucket, and `region`.

| Call | Does |
|---|---|
| `create_bucket` | The bucket and the first key that opens it, as `{"name", "region", "credentials": {"access_key", "secret_access_key"}}`. The secret is handed back here and nowhere else. Either both exist afterwards or neither. |
| `delete_bucket` | Drops the bucket and all its keys. Garage refuses a bucket that still holds objects. |
| `add_credentials` | One more key, in the same `credentials` shape, once. |
| `rotate_credentials` | Takes `access_key`; the new key is made before the old one goes. |
| `remove_credentials` | Takes `access_key`; refuses a bucket's last key. |
| `get_usage` | What the bucket holds, against its caps. |
| `set_quota` | Takes `size_gib` and `max_objects`; zero lifts a cap. |

### Mail sites

Central registers every site it places in the region, Suite site or not, and tells Cargo what the site may do. All under `cargo.cloud_mail.api.central`.

| Call | Does |
|---|---|
| `create_site(site, mailboxes_allowed=True, ownership_token=None, title, contact_email, max_domains, max_accounts, max_groups, max_mailing_lists, max_disk_gb, default_disk_quota_gb)` | A Mail Site named `site`, Central's own name for it: the string its tokens will carry. `mailboxes_allowed` off means the site only sends. `ownership_token` is the team's, so one TXT record proves its domains in every region. Answers 201 with the site's profile, which carries `jmap_url` and `mail_hostname`. No secret is handed out. |
| `get_site(site)` | The profile: status, cluster, title, contact, entitlement, limits and usage. |
| `update_site(site, ...)` | Changes entitlement, title, contact or limits; omitted fields stay. |
| `suspend_site(site)` | Stops the site's API and locks every mailbox the owner left enabled. |
| `resume_site(site)` | Unlocks exactly what suspension locked. |
| `archive_site(site, delete_data=False)` | Final. Without `delete_data`, locks the mailboxes and disables every domain, which are kept for the hold in Mail Settings and may be claimed by a new site that proves control; with it, every directory object is removed at once. |

### Domain grants

Once Central keeps the domain registry, a site adds a domain with a grant: a `mail:domain` token Central mints for ten minutes, carrying `site`, `domain` and `holds_mailboxes`. `mail.domains.create_domain(domain, grant=...)` verifies it, requires it to name the calling site and that domain, runs the TXT proof as before, and stores `holds_mailboxes`. With `require_domain_grant` on in Mail Settings, a region refuses to add a domain without one.

### Domain events

Cargo tells Central about a site's domains through three deliveries on Mail Domain, to the same receiver and with the same headers as the service reports: `event` is `registered` (on insert), `changed` (when `enabled`, `is_verified` or `holds_mailboxes` changes) or `purged` (on delete), with `kind: "domain"`, `domain`, `site`, `enabled`, `verified` and `holds_mailboxes` carrying the domain's state at that moment. Domains nobody owns are not reported.

### SFU

Central fetches a region's SFU credential, `sfu_server_url` and `sfu_secret`, from `cargo.sfu.api.get_credential` with a token of scope `sfu:*`, which only Central holds, and configures sites with it. The SFU reports as service `sfu` with its public HTTPS URL.

## A site calling Cargo

A site reaches Cargo with a token Central minted for that site:

| Claim | Value |
|---|---|
| `iss`, `sub` | `central` |
| `aud` | `atlas-cargo:<region-id>` |
| `site` | Central's `Site.name`, the same string `create_site` was given |
| `scope` | `mail` |
| `exp` | About an hour after `iat` |

Cargo resolves the site from the `site` claim and nothing else. A suspended site is told so (`SiteSuspendedError`, 403); an archived or unknown one is refused (`SiteAuthError`, 401). Each site may make 300 requests per minute. Anything that belongs to another site is reported as not found, never as forbidden. If the cluster refuses a change, the site gets 422 with Stalwart's error type (`StalwartRejected`); if Cargo's own cluster credentials are wrong, 502 (`ClusterMisconfiguredError`).

The methods are those Suite Cloud served, under `cargo.cloud_mail.api`: `site.ping`, `site.update_site_profile`, and the `mail.domains`, `mail.accounts`, `mail.groups`, `mail.mailing_lists`, `mail.meta`, `mail.dmarc` and `mail.tls` modules. `cargo/cloud_mail/tests/test_api_contract.py` pins the list and the exception names a client switches on.

How a site obtains its token, and how Central hands a plain site its send-only credential, are set out in `suite-cloud-migration.md`: Pilot fetches the token through its per-site Central proxy, and Central pushes the credential to Pilot as a site action.

## Who holds which key

Cargo keeps the powerful tokens. Central never sees one.

Garage's `rpc_secret`, admin token and metrics token, and Stalwart's admin password and management key, are minted on the host and stay there. Central's reach is exactly the calls above: it cannot change a layout, read a node, touch an object, or log into a mailbox. Object and mail traffic never goes near either of them: a bench speaks S3 to the gateway and a mail client speaks IMAP, JMAP and SMTP to the cluster, so a Cargo host being down stops new buckets and directory changes, not existing ones.
