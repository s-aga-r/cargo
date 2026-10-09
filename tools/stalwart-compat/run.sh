#!/bin/bash
# Prove the scripts Cargo renders against the pinned Stalwart release.
#
# Renders install.sh and bootstrap.sh for a Postgres-backed cluster through Cargo's own
# code on SITE, runs them in a systemd container that has Postgres on localhost, then
# reads Domain, Role and SystemSettings back through Cargo's client. The fake Stalwart the
# unit tests use accepts whatever it is given; this is what checks the wire format.
#
# Needs docker and a bench with SITE (cargo installed). Published port 443 and the cluster
# hostname in /etc/hosts are what let the client reach the container; in CI (CI set) the
# hosts line is written with sudo, elsewhere it is printed for you.
set -euo pipefail

SITE="${SITE:-cargo.localhost}"
NAME="cargo-stalwart-compat"
IMAGE="cargo-e2e:latest"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BENCH_PATH="${BENCH_PATH:-$(cd "$REPO_ROOT/../.." && pwd)}"
WORK="$(mktemp -d)"
KEEP=""
[ "${1:-}" = "--keep" ] && KEEP=1

cleanup() {
	rm -rf "$WORK" # the rendered scripts carry the cluster's secrets
	[ -n "$KEEP" ] && { echo "Container '$NAME' left running."; return; }
	docker rm -f "$NAME" > /dev/null 2>&1 || true
}
trap cleanup EXIT

execute() {
	(cd "$BENCH_PATH" && bench --site "$SITE" execute "cargo.cloud_mail.compat.$1" ${2:+--kwargs "$2"})
}

value() {
	grep -m1 "^$2=" <<<"$1" | cut -d= -f2-
}

docker rm -f "$NAME" > /dev/null 2>&1 || true

echo "==> Rendering the scripts"
rendered="$(execute render "{\"directory\": \"$WORK\"}")"
HOSTNAME_UNDER_TEST="$(value "$rendered" hostname)"
MARKER="$(value "$rendered" marker)"
echo "    $HOSTNAME_UNDER_TEST"
if ! getent hosts "$HOSTNAME_UNDER_TEST" | grep -q '^127\.0\.0\.1'; then
	if [ -n "${CI:-}" ]; then
		echo "127.0.0.1 $HOSTNAME_UNDER_TEST" | sudo tee -a /etc/hosts > /dev/null
	else
		echo "Add this to /etc/hosts and run again:" >&2
		echo "  127.0.0.1 $HOSTNAME_UNDER_TEST" >&2
		exit 2
	fi
fi

echo "==> Booting a systemd container with Postgres"
docker build -q -t "$IMAGE" "$REPO_ROOT/tools/e2e" > /dev/null
docker run -d --name "$NAME" \
	--privileged \
	--cgroupns=host \
	-v /sys/fs/cgroup:/sys/fs/cgroup:rw \
	--tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
	-p 127.0.0.1:443:443 \
	"$IMAGE" > /dev/null
for _ in $(seq 1 60); do
	docker exec "$NAME" systemctl is-system-running --wait 2>/dev/null | grep -qE "running|degraded" && break
	sleep 1
done
docker exec "$NAME" bash -euo pipefail -c "
	export DEBIAN_FRONTEND=noninteractive
	apt-get update -qq
	apt-get install -y -qq postgresql > /dev/null
	systemctl start postgresql
	su postgres -c \"psql -qc \\\"create role stalwart login password 'compat-secret'\\\" -c 'create database stalwart owner stalwart'\"
"

echo "==> install.sh"
docker exec -i "$NAME" bash -s < "$WORK/install.sh"
echo "==> bootstrap.sh"
docker exec -i "$NAME" bash -s < "$WORK/bootstrap.sh"

echo "==> Reading the result back through Cargo"
status=0
verified="$(execute verify)" || status=$?
sed 's/^/  /' <<<"$verified" | grep -v '^  failed='
failed="$(value "$verified" failed)"
[ "${failed:-1}" = 0 ] || status=1

echo "==> What the node looks like"
docker exec "$NAME" bash -c "test -f /etc/stalwart/$MARKER" && echo "  ok    the plan marker is there" || { echo "  FAIL  no plan marker"; status=1; }
docker exec "$NAME" bash -c "! grep -q STALWART_RECOVERY /etc/stalwart/stalwart.env" && echo "  ok    no recovery variables remain" || { echo "  FAIL  recovery variables remain"; status=1; }
docker exec "$NAME" bash -c "! journalctl -u stalwart --no-pager | grep -qE 'registry\\.(validation|build)-error'" && echo "  ok    the registry logged no errors" || { echo "  FAIL  registry errors in the journal"; status=1; }

exit $status
