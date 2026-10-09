#!/usr/bin/env bash
# Runs over SSH on the Postgres machine. Dumps each named database and puts it in the
# region's object storage with curl's SigV4 signing; nothing but Postgres and curl needed.
set -euo pipefail

: "${DATABASES:?DATABASES is required}"
: "${S3_ENDPOINT:?S3_ENDPOINT is required}"
: "${S3_BUCKET:?S3_BUCKET is required}"
: "${S3_REGION:?S3_REGION is required}"
: "${S3_ACCESS_KEY:?S3_ACCESS_KEY is required}"
: "${S3_SECRET_KEY:?S3_SECRET_KEY is required}"
PORT="${PORT:-5432}"
STAMP="$(date -u +%Y%m%d-%H%M%S)"

for database in $DATABASES; do
	file="/tmp/$database-$STAMP.sql.gz"
	su postgres -c "pg_dump -p '$PORT' --no-owner --no-privileges '$database'" | gzip > "$file"
	curl -fsS --aws-sigv4 "aws:amz:$S3_REGION:s3" --user "$S3_ACCESS_KEY:$S3_SECRET_KEY" \
		-T "$file" "$S3_ENDPOINT/$S3_BUCKET/$database/$STAMP.sql.gz" > /dev/null
	rm -f "$file"
	echo "dumped $database"
done
