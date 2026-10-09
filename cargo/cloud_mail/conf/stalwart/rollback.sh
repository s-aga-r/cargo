#!/bin/bash
# Runs over SSH to put a node back on the version it ran before the last upgrade.
set -euo pipefail

WAIT_PORTS="${WAIT_PORTS:-}"

previous=/usr/local/bin/stalwart.previous
if [ ! -L "$previous" ]; then
	echo "no previous version is kept on this node" >&2
	exit 1
fi
target="$(readlink -f "$previous")"
[ -x "$target" ] || { echo "$target is gone" >&2; exit 1; }
ln -sfn "$target" /usr/local/bin/stalwart
rm -f "$previous"

systemctl restart stalwart
for port in $WAIT_PORTS; do
	deadline=$((SECONDS + 180))
	until (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; do
		if [ "$SECONDS" -ge "$deadline" ]; then
			echo "nothing answered on port $port within 180s" >&2
			journalctl -u stalwart --no-pager --lines 30 >&2 || true
			exit 1
		fi
		sleep 2
	done
done
[ -n "$WAIT_PORTS" ] || sleep 10
systemctl is-active --quiet stalwart || { journalctl -u stalwart --no-pager --lines 30 >&2; exit 1; }
/usr/local/bin/stalwart --version
