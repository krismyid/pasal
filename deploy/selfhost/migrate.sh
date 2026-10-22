#!/bin/sh
# Apply packages/supabase/migrations in order, each in its own transaction.
# A file in overrides/ with the same name replaces the upstream migration
# (for Supabase-only syntax). Applied names are recorded in selfhost.migrations.
set -eu

for path in $(ls /migrations/*.sql | LC_ALL=C sort); do
  name=$(basename "$path")
  done_already=$(psql -tAc "SELECT 1 FROM selfhost.migrations WHERE name = '$name'")
  if [ "$done_already" = "1" ]; then
    continue
  fi
  src="$path"
  if [ -f "/overrides/$name" ]; then
    src="/overrides/$name"
    echo "apply $name (override)"
  else
    echo "apply $name"
  fi
  psql -q -v ON_ERROR_STOP=1 --single-transaction \
    -c "SET statement_timeout = 0" -f "$src" \
    -c "INSERT INTO selfhost.migrations (name) VALUES ('$name')"
done

# Tell PostgREST to reload its schema cache.
psql -qc "NOTIFY pgrst, 'reload schema'"
echo "migrations up to date"
