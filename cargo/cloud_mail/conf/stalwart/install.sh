#!/bin/bash
# Runs over SSH on a Stalwart node or gateway. Arguments come from the environment.
# Puts the binaries, the resolver, the user, the unit and (when asked) the firewall in place;
# a second run changes nothing that is already there.
set -euo pipefail

: "${STALWART_VERSION:?STALWART_VERSION is required}"
: "${STALWART_URL_TEMPLATE:?STALWART_URL_TEMPLATE is required}"
: "${STALWART_CLI_VERSION:?STALWART_CLI_VERSION is required}"
: "${STALWART_CLI_URL_TEMPLATE:?STALWART_CLI_URL_TEMPLATE is required}"
: "${SYSTEMD_UNIT:?SYSTEMD_UNIT is required}"
: "${RECOVERY_PORT:?RECOVERY_PORT is required}"
FIREWALL_PORTS="${FIREWALL_PORTS:-}"
RELAY_PORTS="${RELAY_PORTS:-}"
RELAY_SOURCES="${RELAY_SOURCES:-}"
# Atlas's firewall is the usual one; ufw is written only where its rules cannot be changed later.
USE_UFW="${USE_UFW:-0}"
MESH_NETWORK="${MESH_NETWORK:-fdaa::/16}"

export DEBIAN_FRONTEND=noninteractive

if ! grep -qiE '^ID(_LIKE)?=.*(debian|ubuntu)' /etc/os-release; then
	echo "Only Debian or Ubuntu nodes are supported" >&2
	exit 1
fi

apt-get update -qq
apt-get install -qq -y ca-certificates curl gzip tar xz-utils unbound bind9-dnsutils >/dev/null

# Stalwart resolves through a local validating resolver: DNSSEC lets it enforce DANE, and
# blocklists see the node's own address rather than a public resolver's.
systemctl enable --quiet unbound
systemctl start unbound

# The "ad" flag means Unbound validated the answer. It may still be fetching the root trust
# anchor right after install, hence the retries.
for attempt in $(seq 1 10); do
	if dig @127.0.0.1 +dnssec +time=5 +tries=1 . DNSKEY | grep -qE ';; flags:[^;]* ad[ ;]'; then
		break
	fi
	if [ "$attempt" = 10 ]; then
		echo "Unbound does not validate DNSSEC" >&2
		exit 1
	fi
	sleep 3
done

id stalwart >/dev/null 2>&1 || useradd --system --shell /usr/sbin/nologin --home-dir /var/lib/stalwart --no-create-home stalwart
install -d -m 750 -o stalwart -g stalwart /etc/stalwart /var/lib/stalwart /var/log/stalwart
# Rotated logs are pruned after fourteen days.
echo "d /var/log/stalwart 0750 stalwart stalwart 14d" > /etc/tmpfiles.d/stalwart.conf

case "$(uname -m)" in
	x86_64) target=x86_64-unknown-linux-gnu; cli_target=x86_64-unknown-linux-musl ;;
	aarch64) target=aarch64-unknown-linux-gnu; cli_target=aarch64-unknown-linux-musl ;;
	*) echo "No Stalwart build for $(uname -m)" >&2; exit 1 ;;
esac

# Binaries are kept per version behind a symlink, so rollback.sh can point back at the last one.
install_release() {
	local name="$1" version="$2" url_template="$3" archive_target="$4" archive_suffix="$5"
	local versioned="/usr/local/bin/${name}-${version}"
	if [ ! -x "$versioned" ]; then
		local url="${url_template//\{version\}/$version}"
		url="${url//\{target\}/$archive_target}"
		local scratch
		scratch="$(mktemp -d)"
		curl -fsSL -o "$scratch/archive" "$url"
		tar -xf "$scratch/archive" -C "$scratch"
		local binary
		binary="$(find "$scratch" -type f -name "$name" | head -n 1)"
		[ -n "$binary" ] || { echo "$url holds no $name binary" >&2; exit 1; }
		install -m 755 "$binary" "$versioned"
		rm -rf "$scratch"
	fi
	if [ -e "/usr/local/bin/$name" ] && [ ! -L "/usr/local/bin/$name" ]; then
		rm -f "/usr/local/bin/$name"
	fi
	if [ "$(readlink -f "/usr/local/bin/$name" 2>/dev/null)" != "$versioned" ]; then
		[ -L "/usr/local/bin/$name" ] && cp -P "/usr/local/bin/$name" "/usr/local/bin/$name.previous"
		ln -sfn "$versioned" "/usr/local/bin/$name"
	fi
	# Two versions are kept: the one serving and the one before it.
	ls -1t "/usr/local/bin/$name"-* 2>/dev/null | tail -n +3 | xargs -r rm -f
}

install_release stalwart "$STALWART_VERSION" "$STALWART_URL_TEMPLATE" "$target" tar.gz
install_release stalwart-cli "$STALWART_CLI_VERSION" "$STALWART_CLI_URL_TEMPLATE" "$cli_target" tar.xz

unit_path=/etc/systemd/system/stalwart.service
if [ ! -f "$unit_path" ] || [ "$(cat "$unit_path")" != "$SYSTEMD_UNIT" ]; then
	printf '%s\n' "$SYSTEMD_UNIT" > "$unit_path"
	systemctl daemon-reload
fi
systemctl enable --quiet stalwart

if [ "$USE_UFW" = "1" ]; then
	apt-get install -qq -y ufw >/dev/null
	ufw --force reset >/dev/null
	ufw default deny incoming >/dev/null
	ufw default allow outgoing >/dev/null
	ufw allow from "$MESH_NETWORK" >/dev/null
	for port in $FIREWALL_PORTS; do
		ufw allow "$port/tcp" >/dev/null
	done
	for port in $RELAY_PORTS; do
		for source in $RELAY_SOURCES; do
			ufw allow from "$source" to any port "$port" proto tcp >/dev/null
		done
	done
	ufw allow from 127.0.0.1 to any port "$RECOVERY_PORT" proto tcp >/dev/null
	ufw --force enable >/dev/null
fi

echo "installed stalwart $STALWART_VERSION and stalwart-cli $STALWART_CLI_VERSION"
