"""The keyword (BM25) passage index: query sanitising, storage and ranking.

Pure SQLite: nothing here needs ChromaDB or an embedding model, so these run
on every supported Python, the base install included.
"""

import json
import sqlite3

import pytest

from zotero_mcp import keyword_index
from zotero_mcp.keyword_index import (
    KeywordIndex,
    KeywordIndexError,
    build_match_query,
    keyword_index_path,
    load_keyword_config,
)

pytestmark = pytest.mark.skipif(
    not keyword_index.fts5_available(), reason="this Python's SQLite has no FTS5"
)


def _passages(*texts, pages=None):
    return [
        {"chunk_index": i, "text": t, "char_start": i * 100, "char_end": i * 100 + len(t),
         **({"page": pages[i], "page_end": pages[i]} if pages else {})}
        for i, t in enumerate(texts)
    ]


def _meta(**kw):
    return {"group_id": 0, "item_type": "journalArticle", "title": "T", "creators": [],
            "fulltext": "yes", **kw}


@pytest.fixture
def index(tmp_path):
    with KeywordIndex(tmp_path / "kw.sqlite3") as idx:
        yield idx


def _keys(found):
    return [hit["item_key"] for hit in found["results"]]


# ---------------------------------------------------------------------------
# Query sanitising
# ---------------------------------------------------------------------------

NASTY_QUERIES = [
    'NEAR(foo bar, 3)',
    'title:measure',
    'foo* AND -bar OR NOT baz',
    '"unbalanced quote',
    'unbalanced" quote"',
    '(((parentheses',
    '^start + plus - minus',
    "'; DROP TABLE items; --",
    "$\\int_0^1 f(x)\\,dx \\leq \\|f\\|_{L^2}$",
    "\\frac{\\partial u}{\\partial t} = \\Delta u",
    "Erdős–Rényi random graph",
    "Banach–Steinhaus theorem (uniform-boundedness)",
    "f: X → Y is σ-finite",
    "emoji 🎉 and\x00control\x07chars\ttab",
    "“curly quoted phrase” and ‘single’",
    "a" * 5000,
    " ".join(f"word{i}" for i in range(500)),
    "AND",
    "OR OR OR",
    "* : ^ - + ( ) { } [ ] \\ / \" '",
    "Theorem 3.2.1 (Lemma 4.1')",
    "x",
]


@pytest.mark.parametrize("query", NASTY_QUERIES)
def test_nasty_input_never_breaks_the_match_syntax(index, query):
    index.replace_item("ITEMAAAA", _meta(), _passages("measure theory and the Banach Steinhaus theorem"))
    index.commit()
    # Must not raise: whatever came in, the MATCH expression is valid FTS5.
    index.search(query)


def test_terms_are_quoted_and_ored():
    assert build_match_query("Hoeffding inequality") == '"Hoeffding" OR "inequality"'


def test_fts_operators_in_the_input_are_plain_words():
    match = build_match_query("NEAR AND foo")
    assert match == '"NEAR" OR "foo"'


def test_a_quoted_phrase_is_kept_as_a_phrase():
    match = build_match_query('"uniform boundedness" principle')
    assert match.split(" OR ") == ['"uniform boundedness"', '"principle"']


def test_hyphenated_names_and_dotted_numbers_become_phrases():
    assert build_match_query("Radon–Nikodym") == '"Radon Nikodym"'
    assert build_match_query("Banach-Steinhaus") == '"Banach Steinhaus"'
    assert build_match_query("Theorem 3.2") == '"Theorem" OR "3 2"'


def test_latex_greek_matches_the_letter_or_its_name_and_markup_is_dropped():
    match = build_match_query("\\sigma-algebra on \\mathbb{R}")
    assert '("sigma" OR "σ")' in match
    assert "mathbb" not in match
    assert '"algebra"' in match


def test_other_latex_commands_are_kept_as_words():
    assert build_match_query("\\limsup") == '"limsup"'


def test_stopwords_are_dropped_unless_nothing_else_is_left():
    assert build_match_query("what is the proof of the theorem") == '"proof" OR "theorem"'
    assert build_match_query("the of") == '"the" OR "of"'


def test_possessive_s_and_single_letters_are_dropped():
    assert build_match_query("Doob's inequality for f") == '"Doob" OR "inequality"'


def test_nothing_searchable_gives_none():
    for query in ("", "   ", "* ^ : ()", '"', "x y z"):
        assert build_match_query(query) is None, query


def test_terms_are_capped_and_deduplicated():
    match = build_match_query(" ".join(f"word{i}" for i in range(100)) + " word1 WORD1")
    terms = match.split(" OR ")
    assert len(terms) == keyword_index.MAX_QUERY_TERMS
    assert len(set(t.lower() for t in terms)) == len(terms)


def test_double_quotes_inside_a_phrase_cannot_escape():
    # The phrase regex never captures a quote, and _fts_string doubles any.
    assert keyword_index._fts_string(['a"b']) == '"a""b"'


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def test_replace_item_replaces_every_old_passage(index):
    index.replace_item("ITEMAAAA", _meta(), _passages("old wording about Gronwall", "more Gronwall"))
    index.commit()
    assert _keys(index.search("Gronwall")) == ["ITEMAAAA"]

    index.replace_item("ITEMAAAA", _meta(), _passages("new wording about Grothendieck"))
    index.commit()
    assert index.search("Gronwall")["results"] == []
    assert _keys(index.search("Grothendieck")) == ["ITEMAAAA"]
    assert index.item_state("ITEMAAAA")["n_passages"] == 1
    index._con.execute("INSERT INTO passages_fts(passages_fts) VALUES ('integrity-check')")


def test_delete_items_removes_rows_and_passages(index):
    index.replace_item("ITEMAAAA", _meta(), _passages("Kolmogorov extension theorem"))
    index.replace_item("ITEMBBBB", _meta(), _passages("Kolmogorov complexity"))
    index.commit()
    assert index.delete_items(["ITEMAAAA", "NOTTHERE"]) == 1
    index.commit()
    assert _keys(index.search("Kolmogorov")) == ["ITEMBBBB"]
    assert index.item_keys() == {"ITEMBBBB"}
    assert index._con.execute("SELECT COUNT(*) FROM passages").fetchone()[0] == 1
    index._con.execute("INSERT INTO passages_fts(passages_fts) VALUES ('integrity-check')")


def test_clear_empties_the_index(index):
    index.replace_item("ITEMAAAA", _meta(), _passages("Fatou lemma"))
    index.commit()
    index.clear()
    assert index.stats()["items"] == 0
    assert index.search("Fatou")["results"] == []


def test_item_state_records_what_the_passages_were_built_from(index):
    index.replace_item("ITEMAAAA", _meta(date_modified="2026-01-01", attachment_keys="ATT1",
                                         attachment_priority="pdf", profile="p1"), [])
    state = index.item_state("ITEMAAAA")
    assert (state["date_modified"], state["attachment_keys"], state["profile"]) == ("2026-01-01", "ATT1", "p1")
    assert index.item_state("MISSING1") is None
    assert set(index.item_states()) == {"ITEMAAAA"}


def test_stats_counts_items_and_passages(index):
    index.replace_item("ITEMAAAA", _meta(), _passages("a b", "c d"))
    index.replace_item("ITEMBBBB", _meta(fulltext="none"), _passages("e f"))
    index.commit()
    stats = index.stats()
    assert (stats["items"], stats["items_with_fulltext"], stats["passages"]) == (2, 1, 3)
    assert stats["size_bytes"] > 0


def test_another_schema_version_is_rebuilt_on_open(tmp_path):
    path = tmp_path / "kw.sqlite3"
    with KeywordIndex(path) as idx:
        idx.replace_item("ITEMAAAA", _meta(), _passages("Lusin theorem"))
        idx.commit()
        idx._con.execute("UPDATE meta SET value = '0' WHERE key = 'schema_version'")
        idx.commit()
    with pytest.raises(KeywordIndexError, match="another version"):
        KeywordIndex(path, create=False)
    with KeywordIndex(path) as idx:
        assert idx.stats()["items"] == 0


def test_opening_a_missing_index_for_reading_says_so(tmp_path):
    with pytest.raises(KeywordIndexError, match="No keyword index"):
        KeywordIndex(tmp_path / "absent.sqlite3", create=False)
    assert not (tmp_path / "absent.sqlite3").exists()


def test_no_fts5_fails_with_a_message_that_says_what_to_do(tmp_path, monkeypatch):
    monkeypatch.setattr(keyword_index, "fts5_available", lambda: False)
    with pytest.raises(KeywordIndexError, match="FTS5"):
        KeywordIndex(tmp_path / "kw.sqlite3")


def test_readers_see_committed_writes_while_a_writer_is_open(tmp_path):
    path = tmp_path / "kw.sqlite3"
    with KeywordIndex(path) as writer:
        writer.replace_item("ITEMAAAA", _meta(), _passages("Prokhorov theorem"))
        writer.commit()
        writer.replace_item("ITEMBBBB", _meta(), _passages("Prokhorov metric"))  # uncommitted
        with KeywordIndex(path, create=False) as reader:
            assert _keys(reader.search("Prokhorov")) == ["ITEMAAAA"]


# ---------------------------------------------------------------------------
# Ranking and matching
# ---------------------------------------------------------------------------


def test_rare_terms_outrank_common_ones(index):
    filler = "theorem proof lemma measure " * 5
    for n in range(20):
        index.replace_item(f"FILL{n:04d}", _meta(), _passages(filler))
    index.replace_item("TARGET01", _meta(), _passages(filler + " Azuma Hoeffding martingale bound"))
    index.replace_item("PARTIAL1", _meta(), _passages(filler + " Hoeffding"))
    index.commit()
    keys = _keys(index.search("Azuma Hoeffding martingale theorem", limit=5))
    assert keys[:2] == ["TARGET01", "PARTIAL1"]


def test_matching_ignores_accents_word_endings_and_dash_kind(index):
    index.replace_item("ERDOS001", _meta(), _passages("the Erdős–Rényi random graph theorems"))
    index.commit()
    assert _keys(index.search("Erdos Renyi")) == ["ERDOS001"]
    assert _keys(index.search("theorem")) == ["ERDOS001"]
    assert _keys(index.search("Erdős-Rényi")) == ["ERDOS001"]


def test_a_phrase_requires_the_words_together(index):
    index.replace_item("APART001", _meta(), _passages("uniform convergence is bounded"))
    index.replace_item("TOGETHER", _meta(), _passages("the uniform boundedness principle"))
    index.commit()
    assert _keys(index.search('"uniform boundedness"')) == ["TOGETHER"]


def test_one_hit_per_item_by_default_and_more_when_asked(index):
    index.replace_item("BOOK0001", _meta(), _passages("Fubini theorem", "Fubini again", "Fubini thrice"))
    index.replace_item("PAPER001", _meta(), _passages("Fubini Tonelli"))
    index.commit()
    assert sorted(_keys(index.search("Fubini"))) == ["BOOK0001", "PAPER001"]
    found = index.search("Fubini", per_item=3)
    assert _keys(found).count("BOOK0001") == 3
    assert len(index.search("Fubini", per_item=3, limit=2)["results"]) == 2


def test_grouping_reads_past_the_first_page_of_rows(index):
    # One book that matches everywhere must not crowd out a second item.
    index.replace_item("BOOK0001", _meta(), _passages(*["Lebesgue Lebesgue Lebesgue"] * 120))
    index.replace_item("PAPER001", _meta(), _passages("Lebesgue"))
    index.commit()
    assert sorted(_keys(index.search("Lebesgue", limit=2))) == ["BOOK0001", "PAPER001"]


def test_group_id_limits_the_search_to_one_library(index):
    index.replace_item("PERSONAL", _meta(group_id=0), _passages("Riesz representation"))
    index.replace_item("GROUP001", _meta(group_id=42), _passages("Riesz representation"))
    index.commit()
    assert _keys(index.search("Riesz", group_id=42)) == ["GROUP001"]
    assert _keys(index.search("Riesz", group_id=0)) == ["PERSONAL"]
    assert sorted(_keys(index.search("Riesz"))) == ["GROUP001", "PERSONAL"]


def test_hit_shape_gives_what_read_pdf_pages_needs(index):
    index.replace_item(
        "ITEMAAAA",
        _meta(title="Real Analysis", creators=[{"lastName": "Folland", "creatorType": "author"}],
              date="1999", attachment_key="ATTKEY01", fulltext_source="pdf"),
        [{"chunk_index": 0, "text": "intro", "char_start": 0, "char_end": 5, "page": 1, "page_end": 1},
         {"chunk_index": 1, "text": "Theorem 5.13 (Banach-Steinhaus) Suppose X is a Banach space",
          "char_start": 5, "char_end": 70, "page": 168, "page_end": 169, "section": "Results"}],
    )
    index.commit()
    found = index.search("Banach-Steinhaus")
    assert found["match"] == '"Banach Steinhaus"'
    (hit,) = found["results"]
    assert hit["item_key"] == "ITEMAAAA"
    assert hit["attachment_key"] == "ATTKEY01"
    assert (hit["page"], hit["page_end"]) == (168, 169)
    assert (hit["chunk_index"], hit["n_chunks"]) == (1, 2)
    assert hit["section"] == "Results"
    assert hit["title"] == "Real Analysis"
    assert hit["creators"][0]["lastName"] == "Folland"
    assert hit["score"] > 0
    assert "Banach" in hit["snippet"]
    assert hit["text"].startswith("Theorem 5.13")


def test_hits_without_pages_leave_the_page_fields_out(index):
    index.replace_item("ITEMAAAA", _meta(), _passages("Zorn lemma"))
    index.commit()
    (hit,) = index.search("Zorn")["results"]
    assert "page" not in hit and "attachment_key" not in hit


def test_a_query_with_nothing_searchable_returns_no_match(index):
    assert index.search("* ^ ()") == {"match": None, "results": []}


def test_a_broken_file_is_reported_as_a_keyword_index_error(tmp_path):
    path = tmp_path / "kw.sqlite3"
    with KeywordIndex(path) as idx:
        idx.replace_item("ITEMAAAA", _meta(), _passages("Hahn Banach"))
        idx.commit()
        idx._con.execute("DROP TABLE passages_fts")
        with pytest.raises(KeywordIndexError):
            idx.search("Hahn")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_config_defaults_and_inheritance(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"semantic_search": {
        "chunking": {"chunk_size": 1200, "overlap": 100, "max_chunks_per_item": 20},
        "extraction": {"pdf_max_pages": 75},
        "keyword_index": {"enabled": True},
    }}))
    cfg = load_keyword_config(config)
    assert cfg["enabled"] is True and cfg["keyword_only"] is False
    assert (cfg["chunk_size"], cfg["overlap"]) == (1200, 100)
    # The embedding cap does not carry over: storage is cheap here.
    assert cfg["max_chunks_per_item"] == 0
    assert cfg["pdf_max_pages"] == 75


def test_keyword_pdf_max_pages_overrides_extraction(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"semantic_search": {
        "extraction": {"pdf_max_pages": 50},
        "keyword_index": {"pdf_max_pages": 2000},
    }}))
    assert load_keyword_config(config)["pdf_max_pages"] == 2000


def test_missing_config_gives_defaults(tmp_path):
    cfg = load_keyword_config(tmp_path / "absent.json")
    assert cfg["enabled"] is False
    assert (cfg["chunk_size"], cfg["overlap"]) == (1500, 200)


def test_index_lives_beside_the_chroma_directory(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"semantic_search": {"persist_directory": str(tmp_path / "chroma")}}))
    assert keyword_index_path(config) == tmp_path / "chroma-keyword.sqlite3"


def test_index_path_can_be_set(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"semantic_search": {"keyword_index": {"path": str(tmp_path / "x.db")}}}))
    assert keyword_index_path(config) == tmp_path / "x.db"


def test_fts5_is_detected():
    assert keyword_index.fts5_available() is True
    sqlite3.connect(":memory:").close()


def test_typed_config_reads_the_keyword_section(tmp_path, monkeypatch):
    from zotero_mcp.config import load_config

    path = tmp_path / "config.json"
    path.write_text(json.dumps({"semantic_search": {"keyword_index": {
        "enabled": True, "keyword_only": True, "pdf_max_pages": 2000}}}))
    monkeypatch.setattr("zotero_mcp.config.ZOTERO_MCP_CONFIG_PATH", path)
    kw = load_config().semantic_search.keyword_index
    assert (kw.enabled, kw.keyword_only, kw.pdf_max_pages, kw.max_chunks_per_item) == (True, True, 2000, 0)
