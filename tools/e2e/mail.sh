#!/bin/bash
# Bring one single-node Stalwart cluster up through Cargo's real flows, on fake_atlas.
#
# Needs: docker; fake_atlas running with --systemd (FAKE_ATLAS_URL); a bench whose
# `bench start` is running, since the workflow engine and sync_pending_machines run on the
# scheduler and workers; SITE with cargo installed and a wildcard domain in Cargo Settings.
# The cluster hostname must resolve to 127.0.0.1: the script prints the /etc/hosts line.
#
# Leaves the cluster running. `tools/e2e/mail.sh --clean` throws it away first.
set -euo pipefail

SITE="${SITE:-cargo.localhost}"
FAKE_ATLAS_URL="${FAKE_ATLAS_URL:-http://127.0.0.1:8100}"
TIMEOUT_MINUTES="${TIMEOUT_MINUTES:-30}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BENCH_PATH="${BENCH_PATH:-$(cd "$REPO_ROOT/../.." && pwd)}"

execute() {
	(cd "$BENCH_PATH" && bench --site "$SITE" execute "cargo.mail.e2e.$1" ${2:+--kwargs "$2"})
}

value() {
	grep -m1 "^$2=" <<<"$1" | cut -d= -f2-
}

in_container() {
	docker exec "$CONTAINER" bash -c "$1"
}

passed=0
failed=0
check() {
	local label="$1"
	shift
	if "$@" > /tmp/mail-e2e-check.out 2>&1; then
		echo "  ok    $label"
		passed=$((passed + 1))
	else
		echo "  FAIL  $label"
		sed 's/^/          /' /tmp/mail-e2e-check.out | tail -5
		failed=$((failed + 1))
	fi
}

wait_for_node() {
	local want="$1" deadline=$((SECONDS + TIMEOUT_MINUTES * 60)) report
	while :; do
		report="$(execute status)"
		printf '   %s  cluster=%s node=%s machine=%s %s\n' "$(date +%H:%M:%S)" \
			"$(value "$report" cluster)" "$(value "$report" node)" "$(value "$report" machine)" \
			"$(value "$report" error)"
		case "$(value "$report" node)" in
			"$want") return 0 ;;
			Failed) echo "The node failed. Its Setup Log on the desk has the script output." >&2; return 1 ;;
		esac
		if [ "$SECONDS" -ge "$deadline" ]; then
			echo "Nothing happened for $TIMEOUT_MINUTES minutes. Is \`bench start\` running?" >&2
			return 1
		fi
		sleep 15
	done
}

docker ps > /dev/null || { echo "docker is not usable here" >&2; exit 2; }
curl -fsS -o /dev/null "$FAKE_ATLAS_URL/api/atlas/images?image_type=system&limit=1" \
	-H "Authorization: Bearer e2e" -H "X-Tenant-ID: 0" \
	|| { echo "fake_atlas does not answer on $FAKE_ATLAS_URL" >&2; exit 2; }

echo "==> Preparing the site"
prepared="$(execute prepare "{\"atlas_url\": \"$FAKE_ATLAS_URL\"}")"
CLUSTER="$(value "$prepared" cluster)"
HOSTS_LINE="$(value "$prepared" hosts)"
HOSTNAME_UNDER_TEST="${HOSTS_LINE#127.0.0.1 }"
HOSTNAME_UNDER_TEST="${HOSTNAME_UNDER_TEST%% *}"
if ! getent hosts "$HOSTNAME_UNDER_TEST" | grep -q '^127\.0\.0\.1'; then
	echo "Add this to /etc/hosts and run again:" >&2
	echo "  $HOSTS_LINE" >&2
	exit 2
fi
echo "    cluster $CLUSTER"

echo "==> Asking fake_atlas for the node's machine"
requested="$(execute request_node)"
echo "    node $(value "$requested" node) on $(value "$requested" machine)"

echo "==> Waiting for the node to come up (install.sh, bootstrap.sh, the lease)"
wait_for_node Active

report="$(execute status)"
CONTAINER="$(value "$report" container)"
MARKER="$(value "$report" marker)"

echo "==> What the node looks like"
check "the normal environment has no recovery variables" in_container "! grep -q STALWART_RECOVERY /etc/stalwart/stalwart.env"
check "no plan file was left on disk" in_container "! ls /etc/stalwart/*.ndjson 2>/dev/null | grep -q ."
check "exactly one plan marker" in_container "[ \"\$(ls -A /etc/stalwart | grep -c '^\\.suite-cloud-plan-')\" = 1 ]"
check "the marker is this plan's" in_container "test -f /etc/stalwart/$MARKER"
check "stalwart is running" in_container "systemctl is-active stalwart"
check "the registry logged no errors" in_container "! journalctl -u stalwart --no-pager | grep -qE 'registry\\.(validation|build)-error'"
check "the config file is the stalwart user's alone" in_container "[ \"\$(stat -c %a:%U /etc/stalwart/config.json)\" = 600:stalwart ]"

echo "==> What Cargo sees"
verified="$(execute verify)"
sed 's/^/  /' <<<"$verified" | grep -v '^  failed='
failed=$((failed + $(value "$verified" failed)))

echo "==> Provisioning the node again"
execute reprovision > /dev/null
wait_for_node Active
check "still exactly one plan marker" in_container "[ \"\$(ls -A /etc/stalwart | grep -c '^\\.suite-cloud-plan-')\" = 1 ]"
check "stalwart is still running" in_container "systemctl is-active stalwart"

echo
echo "$passed passed, $failed failed"
echo "The cluster is left running: container $CONTAINER, cluster $CLUSTER on $SITE."
exit $((failed > 0))
