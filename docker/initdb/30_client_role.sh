#!/bin/sh
# Creates the client's own role in both databases when the database volume is first created.
# For a volume created before this script existed, run it once by hand:
#   docker compose exec db sh /docker-entrypoint-initdb.d/30_client_role.sh
set -eu
password="${CDSE_DB_PASSWORD:-}"
if [ -z "$password" ] && [ -n "${CDSE_DB_PASSWORD_FILE:-}" ]; then
    password="$(cat "$CDSE_DB_PASSWORD_FILE")"
fi
if [ -z "$password" ]; then
    echo "set CDSE_DB_PASSWORD in .env, see .env.example" >&2
    exit 1
fi
for database in "$POSTGRES_DB" iris_test; do
    psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$database" \
        -v password="$password" -f /opt/cdse/client_role.sql
done
