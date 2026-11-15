#!/bin/sh
set -eu

export PGPASSWORD="$POSTGRES_PASSWORD"
psql -X -v ON_ERROR_STOP=1 \
  -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -v app_password="$APP_DB_PASSWORD" \
  -f /app/scripts/provision-role.sql

python /app/scripts/run-migrations.py

psql -X -v ON_ERROR_STOP=1 \
  -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -f /app/scripts/grant-app-role.sql
