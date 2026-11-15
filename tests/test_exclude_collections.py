"""`semantic_search.exclude_collections` keeps named collections out of the index.

The setting is a confidentiality control (manuscripts under review, indexed on
a rented machine through the web API), so these tests check what is *fetched*,
not only what is embedded: an excluded item's attachments are never listed
under it and its full text is never requested or extracted. And it fails
closed: anything that stops the exclusion from being applied stops the run
before anything is indexed.

The library used throughout:

    _review (REVIEW01)            -> CONF1, BOTH1
    └── round 2 (REVIEW02)        -> CONF2
    Reading (READ0001)            -> OPEN1, BOTH1
    unfiled                       -> OPEN2
"""

from __future__ import annotations

import json
import sqlite3
import sys

import pytest

if sys.version_info >= (3, 14):
    pytest.skip(
        "chromadb currently relies on pydantic v1 paths that are incompatible with Python 3.14+",
        allow_module_level=True,
    )

from zotero_mcp import index_exclusions, semantic_search
from zotero_mcp.index_exclusions import (
    CollectionRow,
    ExcludedCollectionsError,
    configured_entries,
    resolve_collections,
)
from zotero_mcp.local_db import LocalZoteroReader

REVIEW, ROUND2, READING = "REVIEW01", "REVIEW02", "READ0001"
EXCLUDED = {"CONF1", "CONF2", "BOTH1"}
KEPT = {"OPEN1", "OPEN2"}

COLLECTIONS = [
    {"key": REVIEW, "data": {"key": REVIEW, "name": "_review", "parentCollection": False}},
    {"key": ROUND2, "data": {"key": ROUND2, "name": "round 2", "parentCollection": REVIEW}},
    {"key": READING, "data": {"key": READING, "name": "Reading", "parentCollection": False}},
]
MEMBERS = {REVIEW: ["CONF1", "BOTH1"], ROUND2: ["CONF2"], READING: ["OPEN1", "BOTH1"]}


def _paper(key, collections=(), version=1):
    return {
        "key": key,
        "version": version,
        "data": {
            "key": key,
            "itemType": "journalArticle",
            "title": f"Title {key}",
            "abstractNote": f"Abstract {key}",
            "creators": [],
            "collections": list(collections),
            "dateAdded": "2026-01-01T00:00:00Z",
            "dateModified": "2026-01-01T00:00:00Z",
        },
    }


def _pdf(key, parent):
    return {
        "key": key,
        "version": 1,
        "data": {"key": key, "itemType": "attachment", "contentType": "application/pdf", "parentItem": parent},
    }


def _collections_of(key):
    return [c for c, members in MEMBERS.items() if key in members]


class FakeZotero:
    """The pyzotero surface update_database uses, recording every call."""

    library_type = "users"
    library_id = "1"

    def __init__(self, items, members=MEMBERS, collections=COLLECTIONS, version=10):
        self.items_by_key = {it["key"]: it for it in items}
        self.order = [it["key"] for it in items]
        self.members = members
        self.collection_rows = collections
        self.children_by_parent = {
            it["key"]: [_pdf(f"PDF{it['key']}", it["key"])] for it in items
        }
        self.versions = {it["key"]: it.get("version", 1) for it in items}
        self.version = version
        self.calls = []
        self.fail_collections = False

    def collections(self, start=0, limit=100, **kwargs):
        self.calls.append(("collections", start))
        if self.fail_collections:
            raise ConnectionError("collections listing failed")
        return self.collection_rows[start:start + limit]

    def collection_items_top(self, key, start=0, limit=100, **kwargs):
        self.calls.append(("collection_items_top", key, start))
        return [_paper(k) for k in self.members.get(key, [])][start:start + limit]

    def items(self, start=0, limit=100, **kwargs):
        if kwargs.get("itemType") == "attachment":
            self.calls.append(("attachments", start))
            every = [c for kids in self.children_by_parent.values() for c in kids]
            return every[start:start + limit]
        self.calls.append(("items", start))
        return [self.items_by_key[k] for k in self.order[start:start + limit]]

    def item(self, key):
        self.calls.append(("item", key))
        return self.items_by_key[key]

    def children(self, key, start=0, limit=100, **kwargs):
        self.calls.append(("children", key))
        return self.children_by_parent.get(key, [])[start:start + limit]

    def fulltext_item(self, key):
        self.calls.append(("fulltext_item", key))
        return {"content": f"Full text of {key}"}

    def item_versions(self, since=None, **kwargs):
        if since is None:
            return dict(self.versions)
        return {k: v for k, v in self.versions.items() if v > since}

    def last_modified_version(self, **kwargs):
        return self.version

    # What the tests assert on.
    def asked_about(self, call):
        return {c[1] for c in self.calls if c[0] == call}


class FakeChroma:
    embedding_max_tokens = 8000

    def __init__(self, stored=None):
        # doc id -> metadata; "<key>#<n>" ids are passages.
        self.metas = dict(stored or {})
        self.upserted = []
        self.deleted = []
        self.reset_calls = 0

    def read_error(self):
        return None

    def truncate_text(self, text, max_tokens=None):
        return text

    def get_all_ids(self, where=None):
        if where and "group_id" in where:
            return {i for i, m in self.metas.items() if m.get("group_id") == where["group_id"]}
        return set(self.metas)

    def get_existing_ids(self, ids):
        return {i for i in ids if i in self.metas}

    def get_document_metadata(self, doc_id):
        return self.metas.get(doc_id) or self.metas.get(f"{doc_id}#0")

    def iter_metadatas(self, batch_size=500):
        return iter(())

    def update_metadatas(self, ids, metadatas):
        pass

    def upsert_documents(self, documents, metadatas, ids):
        self.upserted.extend(zip(ids, documents))
        for i, m in zip(ids, metadatas):
            self.metas[i] = dict(m)

    def delete_documents(self, ids):
        self.deleted.extend(ids)
        for i in ids:
            self.metas.pop(i, None)

    def delete_item_chunks(self, item_key, group_id=None):
        pass

    def reset_collection(self):
        self.reset_calls += 1
        self.metas = {}


def indexed_keys(chroma):
    return {doc_id.split("#", 1)[0] for doc_id, _doc in chroma.upserted}


def _config(tmp_path, exclude=("_review",), **extra):
    section = {
        "embedding_model": "default",
        "update_config": {"auto_update": False, "update_frequency": "manual"},
        "include_fulltext": True,
        **extra,
    }
    if exclude is not None:
        section["exclude_collections"] = list(exclude)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"semantic_search": section}))
    return str(path)


def _search(monkeypatch, zot, chroma, config_path, local=False):
    monkeypatch.setattr(semantic_search, "get_zotero_client", lambda: zot)
    monkeypatch.setattr(semantic_search, "is_local_mode", lambda: local)
    monkeypatch.delenv(index_exclusions.ENV_VAR, raising=False)
    return semantic_search.ZoteroSemanticSearch(chroma_client=chroma, config_path=config_path)


def _library():
    return [
        _paper(k, _collections_of(k)) for k in ("CONF1", "CONF2", "BOTH1", "OPEN1", "OPEN2")
    ]


def _assert_never_fetched(zot, keys):
    """No request about an excluded item's attachments or text was made."""
    pdfs = {f"PDF{k}" for k in keys}
    assert not (zot.asked_about("children") & keys)
    assert not (zot.asked_about("fulltext_item") & (pdfs | keys))


# ---------------------------------------------------------------------------
# Reading the setting
# ---------------------------------------------------------------------------


def test_config_and_environment_add_up(tmp_path):
    path = _config(tmp_path, exclude=["_review"])
    entries = configured_entries(path, environ={"ZOTERO_EXCLUDE_COLLECTIONS": "Drafts, ABCD2345"})
    assert entries == ["_review", "Drafts", "ABCD2345"]


def test_environment_takes_a_json_list_for_names_with_commas(tmp_path):
    entries = configured_entries(None, environ={"ZOTERO_EXCLUDE_COLLECTIONS": '["Smith, review"]'})
    assert entries == ["Smith, review"]


@pytest.mark.parametrize("value", ["_review", {"name": "_review"}, [1]])
def test_a_value_that_is_not_a_list_of_strings_refuses(tmp_path, value):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"semantic_search": {"exclude_collections": value}}))
    with pytest.raises(ExcludedCollectionsError):
        configured_entries(str(path), environ={})


def test_setting_at_the_top_level_refuses(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"exclude_collections": ["_review"]}))
    with pytest.raises(ExcludedCollectionsError, match="semantic_search"):
        configured_entries(str(path), environ={})


def test_broken_config_that_mentions_the_setting_refuses(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"semantic_search": {"exclude_collections": ["_review"],}}')
    with pytest.raises(ExcludedCollectionsError):
        configured_entries(str(path), environ={})


def test_broken_config_without_the_setting_is_no_exclusion(tmp_path):
    """Users who never set it see no change on a broken file."""
    path = tmp_path / "config.json"
    path.write_text("{not json")
    assert configured_entries(str(path), environ={}) == []


# ---------------------------------------------------------------------------
# Resolving names and keys
# ---------------------------------------------------------------------------


def _rows(group_id=0):
    return [
        CollectionRow(group_id, c["key"], c["data"]["name"], c["data"]["parentCollection"] or None)
        for c in COLLECTIONS
    ]


def test_name_resolves_with_its_subcollections():
    matches, keys, _ = resolve_collections(["_review"], _rows(), {0})
    assert keys == {0: {REVIEW, ROUND2}}
    assert [(m.key, m.subcollections) for m in matches] == [(REVIEW, 1)]


def test_key_and_case_insensitive_name_both_resolve():
    _, by_key, _ = resolve_collections([ROUND2], _rows(), {0})
    _, by_name, _ = resolve_collections(["_REVIEW"], _rows(), {0})
    assert by_key == {0: {ROUND2}}
    assert by_name == {0: {REVIEW, ROUND2}}


def test_a_name_matching_several_collections_excludes_all():
    rows = _rows(0) + [CollectionRow(0, "OTHERREV", "_review", READING)] + _rows(555)
    matches, keys, _ = resolve_collections(["_review"], rows, {0, 555})
    assert keys[0] == {REVIEW, ROUND2, "OTHERREV"}
    assert keys[555] == {REVIEW, ROUND2}
    assert len(matches) == 3


def test_unresolvable_name_raises_and_names_the_libraries():
    with pytest.raises(ExcludedCollectionsError, match=r"'_reveiw'.*My Library"):
        resolve_collections(["_review", "_reveiw"], _rows(), {0})


def test_entry_pinned_to_a_library():
    rows = _rows(0) + [CollectionRow(555, "GRPDRAFT", "Drafts", None)]
    _, keys, not_applicable = resolve_collections(["library:555/Drafts"], rows, {0, 555})
    assert keys == {555: {"GRPDRAFT"}}
    # A run that does not index that library has nothing to resolve.
    _, keys, not_applicable = resolve_collections(["library:555/Drafts"], rows, {0})
    assert keys == {} and not_applicable == ["library:555/Drafts"]
    # A run that does must find it there, not in another library.
    with pytest.raises(ExcludedCollectionsError):
        resolve_collections(["library:0/Drafts"], rows, {0, 555})


def test_parent_cycle_terminates():
    rows = [CollectionRow(0, "A", "a", "B"), CollectionRow(0, "B", "b", "A")]
    _, keys, _ = resolve_collections(["a"], rows, {0})
    assert keys == {0: {"A", "B"}}


# ---------------------------------------------------------------------------
# Web API (and local API: same client surface)
# ---------------------------------------------------------------------------


def test_full_scan_excludes_collection_subcollection_and_doubly_filed_items(monkeypatch, tmp_path):
    zot = FakeZotero(_library())
    chroma = FakeChroma()
    search = _search(monkeypatch, zot, chroma, _config(tmp_path))

    stats = search.update_database()

    assert "error" not in stats
    assert indexed_keys(chroma) == KEPT
    _assert_never_fetched(zot, EXCLUDED)
    assert zot.asked_about("children") == KEPT
    assert stats["excluded_items"] == 3
    assert [c["collection_key"] for c in stats["excluded_collections"]] == [REVIEW]


def test_bulk_attachment_walk_never_reads_excluded_text(monkeypatch, tmp_path):
    """At 25+ items the scan lists attachments once; excluded parents still get nothing."""
    items = _library() + [_paper(f"MORE{i:04d}") for i in range(30)]
    zot = FakeZotero(items)
    chroma = FakeChroma()
    search = _search(monkeypatch, zot, chroma, _config(tmp_path))

    search.update_database()

    assert ("attachments", 0) in zot.calls
    assert indexed_keys(chroma) == {it["key"] for it in items} - EXCLUDED
    _assert_never_fetched(zot, EXCLUDED)


def test_collection_listing_and_membership_are_paged(monkeypatch, tmp_path):
    """Past the first page of 100 collections, and of 100 items in one."""
    filler = [
        {"key": f"FILL{i:04d}", "data": {"key": f"FILL{i:04d}", "name": f"f{i}", "parentCollection": False}}
        for i in range(120)
    ]
    collections = filler + COLLECTIONS  # _review is on the second page
    big = [f"BIG{i:05d}" for i in range(130)]
    members = {**MEMBERS, ROUND2: ["CONF2", *big]}
    items = _library() + [_paper(k) for k in big]  # filed, but their data lists no collection
    zot = FakeZotero(items, members=members, collections=collections)
    chroma = FakeChroma()
    search = _search(monkeypatch, zot, chroma, _config(tmp_path))

    stats = search.update_database()

    assert ("collections", 100) in zot.calls
    assert ("collection_items_top", ROUND2, 100) in zot.calls
    assert stats["excluded_items"] == 3 + 130
    assert indexed_keys(chroma) == KEPT
    _assert_never_fetched(zot, EXCLUDED | set(big))


def test_incremental_sync_never_fetches_an_excluded_change(monkeypatch, tmp_path):
    items = _library()
    for it in items:
        it["version"] = 9 if it["key"] in {"CONF1", "OPEN1"} else 3
    zot = FakeZotero(items)
    chroma = FakeChroma({"OPEN2": {"item_key": "OPEN2", "group_id": 0}})
    search = _search(monkeypatch, zot, chroma, _config(tmp_path, last_sync_versions={"0": 5}))

    stats = search.update_database()

    assert "error" not in stats
    assert indexed_keys(chroma) == {"OPEN1"}
    assert "CONF1" not in zot.asked_about("item")
    _assert_never_fetched(zot, EXCLUDED)


def test_item_filed_after_the_membership_was_read_is_caught_by_its_own_collections(monkeypatch, tmp_path):
    """The item's data lists the excluded subcollection though the listing missed it."""
    late = _paper("LATE1", [ROUND2], version=9)
    zot = FakeZotero(_library() + [late])
    chroma = FakeChroma()
    search = _search(monkeypatch, zot, chroma, _config(tmp_path, last_sync_versions={"0": 5}))

    search.update_database()

    assert "LATE1" in zot.asked_about("item")  # its metadata, to read its collections
    assert "LATE1" not in indexed_keys(chroma)
    _assert_never_fetched(zot, {"LATE1"})


def test_previously_indexed_excluded_items_are_purged_at_every_level(monkeypatch, tmp_path):
    stored = {
        "CONF1": {"item_key": "CONF1", "group_id": 0},
        "CONF1#0": {"parent_item_key": "CONF1", "group_id": 0},
        "CONF1#1": {"parent_item_key": "CONF1", "group_id": 0},
        "CONF2#0": {"parent_item_key": "CONF2", "group_id": 0},
        "BOTH1": {"item_key": "BOTH1"},  # indexed before #163: no library
        "OPEN1": {"item_key": "OPEN1", "group_id": 0},
        "CONF1-in-group": {"item_key": "CONF1", "group_id": 555},  # another library's doc
    }
    zot = FakeZotero(_library())
    chroma = FakeChroma(stored)
    # Library unchanged since the last sync: the purge must not depend on changes.
    search = _search(monkeypatch, zot, chroma, _config(tmp_path, last_sync_versions={"0": 10}))

    stats = search.update_database()

    assert set(chroma.deleted) == {"CONF1", "CONF1#0", "CONF1#1", "CONF2#0", "BOTH1"}
    assert stats["purged_excluded_items"] == 3
    assert "OPEN1" in chroma.metas and "CONF1-in-group" in chroma.metas


def test_gap_fill_skips_excluded_items(monkeypatch, tmp_path):
    zot = FakeZotero(_library())
    chroma = FakeChroma()
    search = _search(monkeypatch, zot, chroma, _config(tmp_path))
    search._run_group_id = 0
    search._exclusions = search._resolve_exclusions(local_scan=False)

    items = search._get_gap_items_from_api(
        {"CONF1": {"passages"}, "OPEN1": {"passages"}},
        already_fetched=set(),
        library_keys=set(zot.versions),
        include_fulltext=True,
    )

    assert [it["key"] for it in items] == ["OPEN1"]
    assert "CONF1" not in zot.asked_about("item")
    _assert_never_fetched(zot, EXCLUDED)


def test_fulltext_fetch_refuses_an_excluded_item(monkeypatch, tmp_path):
    """The last guard: a caller that forgot to filter stops the run instead."""
    zot = FakeZotero(_library())
    search = _search(monkeypatch, zot, FakeChroma(), _config(tmp_path))
    search._run_group_id = 0
    search._exclusions = search._resolve_exclusions(local_scan=False)

    with pytest.raises(ExcludedCollectionsError):
        search._attach_web_fulltext([zot.items_by_key["OPEN1"], zot.items_by_key["CONF1"]])
    assert not zot.asked_about("children")


@pytest.mark.parametrize("force", [False, True])
def test_unresolvable_name_stops_before_anything_is_fetched_or_reset(monkeypatch, tmp_path, force):
    zot = FakeZotero(_library())
    chroma = FakeChroma({"OPEN1": {"item_key": "OPEN1", "group_id": 0}})
    search = _search(monkeypatch, zot, chroma, _config(tmp_path, exclude=["_reveiw"]))

    stats = search.update_database(force_full_rebuild=force)

    assert "_reveiw" in stats["error"]
    assert chroma.reset_calls == 0 and not chroma.upserted and not chroma.deleted
    assert not [c for c in zot.calls if c[0] not in ("collections",)]


def test_failed_collection_listing_stops_the_run(monkeypatch, tmp_path):
    zot = FakeZotero(_library())
    zot.fail_collections = True
    chroma = FakeChroma()
    search = _search(monkeypatch, zot, chroma, _config(tmp_path))

    stats = search.update_database()

    assert "could not list the collections" in stats["error"]
    assert not chroma.upserted and not zot.asked_about("items")


def test_failed_membership_listing_stops_the_run(monkeypatch, tmp_path):
    zot = FakeZotero(_library())

    def broken(key, start=0, limit=100, **kwargs):
        raise TimeoutError("read timed out")

    zot.collection_items_top = broken
    chroma = FakeChroma()
    search = _search(monkeypatch, zot, chroma, _config(tmp_path))

    stats = search.update_database()

    assert "could not read which items" in stats["error"]
    assert not chroma.upserted and not zot.asked_about("items")


def test_no_setting_makes_no_extra_requests(monkeypatch, tmp_path):
    zot = FakeZotero(_library())
    chroma = FakeChroma()
    search = _search(monkeypatch, zot, chroma, _config(tmp_path, exclude=None))

    stats = search.update_database()

    assert "excluded_items" not in stats
    assert indexed_keys(chroma) == EXCLUDED | KEPT
    assert not zot.asked_about("collections") and not zot.asked_about("collection_items_top")


# ---------------------------------------------------------------------------
# Local SQLite scan (`update-db --fulltext`)
# ---------------------------------------------------------------------------

SCHEMA = [
    "CREATE TABLE itemTypes (itemTypeID INTEGER PRIMARY KEY, typeName TEXT)",
    "CREATE TABLE items (itemID INTEGER PRIMARY KEY, itemTypeID INT, dateAdded TEXT, "
    "dateModified TEXT, clientDateModified TEXT, libraryID INT, key TEXT, version INT, synced INT)",
    "CREATE TABLE libraries (libraryID INTEGER PRIMARY KEY, type TEXT, editable INT, filesEditable INT)",
    "CREATE TABLE groups (groupID INTEGER PRIMARY KEY, libraryID INT UNIQUE, name TEXT, "
    "description TEXT, version INT)",
    "CREATE TABLE deletedItems (itemID INTEGER PRIMARY KEY)",
    "CREATE TABLE itemData (itemID INT, fieldID INT, valueID INT)",
    "CREATE TABLE itemDataValues (valueID INTEGER PRIMARY KEY, value TEXT)",
    "CREATE TABLE fields (fieldID INTEGER PRIMARY KEY, fieldName TEXT)",
    "CREATE TABLE baseFieldMappingsCombined (itemTypeID INT, baseFieldID INT, fieldID INT)",
    "CREATE TABLE itemNotes (itemID INT, parentItemID INT, note TEXT)",
    "CREATE TABLE itemCreators (itemID INT, creatorID INT)",
    "CREATE TABLE creators (creatorID INTEGER PRIMARY KEY, firstName TEXT, lastName TEXT)",
    "CREATE TABLE collections (collectionID INTEGER PRIMARY KEY, collectionName TEXT, "
    "parentCollectionID INT, libraryID INT, key TEXT)",
    "CREATE TABLE collectionItems (collectionID INT, itemID INT)",
    "CREATE TABLE itemAttachments (itemID INTEGER PRIMARY KEY, parentItemID INT, linkMode INT, "
    "contentType TEXT, path TEXT)",
    "CREATE TABLE itemAnnotations (itemID INTEGER PRIMARY KEY, parentItemID INT)",
]


def _make_db(path):
    """My Library holds the tree above; group 555 has its own '_review'."""
    conn = sqlite3.connect(path)
    for statement in SCHEMA:
        conn.execute(statement)
    conn.executemany("INSERT INTO itemTypes VALUES (?, ?)", [
        (1, "journalArticle"), (2, "attachment"), (3, "note"), (4, "annotation"),
    ])
    conn.execute("INSERT INTO libraries VALUES (1, 'user', 1, 1)")
    conn.execute("INSERT INTO libraries VALUES (2, 'group', 1, 1)")
    conn.execute("INSERT INTO groups VALUES (555, 2, 'Lab', '', 1)")
    collections = [
        (1, "_review", None, 1, REVIEW), (2, "round 2", 1, 1, ROUND2),
        (3, "Reading", None, 1, READING), (4, "_review", None, 2, "GRPREV01"),
    ]
    conn.executemany("INSERT INTO collections VALUES (?, ?, ?, ?, ?)", collections)
    ids = {}

    def add(key, type_id, library=1):
        ids[key] = len(ids) + 1
        conn.execute(
            "INSERT INTO items VALUES (?, ?, '2026-01-01 00:00:00', '2026-01-01 00:00:00', "
            "'2026-01-01 00:00:00', ?, ?, 1, 0)",
            (ids[key], type_id, library, key),
        )

    for key in ("CONF1", "CONF2", "BOTH1", "OPEN1", "OPEN2"):
        add(key, 1)
        add(f"PDF{key}", 2)
        conn.execute(
            "INSERT INTO itemAttachments VALUES (?, ?, 0, 'application/pdf', 'storage:paper.pdf')",
            (ids[f"PDF{key}"], ids[key]),
        )
    add("GCONF1", 1, library=2)
    add("GOPEN1", 1, library=2)
    add("NOTECONF1", 3)
    conn.execute("INSERT INTO itemNotes VALUES (?, ?, 'secret note')", (ids["NOTECONF1"], ids["CONF1"]))
    add("ANNCONF1", 4)
    conn.execute("INSERT INTO itemAnnotations VALUES (?, ?)", (ids["ANNCONF1"], ids["PDFCONF1"]))
    for collection_id, key in [
        (1, "CONF1"), (1, "BOTH1"), (2, "CONF2"), (3, "OPEN1"), (3, "BOTH1"), (4, "GCONF1"),
    ]:
        conn.execute("INSERT INTO collectionItems VALUES (?, ?)", (collection_id, ids[key]))
    conn.commit()
    conn.close()


class RecordingReader(LocalZoteroReader):
    """The real reader, recording which items it looks for attachments of or extracts."""

    looked_up: list[str] = []
    extracted: list[str] = []

    def get_fulltext_meta_for_item(self, item_id):
        key = self._get_connection().execute("SELECT key FROM items WHERE itemID = ?", (item_id,)).fetchone()[0]
        RecordingReader.looked_up.append(key)
        return super().get_fulltext_meta_for_item(item_id)

    def extract_fulltext_for_items(self, items):
        for item_id, key in items:
            RecordingReader.extracted.append(key)
            yield item_id, (f"text of {key}", "pdf")


def _local_search(monkeypatch, tmp_path, exclude=("_review",)):
    db = tmp_path / "zotero.sqlite"
    _make_db(db)
    RecordingReader.looked_up, RecordingReader.extracted = [], []
    monkeypatch.setattr(semantic_search, "LocalZoteroReader", RecordingReader)
    zot = FakeZotero(_library())
    chroma = FakeChroma()
    search = _search(monkeypatch, zot, chroma, _config(tmp_path, exclude=exclude), local=True)
    search.db_path = str(db)
    return search, chroma, db


def test_local_scan_never_looks_up_or_extracts_excluded_attachments(monkeypatch, tmp_path):
    search, chroma, _db = _local_search(monkeypatch, tmp_path)

    stats = search.update_database(extract_fulltext=True)

    assert "error" not in stats
    # Both libraries are scanned in one run; an unpinned name applies to both.
    assert indexed_keys(chroma) == KEPT | {"GOPEN1"}
    assert not (set(RecordingReader.looked_up) | set(RecordingReader.extracted)) & (EXCLUDED | {"GCONF1"})
    assert set(RecordingReader.extracted) == KEPT | {"GOPEN1"}
    assert stats["excluded_items"] == 4
    assert {(c["library"], c["collection_key"]) for c in stats["excluded_collections"]} == {
        (0, REVIEW), (555, "GRPREV01"),
    }


def test_local_scan_pinned_entry_leaves_other_libraries_alone(monkeypatch, tmp_path):
    search, chroma, _db = _local_search(monkeypatch, tmp_path, exclude=["library:555/_review"])

    search.update_database(extract_fulltext=True)

    assert indexed_keys(chroma) == EXCLUDED | KEPT | {"GOPEN1"}


def test_local_unresolvable_name_stops_before_the_scan(monkeypatch, tmp_path):
    search, chroma, _db = _local_search(monkeypatch, tmp_path, exclude=["nope"])

    stats = search.update_database(extract_fulltext=True)

    assert "nope" in stats["error"]
    assert not chroma.upserted and not RecordingReader.looked_up


def test_local_member_keys_include_children(tmp_path):
    db = tmp_path / "zotero.sqlite"
    _make_db(db)
    with LocalZoteroReader(db_path=str(db)) as reader:
        top, children = reader.get_collection_member_keys(0, {REVIEW, ROUND2})
    assert top == EXCLUDED
    assert children == {"PDFCONF1", "PDFCONF2", "PDFBOTH1", "NOTECONF1", "ANNCONF1"}


def test_local_scan_failure_falls_back_to_the_api_still_excluding(monkeypatch, tmp_path):
    search, chroma, db = _local_search(monkeypatch, tmp_path)
    search._run_group_id = 0
    search._exclusions = search._resolve_exclusions(local_scan=True)

    class Broken(RecordingReader):
        def get_items_with_text(self, *args, **kwargs):
            raise sqlite3.OperationalError("database disk image is malformed")

    monkeypatch.setattr(semantic_search, "LocalZoteroReader", Broken)
    items = search._get_items_from_local_db(extract_fulltext=True)

    assert {it["key"] for it in items} == KEPT


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def test_status_rows_show_the_active_exclusions(monkeypatch):
    from zotero_mcp.semantic_summary import semantic_config_rows

    monkeypatch.setenv("ZOTERO_EXCLUDE_COLLECTIONS", "Drafts")
    rows = dict(semantic_config_rows({"exclude_collections": ["_review"]}))
    assert rows["Excluded collections"] == "_review, Drafts"
    monkeypatch.delenv("ZOTERO_EXCLUDE_COLLECTIONS")
    assert dict(semantic_config_rows({}))["Excluded collections"] == "none"
