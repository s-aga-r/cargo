#!/usr/bin/env bash
# Runs over SSH on the Valkey machine. Arguments come from the environment.
#
# One Valkey for the region's services, bound to the mesh address alone. It holds transient
# state (coordinator pub/sub, rate limits, greylists), so nothing is persisted to disk and a
# restart starts empty. Re-run safe.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

: "${VALKEY_VERSION:?VALKEY_VERSION is required}"
: "${VALKEY_URL_TEMPLATE:?VALKEY_URL_TEMPLATE is required}"
: "${LISTEN_ADDRESS:?LISTEN_ADDRESS is required}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is required}"
PORT="${PORT:-6379}"
MAX_MEMORY_MB="${MAX_MEMORY_MB:-1024}"

case "$(uname -m)" in
	x86_64) ARCH=x86_64 ;;
	aarch64) ARCH=arm64 ;;
	*) echo "unsupported architecture $(uname -m)" >&2; exit 1 ;;
esac

apt-get update -qq
apt-get install -y -qq curl ca-certificates tar gzip > /dev/null

id -u valkey > /dev/null 2>&1 || useradd --system --home /var/lib/valkey --shell /usr/sbin/nologin valkey
install -d -o valkey -g valkey -m 750 /var/lib/valkey /etc/valkey

# Versioned install behind symlinks, so a rollback is pointing them back.
TARGET="/opt/valkey-$VALKEY_VERSION"
if [ ! -x "$TARGET/bin/valkey-server" ]; then
	url="${VALKEY_URL_TEMPLATE//\{version\}/$VALKEY_VERSION}"
	url="${url//\{arch\}/$ARCH}"
	tmp="$(mktemp -d)"
	curl -fsSL "$url" -o "$tmp/valkey.tar.gz"
	mkdir -p "$TARGET"
	tar -xzf "$tmp/valkey.tar.gz" -C "$TARGET" --strip-components=1
	rm -rf "$tmp"
fi
ln -sfn "$TARGET/bin/valkey-server" /usr/local/bin/valkey-server
ln -sfn "$TARGET/bin/valkey-cli" /usr/local/bin/valkey-cli

# The default user carries Cargo's password; every service gets a user of its own through ACL.
install -o valkey -g valkey -m 600 /dev/null /etc/valkey/users.acl.new
echo "user default on >$ADMIN_PASSWORD ~* &* +@all" > /etc/valkey/users.acl.new
if [ -f /etc/valkey/users.acl ]; then
	# Keep the service users an earlier run or Cargo added; only the default line is ours here.
	grep -v '^user default ' /etc/valkey/users.acl >> /etc/valkey/users.acl.new || true
fi
mv /etc/valkey/users.acl.new /etc/valkey/users.acl

cat > /etc/valkey/valkey.conf <<CONF
bind $LISTEN_ADDRESS
port $PORT
protected-mode yes
aclfile /etc/valkey/users.acl
maxmemory ${MAX_MEMORY_MB}mb
maxmemory-policy volatile-lru
save ""
appendonly no
dir /var/lib/valkey
CONF
chown valkey:valkey /etc/valkey/valkey.conf

cat > /etc/systemd/system/valkey.service <<UNIT
[Unit]
Description=Valkey
After=network-online.target
Wants=network-online.target

[Service]
User=valkey
Group=valkey
ExecStart=/usr/local/bin/valkey-server /etc/valkey/valkey.conf
Restart=on-failure
RestartSec=5
LimitNOFILE=65536
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --quiet valkey
systemctl restart valkey

for _ in $(seq 1 30); do
	if valkey-cli -h "$LISTEN_ADDRESS" -p "$PORT" --user default --pass "$ADMIN_PASSWORD" --no-auth-warning ping 2>/dev/null | grep -q PONG; then
		echo "valkey $VALKEY_VERSION is serving on [$LISTEN_ADDRESS]:$PORT"
		exit 0
	fi
	sleep 2
done
journalctl -u valkey --no-pager --lines 30 >&2
exit 1
