"""Keep the items of named Zotero collections out of the semantic index.

``semantic_search.exclude_collections`` (and ``ZOTERO_EXCLUDE_COLLECTIONS``)
names collections, by name or 8-character key, whose items — and everything
filed in their subcollections — ``update-db`` must never index: no metadata
record, no passages, and no attachment download or full-text fetch on the way.
The use case is confidential material (manuscripts under peer review, say)
that must not reach a machine that builds the index, such as a rented GPU
indexing through the web API.

It is a confidentiality control, so it fails closed: an entry that matches no
collection, a collection list or membership that cannot be fetched, or a
setting that cannot be read stops the update with
:class:`ExcludedCollectionsError` before anything is indexed. It never
degrades to "no exclusion".

Scope is the index only. The direct tools (metadata, reading PDFs, Zotero's
own search) still see these items.

This module imports nothing heavy (no chromadb, no pyzotero) so the status
commands can describe the setting cheaply (#445).
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

CONFIG_KEY = "exclude_collections"
ENV_VAR = "ZOTERO_EXCLUDE_COLLECTIONS"
PERSONAL_LIBRARY = 0

# "library:<group id>/<name or key>" pins an entry to one library; 0 is the
# personal library. Anything else applies to every library a run indexes.
_LIBRARY_PREFIX = re.compile(r"^library:(\d+)/(.+)$", re.DOTALL)


class ExcludedCollectionsError(RuntimeError):
    """The exclusion cannot be applied; the update must not go ahead."""


# ---------------------------------------------------------------------------
# Reading the setting
# ---------------------------------------------------------------------------


def _entries_from_value(value: Any, source: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ExcludedCollectionsError(
            f"{source} must be a list of collection names or keys, not {value!r}; "
            "refusing to update the index without the exclusion."
        )
    return [v.strip() for v in value if v.strip()]


def _env_entries(environ: Mapping[str, str]) -> list[str]:
    raw = environ.get(ENV_VAR, "")
    if not raw.strip():
        return []
    if raw.lstrip().startswith("["):
        try:
            value = json.loads(raw)
        except ValueError as e:
            raise ExcludedCollectionsError(
                f"{ENV_VAR} starts with '[' but is not a JSON list ({e}); "
                "refusing to update the index without the exclusion."
            ) from None
        return _entries_from_value(value, ENV_VAR)
    return [part.strip() for part in raw.split(",") if part.strip()]


def configured_entries(
    config_path: str | os.PathLike | None,
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    """Every configured entry: the config file's, then the environment's.

    The environment variable adds to the config file rather than replacing
    it, so nothing set in a shell can switch an exclusion off. A config file
    that exists but cannot be read, or cannot be parsed while it mentions
    this setting, raises: the rest of the update falls back to defaults on
    a broken file, which here would mean indexing what the user excluded.
    """
    environ = os.environ if environ is None else environ
    entries: list[str] = []
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                text = f.read()
        except OSError as e:
            raise ExcludedCollectionsError(
                f"Cannot read {config_path} to check semantic_search.{CONFIG_KEY}: {e}"
            ) from None
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError:
            if CONFIG_KEY in text:
                raise ExcludedCollectionsError(
                    f"{config_path} is not valid JSON and mentions {CONFIG_KEY}; "
                    "refusing to update the index without the exclusion. Fix the file and rerun."
                ) from None
            data = {}
        if isinstance(data, dict):
            if CONFIG_KEY in data:
                raise ExcludedCollectionsError(
                    f"{CONFIG_KEY} in {config_path} is at the top level; it belongs under "
                    f"\"semantic_search\". Refusing to update the index until it is moved."
                )
            section = data.get("semantic_search")
            if isinstance(section, dict):
                entries += _entries_from_value(
                    section.get(CONFIG_KEY), f"semantic_search.{CONFIG_KEY}"
                )
    entries += _env_entries(environ)
    return list(dict.fromkeys(entries))


def describe_setting(config: Mapping[str, Any], environ: Mapping[str, str] | None = None) -> str:
    """One line for the status commands: the active entries, or "none".

    ``config`` is the ``semantic_search`` section. Never raises: a value that
    :func:`configured_entries` would refuse is shown as invalid.
    """
    environ = os.environ if environ is None else environ
    try:
        entries = _entries_from_value(config.get(CONFIG_KEY), f"semantic_search.{CONFIG_KEY}")
        entries += _env_entries(environ)
    except ExcludedCollectionsError:
        return "invalid setting (update-db will refuse to run)"
    entries = list(dict.fromkeys(entries))
    return ", ".join(entries) if entries else "none"


def describe_config_file(config_path: str | os.PathLike | None) -> str:
    """:func:`describe_setting` for a config file; never raises."""
    try:
        configured_entries(config_path, environ={})
        with open(config_path, encoding="utf-8") as f:
            section = (json.load(f) or {}).get("semantic_search") or {}
    except ExcludedCollectionsError:
        return "invalid setting (update-db will refuse to run)"
    except (OSError, TypeError, ValueError, AttributeError):
        section = {}
    return describe_setting(section if isinstance(section, dict) else {})


# ---------------------------------------------------------------------------
# Resolving entries to collections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    raw: str
    target: str
    library: int | None  # None: every library the run indexes


def parse_entry(raw: str) -> Entry:
    match = _LIBRARY_PREFIX.match(raw)
    if match:
        return Entry(raw, match.group(2).strip(), int(match.group(1)))
    return Entry(raw, raw.strip(), None)


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", text or "").strip().casefold()


@dataclass(frozen=True)
class CollectionRow:
    group_id: int
    key: str
    name: str
    parent_key: str | None


@dataclass(frozen=True)
class Match:
    entry: str
    group_id: int
    key: str
    name: str
    subcollections: int


def library_label(group_id: int) -> str:
    return "My Library" if group_id == PERSONAL_LIBRARY else f"group {group_id}"


@dataclass
class Exclusions:
    """What one update run keeps out of the index, per library (group_id)."""

    entries: list[str]
    libraries: set[int]
    matches: list[Match] = field(default_factory=list)
    # Excluded collections, subcollections included.
    collection_keys: dict[int, set[str]] = field(default_factory=dict)
    # Items filed in them (top-level items), and those items' children.
    top_level_keys: dict[int, set[str]] = field(default_factory=dict)
    child_keys: dict[int, set[str]] = field(default_factory=dict)
    # Entries pinned to a library this run does not index.
    not_applicable: list[str] = field(default_factory=list)

    def item_keys(self, group_id: int) -> set[str]:
        return self.top_level_keys.get(group_id, set()) | self.child_keys.get(group_id, set())

    def excludes(
        self,
        group_id: int | None,
        key: str | None,
        collections: Iterable[str] | None = None,
        parent_key: str | None = None,
    ) -> bool:
        """Whether an item stays out of the index.

        By its own key or its parent's, or — for an item fetched after the
        membership was read, say just filed — by the collections its own data
        lists. ``group_id`` None (library unknown) checks every library.
        """
        groups = self.libraries if group_id is None else {group_id}
        for group in groups:
            keys = self.item_keys(group)
            if (key and key in keys) or (parent_key and parent_key in keys):
                return True
            excluded_collections = self.collection_keys.get(group, set())
            if collections and excluded_collections.intersection(collections):
                return True
        return False

    @property
    def excluded_item_count(self) -> int:
        return sum(len(keys) for keys in self.top_level_keys.values())

    def merge(self, other: Exclusions) -> Exclusions:
        """The union of two resolutions: nothing either excluded comes back."""
        merged = Exclusions(
            entries=list(dict.fromkeys(self.entries + other.entries)),
            libraries=self.libraries | other.libraries,
            matches=list(dict.fromkeys(self.matches + other.matches)),
            not_applicable=[
                e for e in dict.fromkeys(self.not_applicable + other.not_applicable)
                if not any(m.entry == e for m in self.matches + other.matches)
            ],
        )
        for attr in ("collection_keys", "top_level_keys", "child_keys"):
            combined: dict[int, set[str]] = {}
            for source in (getattr(self, attr), getattr(other, attr)):
                for group, keys in source.items():
                    combined.setdefault(group, set()).update(keys)
            setattr(merged, attr, combined)
        return merged

    def summary(self) -> str:
        """``update-db``'s line: how many items, and by which collections."""
        parts = []
        for m in self.matches:
            sub = f", with {m.subcollections} subcollection{'s' if m.subcollections != 1 else ''}" if m.subcollections else ""
            parts.append(f"'{m.name}' ({m.key} in {library_label(m.group_id)}{sub}) for {m.entry!r}")
        count = self.excluded_item_count
        line = (
            f"Excluding {count} item{'s' if count != 1 else ''} from the index "
            f"(exclude_collections): {'; '.join(parts)}."
        )
        if self.not_applicable:
            line += f" Not applicable to the libraries indexed here: {', '.join(self.not_applicable)}."
        return line

    def as_stats(self) -> list[dict[str, Any]]:
        return [
            {
                "entry": m.entry,
                "collection_key": m.key,
                "collection_name": m.name,
                "library": m.group_id,
                "subcollections": m.subcollections,
            }
            for m in self.matches
        ]


def resolve_collections(
    entries: list[str],
    collections: Iterable[CollectionRow],
    libraries: set[int],
) -> tuple[list[Match], dict[int, set[str]], list[str]]:
    """Match each entry to collections and expand each match to its subtree.

    An entry matches a collection whose key or name equals it, ignoring case;
    it may match several (same name in two places, or in two libraries), and
    all are excluded. An entry that matches nothing in the libraries it
    applies to raises: a misspelt name must not quietly exclude nothing.
    """
    rows = [c for c in collections if c.group_id in libraries]
    children: dict[tuple[int, str], list[CollectionRow]] = {}
    for row in rows:
        if row.parent_key:
            children.setdefault((row.group_id, row.parent_key), []).append(row)

    matches: list[Match] = []
    excluded: dict[int, set[str]] = {}
    unresolved: list[str] = []
    not_applicable: list[str] = []
    for raw in entries:
        entry = parse_entry(raw)
        if entry.library is not None and entry.library not in libraries:
            not_applicable.append(raw)
            continue
        scope = libraries if entry.library is None else {entry.library}
        target = _fold(entry.target)
        hits = [
            r for r in rows
            if r.group_id in scope and target in (_fold(r.key), _fold(r.name))
        ]
        if not hits:
            unresolved.append(raw)
            continue
        for hit in hits:
            subtree: list[str] = []
            seen: set[str] = set()
            stack = [hit]
            while stack:  # bounded by `seen` on a parentCollection cycle (#451)
                row = stack.pop()
                if row.key in seen:
                    continue
                seen.add(row.key)
                subtree.append(row.key)
                stack.extend(children.get((row.group_id, row.key), []))
            excluded.setdefault(hit.group_id, set()).update(subtree)
            matches.append(Match(raw, hit.group_id, hit.key, hit.name, len(subtree) - 1))

    if unresolved:
        searched = ", ".join(library_label(g) for g in sorted(libraries)) or "no library"
        raise ExcludedCollectionsError(
            f"exclude_collections: no collection named or keyed {', '.join(map(repr, unresolved))} "
            f"in the libraries this update indexes ({searched}). Refusing to update the index "
            "rather than index without the exclusion. Check the spelling, or, if the collection "
            "lives in another library, pin the entry to it as 'library:<group id>/<name>' "
            "(library:0/ is My Library)."
        )
    return matches, excluded, not_applicable


def build_exclusions(
    entries: list[str],
    collections: Iterable[CollectionRow],
    libraries: set[int],
    members: Callable[[int, set[str]], tuple[set[str], set[str]]],
) -> Exclusions:
    """Resolve ``entries`` and read each library's excluded items.

    ``members(group_id, collection_keys)`` returns ``(top-level item keys,
    child item keys)`` filed in those collections; any error it raises
    stops the update.
    """
    matches, collection_keys, not_applicable = resolve_collections(entries, collections, libraries)
    exclusions = Exclusions(
        entries=list(entries),
        libraries=set(libraries),
        matches=matches,
        collection_keys=collection_keys,
        not_applicable=not_applicable,
    )
    for group_id, keys in collection_keys.items():
        try:
            top, kids = members(group_id, keys)
        except ExcludedCollectionsError:
            raise
        except Exception as e:
            raise ExcludedCollectionsError(
                f"exclude_collections: could not read which items are in the excluded "
                f"collections of {library_label(group_id)} ({e}). Refusing to update the index "
                "rather than index without the exclusion."
            ) from e
        exclusions.top_level_keys[group_id] = set(top)
        exclusions.child_keys[group_id] = set(kids)
    return exclusions


# ---------------------------------------------------------------------------
# Sources: the Zotero API (web or local) and the local SQLite database
# ---------------------------------------------------------------------------


def resolve_via_api(entries: list[str], zot: Any, group_id: int, paginate: Callable) -> Exclusions:
    """Resolve against the one library ``zot`` is scoped to.

    Collections and each excluded collection's items are read in full pages
    through ``paginate`` (``utils._paginate``). Children are not listed: on
    this path an attachment is only ever reached through its parent, which
    is excluded.
    """
    try:
        rows = paginate(zot.collections) or []
    except Exception as e:
        raise ExcludedCollectionsError(
            f"exclude_collections: could not list the collections of {library_label(group_id)} "
            f"({e}). Refusing to update the index rather than index without the exclusion."
        ) from e
    collections = []
    for row in rows:
        data = row.get("data") or {}
        key = row.get("key") or data.get("key")
        if key:
            collections.append(
                CollectionRow(group_id, key, data.get("name") or "", data.get("parentCollection") or None)
            )

    def members(_group: int, keys: set[str]) -> tuple[set[str], set[str]]:
        top: set[str] = set()
        for key in sorted(keys):
            for item in paginate(zot.collection_items_top, key) or []:
                item_key = item.get("key") or (item.get("data") or {}).get("key")
                if item_key:
                    top.add(item_key)
        return top, set()

    return build_exclusions(entries, collections, {group_id}, members)


def resolve_via_local(entries: list[str], reader: Any) -> Exclusions:
    """Resolve against every library in the local database.

    The local scan indexes every user and group library in ``zotero.sqlite``
    in one run, so an unpinned entry is looked up in all of them.
    """
    libraries = {group_id for group_id, _name in reader.get_library_labels().values()}
    collections = [
        CollectionRow(int(r["group_id"]), r["key"], r["name"] or "", r["parent_key"])
        for r in reader.get_collection_tree()
    ]
    return build_exclusions(entries, collections, libraries, reader.get_collection_member_keys)
