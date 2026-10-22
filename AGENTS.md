# AGENTS.md

Guide for coding agents working on this fork (`krismyid/pasal`, branch `selfhost-v2-tools`).
Upstream project docs still apply: read `CLAUDE.md` (root) and `apps/mcp-server/CLAUDE.md` first.
This file covers only what this fork adds: the v2 MCP tools and the self-hosted stack.

## What this fork changes

| Area | Path | Summary |
|------|------|---------|
| v2 MCP tools | `apps/mcp-server/server.py`, `apps/mcp-server/v2.py` | `search_legal`, `resolve_law`, `read_law` — same names/behaviour as live pasal.id, rebuilt on the public schema |
| v2 tests | `apps/mcp-server/test_v2.py` | Supabase mocked; pure helpers tested directly |
| Self-host stack | `deploy/selfhost/` | PostgreSQL + PostgREST + Caddy + MCP + crawler worker, no Supabase |
| Local storage switch | `scripts/worker/process.py`, `scripts/loader/load_to_supabase.py` | `PASAL_LOCAL_STORAGE=1` skips Supabase Storage uploads and page-image rendering |

Remotes: `origin` = `git@github.com:krismyid/pasal.git` (push here), `upstream` = `https://github.com/ilhamfp/pasal` (read only).
Upstream's v2 tools are not public; ours are an independent implementation.

## v2 tools

- `search_legal(query, regulation_types?, status?, year?, year_from?, year_to?, law_id?, issuing_body?, region?, limit≤20)`
  Calls `search_legal_chunks` RPC, groups by work, returns `law_id`. `year_to`, `issuing_body` (matched on `works.pemrakarsa`) and `region` (matched on title) are client-side filters. With `law_id`, runs `document_nodes.fts` search inside that law.
- `resolve_law(citation)` — law_id / slug / FRBR URI / parsed citation (`v2.parse_citation`) / search fallback. Returns `resolved: false` + `candidates` when ambiguous.
- `read_law(law_id, selector="outline")` — selectors parsed by `v2.parse_selector`: `outline`, `all`, `pasal N`, `pasal A-B`, `pasal 13-16, 40-41`, `pasal N ayat M`, `bab III`, `menimbang`, `mengingat`, `pembukaan`, `penjelasan`, `penjelasan umum`, `penjelasan pasal N`. `lampiran` / `source …` only point to `source.pdf_url` (no PDF reading on this server). Output capped at `READ_LAW_MAX_CHARS` (30k).

Rules when editing:
- Keep parsing logic in `v2.py` free of DB/FastMCP imports so it stays unit-testable.
- Pasal content already contains its ayat text; `ayat` nodes are duplicates. Don't concatenate them.
- `bab` selection walks nodes by `sort_order`, not `parent_id`, so it works whether or not the parser nested nodes.
- postgrest-py `text_search()` returns a terminal builder: call it last, after `.order()` / `.limit()`. Use `type: "web_search"` (not `websearch`).
- Every response goes through `_with_disclaimer`; include `source` (with `verified`) when returning law text.

Tests (3 failures in `test_server.py` are pre-existing upstream, not ours):

```bash
cd apps/mcp-server
python -m pytest -q test_v2.py test_server.py
```

## Self-hosted stack (`deploy/selfhost/`)

```
db       postgres:17.6         data volume pgdata; initdb/00-supabase-shim.sh runs on first start
rest     postgrest v12         connects as `authenticator`, JWT secret from .env
gateway  caddy                 :8080, maps /rest/v1/* → rest:3000 (what supabase-py expects); internal only
mcp      apps/mcp-server       published on :8000, path /mcp (no trailing slash), uses ANON_KEY
worker   scripts/worker        crawler/parser/loader, uses SERVICE_ROLE_KEY, PDFs in volume pdfs
migrate  (profile tools)       one-shot: applies packages/supabase/migrations via migrate.sh
```

Bring-up from scratch:

```bash
cd deploy/selfhost
(umask 077; python3 gen_keys.py > .env)   # never commit .env
docker compose up -d db rest gateway
docker compose run --rm migrate
docker compose up -d --build mcp worker
```

Shim (`initdb/00-supabase-shim.sh`) recreates what migrations expect from Supabase: roles `anon`, `authenticated`, `service_role` (BYPASSRLS), `authenticator`; `auth.role()`; `extensions` schema with `ltree` + `pg_trgm`; stub `storage.buckets/objects`; `selfhost.migrations` log.
**anon/authenticated are SELECT + EXECUTE only** (stricter than Supabase). If a future feature needs anon writes, grant explicitly — don't widen the defaults.

Migrations:
- `migrate.sh` applies each file once, in `LC_ALL=C` order, in a single transaction, and records it in `selfhost.migrations`.
- If an upstream migration uses Supabase-only syntax, add a same-named file in `deploy/selfhost/overrides/` instead of editing `packages/supabase/migrations/`. Existing override: `023_pdf_storage_url.sql` (Postgres has no `CREATE POLICY IF NOT EXISTS`).
- After pulling new upstream migrations: `docker compose run --rm migrate` (it also sends `NOTIFY pgrst, 'reload schema'`).

Worker:
- Types via `WORKER_TYPES` (default `uu,pp,perpres`). Full list in `scripts/worker/entrypoint.sh`.
- `WORKER_DISCOVERY_FIRST=true`: lists all types first, then processes the queue.
- Not available here: Supabase Storage, page PNGs for the web PDF viewer, `scripts/load_uud.py` upload path, the web app.
- If peraturan.go.id starts blocking the crawler (403s, challenge pages), fall back to camofox (owner's preference).

## Deployment target

- Proxmox LXC `pasal-selfhost`, `root@192.168.102.118` (Debian 13, unprivileged, nesting+keyctl, 4 cores / 8 GB / 100 GB ZFS).
- Repo lives at `/opt/pasal`; secrets only in `/opt/pasal/deploy/selfhost/.env` (mode 600).
- MCP endpoint: `http://192.168.102.118:8000/mcp` — LAN only, **no auth**. Don't expose it to the internet without adding auth or a VPN (Tailscale).
- Updating code on the server: pull the branch in `/opt/pasal`, then `docker compose up -d --build mcp` (and `worker` if scripts changed).

Useful checks:

```bash
cd /opt/pasal/deploy/selfhost
docker compose ps
docker compose logs --tail 50 worker
docker compose exec -T db psql -U postgres -d pasal -c \
  "select status, count(*) from crawl_jobs group by 1; select count(*) from works;"
```

## Secrets

- Never commit `.env` (any directory — root `.gitignore` covers `.env` and `.env.*`).
- Never print `.env` values or JWTs in logs, commits, or chat. Source the file inside remote shells instead.
- Before pushing, scan the staged diff for `eyJ…` JWTs, `PASSWORD=`, `JWT_SECRET=`, API key prefixes.

## Known issues / next steps

- `menimbang`/`mengingat` slicing (`v2.slice_preamble`) misfires on some laws where OCR puts the title after the "Menimbang" marker.
- Some `works.title_id` values have parser artifacts (e.g. missing space before "NOMOR") — upstream parser issue.
- Not implemented: `get_law_context`, `search_court_decisions`, `report_issue`, PDF `source find/pages` in `read_law`.
- Disk: 100 GB is fine for UU/PP/Perpres; expanding to Permen/Perda likely needs more.
