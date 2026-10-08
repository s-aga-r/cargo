# Licensing

Cargo is MIT. Code that arrives from a repository under another licence is recorded here, with the approval that lets it be relicensed.

## Suite Cloud

| | |
|---|---|
| Origin | `frappe/suite_cloud`, AGPL-3.0 |
| Pinned commit | `353a8ede7ac8fb3273577ddc58b1451390cdb038` (`feat: adopt groups with receiving already disabled as such`) |
| Copyright holder | Frappe Technologies Pvt Ltd, the author named in its `pyproject.toml` |
| Copied into | `cargo/cloud_mail/`, `cargo/dns/`, `cargo/cargo/doctype/dns_zone/`, `cargo/cargo/doctype/dns_record/` |
| Relicensed as | MIT, with Cargo |
| Approved by | Pending |
| Approved on | Pending |
| `last_forwarded_commit` | `353a8ede7ac8fb3273577ddc58b1451390cdb038` |

Nothing copied from Suite Cloud is merged to `develop` before the two pending rows are filled.

`last_forwarded_commit` is the Suite Cloud commit up to which every change under `cloud_mail/`, `dns/`, the DNS doctypes, `api/` and `Suite Site` has been ported to Cargo or rejected with a note. It is bumped by the fix-forwarding step described in `docs/suite-cloud-migration.md`.

## Workflow engine

`cargo/workflow_engine/` is vendored from `frappe/press`, AGPL-3.0, and carries no approval record. The same approval is needed for it.
