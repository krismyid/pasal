"""Tests for v2 tools (search_legal, resolve_law, read_law) — Supabase mocked."""

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "fake-key")

with patch("supabase.create_client", return_value=MagicMock()):
    import server

import v2

search_legal = server.search_legal.fn
resolve_law = server.resolve_law.fn
read_law = server.read_law.fn

_CHAINABLE = (
    "select", "eq", "neq", "in_", "ilike", "or_", "match",
    "order", "range", "limit", "single", "text_search",
)


def _qm(data: list | None = None):
    m = MagicMock()
    for attr in _CHAINABLE:
        getattr(m, attr).return_value = m
    m.execute.return_value = MagicMock(data=data or [], count=len(data or []))
    return m


WORK = {
    "id": 7, "frbr_uri": "/akn/id/act/uu/2003/13", "slug": "uu-13-2003", "number": "13",
    "year": 2003, "title_id": "Undang-Undang Nomor 13 Tahun 2003 tentang Ketenagakerjaan",
    "status": "diubah", "regulation_type_id": 1, "source_url": "https://peraturan.go.id/x",
    "source_pdf_url": "https://peraturan.go.id/x.pdf", "content_verified": False,
    "updated_at": "2026-01-01", "pemrakarsa": "Kementerian Ketenagakerjaan",
}

NODES = [
    {"id": 1, "node_type": "preamble", "number": "", "heading": "", "parent_id": None, "sort_order": 1,
     "content_text": "PRESIDEN REPUBLIK INDONESIA, Menimbang: a. bahwa pembangunan. Mengingat: Pasal 5 UUD. "
                     "Dengan Persetujuan Bersama DPR MEMUTUSKAN"},
    {"id": 2, "node_type": "bab", "number": "I", "heading": "KETENTUAN UMUM", "parent_id": None,
     "sort_order": 2, "content_text": ""},
    {"id": 3, "node_type": "pasal", "number": "1", "heading": "", "parent_id": 2, "sort_order": 3,
     "content_text": "Dalam undang-undang ini yang dimaksud dengan..."},
    {"id": 4, "node_type": "bab", "number": "II", "heading": "UPAH", "parent_id": None,
     "sort_order": 4, "content_text": ""},
    {"id": 5, "node_type": "pasal", "number": "88", "heading": "", "parent_id": 4, "sort_order": 5,
     "content_text": "(1) Setiap pekerja berhak. (2) Upah minimum sebagaimana dimaksud dalam Pasal 89."},
    {"id": 6, "node_type": "ayat", "number": "2", "heading": "", "parent_id": 5, "sort_order": 6,
     "content_text": "Upah minimum sebagaimana dimaksud dalam Pasal 89."},
    {"id": 8, "node_type": "pasal", "number": "88A", "heading": "", "parent_id": 4, "sort_order": 7,
     "content_text": "Hak pekerja atas upah timbul."},
    {"id": 9, "node_type": "pasal", "number": "89", "heading": "", "parent_id": 4, "sort_order": 8,
     "content_text": "Upah minimum terdiri atas..."},
    {"id": 10, "node_type": "penjelasan_umum", "number": "", "heading": "", "parent_id": None,
     "sort_order": 9, "content_text": "Pembangunan ketenagakerjaan..."},
]


@pytest.fixture(autouse=True)
def _reset():
    server._reg_types = {"UU": 1, "PP": 2, "PERPRES": 3}
    server._reg_types_by_id = {1: "UU", 2: "PP", 3: "PERPRES"}
    server._nodes_cache.clear()
    for limiter in server._rate_limiters.values():
        limiter.reset()
    server.sb.reset_mock()
    yield


def _tables(**by_name):
    def table(name):
        return by_name[name]
    server.sb.table.side_effect = table


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestParseCitation:

    @pytest.mark.parametrize("text,expected", [
        ("UU 13/2003", ("UU", "13", 2003)),
        ("UU No. 13 Tahun 2003", ("UU", "13", 2003)),
        ("Undang-Undang Nomor 13 Tahun 2003", ("UU", "13", 2003)),
        ("PP 35 2021", ("PP", "35", 2021)),
        ("Perpu 1/2016", ("PERPPU", "1", 2016)),
        ("Peraturan Pemerintah Pengganti Undang-Undang Nomor 2 Tahun 2022", ("PERPPU", "2", 2022)),
        ("/akn/id/act/uu/2003/13", ("UU", "13", 2003)),
        ("uu-13-2003", ("UU", "13", 2003)),
    ])
    def test_type_number_year(self, text, expected):
        c = v2.parse_citation(text)
        assert (c.type_code, c.number, c.year) == expected

    def test_topic_only(self):
        c = v2.parse_citation("UU Ketenagakerjaan")
        assert c.type_code == "UU"
        assert c.number is None
        assert c.topic == "ketenagakerjaan"

    def test_year_only(self):
        c = v2.parse_citation("UUD 1945")
        assert (c.type_code, c.number, c.year) == ("UUD", None, 1945)


class TestParseSelector:

    def test_default_is_outline(self):
        assert v2.parse_selector("")[0].kind == "outline"

    def test_single_pasal(self):
        s = v2.parse_selector("Pasal 27")[0]
        assert (s.kind, s.start, s.end) == ("pasal", (27, ""), (27, ""))

    def test_multi_range_with_bare_continuation(self):
        parts = v2.parse_selector("pasal 13-16, 40-41")
        assert [(p.kind, p.start, p.end) for p in parts] == [
            ("pasal", (13, ""), (16, "")), ("pasal", (40, ""), (41, "")),
        ]

    def test_ayat(self):
        s = v2.parse_selector("pasal 81 ayat (2)")[0]
        assert (s.start, s.ayat) == ((81, ""), "2")

    def test_bab_roman(self):
        assert v2.parse_selector("bab III")[0].bab == 3

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            v2.parse_selector("halaman 5")

    def test_range_includes_suffixed(self):
        sel = v2.parse_selector("pasal 88-89")[0]
        assert [n for n in ["87", "88", "88A", "89", "89A", "90"] if v2.in_range(n, sel)] == ["88", "88A", "89", "89A"]


def test_format_citation():
    assert v2.format_citation("UU", "13", 2003, WORK["title_id"]) == "UU No. 13 Tahun 2003 tentang Ketenagakerjaan"


def test_slice_preamble():
    text = NODES[0]["content_text"]
    assert v2.slice_preamble(text, "menimbang").startswith("Menimbang")
    assert "Mengingat" not in v2.slice_preamble(text, "menimbang")
    assert v2.slice_preamble(text, "mengingat").endswith("UUD.")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class TestResolveLaw:

    def test_exact_citation(self):
        _tables(works=_qm([WORK]))
        r = resolve_law("UU No. 13 Tahun 2003")
        assert r["resolved"] is True
        assert r["law_id"] == 7
        assert r["citation"] == "UU No. 13 Tahun 2003 tentang Ketenagakerjaan"
        assert r["source"]["law_url"].endswith("/peraturan/uu/uu-13-2003")
        assert "disclaimer" in r

    def test_topic_falls_back_to_search(self):
        _tables(works=_qm([WORK]))
        server.sb.rpc.return_value = _qm([{"work_id": 7, "score": 12.0}])
        r = resolve_law("UU Ketenagakerjaan")
        assert r["resolved"] is True
        assert r["matched_by"] == "search"

    def test_not_found(self):
        _tables(works=_qm([]))
        server.sb.rpc.return_value = _qm([])
        r = resolve_law("UU 999 2099")
        assert r["resolved"] is False

    def test_empty(self):
        assert resolve_law(" ")["error_code"] == "empty_citation"


class TestSearchLegal:

    def test_groups_by_work_and_passes_filters(self):
        _tables(works=_qm([WORK]))
        server.sb.rpc.return_value = _qm([
            {"work_id": 7, "score": 0.2, "snippet": "a", "metadata": {"pasal": "88"}},
            {"work_id": 7, "score": 0.5, "snippet": "b", "metadata": {"pasal": "89"}},
        ])
        r = search_legal("upah minimum", regulation_types=["uu"], status=["berlaku", "diubah"], year_from=2000)
        args = server.sb.rpc.call_args[0][1]
        assert args["metadata_filter"] == {"type": "UU", "status": "berlaku,diubah", "year_from": 2000}
        assert len(r["results"]) == 1
        assert r["results"][0]["pasal"] == "Pasal 89"
        assert r["results"][0]["law_id"] == 7

    def test_year_to_filters_client_side(self):
        _tables(works=_qm([WORK]))
        server.sb.rpc.return_value = _qm([{"work_id": 7, "score": 1.0, "metadata": {}}])
        assert search_legal("upah", year_to=2000)["results"] == []

    def test_rpc_error(self):
        server.sb.rpc.side_effect = Exception("db down")
        r = search_legal("upah")
        server.sb.rpc.side_effect = None
        assert r["error_code"] == "search_failed"

    def test_within_law(self):
        _tables(works=_qm([WORK]), document_nodes=_qm([NODES[4]]))
        r = search_legal("upah", law_id=7)
        assert r["results"][0]["selector"] == "pasal 88"


class TestReadLaw:

    def _setup(self):
        _tables(works=_qm([WORK]), document_nodes=_qm(NODES))

    def test_outline(self):
        self._setup()
        r = read_law(7)
        o = r["outline"]
        assert o["pasal_total"] == 4
        assert o["chapters"][1] == {"selector": "bab II", "heading": "UPAH", "pasal_range": "Pasal 88–89", "pasal_count": 3}

    def test_pasal_range_and_cross_refs(self):
        self._setup()
        r = read_law(7, "pasal 88-89")
        assert [c["label"] for c in r["content"]] == ["Pasal 88", "Pasal 88A", "Pasal 89"]
        assert {"pasal": "89"} in r["cross_references"]
        assert r["source"]["verified"] is False
        assert "verification_note" in r

    def test_ayat(self):
        self._setup()
        r = read_law(7, "pasal 88 ayat 2")
        assert r["content"][0]["text"].startswith("Upah minimum")

    def test_bab(self):
        self._setup()
        r = read_law(7, "bab I")
        assert [c["label"] for c in r["content"]] == ["BAB I", "Pasal 1"]

    def test_menimbang_and_mengingat(self):
        self._setup()
        r = read_law(7, "menimbang, mengingat")
        assert r["content"][0]["text"].startswith("Menimbang")
        assert r["content"][1]["text"].startswith("Mengingat")

    def test_penjelasan(self):
        self._setup()
        assert read_law(7, "penjelasan")["content"][0]["label"] == "Penjelasan Umum"

    def test_missing_pasal_note(self):
        self._setup()
        r = read_law(7, "pasal 500")
        assert "pasal 500 tidak ditemukan." in r["notes"]
        assert "outline" in r

    def test_invalid_selector(self):
        assert read_law(7, "halaman 3")["error_code"] == "invalid_selector"

    def test_unknown_law(self):
        _tables(works=_qm([]))
        assert read_law(999)["error_code"] == "law_not_found"

    def test_truncation(self, monkeypatch):
        self._setup()
        monkeypatch.setattr(server, "READ_LAW_MAX_CHARS", 60)
        r = read_law(7, "all")
        assert any("Truncated" in n for n in r["notes"])

    def test_lampiran_points_to_pdf(self):
        self._setup()
        r = read_law(7, "lampiran")
        assert r["source"]["pdf_url"].endswith(".pdf")
        assert any("pdf_url" in n for n in r["notes"])
