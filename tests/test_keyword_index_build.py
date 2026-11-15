"""Building the keyword passage index from the local library, and its place in update-db.

The reader is a double, so no Zotero database or PDF is needed; nothing here
opens ChromaDB or loads an embedding model.
"""

import json
import sys

import pytest

if sys.version_info >= (3, 14):
    pytest.skip(
        "chromadb currently relies on pydantic v1 paths that are incompatible with Python 3.14+",
        allow_module_level=True,
    )

from zotero_mcp import keyword_index, semantic_search
from zotero_mcp.extract import DEFAULT_ATTACHMENT_PRIORITY, PAGE_SEPARATOR
from zotero_mcp.keyword_index import KeywordIndex

pytestmark = pytest.mark.skipif(
    not keyword_index.fts5_available(), reason="this Python's SQLite has no FTS5"
)

DATE = "2026-07-02 01:01:48"

BOOK_PAGES = [
    "Preface. This book is about functional analysis.",
    "Chapter one introduces normed spaces and the Hahn-Banach theorem.",
    "Theorem 5.13 (Banach-Steinhaus). A pointwise bounded family of operators is uniformly bounded.",
]


class FakeItem:
    def __init__(self, item_id, key, title, *, item_type="book", date_modified=DATE, creators="Folland, Gerald"):
        self.item_id = item_id
        self.key = key
        self.item_type = item_type
        self.title = title
        self.abstract = ""
        self.extra = ""
        self.doi = None
        self.notes = None
        self.creators = creators
        self.date_added = DATE
        self.date_modified = date_modified
        self.fulltext = None
        self.fulltext_source = None


class FakeReader:
    """The parts of LocalZoteroReader the keyword build uses."""

    attachment_priority = DEFAULT_ATTACHMENT_PRIORITY

    def __init__(self, items, attachments, texts):
        self.items = items
        self.attachments = attachments  # item_id -> [(key, path, ctype)]
        self.texts = texts  # item_id -> (text, source) or None
        self.extract_calls: list[int] = []
        self.kwargs = None

    def __call__(self, *args, **kwargs):
        self.kwargs = kwargs
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_key_group_map(self):
        return ({it.key: 0 for it in self.items}, set())

    def get_items_with_text(self, limit=None, include_fulltext=False, collection_keys=None):
        return list(self.items[:limit] if limit else self.items)

    def get_fulltext_meta_for_item(self, item_id):
        return [list(row) for row in self.attachments.get(item_id, [])]

    def extract_fulltext_for_item(self, item_id):
        self.extract_calls.append(item_id)
        return self.texts.get(item_id)

    def extraction_attachment_key(self, item_id):
        rows = self.attachments.get(item_id) or []
        return rows[0][0] if rows else None

    def get_items_by_keys(self, keys):
        return {
            it.key: {"key": it.key, "data": {"itemType": it.item_type, "title": it.title, "date": "1999",
                                             "creators": [{"lastName": "Folland", "firstName": "Gerald",
                                                           "creatorType": "author"}]}}
            for it in self.items if it.key in keys
        }


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A configured keyword-only search over a fake two-item library."""
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"semantic_search": {
        "chunking": {"chunk_size": 200, "overlap": 20},
        "keyword_index": {"enabled": True, "path": str(tmp_path / "kw.sqlite3")},
    }}))
    monkeypatch.setattr(semantic_search, "_update_lock_path", lambda: tmp_path / "update.lock")
    monkeypatch.setattr(semantic_search, "get_zotero_client", lambda: object())
    monkeypatch.setattr(semantic_search, "is_local_mode", lambda: True)

    def no_chroma(*a, **kw):
        raise AssertionError("the keyword index must not open ChromaDB")

    monkeypatch.setattr(semantic_search, "create_chroma_client", no_chroma)

    book = FakeItem(1, "BOOKKEY1", "Real Analysis")
    note = FakeItem(2, "NOATTKEY", "A metadata-only item about Gronwall inequality", item_type="journalArticle")
    reader = FakeReader(
        [book, note],
        {1: [("ATTKEY01", "storage:book.pdf", "application/pdf")]},
        {1: (PAGE_SEPARATOR.join(BOOK_PAGES), "pdf")},
    )
    monkeypatch.setattr(semantic_search, "LocalZoteroReader", reader)

    class Env:
        pass

    e = Env()
    e.config, e.reader, e.tmp_path, e.book, e.note = config, reader, tmp_path, book, note

    def search():
        return semantic_search.ZoteroSemanticSearch(config_path=str(config), keyword_only=True)

    e.search = search

    def index():
        return KeywordIndex(tmp_path / "kw.sqlite3", create=False)

    e.index = index
    return e


def test_builds_with_no_chroma_and_no_embedding_model(env):
    stats = env.search().update_keyword_index()

    assert "error" not in stats, stats
    assert (stats["total_items"], stats["indexed_items"], stats["metadata_only_items"]) == (2, 1, 1)
    assert env.reader.kwargs["fulltext_cache_enabled"] is False
    with env.index() as idx:
        found = idx.search("Banach-Steinhaus uniformly bounded")
        (hit,) = found["results"]
        assert hit["item_key"] == "BOOKKEY1"
        assert hit["attachment_key"] == "ATTKEY01"
        assert hit["page"] == 3
        assert hit["title"] == "Real Analysis"
        assert hit["date"] == "1999"
        # A metadata-only item is searchable by its title.
        assert [h["item_key"] for h in idx.search("Gronwall")["results"]] == ["NOATTKEY"]
        assert idx.item_state("NOATTKEY")["fulltext"] == "none"


def test_a_second_run_extracts_nothing_that_has_not_changed(env):
    env.search().update_keyword_index()
    env.reader.extract_calls.clear()

    stats = env.search().update_keyword_index()

    assert env.reader.extract_calls == []
    assert stats["unchanged_items"] == 2 and stats["indexed_items"] == 0


def test_a_changed_item_is_extracted_again_and_its_passages_replaced(env):
    env.search().update_keyword_index()
    env.reader.extract_calls.clear()
    env.book.date_modified = "2026-08-01 00:00:00"
    env.reader.texts[1] = ("Completely new text about the Krein-Milman theorem.", "pdf")

    stats = env.search().update_keyword_index()

    assert env.reader.extract_calls == [1]
    assert stats["indexed_items"] == 1 and stats["unchanged_items"] == 1
    with env.index() as idx:
        assert idx.search("Steinhaus")["results"] == []
        assert [h["item_key"] for h in idx.search("Krein Milman")["results"]] == ["BOOKKEY1"]


def test_a_new_attachment_triggers_extraction(env):
    env.search().update_keyword_index()
    env.reader.extract_calls.clear()
    env.reader.attachments[2] = [("ATTKEY02", "storage:paper.pdf", "application/pdf")]
    env.reader.texts[2] = ("The Gronwall inequality bounds solutions.", "pdf")

    env.search().update_keyword_index()

    assert env.reader.extract_calls == [2]
    with env.index() as idx:
        assert idx.item_state("NOATTKEY")["fulltext"] == "yes"


def test_changing_the_passage_settings_reindexes(env):
    env.search().update_keyword_index()
    env.reader.extract_calls.clear()
    cfg = json.loads(env.config.read_text())
    cfg["semantic_search"]["chunking"]["chunk_size"] = 400
    env.config.write_text(json.dumps(cfg))

    env.search().update_keyword_index()

    assert env.reader.extract_calls == [1]


def test_an_attachment_that_gives_no_text_is_not_parsed_again(env):
    env.reader.texts[1] = None
    stats = env.search().update_keyword_index()
    assert stats["failed_items"] == 1
    with env.index() as idx:
        assert idx.item_state("BOOKKEY1")["fulltext"] == "failed"
        # Its metadata is still searchable.
        assert [h["item_key"] for h in idx.search("Real Analysis")["results"]] == ["BOOKKEY1"]
    env.reader.extract_calls.clear()

    env.search().update_keyword_index()

    assert env.reader.extract_calls == []


def test_items_gone_from_the_library_are_removed(env):
    env.search().update_keyword_index()
    env.reader.items = [env.book]

    stats = env.search().update_keyword_index()

    assert stats["deleted_items"] == 1
    with env.index() as idx:
        assert idx.item_keys() == {"BOOKKEY1"}
        assert idx.search("Gronwall")["results"] == []


def test_a_limited_run_deletes_nothing(env):
    env.search().update_keyword_index()
    env.reader.items = [env.book]

    stats = env.search().update_keyword_index(limit=1)

    assert stats["deleted_items"] == 0
    with env.index() as idx:
        assert idx.item_keys() == {"BOOKKEY1", "NOATTKEY"}


def test_an_empty_scan_does_not_wipe_the_index(env):
    env.search().update_keyword_index()
    env.reader.items = []

    stats = env.search().update_keyword_index()

    assert stats["deletion_skipped_reason"] == "mass_deletion_guard"
    with env.index() as idx:
        assert len(idx.item_keys()) == 2
    stats = env.search().update_keyword_index(allow_mass_deletion=True)
    assert stats["deleted_items"] == 2


def test_force_rebuild_extracts_everything_again(env):
    env.search().update_keyword_index()
    env.reader.extract_calls.clear()

    env.search().update_keyword_index(force_rebuild=True)

    assert env.reader.extract_calls == [1]


def test_passages_past_the_embedding_cap_are_kept(env):
    long_text = PAGE_SEPARATOR.join(f"Page {n} discusses lemma number {n}. " * 12 for n in range(1, 61))
    env.reader.texts[1] = (long_text, "pdf")

    env.search().update_keyword_index()

    with env.index() as idx:
        state = idx.item_state("BOOKKEY1")
        assert state["n_passages"] > 20
        (hit,) = idx.search('"Page 57 discusses"')["results"]
        assert hit["page"] == 57


def test_max_chunks_per_item_caps_the_keyword_index_when_set(env):
    cfg = json.loads(env.config.read_text())
    cfg["semantic_search"]["keyword_index"]["max_chunks_per_item"] = 2
    env.config.write_text(json.dumps(cfg))
    env.reader.texts[1] = ("word " * 2000, "pdf")

    env.search().update_keyword_index()

    with env.index() as idx:
        assert idx.item_state("BOOKKEY1")["n_passages"] == 2


def test_web_mode_is_refused_with_a_reason(env, monkeypatch):
    monkeypatch.setattr(semantic_search, "is_local_mode", lambda: False)
    stats = env.search().update_keyword_index()
    assert "local mode" in stats["error"]


def test_a_held_update_lock_skips_the_run(env):
    lock = semantic_search._acquire_update_lock(env.tmp_path / "update.lock")
    assert lock.__enter__()
    try:
        stats = env.search().update_keyword_index()
    finally:
        lock.__exit__(None, None, None)
    if sys.platform != "win32":
        assert stats.get("skipped_reason") == "another_update_in_progress"


# ---------------------------------------------------------------------------
# Passages
# ---------------------------------------------------------------------------


def test_passage_pages_and_sections_come_from_the_text():
    search = semantic_search.ZoteroSemanticSearch.__new__(semantic_search.ZoteroSemanticSearch)
    search._keyword_config = {"chunk_size": 60, "overlap": 10, "max_chunks_per_item": 0}
    text = "Title line\n\nIntroduction\nfirst page text here" + PAGE_SEPARATOR + "second page" + \
        PAGE_SEPARATOR + "\nResults\nthird page holds the main theorem statement in full."
    passages = search._keyword_passages(text)
    assert passages[0]["page"] == 1
    assert passages[-1]["page_end"] == 3
    for p in passages:
        assert p["page"] <= p["page_end"]
        assert p["page"] == text.count(PAGE_SEPARATOR, 0, p["char_start"]) + 1
    assert passages[-1].get("section") == "Results"


def test_passages_without_page_breaks_have_no_pages():
    search = semantic_search.ZoteroSemanticSearch.__new__(semantic_search.ZoteroSemanticSearch)
    search._keyword_config = {"chunk_size": 1500, "overlap": 200, "max_chunks_per_item": 0}
    (p,) = search._keyword_passages("flat text from zotero's own cache")
    assert "page" not in p and "page_end" not in p


# ---------------------------------------------------------------------------
# update_database
# ---------------------------------------------------------------------------


def _stub_update(search, embed_stats):
    calls = {"embed": 0, "keyword": 0}

    def embed(**kwargs):
        calls["embed"] += 1
        return dict(embed_stats)

    def keyword(**kwargs):
        calls["keyword"] += 1
        calls["keyword_kwargs"] = kwargs
        return {"indexed_items": 1, "passages_written": 3}

    search._update_embedding_index = embed
    search.update_keyword_index = keyword
    return calls


def test_keyword_only_config_skips_embeddings(env):
    cfg = json.loads(env.config.read_text())
    cfg["semantic_search"]["keyword_index"]["keyword_only"] = True
    env.config.write_text(json.dumps(cfg))
    search = env.search()
    calls = _stub_update(search, {})

    stats = search.update_database(extract_fulltext=True, force_full_rebuild=True)

    assert calls["embed"] == 0 and calls["keyword"] == 1
    assert calls["keyword_kwargs"]["force_rebuild"] is True
    assert stats["passages_written"] == 3


def test_enabled_keyword_index_follows_the_embedding_run(env):
    search = env.search()
    calls = _stub_update(search, {"processed_items": 5})

    stats = search.update_database(extract_fulltext=True, force_full_rebuild=True)

    assert calls == {**calls, "embed": 1, "keyword": 1}
    # An embedding rebuild does not rebuild the keyword index.
    assert "force_rebuild" not in calls["keyword_kwargs"]
    assert stats["keyword_index"]["passages_written"] == 3


@pytest.mark.parametrize("embed_stats", [
    {"error": "embedding API down"},
    {"skipped_reason": "another_update_in_progress"},
])
def test_keyword_index_waits_when_the_embedding_run_did_not_happen(env, embed_stats):
    search = env.search()
    calls = _stub_update(search, embed_stats)

    stats = search.update_database()

    assert calls["keyword"] == 0
    assert "keyword_index" not in stats


def test_keyword_index_off_leaves_update_database_as_it_was(env):
    cfg = json.loads(env.config.read_text())
    cfg["semantic_search"]["keyword_index"]["enabled"] = False
    env.config.write_text(json.dumps(cfg))
    search = env.search()
    calls = _stub_update(search, {"processed_items": 1})

    stats = search.update_database()

    assert calls["keyword"] == 0 and "keyword_index" not in stats


def test_enabled_keyword_index_in_web_mode_reports_why_it_did_not_run(env, monkeypatch):
    monkeypatch.setattr(semantic_search, "is_local_mode", lambda: False)
    search = env.search()
    calls = _stub_update(search, {"processed_items": 1})

    stats = search.update_database()

    assert calls["keyword"] == 0
    assert "error" not in stats["keyword_index"]
    assert "local mode" in stats["keyword_index"]["skipped_message"]


def test_status_reports_the_keyword_index_without_creating_it(env):
    status = env.search().keyword_index_status()
    assert status["enabled"] is True and status["exists"] is False
    assert not (env.tmp_path / "kw.sqlite3").exists()
    env.search().update_keyword_index()
    status = env.search().keyword_index_status()
    assert status["items"] == 2 and status["passages"] >= 2


# ---------------------------------------------------------------------------
# zotero-mcp update-db
# ---------------------------------------------------------------------------


class _RecordingSearch:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []

    def update_keyword_index(self, **kwargs):
        self.calls.append(("keyword", kwargs))
        return {"total_items": 2, "indexed_items": 2, "passages_written": 9,
                "index": {"items": 2, "passages": 9, "size_bytes": 4096, "path": "/x"}}

    def update_database(self, **kwargs):
        self.calls.append(("database", kwargs))
        return {}


def _run_update_db(argv, monkeypatch, config):
    from zotero_mcp import cli

    made = []

    def factory(config_path, **kwargs):
        made.append(_RecordingSearch(config_path=config_path, **kwargs))
        return made[-1]

    monkeypatch.setattr(cli, "setup_zotero_environment", lambda *a, **kw: None)
    # By name: another test may have reloaded the module since this one imported it.
    monkeypatch.setattr("zotero_mcp.semantic_search.create_semantic_search", factory)
    # The environment, not a patched function: the CLI imports is_local_mode
    # at call time, possibly from a module another test has reloaded.
    monkeypatch.setenv("ZOTERO_LOCAL", "true")
    args = cli._build_parser().parse_args(["update-db", "--config-path", str(config), *argv])
    cli.run_update_db(args)
    return made


def test_update_db_keyword_only_flag_opens_no_embedding_index(env, monkeypatch, capsys):
    made = _run_update_db(["--keyword-only", "--force-rebuild"], monkeypatch, env.config)

    (search,) = made
    assert search.kwargs["keyword_only"] is True
    assert search.calls == [("keyword", {"force_rebuild": True, "limit": None, "allow_mass_deletion": False})]
    out = capsys.readouterr().out
    assert "Keyword index update completed" in out
    assert "Passages written: 9" in out


def test_update_db_follows_keyword_only_in_config(env, monkeypatch):
    cfg = json.loads(env.config.read_text())
    cfg["semantic_search"]["keyword_index"]["keyword_only"] = True
    env.config.write_text(json.dumps(cfg))

    (search,) = _run_update_db([], monkeypatch, env.config)

    assert search.kwargs["keyword_only"] is True
    assert [name for name, _ in search.calls] == ["keyword"]


def test_update_db_keyword_only_refuses_web_mode(env, monkeypatch, capsys):
    from zotero_mcp import cli

    monkeypatch.setattr(cli, "setup_zotero_environment", lambda *a, **kw: None)
    monkeypatch.setenv("ZOTERO_LOCAL", "false")
    args = cli._build_parser().parse_args(["update-db", "--config-path", str(env.config), "--keyword-only"])
    with pytest.raises(SystemExit) as exit_info:
        cli.run_update_db(args)
    assert exit_info.value.code == 1
    assert "local mode" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# End to end: a real zotero.sqlite, a real PDF, the real reader
# ---------------------------------------------------------------------------


def test_end_to_end_with_the_real_reader_and_a_real_pdf(tmp_path, monkeypatch):
    import sqlite3

    fitz = pytest.importorskip("fitz")
    from _search_corpus import Item, build_sqlite

    db_path = tmp_path / "zotero" / "zotero.sqlite"
    db_path.parent.mkdir()
    build_sqlite(db_path, [
        Item(key="BOOKKEY1", item_type="book", title="Lectures on Functional Analysis",
             date="1999-01-01 1999", creators=[("Gerald", "Folland")]),
        Item(key="PAPERKEY", title="A paper without a file about Gronwall"),
    ])
    con = sqlite3.connect(db_path)
    con.execute("INSERT INTO items (itemID, key, itemTypeID, libraryID, dateAdded, dateModified) "
                "VALUES (10, 'ATTKEY01', 2, 1, '2024-01-01 00:00:00', '2024-01-01 00:00:00')")
    con.execute("CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY, parentItemID INTEGER, "
                "linkMode INTEGER, contentType TEXT, charsetID INTEGER, path TEXT)")
    con.execute("INSERT INTO itemAttachments VALUES (10, 1, 0, 'application/pdf', NULL, 'storage:book.pdf')")
    con.commit()
    con.close()

    storage = db_path.parent / "storage" / "ATTKEY01"
    storage.mkdir(parents=True)
    doc = fitz.open()
    for text in (
        "Preface. These lectures cover normed spaces.",
        "Chapter one. Normed spaces and the Hahn-Banach extension theorem.",
        "Theorem 5.13 (Banach-Steinhaus). A pointwise bounded family of operators is uniformly bounded.",
    ):
        page = doc.new_page()
        page.insert_text((72, 72), text, fontsize=10)
    doc.save(storage / "book.pdf")
    doc.close()

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"semantic_search": {
        "zotero_db_path": str(db_path),
        # Small passages, so each page's text lands in a passage of its own.
        "chunking": {"chunk_size": 80, "overlap": 10},
        "keyword_index": {"keyword_only": True, "path": str(tmp_path / "kw.sqlite3")},
    }}))
    monkeypatch.setattr(semantic_search, "_update_lock_path", lambda: tmp_path / "update.lock")
    monkeypatch.setattr(semantic_search, "get_zotero_client", lambda: object())
    monkeypatch.setattr(semantic_search, "is_local_mode", lambda: True)

    search = semantic_search.ZoteroSemanticSearch(config_path=str(config), keyword_only=True)
    stats = search.update_database()

    assert "error" not in stats, stats
    assert stats["indexed_items"] == 1 and stats["metadata_only_items"] == 1
    with KeywordIndex(tmp_path / "kw.sqlite3", create=False) as idx:
        (hit,) = idx.search("Banach-Steinhaus uniformly bounded")["results"]
        assert hit["item_key"] == "BOOKKEY1"
        assert hit["attachment_key"] == "ATTKEY01"
        assert hit["page"] == 3 and hit["page_end"] == 3
        assert hit["fulltext_source"] == "pdf"
        assert hit["title"] == "Lectures on Functional Analysis"
        (hit,) = idx.search('"Hahn Banach extension"')["results"]
        assert hit["page"] == 2
        assert hit["creators"][0]["lastName"] == "Folland"
        assert idx.item_state("BOOKKEY1")["attachment_keys"] == "ATTKEY01"

    # Nothing changed: a second run reads no PDF.
    stats = search.update_database()
    assert stats["unchanged_items"] == 2 and stats["indexed_items"] == 0
