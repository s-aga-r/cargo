#!/bin/bash
# Runs over SSH after install.sh has put the new version behind the symlink: restarts onto it
# and reports what is running. The old binary stays, for rollback.sh.
set -euo pipefail

WAIT_PORTS="${WAIT_PORTS:-}"

systemctl restart stalwart
systemctl enable --quiet stalwart
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
/usr/local/bin/stalwart --version
