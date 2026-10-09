#!/usr/bin/env bash
# Runs over SSH on the Postgres machine. Brings one database back from a dump in the
# region's object storage: the database is created if it is missing, its owner must exist.
set -euo pipefail

: "${DATABASE:?DATABASE is required}"
: "${OWNER:?OWNER is required}"
: "${OBJECT_KEY:?OBJECT_KEY is required}"
: "${S3_ENDPOINT:?S3_ENDPOINT is required}"
: "${S3_BUCKET:?S3_BUCKET is required}"
: "${S3_REGION:?S3_REGION is required}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY is required}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY is required}"
PORT="${PORT:-5432}"
umask 077

file="/tmp/restore-$DATABASE.sql.gz"
curl -fsS --aws-sigv4 "aws:amz:$S3_REGION:s3" -K <(printf 'user = "%s:%s"\n' "$S3_ACCESS_KEY" "$S3_SECRET_KEY") \
	-o "$file" "$S3_ENDPOINT/$S3_BUCKET/$OBJECT_KEY"
# Whatever is there goes first: a dump loaded over existing tables stops at the first CREATE.
su postgres -c "psql -p '$PORT' -tAc \"SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$DATABASE' AND pid <> pg_backend_pid()\"" > /dev/null
su postgres -c "dropdb --if-exists -p '$PORT' '$DATABASE'"
su postgres -c "createdb -p '$PORT' -O '$OWNER' '$DATABASE'"
gunzip -c "$file" | su postgres -c "psql -v ON_ERROR_STOP=1 -q -p '$PORT' -d '$DATABASE'"
rm -f "$file"
echo "restored $DATABASE from $OBJECT_KEY"
