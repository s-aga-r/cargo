# Valkey

## Purpose

Each region runs one Valkey, and Cargo owns it. It holds the transient state of the services Cargo runs: the mail cluster's coordinator pub/sub, its rate-limit counters, greylists and Bayes state. Nothing in it is data anyone keeps, so it is not persisted or backed up: a restart starts empty, counters and learning begin again, and the services reconnect.

## Records

A **Valkey Server** is the region's one server, a Single in the same shape as the Postgres Server: a machine, a Status for the build lifecycle, a Health for how it is doing now, the release installed, the port, `maxmemory`, and the default user's password that Cargo connects with.

A **Valkey Credential** is one service's ACL user. Inserting one runs `ACL SETUSER` with a generated password and saves the ACL file; deleting it removes the user. `rotate_credentials` replaces the password at once, so the consumer is told first. `connection()` gives the pieces and the `redis://user:password@[mesh address]:port/0` URL a consumer connects with.

## How it is built

The install script takes the pinned release from valkey.io's binary tarballs into `/opt/valkey-<version>` behind symlinks, runs it as its own user, binds it to the machine's mesh address alone with `protected-mode`, writes the ACL file with the default user's password while keeping any service users already there, sets `maxmemory` with `volatile-lru` eviction, and turns persistence off. The tarball name assumes an Ubuntu 24.04 (noble) build for the machine's architecture; the first real machine confirms it.

## Auto spawn

```json
"default_valkey_config": {
  "valkey": {"cpu_millicores": 1000, "ram_gb": 2, "disk_gb": 10},
  "max_memory_mb": 1024
}
```

The machine size is required; `version` and `max_memory_mb` fall back to the record's defaults. `ensure_valkey` claims the empty Single, rents the machine, sets the server up once it runs, and retries a failed bring-up up to three times.

## Health

Every minute Cargo reads `INFO memory` as the default user. Unreachable is Critical; nine tenths of `maxmemory` in use is Degraded, since keys with a lifetime are then being evicted and limiters forget early. Readings go to `logs/valkey_health.json.log`.
