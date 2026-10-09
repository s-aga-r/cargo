#!/bin/bash
# Runs over SSH on a Stalwart node joining a cluster that is already up, or on one whose store
# connection changed. Writes the store configuration and the environment, restarts, and checks
# the start. Nothing here bootstraps a store.
set -euo pipefail

: "${CONFIG_JSON:?CONFIG_JSON is required}"
: "${ENV_NORMAL:?ENV_NORMAL is required}"
WAIT_PORTS="${WAIT_PORTS:-}"

ETC=/etc/stalwart

write_secret() {
	install -m 600 -o stalwart -g stalwart /dev/null "$1"
	printf '%s\n' "$2" > "$1"
}

wait_for_port() {
	local port="$1" deadline=$((SECONDS + 180))
	until (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; do
		if [ "$SECONDS" -ge "$deadline" ]; then
			echo "nothing answered on port $port within 180s" >&2
			journalctl -u stalwart --no-pager --lines 30 >&2 || true
			return 1
		fi
		sleep 2
	done
}

write_secret "$ETC/config.json" "$CONFIG_JSON"
write_secret "$ETC/stalwart.env" "$ENV_NORMAL"
systemctl restart stalwart
systemctl enable --quiet stalwart

if [ -n "$WAIT_PORTS" ]; then
	for port in $WAIT_PORTS; do
		wait_for_port "$port"
	done
else
	# An outbound-only node opens nothing to wait for; give it a moment to finish starting.
	sleep 10
fi
systemctl is-active --quiet stalwart || { journalctl -u stalwart --no-pager --lines 30 >&2; exit 1; }

since="$(date -u -d "$(systemctl show -p ActiveEnterTimestamp --value stalwart)" +%Y-%m-%dT%H:%M:%SZ)"
errors="$(grep -hE 'registry\.(validation-error|build-error)' /var/log/stalwart/stalwart* 2>/dev/null | awk -v since="$since" '$1 >= since' || true)"
if [ -n "$errors" ]; then
	echo "$errors" | tail -n 20 >&2
	exit 1
fi
echo "configured"
