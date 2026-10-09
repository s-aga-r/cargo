# Postgres

## Purpose

Each region runs one Postgres, and Cargo owns it. It holds the databases of the services Cargo runs that need a relational store, mail first, each in a database of its own with a role of its own. Nothing outside the region's mesh can reach it. This document covers the server's lifecycle, the per-service databases, and the nightly dumps.

## Records

A **Postgres Server** is the region's one server: the machine it runs on and what it was built with. It is a Single, because a region has one. Its **Status** is the build lifecycle and its **Health** is how the server is doing now.

| Status | Meaning |
|---|---|
| Draft | It has a machine, or is waiting for one. Nothing is installed. |
| Setting Up | A setup run owns the server. |
| Active | Postgres listens on the mesh address and Cargo's role can connect. |
| Failed | The last run could not install it. See Error. |

A **Postgres Database** is one service's database and the role that owns it. Inserting one creates both on the server; deleting it ends the database's connections and drops both. `rotate_credentials` gives the role a new password and leaves telling the consumer to the consumer. `connection()` is what a consumer connects with: the server's mesh address, its port, the database, the role and its password, no TLS.

## How it is built

The install script puts the distribution's PostgreSQL on the machine, makes it listen on the machine's mesh address alone, admits the mesh network with scram passwords and nothing else, and creates Cargo's role: `cargo`, with CREATEDB and CREATEROLE, not a superuser. A leaked Cargo password can make databases, not read another service's. The script is re-run safe, so setting up again is also how a failed run is retried.

Cargo connects as that role over the mesh to make databases and to read health. Consumers connect as the role that owns their database.

## Auto spawn

Put `default_postgres_config` in the site config:

```json
"default_postgres_config": {
  "postgres": {"cpu_millicores": 2000, "ram_gb": 4, "disk_gb": 50},
  "version": "16",
  "max_connections": 200
}
```

The machine size is required; `version` and `max_connections` fall back to the record's defaults. `ensure_postgres` runs on the scheduler under a site lock: it claims the empty Single, asks Atlas for the machine, sets the server up once the machine runs, and retries a failed bring-up up to three times. A server filled in by hand is left alone.

## Health

Every minute Cargo connects as its role and reads the connection count against `max_connections`. Unreachable is Critical; nine tenths of the connections in use is Degraded. Readings go to `logs/postgres_health.json.log`, pruned hourly.

## Dumps

Every night the server dumps each Postgres Database with `pg_dump` and puts it in a `postgres-backups` bucket on the region's Active Object Storage Cluster, under `<database>/<timestamp>.sql.gz`, signed with curl's own SigV4. Dumps older than thirty days are deleted from Cargo. The bucket is made the first night the region's storage serves; until then nothing is dumped.

`cargo/postgres/conf/postgres/restore.sh` brings one database back from a dump: it ends the database's connections, drops whatever is there, creates it afresh with its owner, and feeds the dump to `psql`. It replaces, never merges. It is run over SSH by hand, with the same S3 variables the dump used plus `DATABASE`, `OWNER` and `OBJECT_KEY`. A restore drill in a real region is owed before mail depends on the server.

## What it does not do

No replication and no failover: the server is one machine. Its loss is recovered from the last dump, and what was written since is lost. Mail keeps its DKIM keys and directory here, which is why the dumps exist.
