"""Pure helpers for the v2 tools (search_legal, resolve_law, read_law).

Kept free of Supabase/FastMCP imports so citation and selector parsing can be
unit-tested without a database.
"""
import re
from dataclasses import dataclass, field

# Common names/aliases → regulation_types.code. Longest phrases first so
# "peraturan pemerintah pengganti undang-undang" wins over "peraturan pemerintah".
TYPE_ALIASES: list[tuple[str, str]] = [
    ("peraturan pemerintah pengganti undang undang", "PERPPU"),
    ("undang undang dasar sementara", "UUDS"),
    ("undang undang dasar", "UUD"),
    ("undang undang darurat", "UUDRT"),
    ("ketetapan mpr", "TAP_MPR"),
    ("tap mpr", "TAP_MPR"),
    ("undang undang", "UU"),
    ("peraturan pemerintah", "PP"),
    ("peraturan presiden", "PERPRES"),
    ("keputusan presiden", "KEPPRES"),
    ("instruksi presiden", "INPRES"),
    ("penetapan presiden", "PENPRES"),
    ("peraturan menteri", "PERMEN"),
    ("keputusan menteri", "KEPMEN"),
    ("peraturan badan", "PERBAN"),
    ("peraturan lembaga", "PERBAN"),
    ("peraturan daerah provinsi", "PERDA_PROV"),
    ("peraturan daerah kabupaten", "PERDA_KAB"),
    ("peraturan daerah kota", "PERDA_KAB"),
    ("peraturan daerah", "PERDA"),
    ("surat edaran", "SE"),
    ("perpu", "PERPPU"),
    ("perppu", "PERPPU"),
    ("perpres", "PERPRES"),
    ("keppres", "KEPPRES"),
    ("kepres", "KEPPRES"),
    ("inpres", "INPRES"),
    ("penpres", "PENPRES"),
    ("permen", "PERMEN"),
    ("kepmen", "KEPMEN"),
    ("perban", "PERBAN"),
    ("perda", "PERDA"),
    ("uuds", "UUDS"),
    ("uudrt", "UUDRT"),
    ("uud", "UUD"),
    ("uu", "UU"),
    ("pp", "PP"),
    ("se", "SE"),
]

_ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100}


def roman_to_int(s: str) -> int | None:
    """Convert a Roman numeral (I..CCC) to int, or None if not Roman."""
    s = s.strip().upper()
    if not s or any(c not in _ROMAN for c in s):
        return None
    total = 0
    for i, c in enumerate(s):
        v = _ROMAN[c]
        if i + 1 < len(s) and _ROMAN[s[i + 1]] > v:
            total -= v
        else:
            total += v
    return total


def chapter_number(s: str | None) -> int | None:
    """Normalize a BAB number ('III', '3', 'iii') to int."""
    if not s:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    return roman_to_int(s)


def normalize(text: str) -> str:
    """Lowercase, replace non-alphanumerics with spaces, collapse whitespace."""
    text = re.sub(r"[^a-z0-9 ]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Citation:
    type_code: str | None = None
    number: str | None = None
    year: int | None = None
    topic: str | None = None  # leftover words, e.g. "ketenagakerjaan"


def parse_citation(text: str, extra_aliases: list[tuple[str, str]] | None = None) -> Citation:
    """Parse a free-form regulation citation.

    Handles 'UU 13/2003', 'UU No. 13 Tahun 2003', 'Undang-Undang Nomor 13
    Tahun 2003', 'PP 35 2021', 'uu-13-2003', 'UU Ketenagakerjaan',
    '/akn/id/act/uu/2003/13'.
    """
    raw = text.strip()

    frbr = re.match(r"^/?akn/id/act/([a-z_]+)/(\d{4})/([\w.-]+)", raw, re.IGNORECASE)
    if frbr:
        return Citation(frbr.group(1).upper(), frbr.group(3), int(frbr.group(2)))

    norm = normalize(raw)
    aliases = sorted(
        TYPE_ALIASES + (extra_aliases or []), key=lambda a: len(a[0]), reverse=True
    )
    cit = Citation()
    rest = norm
    for phrase, code in aliases:
        if norm == phrase or norm.startswith(phrase + " "):
            cit.type_code = code
            rest = norm[len(phrase):].strip()
            break

    # Drop filler words that commonly appear between parts.
    tokens = [t for t in rest.split() if t not in {"no", "nomor", "tahun", "thn", "th", "tentang"}]
    nums = [t for t in tokens if t.isdigit()]
    words = [t for t in tokens if not t.isdigit()]

    if len(nums) >= 2:
        # "13 2003" → number 13, year 2003; "2003 13" (FRBR order) also accepted.
        a, b = nums[0], nums[1]
        if len(a) == 4 and len(b) != 4:
            cit.number, cit.year = b, int(a)
        else:
            cit.number, cit.year = a, int(b)
    elif len(nums) == 1:
        n = nums[0]
        if len(n) == 4 and 1900 < int(n) < 2100:
            cit.year = int(n)
        else:
            cit.number = n

    if words:
        cit.topic = " ".join(words)
    return cit


# ---------------------------------------------------------------------------
# read_law selectors
# ---------------------------------------------------------------------------

_PASAL_NUM = re.compile(r"^(\d+)\s*([a-z]?)$", re.IGNORECASE)


def pasal_key(number: str | None) -> tuple[int, str] | None:
    """'27A' → (27, 'A'); None if not a pasal number."""
    if not number:
        return None
    m = _PASAL_NUM.match(number.strip())
    if not m:
        return None
    return int(m.group(1)), m.group(2).upper()


@dataclass
class Selector:
    kind: str  # outline | all | pasal | bab | menimbang | mengingat | preamble | penjelasan | penjelasan_umum | penjelasan_pasal | lampiran | source
    start: tuple[int, str] | None = None
    end: tuple[int, str] | None = None
    ayat: str | None = None
    bab: int | None = None
    raw: str = ""
    extra: dict = field(default_factory=dict)


def _parse_range(text: str) -> tuple[tuple[int, str], tuple[int, str]] | None:
    m = re.match(r"^(\d+\s*[a-z]?)\s*(?:-|s/d|sampai|hingga)\s*(\d+\s*[a-z]?)$", text, re.IGNORECASE)
    if m:
        a, b = pasal_key(m.group(1)), pasal_key(m.group(2))
        if a and b:
            return (a, b) if a <= b else (b, a)
        return None
    k = pasal_key(text)
    return (k, k) if k else None


def parse_selector(selector: str | None) -> list[Selector]:
    """Parse a read_law selector into parts.

    Examples: 'pasal 27', 'pasal 27-30', 'pasal 13-16, pasal 40-41',
    'pasal 13-16, 40-41', 'pasal 81 ayat 2', 'bab III', 'menimbang',
    'mengingat', 'penjelasan', 'penjelasan pasal 5', 'lampiran', 'all',
    'outline'. Raises ValueError on an unknown part.
    """
    if not selector or not selector.strip():
        return [Selector("outline", raw="outline")]

    parts: list[Selector] = []
    last_kind: str | None = None
    for chunk in selector.split(","):
        raw = chunk.strip()
        s = re.sub(r"\s+", " ", raw.lower())
        if not s:
            continue

        if s in {"all", "semua", "seluruh"}:
            parts.append(Selector("all", raw=raw))
        elif s in {"outline", "daftar isi", "struktur"}:
            parts.append(Selector("outline", raw=raw))
        elif s in {"menimbang", "konsiderans"}:
            parts.append(Selector("menimbang", raw=raw))
        elif s in {"mengingat", "dasar hukum"}:
            parts.append(Selector("mengingat", raw=raw))
        elif s in {"pembukaan", "preamble", "mukadimah"}:
            parts.append(Selector("preamble", raw=raw))
        elif s in {"lampiran"}:
            parts.append(Selector("lampiran", raw=raw))
        elif s.startswith("source"):
            parts.append(Selector("source", raw=raw))
        elif s in {"penjelasan", "penjelasan umum"}:
            parts.append(Selector("penjelasan_umum" if s.endswith("umum") else "penjelasan", raw=raw))
        elif m := re.match(r"^bab\s+([ivxlc]+|\d+)$", s):
            n = chapter_number(m.group(1))
            if n is None:
                raise ValueError(f"Nomor BAB tidak dikenali: {raw!r}")
            parts.append(Selector("bab", bab=n, raw=raw))
        elif m := re.match(r"^penjelasan\s+pasal\s+(.+)$", s):
            rng = _parse_range(m.group(1))
            if not rng:
                raise ValueError(f"Selector tidak dikenali: {raw!r}")
            parts.append(Selector("penjelasan_pasal", start=rng[0], end=rng[1], raw=raw))
            last_kind = "penjelasan_pasal"
            continue
        elif m := re.match(r"^pasal\s+(\d+\s*[a-z]?)\s+ayat\s+\(?(\d+)\)?$", s):
            k = pasal_key(m.group(1))
            parts.append(Selector("pasal", start=k, end=k, ayat=m.group(2), raw=raw))
        elif m := re.match(r"^pasal\s+(.+)$", s):
            rng = _parse_range(m.group(1))
            if not rng:
                raise ValueError(f"Selector tidak dikenali: {raw!r}")
            parts.append(Selector("pasal", start=rng[0], end=rng[1], raw=raw))
        elif (rng := _parse_range(s)) and last_kind in {"pasal", "penjelasan_pasal"}:
            # Bare range continues the previous kind: 'pasal 13-16, 40-41'.
            parts.append(Selector(last_kind, start=rng[0], end=rng[1], raw=raw))
        else:
            raise ValueError(f"Selector tidak dikenali: {raw!r}")
        last_kind = parts[-1].kind
    return parts or [Selector("outline", raw="outline")]


def in_range(number: str | None, sel: Selector) -> bool:
    """True if a pasal number falls inside the selector's [start, end] range.

    Suffixed pasal (27A) inside a numeric range are included: 'pasal 27-28'
    returns 27, 27A, 28.
    """
    k = pasal_key(number)
    if k is None or sel.start is None or sel.end is None:
        return False
    if sel.start == sel.end:
        return k == sel.start
    lo = sel.start
    hi = sel.end if sel.end[1] else (sel.end[0], "ZZ")
    return lo <= k <= hi


def slice_preamble(text: str, part: str) -> str | None:
    """Extract the 'Menimbang' or 'Mengingat' block from preamble text."""
    markers = {
        "menimbang": (r"\bMenimbang\b", r"\bMengingat\b"),
        "mengingat": (r"\bMengingat\b", r"\b(?:Dengan Persetujuan|MEMUTUSKAN|Memutuskan|Menetapkan)\b"),
    }
    start_re, end_re = markers[part]
    m = re.search(start_re, text, re.IGNORECASE)
    if not m:
        return None
    tail = text[m.start():]
    e = re.search(end_re, tail[len(m.group(0)):])
    return (tail[: len(m.group(0)) + e.start()] if e else tail).strip()


def format_citation(type_code: str, number: str | None, year: int, title: str | None = None) -> str:
    """'UU No. 13 Tahun 2003 tentang Ketenagakerjaan'."""
    label = type_code.replace("_", " ")
    base = f"{label} No. {number} Tahun {year}" if number else f"{label} Tahun {year}"
    if title and " tentang " in title.lower():
        topic = title[title.lower().index(" tentang ") + len(" tentang "):].strip()
        base += f" tentang {topic}"
    return base
