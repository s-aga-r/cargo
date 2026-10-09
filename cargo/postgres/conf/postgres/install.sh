#!/usr/bin/env bash
# Runs over SSH on the Postgres machine. Arguments come from the environment.
#
# One Postgres for the region's services, listening on the mesh address only: the mesh is
# WireGuard, so connections are plain TCP with scram passwords. Re-run safe.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

: "${POSTGRES_VERSION:?POSTGRES_VERSION is required}"
: "${LISTEN_ADDRESS:?LISTEN_ADDRESS is required}"
: "${ADMIN_ROLE:?ADMIN_ROLE is required}"
: "${ADMIN_PASSWORD:?ADMIN_PASSWORD is required}"
PORT="${PORT:-5432}"
MAX_CONNECTIONS="${MAX_CONNECTIONS:-200}"
MESH_NETWORK="${MESH_NETWORK:-fdaa::/16}"

CONF_DIR="/etc/postgresql/$POSTGRES_VERSION/main"

apt-get update -qq
apt-get install -y -qq "postgresql-$POSTGRES_VERSION" postgresql-client-"$POSTGRES_VERSION" > /dev/null

# Cargo's settings sit in their own file, so an upgrade of the stock one changes nothing here.
install -d -m 755 "$CONF_DIR/conf.d"
cat > "$CONF_DIR/conf.d/cargo.conf" <<CONF
listen_addresses = '$LISTEN_ADDRESS'
port = $PORT
max_connections = $MAX_CONNECTIONS
password_encryption = scram-sha-256
CONF

# Who may connect: the postgres OS user locally, and anyone on the mesh with a password.
cat > "$CONF_DIR/pg_hba.conf" <<HBA
local   all   postgres   peer
local   all   all        scram-sha-256
host    all   all        $MESH_NETWORK   scram-sha-256
HBA
chown postgres:postgres "$CONF_DIR/pg_hba.conf" "$CONF_DIR/conf.d/cargo.conf"
chmod 640 "$CONF_DIR/pg_hba.conf"

systemctl enable --quiet postgresql
systemctl restart postgresql

for _ in $(seq 1 30); do
	if su postgres -c "pg_isready -q -h '$LISTEN_ADDRESS' -p '$PORT'"; then
		break
	fi
	sleep 2
done
su postgres -c "pg_isready -h '$LISTEN_ADDRESS' -p '$PORT'" || { journalctl -u postgresql --no-pager --lines 30 >&2; exit 1; }

# Cargo's role: it makes databases and the roles that own them, and reads health. Not a
# superuser, so a leaked password cannot read another service's data.
ROLE_SQL=$(cat <<SQL
DO \$\$
BEGIN
	IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '$ADMIN_ROLE') THEN
		EXECUTE format('CREATE ROLE %I LOGIN CREATEDB CREATEROLE PASSWORD %L', '$ADMIN_ROLE', '$ADMIN_PASSWORD');
	ELSE
		EXECUTE format('ALTER ROLE %I LOGIN CREATEDB CREATEROLE PASSWORD %L', '$ADMIN_ROLE', '$ADMIN_PASSWORD');
	END IF;
END
\$\$;
SQL
)
printf '%s\n' "$ROLE_SQL" | su postgres -c "psql -v ON_ERROR_STOP=1 -q -p '$PORT'"

echo "postgres $POSTGRES_VERSION is serving on [$LISTEN_ADDRESS]:$PORT"
