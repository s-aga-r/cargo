# The rendered scripts against a real Stalwart

The unit tests run the mail cluster against a fake Stalwart that accepts whatever it is
given. This runs `install.sh` and `bootstrap.sh`, exactly as Cargo renders them for a
Postgres and Redis cluster, in a systemd container against the pinned Stalwart release, then
reads the result back through Cargo's own client: the default domain, the disabled-accounts
role, the system hostname, and an empty drift report. That is what checks the wire format
and the playbook gates the scripts kept.

```bash
SITE=cargo.localhost tools/stalwart-compat/run.sh          # --keep leaves the container up
```

Needs docker and a bench with the site. The cluster hostname (`mail.compat.compat.test`)
must resolve to 127.0.0.1, where the container publishes port 443; the script prints the
`/etc/hosts` line, and writes it itself in CI. The rendered scripts carry the cluster's
secrets and are removed when the run ends.

In CI this is the `Stalwart compat` job, run on pull requests that touch the scripts, the
plan, `cargo/ssh.py` or this tool. It does not exercise ACME: the cluster keeps a manual
certificate, so a failed order never appears here.
