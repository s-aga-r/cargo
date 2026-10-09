#!/usr/bin/env bash
# Runs over SSH on the SFU machine. Arguments come from the environment.
#
# Puts Docker on the machine, takes the Suite project's own SFU deployment (compose file,
# nginx, certbot) from the pinned ref, writes its environment, and runs its setup, which pulls
# the image, provisions the certificate and starts everything. Re-run safe: the deployment's
# own setup skips a certificate it already has.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

: "${SUITE_REF:?SUITE_REF is required}"
: "${SFU_IMAGE:?SFU_IMAGE is required}"
: "${DOMAIN:?DOMAIN is required}"
: "${SSL_EMAIL:?SSL_EMAIL is required}"
: "${JWT_SECRET:?JWT_SECRET is required}"
: "${WEBRTC_ANNOUNCED_IP:?WEBRTC_ANNOUNCED_IP is required}"
METRICS_TOKEN="${METRICS_TOKEN:-}"
WEBRTC_SERVER_PORT="${WEBRTC_SERVER_PORT:-40000}"
MEDIASOUP_NUM_WORKERS="${MEDIASOUP_NUM_WORKERS:-4}"
INSTALL_DIR="${SFU_INSTALL_DIR:-/opt/meet-sfu}"
INSTALLER="https://raw.githubusercontent.com/frappe/suite/$SUITE_REF/suite/meet/sfu-server/deploy/install.sh"

apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg > /dev/null

# --- Docker, from Docker's own repository ------------------------------------------------
if ! docker compose version > /dev/null 2>&1; then
	install -m 0755 -d /etc/apt/keyrings
	curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor --yes -o /etc/apt/keyrings/docker.gpg
	chmod a+r /etc/apt/keyrings/docker.gpg
	. /etc/os-release
	echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
		> /etc/apt/sources.list.d/docker.list
	apt-get update -qq
	apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin > /dev/null
fi
systemctl enable --quiet --now docker

# --- the Suite project's deployment ------------------------------------------------------------
export SFU_INSTALL_DIR="$INSTALL_DIR"
curl -fsSL "$INSTALLER" | bash -s "$SUITE_REF"

# The environment is Cargo's, written whole each run; the installer's template is not kept.
install -m 600 /dev/null "$INSTALL_DIR/.env.new"
cat > "$INSTALL_DIR/.env.new" <<ENV
DOMAIN=$DOMAIN
SSL_EMAIL=$SSL_EMAIL
SFU_IMAGE=$SFU_IMAGE
JWT_SECRET=$JWT_SECRET
METRICS_TOKEN=$METRICS_TOKEN
PORT=3000
HOST=127.0.0.1
SOCKET_PING_TIMEOUT=60000
SOCKET_PING_INTERVAL=25000
WEBRTC_LISTEN_IP=
WEBRTC_ANNOUNCED_IP=$WEBRTC_ANNOUNCED_IP
WEBRTC_SERVER_PORT=$WEBRTC_SERVER_PORT
MEDIASOUP_NUM_WORKERS=$MEDIASOUP_NUM_WORKERS
MEDIASOUP_WORKER_LOGLEVEL=warn
STT_SERVER_URL=${STT_SERVER_URL:-http://127.0.0.1:8000}
SFU_LOG_LEVEL=info
ALLOY_ENVIRONMENT=production
SENTRY_ENVIRONMENT=production
ENV
mv "$INSTALL_DIR/.env.new" "$INSTALL_DIR/.env"

cd "$INSTALL_DIR"
./deploy.sh setup

for _ in $(seq 1 60); do
	if curl -fs -o /dev/null http://127.0.0.1:3000/health; then
		echo "the sfu is answering on $DOMAIN"
		exit 0
	fi
	sleep 2
done
./deploy.sh status >&2 || true
exit 1
