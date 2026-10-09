#!/bin/bash
# Runs over SSH on a Stalwart node after install.sh. Bootstraps the data store once, applies
# the cluster plan in recovery mode once per plan version, and leaves Stalwart running normally.
# Every step is gated on what is already on the node, so a second run repeats nothing.
set -euo pipefail

: "${RECOVERY_PORT:?RECOVERY_PORT is required}"
: "${ADMIN_USER:?ADMIN_USER is required}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is required}"
: "${PLAN_MARKER:?PLAN_MARKER is required}"
: "${CONFIG_VERSION:?CONFIG_VERSION is required}"
: "${ENV_NORMAL:?ENV_NORMAL is required}"
: "${ENV_BOOTSTRAP:?ENV_BOOTSTRAP is required}"
: "${ENV_RECOVERY:?ENV_RECOVERY is required}"
: "${CONFIG_JSON:?CONFIG_JSON is required}"
: "${BOOTSTRAP_NDJSON:?BOOTSTRAP_NDJSON is required}"
: "${DEFAULTS_NDJSON:?DEFAULTS_NDJSON is required}"
: "${CLUSTER_NDJSON:?CLUSTER_NDJSON is required}"
WAIT_PORTS="${WAIT_PORTS:-}"

ETC=/etc/stalwart
CLI=/usr/local/bin/stalwart-cli
export STALWART_URL="http://127.0.0.1:$RECOVERY_PORT"
export STALWART_USER="$ADMIN_USER"
export STALWART_PASSWORD="$ADMIN_PASSWORD"

# Plans name the store credentials; whatever happens they must not outlive this run, and the
# node must be left on its normal environment, never a recovery one.
cleanup() {
	local status=$?
	rm -f "$ETC"/bootstrap.ndjson "$ETC"/defaults.ndjson "$ETC"/cluster.ndjson "$ETC"/apply.out
	write_secret "$ETC/stalwart.env" "$ENV_NORMAL"
	# A failed run must not leave Stalwart serving a recovery admin on the recovery port.
	[ "$status" = 0 ] || systemctl stop stalwart || true
}

# The applied records echo every object, store credentials included; they are read only when
# the apply failed, and the masker covers what it knows of.
apply_quietly() {
	if ! "$CLI" apply --file "$1" --json > "$ETC/apply.out" 2>&1; then
		cat "$ETC/apply.out" >&2
		exit 1
	fi
	rm -f "$ETC/apply.out"
}
trap cleanup EXIT

# Secrets go through files created empty with the right mode, then filled: nothing secret is
# ever on a command line.
write_secret() {
	install -m 600 -o stalwart -g stalwart /dev/null "$1"
	printf '%s\n' "$2" > "$1"
}

wait_for_port() {
	local port="$1" deadline=$((SECONDS + 120))
	until (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; do
		if [ "$SECONDS" -ge "$deadline" ]; then
			echo "nothing answered on port $port within 120s" >&2
			journalctl -u stalwart --no-pager --lines 30 >&2 || true
			return 1
		fi
		sleep 2
	done
}

restart_in() {
	write_secret "$ETC/stalwart.env" "$1"
	systemctl restart stalwart
}

if [ ! -f "$ETC/config.json" ]; then
	# A marker left from an earlier data store would skip the cluster plan for this one.
	find "$ETC" -maxdepth 1 -name ".suite-cloud-plan-*" -delete
	restart_in "$ENV_BOOTSTRAP"
	wait_for_port "$RECOVERY_PORT"
	write_secret "$ETC/bootstrap.ndjson" "$BOOTSTRAP_NDJSON"
	if ! output="$("$CLI" apply --file "$ETC/bootstrap.ndjson" --json 2>&1)"; then
		case "$output" in
			*"bootstrap mode"*|*"already been initialized"*) ;;
			*) echo "$output" >&2; exit 1 ;;
		esac
	fi
	# config.json only names the data store. A store initialised by an earlier run refuses to
	# bootstrap again, and writing the file back is all it needs: every node gets the same one.
	case "${output:-}" in
		*"already been initialized"*) write_secret "$ETC/config.json" "$CONFIG_JSON" ;;
	esac
	deadline=$((SECONDS + 120))
	until [ -f "$ETC/config.json" ]; do
		[ "$SECONDS" -lt "$deadline" ] || { echo "Stalwart wrote no config.json" >&2; exit 1; }
		sleep 2
	done

	# The first normal start names the node's cluster role, checks its resolver for DNSSEC and
	# imports the spam filter rules once. Only those are created here: an object Stalwart counts
	# before inserting its defaults (routes, roles, tracers) would suppress them.
	restart_in "$ENV_RECOVERY"
	wait_for_port "$RECOVERY_PORT"
	write_secret "$ETC/defaults.ndjson" "$DEFAULTS_NDJSON"
	apply_quietly "$ETC/defaults.ndjson"

	# Stalwart provisions its built-in roles and default lists only on a normal start; without
	# it the administrator and every account would resolve to no permissions at all.
	restart_in "$ENV_NORMAL"
	wait_for_port 443
	systemctl stop stalwart
fi

if [ ! -f "$ETC/$PLAN_MARKER" ]; then
	restart_in "$ENV_RECOVERY"
	wait_for_port "$RECOVERY_PORT"
	write_secret "$ETC/cluster.ndjson" "$CLUSTER_NDJSON"
	apply_quietly "$ETC/cluster.ndjson"
	# Written only after a successful apply, so a failed plan is tried again next run; the
	# marker of the plan before it goes, so exactly one says what the node holds.
	find "$ETC" -maxdepth 1 -name ".suite-cloud-plan-*" -delete
	echo "$CONFIG_VERSION" > "$ETC/$PLAN_MARKER"
fi

write_secret "$ETC/stalwart.env" "$ENV_NORMAL"
if grep -q STALWART_RECOVERY "$ETC/stalwart.env"; then
	echo "a recovery credential is still in the environment" >&2
	exit 1
fi
systemctl restart stalwart
systemctl enable --quiet stalwart
for port in $WAIT_PORTS; do
	wait_for_port "$port"
done

# Stalwart starts even when stored objects fail to load: it logs and skips them. It opens its
# listeners only after logging, so this start's errors are in the log files by now. The journal
# gets nothing until a Journal tracer is configured, so the files are read.
since="$(date -u -d "$(systemctl show -p ActiveEnterTimestamp --value stalwart)" +%Y-%m-%dT%H:%M:%SZ)"
errors="$(grep -hE 'registry\.(validation-error|build-error)' /var/log/stalwart/stalwart* 2>/dev/null | awk -v since="$since" '$1 >= since' || true)"
if [ -n "$errors" ]; then
	echo "$errors" | tail -n 20 >&2
	exit 1
fi
echo "bootstrap complete at plan $CONFIG_VERSION"
