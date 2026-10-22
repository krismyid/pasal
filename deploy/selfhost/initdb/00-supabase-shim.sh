#!/bin/sh
# Recreate the parts of Supabase the migrations and PostgREST expect:
# API roles, auth.role(), a stub storage schema, and the extensions schema.
# Runs once, on first start of an empty data volume.
set -eu

psql -v ON_ERROR_STOP=1 -v auth_pw="$AUTHENTICATOR_PASSWORD" \
     --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<'SQL'
-- API roles. PostgREST logs in as authenticator and switches to the role in the JWT.
CREATE ROLE anon NOLOGIN;
CREATE ROLE authenticated NOLOGIN;
CREATE ROLE service_role NOLOGIN BYPASSRLS;
CREATE ROLE authenticator LOGIN NOINHERIT PASSWORD :'auth_pw';
GRANT anon, authenticated, service_role TO authenticator;

-- Same per-role timeout Supabase applies to anon (PostgREST applies role settings).
ALTER ROLE anon SET statement_timeout = '3s';
ALTER ROLE authenticated SET statement_timeout = '8s';

-- Extensions live in their own schema (migration 049 expects this).
CREATE SCHEMA extensions;
CREATE EXTENSION ltree SCHEMA extensions;
CREATE EXTENSION pg_trgm SCHEMA extensions;
GRANT USAGE ON SCHEMA extensions TO anon, authenticated, service_role;
SELECT format('ALTER DATABASE %I SET search_path = "$user", public, extensions', current_database()) \gexec

-- auth.role() reads the role claim PostgREST puts in request.jwt.claims.
CREATE SCHEMA auth;
GRANT USAGE ON SCHEMA auth TO anon, authenticated, service_role;
CREATE FUNCTION auth.role() RETURNS text LANGUAGE sql STABLE AS $$
  SELECT coalesce(
    nullif(current_setting('request.jwt.claim.role', true), ''),
    (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role')
  )::text
$$;

-- Stub storage schema so migration 023 applies. Nothing serves these files.
CREATE SCHEMA storage;
CREATE TABLE storage.buckets (id text PRIMARY KEY, name text NOT NULL, public boolean DEFAULT false);
CREATE TABLE storage.objects (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  bucket_id text REFERENCES storage.buckets(id),
  name text
);
ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;

-- Privileges. Reads go through RLS policies; anon and authenticated only read.
-- Unlike Supabase defaults they get no INSERT/UPDATE/DELETE, since the self-hosted
-- stack serves no web app. service_role (the worker) gets full access.
GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT SELECT ON TABLES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT ALL ON TABLES TO service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO anon, authenticated;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT ALL ON SEQUENCES TO service_role;
ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public
  GRANT EXECUTE ON FUNCTIONS TO anon, authenticated, service_role;

-- Applied-migration log for migrate.sh.
CREATE SCHEMA selfhost;
CREATE TABLE selfhost.migrations (name text PRIMARY KEY, applied_at timestamptz DEFAULT now());
SQL
