"""Pasal.id MCP Server — Indonesian Legal Database (v0.4).

Provides Claude with grounded access to Indonesian legislation.

v2 tools (preferred):
- search_legal: Full-text search with type/status/year filters, or within one law
- resolve_law: Resolve a citation/name/slug to a canonical law_id
- read_law: Read a law by law_id with selectors ('pasal 27-30', 'bab III', ...)

Legacy tools:
- search_laws: Full-text search across Indonesian legal provisions
- get_pasal: Get exact text of a specific article
- get_law_status: Check if a law is still in force
- list_laws: Browse available regulations
"""
import logging
import os
import re
import time
from typing import Any

from dotenv import load_dotenv
from fastmcp import FastMCP
from supabase import create_client

import v2

load_dotenv()

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("pasal.mcp")

DISCLAIMER = (
    "Informasi ini bukan nasihat hukum. Selalu verifikasi dengan sumber resmi. "
    "Database Pasal.id saat ini mencakup sebagian kecil peraturan Indonesia."
)

STATUS_EXPLANATIONS: dict[str, str] = {
    "berlaku": "This law is currently in force.",
    "diubah": "This law has been partially amended. Most provisions remain in force unless specifically changed.",
    "dicabut": "This law has been revoked and is no longer in force.",
    "tidak_berlaku": "This law is no longer effective.",
}

AMENDMENT_REL_CODES = frozenset({"mengubah", "diubah_oleh", "mencabut", "dicabut_oleh"})

mcp = FastMCP(
    "Pasal.id — Indonesian Legal Database",
    instructions=(
        "Search, read, and analyze Indonesian laws and regulations. "
        "Provides grounded legal information with exact article citations "
        "to prevent hallucination. Covers Indonesian laws including labor, "
        "marriage, criminal code, anti-corruption, corporate, consumer "
        "protection, data privacy, and more.\n\n"
        "LEGAL HIERARCHY (highest to lowest authority):\n"
        "UUD/UUDS (Constitution) → TAP_MPR (MPR Resolution) → "
        "UU/PERPPU/UUDRT (Laws) → PP (Govt Regulation) → "
        "PERPRES/KEPPRES/INPRES/PENPRES (Presidential) → "
        "PERMEN/PERMENKUMHAM/PERMENKUM (Ministerial) → "
        "PERBAN (Agency) → PERDA/PERDA_PROV/PERDA_KAB (Regional) → "
        "KEPMEN (Ministerial Decision) → SE (Circular Letter)\n\n"
        "WORKFLOW:\n"
        "- Law named or implied: resolve_law → read_law(law_id, selector).\n"
        "- Law unknown: search_legal → pick law_id → read_law.\n"
        "- read_law selectors: 'outline' (start here), 'pasal 27', 'pasal 27-30', "
        "'pasal 13-16, pasal 40-41', 'bab III', 'menimbang', 'mengingat', "
        "'penjelasan', 'all'.\n"
        "- Pass law_id from previous responses as-is; don't re-parse display strings.\n"
        "- Don't use AND/OR/NOT in queries; write plain Indonesian legal terms.\n"
        "- If source.verified is false, say the text is not yet verified against the official source.\n"
        "- get_law_status shows the amendment/revocation chain.\n"
        "- Legacy tools search_laws, get_pasal, list_laws still work.\n\n"
        "CITATION FORMAT: Always cite as 'Pasal X UU No. Y Tahun Z'\n"
        "Example: 'Pasal 81 UU No. 13 Tahun 2003 tentang Ketenagakerjaan'\n\n"
        "SEARCH TIPS:\n"
        "- Search in Bahasa Indonesia for best results (e.g., 'upah minimum' not 'minimum wage')\n"
        "- Use specific legal terms: 'pemutusan hubungan kerja' not 'fired from job'\n"
        "- The database covers a limited set of regulations — if no results, "
        "it does NOT mean the law doesn't exist"
    ),
)

# Require anon key (read-only via RLS) — never fall back to service role key
_supabase_key = os.environ.get("SUPABASE_ANON_KEY")
if not _supabase_key:
    raise RuntimeError(
        "SUPABASE_ANON_KEY is required. The MCP server must not use the service role key. "
        "Set SUPABASE_ANON_KEY in your .env file."
    )
sb = create_client(
    os.environ["SUPABASE_URL"],
    _supabase_key,
)

_reg_types: dict[str, int] = {}
_reg_types_by_id: dict[int, str] = {}


def _ensure_reg_types() -> None:
    """Populate the regulation type caches on first call."""
    global _reg_types, _reg_types_by_id
    if _reg_types:
        return
    result = sb.table("regulation_types").select("id, code").execute()
    _reg_types = {r["code"]: r["id"] for r in result.data}
    _reg_types_by_id = {r["id"]: r["code"] for r in result.data}


def _get_law_count() -> int:
    """Return cached count of laws in the database (5-min TTL)."""
    cached = _law_count_cache.get("count")
    if cached is not None:
        return cached
    try:
        result = sb.table("works").select("id", count="exact").execute()
        count = result.count or 0
    except Exception:
        count = 0
    _law_count_cache.set("count", count)
    return count


def _with_disclaimer(result: dict | list) -> dict | list:
    """Append legal disclaimer to every tool response."""
    if isinstance(result, dict):
        result["disclaimer"] = DISCLAIMER
        return result
    for item in result:
        if isinstance(item, dict):
            item["disclaimer"] = DISCLAIMER
    return result


def _no_results_message(context: str) -> str:
    """Build a 'not in DB' caveat message."""
    n = _get_law_count()
    return (
        f"No results found for {context} in our database of {n} laws. "
        "This does NOT mean no such law exists — our database covers "
        "a limited set of Indonesian regulations."
    )


# ---------------------------------------------------------------------------
# Cross-reference extraction
# ---------------------------------------------------------------------------

CROSS_REF_PATTERN = re.compile(
    r'(?:sebagaimana\s+dimaksud\s+(?:dalam|pada)\s+)?'
    r'Pasal\s+(\d+[A-Z]?)'
    r'(?:\s+ayat\s+\((\d+)\))?'
    r'(?:\s+(?:huruf\s+([a-z])\.?))?'
    r'(?:\s+(?:Undang-Undang|UU)\s+(?:Nomor\s+)?(\d+)\s+Tahun\s+(\d{4}))?',
    re.IGNORECASE,
)


def extract_cross_references(text: str) -> list[dict]:
    """Extract cross-references to other articles from legal text."""
    refs, seen = [], set()
    for m in CROSS_REF_PATTERN.finditer(text):
        key = (m.group(1), m.group(2), m.group(4), m.group(5))
        if key in seen:
            continue
        seen.add(key)
        ref: dict[str, str | int] = {"pasal": m.group(1)}
        if m.group(2):
            ref["ayat"] = m.group(2)
        if m.group(3):
            ref["huruf"] = m.group(3)
        if m.group(4) and m.group(5):
            ref["law_number"] = m.group(4)
            ref["law_year"] = int(m.group(5))
        refs.append(ref)
    return refs


# ---------------------------------------------------------------------------
# TTL Cache
# ---------------------------------------------------------------------------

class TTLCache:
    """Simple in-memory cache with per-key TTL expiration and max size."""

    def __init__(self, ttl_seconds: int = 3600, maxsize: int = 1000):
        self._ttl = ttl_seconds
        self._maxsize = maxsize
        self._data: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._data.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.time() - ts > self._ttl:
            del self._data[key]
            return None
        return value

    def set(self, key: str, value: Any) -> None:
        # Evict expired entries when at capacity
        if len(self._data) >= self._maxsize:
            now = time.time()
            expired = [k for k, (ts, _) in self._data.items() if now - ts > self._ttl]
            for k in expired:
                del self._data[k]
        # If still at capacity, evict oldest entry
        if len(self._data) >= self._maxsize:
            oldest_key = min(self._data, key=lambda k: self._data[k][0])
            del self._data[oldest_key]
        self._data[key] = (time.time(), value)

    def clear(self) -> None:
        self._data.clear()


_pasal_cache = TTLCache(ttl_seconds=3600, maxsize=2000)
_status_cache = TTLCache(ttl_seconds=3600, maxsize=2000)
_law_count_cache = TTLCache(ttl_seconds=300, maxsize=10)


# ---------------------------------------------------------------------------
# Rate Limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple sliding window rate limiter per tool."""

    def __init__(self, max_calls: int, window_seconds: int = 60):
        self._max = max_calls
        self._window = window_seconds
        self._calls: list[float] = []

    def check(self) -> int | None:
        """Return None if allowed, or seconds to wait if rate-limited."""
        now = time.time()
        cutoff = now - self._window
        self._calls = [t for t in self._calls if t > cutoff]
        if len(self._calls) >= self._max:
            oldest = self._calls[0]
            return int(oldest + self._window - now) + 1
        self._calls.append(now)
        return None

    def reset(self) -> None:
        self._calls.clear()


_rate_limiters = {
    "search_laws": RateLimiter(30),
    "get_pasal": RateLimiter(60),
    "get_law_status": RateLimiter(60),
    "list_laws": RateLimiter(30),
}


def _check_rate_limit(tool_name: str) -> dict | None:
    """Return rate limit error dict if exceeded, else None."""
    limiter = _rate_limiters.get(tool_name)
    if not limiter:
        return None
    wait = limiter.check()
    if wait is not None:
        return _with_disclaimer({
            "error": "Rate limit exceeded",
            "retry_after_seconds": wait,
        })
    return None


# ---------------------------------------------------------------------------
# Shared database helpers
# ---------------------------------------------------------------------------

def _find_work(law_type: str, law_number: str, year: int) -> dict | None:
    """Look up a work by regulation type code, number, and year.

    Returns the work row dict, or None if not found.
    Populates the regulation type caches as a side effect.
    """
    _ensure_reg_types()
    reg_type_id = _reg_types.get(law_type.upper())
    if not reg_type_id:
        return None
    result = sb.table("works").select("*").match({
        "regulation_type_id": reg_type_id,
        "number": law_number,
        "year": year,
    }).execute()
    if not result.data:
        return None
    return result.data[0]


def _get_chapter_info(node: dict) -> str:
    """Retrieve the parent chapter (BAB) heading for a document node."""
    if not node.get("parent_id"):
        return ""
    parent = sb.table("document_nodes").select(
        "node_type, number, heading"
    ).eq("id", node["parent_id"]).execute()
    if not parent.data:
        return ""
    p = parent.data[0]
    info = f"{p['node_type'].upper()} {p['number']}"
    if p.get("heading"):
        info += f" - {p['heading']}"
    return info


def _get_available_pasals(work_id: int) -> list[str]:
    """Get list of available pasal numbers for a work."""
    result = sb.table("document_nodes").select("number").match({
        "work_id": work_id,
        "node_type": "pasal",
    }).order("sort_order").limit(200).execute()
    return [r["number"] for r in (result.data or [])]


# ---------------------------------------------------------------------------
# MCP Tool Endpoints
# ---------------------------------------------------------------------------

@mcp.tool
def search_laws(
    query: str,
    regulation_type: str | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    language: str = "id",
    limit: int = 10,
) -> list[dict]:
    """Search Indonesian laws and regulations by keyword.

    USE WHEN: User asks about a legal topic, right, obligation, or regulation.
    This should be your FIRST tool call for any legal question.
    DO NEXT: Use get_pasal to retrieve the full text of relevant articles for citation.

    Uses PostgreSQL full-text search with Indonesian stemming.
    Returns relevant legal provisions with exact citations.
    IMPORTANT: Search in Indonesian for best results (e.g., "upah minimum" not "minimum wage").

    Args:
        query: Search query in Indonesian (e.g., "upah minimum pekerja", "korupsi", "perkawinan")
        regulation_type: Filter by type code — UU, PP, PERPRES, PERMEN, PERPPU, KEPPRES, INPRES, PENPRES, PERBAN, PERMENKUMHAM, PERMENKUM, PERDA, PERDA_PROV, PERDA_KAB, KEPMEN, SE, TAP_MPR, PERMA, PBI, UUDRT, UUDS
        year_from: Only return laws enacted after this year
        year_to: Only return laws enacted before this year
        language: Language filter — "id" (Indonesian, default) or "en" (English translations)
        limit: Maximum number of results (default 10)
    """
    rate_err = _check_rate_limit("search_laws")
    if rate_err:
        return [rate_err]

    t0 = time.time()
    logger.info("search_laws called: query=%r type=%s year_from=%s year_to=%s limit=%s",
                query, regulation_type, year_from, year_to, limit)

    if not query or not query.strip():
        return _with_disclaimer(
            [{"error": "Query cannot be empty", "suggestion": "Provide a search term in Indonesian"}]
        )

    limit = min(limit, 50)

    metadata_filter: dict = {}
    if regulation_type:
        metadata_filter["type"] = regulation_type.upper()
    if language != "id":
        metadata_filter["language"] = language

    try:
        result = sb.rpc("search_legal_chunks", {
            "query_text": query.strip(),
            "match_count": limit * 3,  # fetch extra to filter
            "metadata_filter": metadata_filter,
        }).execute()
    except Exception as e:
        logger.error("search_laws RPC failed: %s", e)
        return _with_disclaimer([{"error": "Search failed. Please try again later."}])

    if not result.data:
        logger.info("search_laws: no results for %r (%.0fms)", query, (time.time() - t0) * 1000)
        return _with_disclaimer([{
            "message": _no_results_message(f"'{query}'"),
            "suggestion": "Try simpler keywords or remove filters",
        }])

    try:
        work_ids = list(set(r["work_id"] for r in result.data))
        works_result = sb.table("works").select(
            "id, frbr_uri, title_id, number, year, status, regulation_type_id"
        ).in_("id", work_ids).execute()
        works_map = {w["id"]: w for w in works_result.data}
    except Exception as e:
        logger.error("search_laws metadata fetch failed: %s", e)
        return _with_disclaimer([{"error": "Failed to fetch law metadata. Please try again later."}])

    _ensure_reg_types()

    enriched = []
    for r in result.data:
        work = works_map.get(r["work_id"])
        if not work:
            continue

        # Apply year filter
        if year_from and work["year"] < year_from:
            continue
        if year_to and work["year"] > year_to:
            continue

        reg_code = _reg_types_by_id.get(work["regulation_type_id"], "")
        meta = r.get("metadata", {})

        enriched.append({
            "law_title": work["title_id"],
            "frbr_uri": work["frbr_uri"],
            "regulation_type": reg_code,
            "year": work["year"],
            "pasal": f"Pasal {meta.get('pasal', '?')}",
            "snippet": r.get("snippet", r["content"][:300]),
            "status": work["status"],
            "relevance_score": round(r["score"], 4),
        })

        if len(enriched) >= limit:
            break

    logger.info("search_laws: %d results for %r (%.0fms)",
                len(enriched), query, (time.time() - t0) * 1000)
    return _with_disclaimer(enriched)


@mcp.tool
def get_pasal(
    law_type: str,
    law_number: str,
    year: int,
    pasal_number: str,
) -> dict:
    """Get the exact text of a specific article (Pasal) from an Indonesian regulation.

    USE WHEN: You know which specific article to cite (from search_laws results).
    DO NEXT: Use get_law_status to verify the law is still in force before presenting to user.

    Args:
        law_type: Regulation type code, e.g., "UU", "PP", "PERPRES"
        law_number: The number of the law, e.g., "13"
        year: Year the law was enacted, e.g., 2003
        pasal_number: Article number, e.g., "81" or "81A"
    """
    rate_err = _check_rate_limit("get_pasal")
    if rate_err:
        return rate_err

    cache_key = f"{law_type.upper()}:{law_number}:{year}:{pasal_number}"
    cached = _pasal_cache.get(cache_key)
    if cached is not None:
        logger.info("get_pasal cache hit: %s", cache_key)
        return cached

    t0 = time.time()
    logger.info("get_pasal called: %s %s/%d pasal %s", law_type, law_number, year, pasal_number)

    try:
        work = _find_work(law_type, law_number, year)
        if not work:
            # Distinguish "unknown type" from "work not found"
            _ensure_reg_types()
            if not _reg_types.get(law_type.upper()):
                return _with_disclaimer({"error": f"Unknown regulation type: {law_type}"})
            return _with_disclaimer({
                "error": _no_results_message(f"'{law_type} {law_number}/{year}'"),
                "suggestion": "Use list_laws to check available regulations, or verify type/number/year.",
            })

        node_result = sb.table("document_nodes").select("*").match({
            "work_id": work["id"],
            "node_type": "pasal",
            "number": pasal_number,
        }).execute()

        if not node_result.data:
            return _with_disclaimer({
                "error": f"Pasal {pasal_number} not found in {law_type} {law_number}/{year}",
                "suggestion": "Check available_pasals below, or use search_laws to find the right article.",
                "available_pasals": _get_available_pasals(work["id"]),
            })

        node = node_result.data[0]

        ayat_result = sb.table("document_nodes").select("number, content_text").match({
            "work_id": work["id"],
            "parent_id": node["id"],
            "node_type": "ayat",
        }).order("sort_order").execute()

        chapter_info = _get_chapter_info(node)

        content = node["content_text"] or ""
        cross_refs = extract_cross_references(content)
        ayat_data = ayat_result.data or []
        if len(content) > 3000:
            content = (
                content[:3000]
                + f"\n\n[...truncated. Full: {len(node['content_text'])} chars. "
                f"This article has {len(ayat_data)} ayat.]"
            )

        logger.info("get_pasal: found pasal %s (%.0fms)", pasal_number, (time.time() - t0) * 1000)
        result = _with_disclaimer({
            "law_title": work["title_id"],
            "frbr_uri": work["frbr_uri"],
            "pasal_number": pasal_number,
            "chapter": chapter_info,
            "content_id": content,
            "ayat": [{"number": a["number"], "text": a["content_text"]} for a in ayat_data],
            "cross_references": cross_refs,
            "status": work["status"],
            "source_url": work.get("source_url", ""),
        })
        _pasal_cache.set(cache_key, result)
        return result
    except Exception as e:
        logger.error("get_pasal failed: %s", e)
        return _with_disclaimer({"error": "Failed to retrieve pasal. Please try again later."})


@mcp.tool
def get_law_status(
    law_type: str,
    law_number: str,
    year: int,
) -> dict:
    """Check whether an Indonesian regulation is still in force, has been amended, or was revoked.

    USE WHEN: You need to verify a law's validity before citing it to the user.
    ALWAYS check status before presenting legal information — a revoked law is misleading.
    Returns the full amendment/revocation chain.

    Args:
        law_type: Regulation type code, e.g., "UU"
        law_number: The number of the law, e.g., "1"
        year: Year the law was enacted, e.g., 1974
    """
    rate_err = _check_rate_limit("get_law_status")
    if rate_err:
        return rate_err

    cache_key = f"{law_type.upper()}:{law_number}:{year}"
    cached = _status_cache.get(cache_key)
    if cached is not None:
        logger.info("get_law_status cache hit: %s", cache_key)
        return cached

    t0 = time.time()
    logger.info("get_law_status called: %s %s/%d", law_type, law_number, year)

    try:
        work = _find_work(law_type, law_number, year)
        if not work:
            _ensure_reg_types()
            if not _reg_types.get(law_type.upper()):
                return _with_disclaimer({"error": f"Unknown regulation type: {law_type}"})
            return _with_disclaimer({
                "error": _no_results_message(f"'{law_type} {law_number}/{year}'"),
            })

        rels = sb.table("work_relationships").select(
            "*, relationship_types(code, name_id, name_en)"
        ).or_(
            f"source_work_id.eq.{work['id']},target_work_id.eq.{work['id']}"
        ).execute()

        rel_rows = rels.data or []
        related_work_ids = {
            wid
            for r in rel_rows
            for wid in (r["source_work_id"], r["target_work_id"])
        } - {work["id"]}

        related_works: dict[int, dict] = {}
        if related_work_ids:
            rw = sb.table("works").select(
                "id, frbr_uri, title_id, number, year, status, regulation_type_id"
            ).in_("id", list(related_work_ids)).execute()
            related_works = {w["id"]: w for w in rw.data}

        amendments = []
        related = []
        for r in rel_rows:
            rel_type = r.get("relationship_types", {})
            other_id = r["target_work_id"] if r["source_work_id"] == work["id"] else r["source_work_id"]
            other_work = related_works.get(other_id)
            if not other_work:
                continue

            other_code = _reg_types_by_id.get(other_work["regulation_type_id"], "")
            entry = {
                "relationship": rel_type.get("name_en", ""),
                "relationship_id": rel_type.get("name_id", ""),
                "law": f"{other_code} {other_work['number']}/{other_work['year']}",
                "full_title": other_work["title_id"],
                "frbr_uri": other_work["frbr_uri"],
            }

            if rel_type.get("code") in AMENDMENT_REL_CODES:
                amendments.append(entry)
            else:
                related.append(entry)

        logger.info("get_law_status: %s %s/%d status=%s (%.0fms)",
                     law_type, law_number, year, work["status"], (time.time() - t0) * 1000)
        result = _with_disclaimer({
            "law_title": work["title_id"],
            "frbr_uri": work["frbr_uri"],
            "status": work["status"],
            "status_explanation": STATUS_EXPLANATIONS.get(work["status"], ""),
            "date_enacted": str(work["date_enacted"]) if work.get("date_enacted") else None,
            "amendments": amendments,
            "related_laws": related,
        })
        _status_cache.set(cache_key, result)
        return result
    except Exception as e:
        logger.error("get_law_status failed: %s", e)
        return _with_disclaimer({"error": "Failed to retrieve law status. Please try again later."})


@mcp.tool
def list_laws(
    regulation_type: str | None = None,
    year: int | None = None,
    status: str | None = None,
    search: str | None = None,
    page: int = 1,
    per_page: int = 20,
) -> dict:
    """Browse available Indonesian regulations with optional filters.

    USE WHEN: User wants to browse or list regulations, or when search_laws returned no results.
    PREFER search_laws for specific legal questions — this tool is for discovery/browsing.

    Args:
        regulation_type: Filter by type — UU, PP, PERPRES, PERMEN, PERPPU, KEPPRES, INPRES, PENPRES, PERBAN, PERMENKUMHAM, PERMENKUM, PERDA, KEPMEN, SE, TAP_MPR, PERMA, PBI, UUDRT, UUDS, etc.
        year: Filter by year enacted
        status: Filter by status — "berlaku" (in force), "dicabut" (revoked), "diubah" (amended)
        search: Keyword filter on law title
        page: Page number (default 1)
        per_page: Results per page (default 20)
    """
    rate_err = _check_rate_limit("list_laws")
    if rate_err:
        return rate_err

    t0 = time.time()
    logger.info("list_laws called: type=%s year=%s status=%s search=%s page=%d",
                regulation_type, year, status, search, page)

    try:
        _ensure_reg_types()

        # Clamp pagination params to safe ranges
        page = max(1, page)
        per_page = max(1, min(100, per_page))

        query = sb.table("works").select("*, regulation_types(code, name_id)", count="exact")

        if regulation_type:
            reg_type_id = _reg_types.get(regulation_type.upper())
            if reg_type_id:
                query = query.eq("regulation_type_id", reg_type_id)

        if year:
            query = query.eq("year", year)

        if status:
            query = query.eq("status", status)

        if search:
            safe_search = search.replace("%", r"\%").replace("_", r"\_")
            query = query.ilike("title_id", f"%{safe_search}%")

        offset = (page - 1) * per_page
        result = query.order("year", desc=True).range(offset, offset + per_page - 1).execute()

        total = result.count or 0
        laws = [
            {
                "frbr_uri": w["frbr_uri"],
                "title": w["title_id"],
                "regulation_type": w.get("regulation_types", {}).get("code", ""),
                "number": w["number"],
                "year": w["year"],
                "status": w["status"],
            }
            for w in (result.data or [])
        ]

        logger.info("list_laws: %d/%d results (%.0fms)", len(laws), total, (time.time() - t0) * 1000)
        return _with_disclaimer({
            "total": total,
            "page": page,
            "per_page": per_page,
            "laws": laws,
        })
    except Exception as e:
        logger.error("list_laws failed: %s", e)
        return _with_disclaimer({"error": "Failed to list laws. Please try again later."})


# ---------------------------------------------------------------------------
# v2 tools: search_legal, resolve_law, read_law
# ---------------------------------------------------------------------------

SITE_URL = os.getenv("PASAL_SITE_URL", "https://pasal.id").rstrip("/")
READ_LAW_MAX_CHARS = 30_000
_NODE_PAGE = 1000

_WORK_FIELDS = (
    "id, frbr_uri, slug, number, year, title_id, status, regulation_type_id, "
    "source_url, source_pdf_url, content_verified, updated_at, pemrakarsa"
)

_nodes_cache = TTLCache(ttl_seconds=3600, maxsize=50)
_rate_limiters.update({
    "search_legal": RateLimiter(30),
    "resolve_law": RateLimiter(60),
    "read_law": RateLimiter(60),
})


def _work_url(work: dict) -> str:
    code = _reg_types_by_id.get(work["regulation_type_id"], "").lower()
    slug = work.get("slug") or f"{code}-{work.get('number')}-{work['year']}"
    return f"{SITE_URL}/peraturan/{code}/{slug}"


def _source_block(work: dict) -> dict:
    return {
        "law_url": _work_url(work),
        "official_url": work.get("source_url") or None,
        "pdf_url": work.get("source_pdf_url") or None,
        "updated_at": str(work["updated_at"]) if work.get("updated_at") else None,
        "verified": bool(work.get("content_verified")),
    }


def _law_summary(work: dict) -> dict:
    code = _reg_types_by_id.get(work["regulation_type_id"], "")
    return {
        "law_id": work["id"],
        "citation": v2.format_citation(code, work.get("number"), work["year"], work.get("title_id")),
        "title": work["title_id"],
        "regulation_type": code,
        "number": work.get("number"),
        "year": work["year"],
        "status": work["status"],
        "frbr_uri": work["frbr_uri"],
    }


def _get_work_by_id(law_id: int) -> dict | None:
    result = sb.table("works").select(_WORK_FIELDS).eq("id", law_id).execute()
    return result.data[0] if result.data else None


def _get_nodes(work_id: int) -> list[dict]:
    """All document nodes of a work in reading order (cached, paginated)."""
    cached = _nodes_cache.get(str(work_id))
    if cached is not None:
        return cached
    nodes: list[dict] = []
    offset = 0
    while True:
        page = sb.table("document_nodes").select(
            "id, node_type, number, heading, content_text, parent_id, sort_order, pdf_page_start"
        ).eq("work_id", work_id).order("sort_order").range(offset, offset + _NODE_PAGE - 1).execute()
        rows = page.data or []
        nodes.extend(rows)
        if len(rows) < _NODE_PAGE:
            break
        offset += _NODE_PAGE
    _nodes_cache.set(str(work_id), nodes)
    return nodes


def _node_label(node: dict) -> str:
    t = node["node_type"]
    num = node.get("number") or ""
    if t == "penjelasan_umum":
        return "Penjelasan Umum"
    if t == "penjelasan_pasal":
        return f"Penjelasan Pasal {num}".strip()
    if t == "preamble":
        return "Pembukaan"
    if t == "content":
        return "Isi"
    return f"{t.capitalize()} {num}".strip() if t != "bab" else f"BAB {num}"


def _render_node(node: dict) -> dict:
    out = {"node_id": node["id"], "label": _node_label(node)}
    if node.get("heading"):
        out["heading"] = node["heading"]
    if node.get("content_text"):
        out["text"] = node["content_text"]
    if node.get("pdf_page_start"):
        out["pdf_page_hint"] = node["pdf_page_start"]
    return out


def _outline(nodes: list[dict]) -> dict:
    """Chapter list with the pasal range of each, plus available selectors."""
    chapters: list[dict] = []
    current: dict | None = None
    loose_pasal: list[str] = []
    has = {t: False for t in ("preamble", "penjelasan_umum", "penjelasan_pasal")}
    for n in nodes:
        t = n["node_type"]
        if t in has:
            has[t] = True
        if t == "bab":
            current = {"selector": f"bab {n.get('number')}", "heading": n.get("heading"), "pasal": []}
            chapters.append(current)
        elif t == "pasal":
            (current["pasal"] if current else loose_pasal).append(n.get("number"))
    for c in chapters:
        p = c.pop("pasal")
        c["pasal_range"] = f"Pasal {p[0]}–{p[-1]}" if p else None
        c["pasal_count"] = len(p)
    selectors = ["pasal <n>", "pasal <a>-<b>", "bab <romawi>", "all"]
    if has["preamble"]:
        selectors[:0] = ["menimbang", "mengingat"]
    if has["penjelasan_umum"] or has["penjelasan_pasal"]:
        selectors += ["penjelasan", "penjelasan pasal <n>"]
    out: dict = {
        "pasal_total": sum(1 for n in nodes if n["node_type"] == "pasal"),
        "chapters": chapters,
        "selectors": selectors,
    }
    if loose_pasal and not chapters:
        out["pasal_range"] = f"Pasal {loose_pasal[0]}–{loose_pasal[-1]}"
    return out


def _select_nodes(nodes: list[dict], sel: "v2.Selector") -> tuple[list[dict], str | None]:
    """Return (nodes, note) for one selector part."""
    if sel.kind == "all":
        return [n for n in nodes if n["node_type"] != "ayat"], None

    if sel.kind in {"menimbang", "mengingat", "preamble"}:
        pre = [n for n in nodes if n["node_type"] == "preamble"]
        if not pre:
            return [], "Bagian pembukaan tidak tersedia pada hasil parsing."
        if sel.kind == "preamble":
            return pre, None
        text = "\n".join(n.get("content_text") or "" for n in pre)
        part = v2.slice_preamble(text, sel.kind)
        if not part:
            return pre, f"Penanda '{sel.kind.capitalize()}' tidak ditemukan; mengembalikan seluruh pembukaan."
        return [{**pre[0], "content_text": part, "node_type": "preamble"}], None

    if sel.kind == "bab":
        out: list[dict] = []
        inside = False
        for n in nodes:
            if n["node_type"] == "bab":
                if inside:
                    break
                inside = v2.chapter_number(n.get("number")) == sel.bab
            elif n["node_type"] in {"penjelasan_umum", "penjelasan_pasal"}:
                if inside:
                    break
            if inside and n["node_type"] != "ayat":
                out.append(n)
        return out, None if out else f"BAB {sel.raw.split()[-1].upper()} tidak ditemukan."

    if sel.kind in {"penjelasan", "penjelasan_umum"}:
        types = {"penjelasan_umum"} if sel.kind == "penjelasan_umum" else {"penjelasan_umum", "penjelasan_pasal"}
        out = [n for n in nodes if n["node_type"] in types]
        return out, None if out else "Penjelasan tidak tersedia pada hasil parsing."

    if sel.kind in {"pasal", "penjelasan_pasal"}:
        out = [n for n in nodes if n["node_type"] == sel.kind and v2.in_range(n.get("number"), sel)]
        if sel.ayat and out:
            ayat = [
                n for n in nodes
                if n["node_type"] == "ayat" and n.get("parent_id") == out[0]["id"] and n.get("number") == sel.ayat
            ]
            if ayat:
                return [{**ayat[0], "number": f"{out[0].get('number')} ayat ({sel.ayat})", "node_type": "pasal"}], None
            return out, f"Ayat ({sel.ayat}) tidak ditemukan sebagai node terpisah; mengembalikan seluruh Pasal."
        if not out:
            return [], f"{sel.raw} tidak ditemukan."
        return out, None

    return [], None


@mcp.tool
def search_legal(
    query: str,
    regulation_types: list[str] | None = None,
    status: list[str] | None = None,
    year: int | None = None,
    year_from: int | None = None,
    year_to: int | None = None,
    law_id: int | None = None,
    issuing_body: str | None = None,
    region: str | None = None,
    limit: int = 10,
) -> dict:
    """WHEN the relevant law is unknown, search Indonesian legal text with filters.

    If a law is already known, prefer resolve_law then read_law.
    DO NEXT: pass a returned law_id to read_law (e.g. selector 'pasal 81').

    Args:
        query: Indonesian legal search terms, e.g. 'upah minimum', 'pemutusan hubungan kerja'. No AND/OR/NOT.
        regulation_types: Type codes, e.g. ['UU', 'PP', 'PERPRES'].
        status: Any of berlaku, diubah, dicabut, tidak_berlaku.
        year: Exact enactment year (takes precedence over year_from/year_to).
        year_from: Inclusive start year.
        year_to: Inclusive end year.
        law_id: Search only inside this law. Other filters are ignored.
        issuing_body: Filter on initiator (pemrakarsa), e.g. 'Kementerian Ketenagakerjaan'.
        region: Province/city/regency for regional regulations, e.g. 'Kota Bekasi' (matched on title).
        limit: Maximum results, 1-20.
    """
    rate_err = _check_rate_limit("search_legal")
    if rate_err:
        return rate_err

    t0 = time.time()
    if not query or not query.strip():
        return _with_disclaimer({"error_code": "empty_query", "message": "Query cannot be empty."})
    limit = max(1, min(20, limit))
    logger.info("search_legal called: query=%r types=%s status=%s law_id=%s", query, regulation_types, status, law_id)

    try:
        _ensure_reg_types()
        if law_id is not None:
            return _search_within_law(query.strip(), law_id, limit)

        metadata_filter: dict = {}
        if regulation_types:
            metadata_filter["type"] = ",".join(t.upper().strip() for t in regulation_types)
        if status:
            metadata_filter["status"] = ",".join(s.lower().strip() for s in status)
        if year:
            metadata_filter["year"] = year
        elif year_from:
            metadata_filter["year_from"] = year_from

        result = sb.rpc("search_legal_chunks", {
            "query_text": query.strip(),
            "match_count": limit * 4,
            "metadata_filter": metadata_filter,
        }).execute()
        rows = result.data or []

        works_map: dict[int, dict] = {}
        if rows:
            ids = list({r["work_id"] for r in rows})
            works_map = {w["id"]: w for w in sb.table("works").select(_WORK_FIELDS).in_("id", ids).execute().data}

        best: dict[int, dict] = {}
        for r in rows:
            work = works_map.get(r["work_id"])
            if not work:
                continue
            if not year and year_to and work["year"] > year_to:
                continue
            if issuing_body and issuing_body.lower() not in (work.get("pemrakarsa") or "").lower():
                continue
            if region and region.lower() not in work["title_id"].lower():
                continue
            prev = best.get(work["id"])
            if prev and prev["score"] >= r["score"]:
                continue
            meta = r.get("metadata") or {}
            entry = _law_summary(work)
            entry.update({
                "pasal": f"Pasal {meta['pasal']}" if meta.get("pasal") else None,
                "snippet": r.get("snippet") or (r.get("content") or "")[:300],
                "score": round(r["score"], 4),
                "url": _work_url(work),
            })
            best[work["id"]] = entry

        results = sorted(best.values(), key=lambda e: e["score"], reverse=True)[:limit]
        logger.info("search_legal: %d results for %r (%.0fms)", len(results), query, (time.time() - t0) * 1000)
        if not results:
            return _with_disclaimer({
                "results": [],
                "message": _no_results_message(f"'{query}'"),
                "suggestion": "Try synonyms, simpler terms, or remove filters.",
            })
        return _with_disclaimer({"results": results, "next": "read_law(law_id, selector)"})
    except Exception as e:
        logger.error("search_legal failed: %s", e)
        return _with_disclaimer({
            "error_code": "search_failed",
            "message": "Search failed unexpectedly.",
            "recovery": {"suggestion": "Retry with simpler terms or remove filters."},
        })


def _search_within_law(query: str, law_id: int, limit: int) -> dict:
    work = _get_work_by_id(law_id)
    if not work:
        return _with_disclaimer({"error_code": "law_not_found", "message": f"law_id {law_id} not found."})
    rows = sb.table("document_nodes").select(
        "id, node_type, number, content_text"
    ).eq("work_id", law_id).in_(
        "node_type", ["pasal", "preamble", "content", "penjelasan_umum", "penjelasan_pasal"]
    ).order("sort_order").limit(limit).text_search(
        # text_search returns a terminal builder, so it must come last.
        "fts", query, options={"config": "indonesian", "type": "web_search"}
    ).execute().data or []
    hits = [{
        "node_id": r["id"],
        "label": _node_label(r),
        "selector": f"pasal {r['number']}" if r["node_type"] == "pasal" else None,
        "snippet": (r.get("content_text") or "")[:300],
    } for r in rows]
    return _with_disclaimer({"law": _law_summary(work), "results": hits, "source": _source_block(work)})


@mcp.tool
def resolve_law(citation: str) -> dict:
    """Resolve a regulation citation or name to a canonical law_id.

    USE WHEN: the user names or implies a regulation, e.g. 'UU 13/2003',
    'UU No. 13 Tahun 2003', 'PP 35 2021', 'UU Ketenagakerjaan', 'uu-13-2003',
    '/akn/id/act/uu/2003/13', or a numeric law_id.
    DO NEXT: read_law(law_id, selector) — start with selector 'outline' if unsure.

    Args:
        citation: Citation, slug, FRBR URI, short name, or law_id.
    """
    rate_err = _check_rate_limit("resolve_law")
    if rate_err:
        return rate_err
    if not citation or not citation.strip():
        return _with_disclaimer({"error_code": "empty_citation", "message": "Citation cannot be empty."})

    t0 = time.time()
    text = citation.strip()
    logger.info("resolve_law called: %r", text)
    try:
        _ensure_reg_types()

        if text.isdigit() and len(text) < 8 and not (len(text) == 4 and text.startswith(("19", "20"))):
            work = _get_work_by_id(int(text))
            if work:
                return _resolved(work, "law_id")

        if re.fullmatch(r"[a-z0-9_]+(?:-[a-z0-9_]+)+", text.lower()):
            res = sb.table("works").select(_WORK_FIELDS).eq("slug", text.lower()).execute()
            if res.data:
                return _resolved(res.data[0], "slug")

        if text.lower().lstrip("/").startswith("akn/"):
            uri = "/" + text.lstrip("/")
            res = sb.table("works").select(_WORK_FIELDS).eq("frbr_uri", uri).execute()
            if res.data:
                return _resolved(res.data[0], "frbr_uri")

        cit = v2.parse_citation(text)
        code = cit.type_code if cit.type_code in _reg_types else None

        if code and cit.number and cit.year:
            res = sb.table("works").select(_WORK_FIELDS).match({
                "regulation_type_id": _reg_types[code], "number": cit.number, "year": cit.year,
            }).execute()
            if res.data:
                return _resolved(res.data[0], "citation")

        # Fallback: rank candidates with the search function (identity + title FTS).
        metadata_filter = {"type": code} if code else {}
        if cit.year and not cit.number:
            metadata_filter["year"] = cit.year
        rows = sb.rpc("search_legal_chunks", {
            "query_text": text, "match_count": 20, "metadata_filter": metadata_filter,
        }).execute().data or []
        ids: list[int] = []
        for r in sorted(rows, key=lambda r: r["score"], reverse=True):
            if r["work_id"] not in ids:
                ids.append(r["work_id"])
        candidates: list[dict] = []
        if ids:
            wm = {w["id"]: w for w in sb.table("works").select(_WORK_FIELDS).in_("id", ids[:5]).execute().data}
            candidates = [_law_summary(wm[i]) for i in ids[:5] if i in wm]

        logger.info("resolve_law: %d candidates for %r (%.0fms)", len(candidates), text, (time.time() - t0) * 1000)
        if len(candidates) == 1:
            return _resolved(wm[candidates[0]["law_id"]], "search")
        if not candidates:
            return _with_disclaimer({
                "resolved": False,
                "message": _no_results_message(f"'{text}'"),
                "suggestion": "Check type/number/year, or use search_legal with topic terms.",
            })
        return _with_disclaimer({
            "resolved": False,
            "message": "Ambiguous citation; pick a law_id from candidates.",
            "candidates": candidates,
        })
    except Exception as e:
        logger.error("resolve_law failed: %s", e)
        return _with_disclaimer({"error_code": "resolve_failed", "message": "Failed to resolve citation."})


def _resolved(work: dict, matched_by: str) -> dict:
    out = {"resolved": True, "matched_by": matched_by, **_law_summary(work)}
    out["status_explanation"] = STATUS_EXPLANATIONS.get(work["status"], "")
    out["source"] = _source_block(work)
    out["next"] = f"read_law(law_id={work['id']}, selector='outline' | 'pasal 1' | 'bab I')"
    return _with_disclaimer(out)


@mcp.tool
def read_law(law_id: int, selector: str = "outline") -> dict:
    """Read part of a regulation by law_id using a simple selector.

    USE WHEN: you have a law_id (from resolve_law or search_legal).
    Selectors: 'outline' (default: chapters + pasal ranges), 'pasal 27',
    'pasal 27-30', 'pasal 13-16, pasal 40-41', 'pasal 81 ayat 2', 'bab III',
    'menimbang', 'mengingat', 'penjelasan', 'penjelasan pasal 5', 'all'.
    Don't read 'all' for long laws — start from 'outline'.

    Args:
        law_id: Canonical law_id.
        selector: What to read (see above). Comma-separate multiple parts.
    """
    rate_err = _check_rate_limit("read_law")
    if rate_err:
        return rate_err

    t0 = time.time()
    logger.info("read_law called: law_id=%s selector=%r", law_id, selector)
    try:
        parts = v2.parse_selector(selector)
    except ValueError as e:
        return _with_disclaimer({
            "error_code": "invalid_selector",
            "message": str(e),
            "examples": ["outline", "pasal 27", "pasal 27-30", "bab III", "menimbang", "penjelasan", "all"],
        })

    try:
        _ensure_reg_types()
        work = _get_work_by_id(law_id)
        if not work:
            return _with_disclaimer({
                "error_code": "law_not_found",
                "message": f"law_id {law_id} not found. Use resolve_law or search_legal first.",
            })
        nodes = _get_nodes(law_id)
        out: dict = {"law": _law_summary(work), "selector": selector}

        if not nodes:
            out["message"] = "No parsed text for this law yet."
            out["source"] = _source_block(work)
            return _with_disclaimer(out)

        notes: list[str] = []
        sections: list[dict] = []
        seen: set[tuple] = set()
        for sel in parts:
            if sel.kind == "outline":
                out["outline"] = _outline(nodes)
                continue
            if sel.kind in {"lampiran", "source"}:
                notes.append(
                    f"'{sel.raw}' is not available from parsed text on this server; "
                    "open pdf_url in source to read it."
                )
                continue
            picked, note = _select_nodes(nodes, sel)
            if note:
                notes.append(note)
            for n in picked:
                # Menimbang and mengingat are slices of the same preamble node.
                key = (n["id"], sel.kind if n["node_type"] == "preamble" else "")
                if key in seen:
                    continue
                seen.add(key)
                sections.append(_render_node(n))

        total = 0
        for i, s in enumerate(sections):
            total += len(s.get("text", "")) + len(s.get("heading", ""))
            if total > READ_LAW_MAX_CHARS:
                remaining = sections[i:]
                sections = sections[:i]
                notes.append(
                    f"Truncated at {READ_LAW_MAX_CHARS} chars; {len(remaining)} more node(s) "
                    f"starting at {remaining[0]['label']}. Use a narrower selector."
                )
                break

        if sections:
            out["content"] = sections
            refs: list[dict] = []
            for s in sections:
                for r in extract_cross_references(s.get("text", "")):
                    if r not in refs:
                        refs.append(r)
            if refs:
                out["cross_references"] = refs[:50]
        if notes:
            out["notes"] = notes
        if not sections and "outline" not in out:
            out["outline"] = _outline(nodes)
        out["status"] = work["status"]
        out["status_explanation"] = STATUS_EXPLANATIONS.get(work["status"], "")
        out["source"] = _source_block(work)
        if not out["source"]["verified"]:
            out["verification_note"] = "Text is parsed from PDF and not yet verified against the official source."
        logger.info("read_law: law_id=%s %d sections (%.0fms)", law_id, len(sections), (time.time() - t0) * 1000)
        return _with_disclaimer(out)
    except Exception as e:
        logger.error("read_law failed: %s", e)
        return _with_disclaimer({"error_code": "read_failed", "message": "Failed to read law."})


@mcp.tool
def ping() -> str:
    """Health check — verify the MCP server is running and connected to the database."""
    try:
        result = sb.table("works").select("id", count="exact").execute()
        count = result.count or 0
        return f"Pasal.id MCP server is running. Database has {count} laws loaded."
    except Exception as e:
        logger.error("ping DB check failed: %s", e)
        return "Server running but database connection failed."


if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port)
