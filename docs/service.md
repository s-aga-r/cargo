# Adding a regional service

## Purpose

Cargo runs one copy of each shared service per region: object storage, telemetry, and from the mail work Postgres, Valkey, Stalwart and the SFU. Each is a doctype that rents machines from Atlas, installs software on them over SSH, publishes its routes, and reports to Central. This document is the pattern they follow and the pieces they share, so a new service is a fourth instance of it rather than a fourth copy.

## The shape of a service

| Part | Where | What it does |
|---|---|---|
| The record | `cargo/<service>/doctype/<service>/` | A `WorkflowBuilder` with `status` (Draft, Setting Up, Active, Failed), `error`, `setup_log`, `health`, `health_reason`, `auto_spawn`, `auto_setup_attempts`, and a link or table of its machines |
| Machines | `cargo/cargo/doctype/machine/` | One `Machine` per VM, rented with `Machine.request(owner, spec, base_image=...)`. The record's role names come from `cargo.client_models.Role` |
| Setup | A `@flow` of `@task`s on the record | Each task catches its own failure, logs it with `frappe.log_error`, marks the record Failed with a reason, and returns False. Scripts under `cargo/<service>/conf/` reach the machine through `cargo.ssh.script()` and `run_over_ssh()`, which take the machine's `host_key_pin()` |
| Routes and names | `cargo/service.py` | `service_domain(site_name)` and `service_endpoint(site_name)` under the region's wildcard domain; `publish_routes(domains, address)` through the Proxy; site names end in `-svc` |
| Reporting | `cargo/service.py` | `configure_service_webhook(doc, service, name, endpoint)` builds the Frappe Webhook that tells Central `Available` or `Not Available` when the record turns Active or Failed. The `service` word must be one Central records |
| Machines settling | `cargo/service.py` | `single_machine_sync(doc)` fails a one-machine service whose machine died; a cluster writes its own `sync_machines` |
| Spawning | `cargo/<service>/spawn.py` on `cargo/spawn.py` | `run_spawner(config_key, lock_name, validate, build)` runs once a minute from `hooks.py`, off until the config key is in site config, one at a time. `report_dead_machines` stops a run that holds a machine that never came up; `retry_setup` runs a failed bring-up again up to `MAX_SETUP_ATTEMPTS` |
| Health | `cargo/health/live.py` | A `LiveHealth` subclass with `findings()` and `log_entry()`; `record()` writes the verdict and a log line. `cargo/health/shipping.py` relays Prometheus metrics to datum under the service's prefix |

## Rules the pattern depends on

- **Shared logic is plain functions, not a mixin.** The workflow engine finds a class's own tasks by name (`SelfCallVisitor` reads `cls.__dict__`), so an inherited `@task` runs but is never recorded as a step. Every step a flow should show stays a literal method on the concrete class.
- **Secrets never travel through the engine.** Task arguments, output and exceptions are stored on `Press Workflow Task`. A task takes document names and plain values, reads secrets inside with `get_password`, and hands them to the machine only through `script(..., environment=)`.
- **A machine that never came up is reported, not replaced.** Replacing one unattended is how a spawner runs away with money. The operator releases it and the next run rents another.
- **Setting up again rents nothing**, so a failed bring-up is retried; a live service that failed is left to its operator, because setting up again cannot raise dead machines.
- **The Proxy fronts HTTP only.** A service reached over anything else needs a public address from Atlas and its own firewall rules, as mail does.
- **Nginx on a machine trusts the mesh**, `fdaa::/16`, and nothing wider: `cargo.service.TRUSTED_PROXIES`.

## Tests

`cargo/testing.py` has `use_test_settings()` for Cargo Settings, `make_dns_zone()` for a zone, and `signed_token()`, `trusted_test_keys()` and `as_request()` for a request carrying a Central-minted token. A service's tests patch `run_over_ssh` in the module that calls it and assert the script text that was sent.
