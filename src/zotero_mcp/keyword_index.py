"""Keyword (BM25) search over passages, in a SQLite FTS5 file.

Semantic passage search needs every passage embedded, which for a large
library is days of GPU time or a paid batch run. For finding a named result
(a theorem, a lemma, an inequality, an author's construction) plain BM25 over
the same passages does about as well, and needs no model at all: the index is
built on the CPU from the text extraction already does, and a query is answered
by SQLite in milliseconds.

The index is one SQLite file next to the ChromaDB directory, written by
``update-db`` from the same splitter the passage collection uses
(:func:`zotero_mcp.semantic_search.split_into_passages`), so a hit carries the
same coordinates: item key, passage number and, when the extracted text has
page breaks, the pages. It does not depend on ChromaDB or on any embedding
being built, and this module imports nothing outside the standard library and
``zotero_mcp.config``, so the search tool can use it on an install without the
``[semantic]`` extra.

Layout:

- ``items`` -- one row per indexed item: what the passages were built from
  (``date_modified``, the attachment set, the extraction profile), so an update
  can tell which items changed, plus enough metadata to print a result without
  asking Zotero.
- ``passages`` -- one row per passage, with its text and position.
- ``passages_fts`` -- an external-content FTS5 table over ``passages.text``,
  kept in step by triggers. ``passages`` is indexed by ``item_key``, so an
  item's passages are replaced without scanning the whole table.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import resolve_chroma_dir

logger = logging.getLogger(__name__)

#: Bumped when the tables or the tokenizer change. A file with another version
#: is emptied and rebuilt on open: the tokenizer is fixed when the FTS table is
#: created, so there is nothing to migrate in place.
SCHEMA_VERSION = 1

#: ``unicode61`` splits on everything that is not a letter or digit, so a
#: hyphenated name ("Banach-Steinhaus", or with an en dash) becomes two tokens
#: that a phrase query matches whichever dash the PDF used; ``remove_diacritics
#: 2`` lets "Erdos" find "Erdős"; ``porter`` lets "theorems" find "theorem".
#: Stemming applies to names too, but the same way on both sides, so it costs
#: nothing for them.
TOKENIZER = "porter unicode61 remove_diacritics 2"
#: SQLite before 3.27 knows only ``remove_diacritics 1``, which misses some
#: combined forms; still better than failing.
_FALLBACK_TOKENIZER = "porter unicode61 remove_diacritics 1"

DEFAULT_FILENAME_SUFFIX = "-keyword.sqlite3"

DEFAULT_KEYWORD_CONFIG: dict[str, Any] = {
    "enabled": False,
    # Build only this index: update-db embeds nothing and needs no model.
    "keyword_only": False,
    # 0: no cap. Storage is cheap here, unlike in the embedding index, and
    # the page cap below already bounds an item's text.
    "max_chunks_per_item": 0,
    # None: semantic_search.extraction.pdf_max_pages (default 50).
    "pdf_max_pages": None,
    # None: next to the ChromaDB directory.
    "path": None,
}

#: Most terms a sanitised query keeps. Every term is a posting list SQLite
#: reads, and a pasted paragraph is not a keyword query anyway.
MAX_QUERY_TERMS = 32


class KeywordIndexError(RuntimeError):
    """The keyword index cannot be opened or used, with a message that says why."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _semantic_section(config_path: str | os.PathLike | None) -> dict[str, Any]:
    if not config_path or not os.path.exists(config_path):
        return {}
    try:
        with open(config_path, encoding="utf-8") as f:
            return (json.load(f) or {}).get("semantic_search") or {}
    except (OSError, ValueError) as e:
        logger.warning(f"Error reading keyword index config: {e}")
        return {}


def load_keyword_config(config_path: str | os.PathLike | None) -> dict[str, Any]:
    """``semantic_search.keyword_index`` with defaults, plus the inherited settings.

    Passage size and overlap come from ``semantic_search.chunking`` so a keyword
    hit and a semantic hit cover the same span; unlike the embedding index,
    the size is never lowered to fit a model. ``pdf_max_pages`` falls back to
    ``semantic_search.extraction.pdf_max_pages``.
    """
    section = _semantic_section(config_path)
    config = dict(DEFAULT_KEYWORD_CONFIG)
    config.update({k: v for k, v in (section.get("keyword_index") or {}).items() if v is not None})
    chunking = section.get("chunking") or {}
    config["chunk_size"] = int(chunking.get("chunk_size") or 1500)
    config["overlap"] = int(chunking.get("overlap") if chunking.get("overlap") is not None else 200)
    if config.get("pdf_max_pages") is None:
        config["pdf_max_pages"] = (section.get("extraction") or {}).get("pdf_max_pages")
    config["persist_directory"] = section.get("persist_directory")
    return config


def keyword_index_path(config_path: str | os.PathLike | None = None, config: dict | None = None) -> Path:
    """Where the keyword index lives: ``keyword_index.path``, else beside ChromaDB.

    Beside the ChromaDB directory rather than inside it, so moving an unreadable
    ChromaDB index aside (``update-db --force-rebuild``) leaves it alone.
    """
    config = config if config is not None else load_keyword_config(config_path)
    if config.get("path"):
        return Path(config["path"]).expanduser()
    chroma_dir = resolve_chroma_dir(config.get("persist_directory"))
    return chroma_dir.with_name(chroma_dir.name + DEFAULT_FILENAME_SUFFIX)


def fts5_available() -> bool:
    """Whether this Python's SQLite was built with FTS5."""
    try:
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
        finally:
            con.close()
        return True
    except sqlite3.Error:
        return False


_NO_FTS5_MESSAGE = (
    "The keyword index needs SQLite's FTS5 extension, and this Python's sqlite3 "
    "module was built without it (SQLite {version}). Python from python.org, "
    "Homebrew, uv and conda all include it; on a self-built Python, rebuild SQLite "
    "with -DSQLITE_ENABLE_FTS5."
)


# ---------------------------------------------------------------------------
# Query sanitising
# ---------------------------------------------------------------------------

# Words that match nearly every passage. BM25 weights them down already, but
# each one still makes SQLite score a large share of the index, and a model
# asked for keywords often sends a sentence anyway.
_STOPWORDS = frozenset(
    """
    a an and are as at be been but by can could did do does for from had has have
    how i if in into is it its may might must not of on or our shall should so
    such than that the their them then there these they this those to was we were
    what when where which while who whom why will with would you your about any
    all also some show shows find paper papers result results using use used
    """.split()
)

_LATEX_GREEK = {
    name: chr(code)
    for name, code in {
        "alpha": 0x3B1, "beta": 0x3B2, "gamma": 0x3B3, "delta": 0x3B4,
        "epsilon": 0x3B5, "varepsilon": 0x3B5, "zeta": 0x3B6, "eta": 0x3B7,
        "theta": 0x3B8, "vartheta": 0x3B8, "iota": 0x3B9, "kappa": 0x3BA,
        "lambda": 0x3BB, "mu": 0x3BC, "nu": 0x3BD, "xi": 0x3BE, "pi": 0x3C0,
        "rho": 0x3C1, "varrho": 0x3C1, "sigma": 0x3C3, "tau": 0x3C4,
        "upsilon": 0x3C5, "phi": 0x3C6, "varphi": 0x3C6, "chi": 0x3C7,
        "psi": 0x3C8, "omega": 0x3C9, "Gamma": 0x393, "Delta": 0x394,
        "Theta": 0x398, "Lambda": 0x39B, "Xi": 0x39E, "Pi": 0x3A0,
        "Sigma": 0x3A3, "Phi": 0x3A6, "Psi": 0x3A8, "Omega": 0x3A9,
    }.items()
}

# LaTeX commands that are layout, not words: dropped. Anything else after a
# backslash (\sup, \log, \det, \lim) is kept as the word PDF text spells out.
_LATEX_MARKUP = frozenset(
    """
    mathbb mathcal mathrm mathbf mathsf mathit mathfrak mathscr boldsymbol bm
    operatorname text textbf textit textrm emph frac dfrac tfrac sqrt left right
    big bigg Big Bigg begin end cdot cdots ldots dots quad qquad over hat bar
    tilde widetilde widehat overline underline vec label ref cite eqref leq geq
    le ge neq approx sim simeq equiv to mapsto infty partial nabla sum prod int
    in notin subset subseteq supset cup cap times otimes oplus circ mid
    """.split()
)

_QUOTED_RE = re.compile(r"[\"“”„]([^\"“”„]+)[\"“”„]")
_LATEX_CMD_RE = re.compile(r"\\([A-Za-z]+)")
# A word, or words joined by hyphens/dashes/apostrophes, or a dotted number
# ("3.2"): each kept together as one phrase.
_TERM_RE = re.compile(r"[^\W_]+(?:(?:[-‐‑‒–—'’]|(?<=\d)\.(?=\d))[^\W_]+)*")
_WORD_RE = re.compile(r"[^\W_]+")


def _fts_string(words: list[str]) -> str:
    """One FTS5 string token: a word, or a phrase when there are several."""
    return '"' + " ".join(words).replace('"', '""') + '"'


def _phrase_words(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text)]


def build_match_query(query: str, max_terms: int = MAX_QUERY_TERMS) -> str | None:
    """Turn free text into an FTS5 MATCH expression that cannot be a syntax error.

    Every term is quoted, so FTS5 operators and punctuation in the input (``-``,
    ``*``, ``:``, ``^``, parentheses, ``AND``/``OR``/``NEAR``) are read as text.
    Terms are ORed: BM25 still ranks passages holding more of the rarer terms
    first, and a passage missing one word of a long query is not lost.

    - ``"..."`` (straight or curly quotes) is kept as an exact phrase.
    - Hyphenated words ("Banach-Steinhaus", "Radon–Nikodym") and dotted
      numbers ("Theorem 3.2") become phrases of their parts, matching the
      tokenizer, which splits them.
    - A LaTeX Greek letter (``\\sigma``) matches the letter or its name; layout
      commands (``\\mathbb``, ``\\frac``) are dropped; other commands
      (``\\sup``, ``\\log``) are kept as words.
    - Common English words are dropped unless nothing else is left, and
      single letters are dropped.

    Returns None when nothing searchable is left.
    """
    text = unicodedata.normalize("NFKC", query or "")
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        key = term.lower()
        if key not in seen and len(terms) < max_terms:
            seen.add(key)
            terms.append(term)

    for match in _QUOTED_RE.finditer(text):
        words = _phrase_words(match.group(1))
        if words:
            add(_fts_string(words))
    text = _QUOTED_RE.sub(" ", text)

    def latex(match: re.Match) -> str:
        name = match.group(1)
        if name in _LATEX_GREEK:
            add(f'("{name.lower()}" OR "{_LATEX_GREEK[name]}")')
            return " "
        return " " if name in _LATEX_MARKUP else f" {name} "

    text = _LATEX_CMD_RE.sub(latex, text)

    candidates: list[list[str]] = []
    for match in _TERM_RE.finditer(text):
        words = _phrase_words(match.group(0))
        # "Doob's" -> "doob": the tokenizer splits off the "s" as well.
        words = [w for w in words if len(w) > 1 or w.isdigit()]
        if words:
            candidates.append(words)

    content = [w for w in candidates if len(w) > 1 or w[0].lower() not in _STOPWORDS]
    for words in content or candidates:
        add(_fts_string(words))

    if not terms:
        return None
    return " OR ".join(terms)


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class KeywordIndex:
    """A SQLite FTS5 index of passages. One writer at a time; readers any time (WAL)."""

    def __init__(self, path: str | os.PathLike, *, create: bool = True):
        self.path = Path(path)
        if not fts5_available():
            raise KeywordIndexError(_NO_FTS5_MESSAGE.format(version=sqlite3.sqlite_version))
        if not create and not self.path.exists():
            raise KeywordIndexError(f"No keyword index at {self.path}.")
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._con = sqlite3.connect(str(self.path), timeout=30)
            self._con.row_factory = sqlite3.Row
            self._con.execute("PRAGMA journal_mode=WAL")
            self._con.execute("PRAGMA synchronous=NORMAL")
            self._con.execute("PRAGMA foreign_keys=OFF")
            if create:
                self._ensure_schema()
            elif self._schema_version() != SCHEMA_VERSION:
                raise KeywordIndexError(
                    f"The keyword index at {self.path} was built by another version of "
                    "zotero-mcp. Run 'zotero-mcp update-db --keyword-only' to rebuild it."
                )
        except sqlite3.Error as e:
            raise KeywordIndexError(f"Cannot open the keyword index at {self.path}: {e}") from e

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        con = getattr(self, "_con", None)
        if con is not None:
            con.close()
            self._con = None

    def __enter__(self) -> KeywordIndex:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def commit(self) -> None:
        self._con.commit()

    def _schema_version(self) -> int | None:
        try:
            row = self._con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        except sqlite3.OperationalError:
            return None
        return int(row[0]) if row else None

    def _ensure_schema(self) -> None:
        version = self._schema_version()
        if version == SCHEMA_VERSION:
            return
        if version is not None:
            logger.warning(
                "Keyword index at %s has schema %s, expected %s; emptying it for a rebuild.",
                self.path, version, SCHEMA_VERSION,
            )
            self._drop_all()
        con = self._con
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS items (
                item_key TEXT PRIMARY KEY,
                group_id INTEGER,
                item_type TEXT,
                title TEXT,
                creators TEXT,
                date TEXT,
                date_modified TEXT,
                attachment_keys TEXT,
                attachment_key TEXT,
                attachment_priority TEXT,
                fulltext TEXT,
                fulltext_source TEXT,
                profile TEXT,
                n_passages INTEGER NOT NULL DEFAULT 0,
                indexed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS items_group ON items(group_id);
            CREATE TABLE IF NOT EXISTS passages (
                id INTEGER PRIMARY KEY,
                item_key TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                page INTEGER,
                page_end INTEGER,
                char_start INTEGER,
                char_end INTEGER,
                section TEXT,
                text TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS passages_item ON passages(item_key);
            CREATE TRIGGER IF NOT EXISTS passages_ai AFTER INSERT ON passages BEGIN
                INSERT INTO passages_fts(rowid, text) VALUES (new.id, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS passages_ad AFTER DELETE ON passages BEGIN
                INSERT INTO passages_fts(passages_fts, rowid, text) VALUES ('delete', old.id, old.text);
            END;
            """
        )
        try:
            self._create_fts(TOKENIZER)
        except sqlite3.OperationalError:
            self._create_fts(_FALLBACK_TOKENIZER)
        con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),)
        )
        con.commit()

    def _create_fts(self, tokenizer: str) -> None:
        self._con.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5("
            f"text, content='passages', content_rowid='id', tokenize='{tokenizer}')"
        )
        self._con.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES ('tokenizer', ?)", (tokenizer,)
        )

    def _drop_all(self) -> None:
        self._con.executescript(
            """
            DROP TRIGGER IF EXISTS passages_ai;
            DROP TRIGGER IF EXISTS passages_ad;
            DROP TABLE IF EXISTS passages_fts;
            DROP TABLE IF EXISTS passages;
            DROP TABLE IF EXISTS items;
            DROP TABLE IF EXISTS meta;
            """
        )
        self._con.commit()

    def clear(self) -> None:
        """Remove every item and passage (``--force-rebuild``)."""
        self._drop_all()
        self._ensure_schema()

    # -- writing ------------------------------------------------------------

    def item_state(self, item_key: str) -> dict[str, Any] | None:
        """What an item's passages were built from, or None if it is not indexed."""
        row = self._con.execute("SELECT * FROM items WHERE item_key = ?", (item_key,)).fetchone()
        return dict(row) if row else None

    def item_states(self) -> dict[str, dict[str, Any]]:
        """``item_state`` for every indexed item, in one query."""
        return {row["item_key"]: dict(row) for row in self._con.execute("SELECT * FROM items")}

    def replace_item(
        self, item_key: str, item: dict[str, Any], passages: Iterable[dict[str, Any]]
    ) -> int:
        """Replace an item's row and all its passages. Returns the passage count.

        Not committed: the caller commits every so often, so a long build is
        not one fsync per item, and an interrupted one keeps what it committed.
        """
        con = self._con
        con.execute("DELETE FROM passages WHERE item_key = ?", (item_key,))
        rows = [
            (
                item_key,
                int(p["chunk_index"]),
                p.get("page"),
                p.get("page_end"),
                p.get("char_start"),
                p.get("char_end"),
                p.get("section"),
                p["text"],
            )
            for p in passages
        ]
        con.executemany(
            "INSERT INTO passages(item_key, chunk_index, page, page_end, char_start, char_end, section, text)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        creators = item.get("creators")
        con.execute(
            """
            INSERT OR REPLACE INTO items(
                item_key, group_id, item_type, title, creators, date, date_modified,
                attachment_keys, attachment_key, attachment_priority, fulltext,
                fulltext_source, profile, n_passages, indexed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item_key,
                item.get("group_id"),
                item.get("item_type"),
                item.get("title"),
                json.dumps(creators) if creators is not None else None,
                item.get("date"),
                item.get("date_modified"),
                item.get("attachment_keys"),
                item.get("attachment_key"),
                item.get("attachment_priority"),
                item.get("fulltext"),
                item.get("fulltext_source"),
                item.get("profile"),
                len(rows),
                _now(),
            ),
        )
        return len(rows)

    def delete_items(self, item_keys: Iterable[str]) -> int:
        """Remove items and their passages. Not committed. Returns how many existed."""
        removed = 0
        for key in item_keys:
            self._con.execute("DELETE FROM passages WHERE item_key = ?", (key,))
            removed += self._con.execute("DELETE FROM items WHERE item_key = ?", (key,)).rowcount
        return removed

    def item_keys(self, group_id: int | None = None) -> set[str]:
        if group_id is None:
            rows = self._con.execute("SELECT item_key FROM items")
        else:
            rows = self._con.execute("SELECT item_key FROM items WHERE group_id = ?", (int(group_id),))
        return {row[0] for row in rows}

    def optimize(self) -> None:
        """Merge the FTS5 index segments; worth it after a large build."""
        self._con.execute("INSERT INTO passages_fts(passages_fts) VALUES ('optimize')")
        self._con.commit()

    # -- reading ------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        con = self._con
        items = con.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        with_text = con.execute("SELECT COUNT(*) FROM items WHERE fulltext = 'yes'").fetchone()[0]
        passages = con.execute("SELECT COUNT(*) FROM passages").fetchone()[0]
        size = 0
        for suffix in ("", "-wal"):
            try:
                size += os.path.getsize(str(self.path) + suffix)
            except OSError:
                pass
        return {
            "path": str(self.path),
            "items": items,
            "items_with_fulltext": with_text,
            "passages": passages,
            "size_bytes": size,
        }

    def search(
        self,
        query: str,
        limit: int = 10,
        group_id: int | None = None,
        per_item: int = 1,
    ) -> dict[str, Any]:
        """BM25-ranked passages for *query*.

        At most ``per_item`` passages per item (the best ones) and ``limit``
        hits in all. ``group_id`` limits the search to one library (0 is the
        personal library). Returns ``{"match": <the FTS5 expression>, "results":
        [...]}``; ``match`` is None, with no results, when the query has no
        searchable words.
        """
        match = build_match_query(query)
        if match is None or limit <= 0:
            return {"match": match, "results": []}
        per_item = max(1, int(per_item))
        sql = (
            "SELECT p.item_key, p.chunk_index, p.page, p.page_end, p.char_start, p.char_end,"
            " p.section, p.text, bm25(passages_fts) AS rank,"
            " snippet(passages_fts, 0, '', '', ' ... ', 64) AS snippet,"
            " i.n_passages, i.group_id, i.item_type, i.title, i.creators, i.date,"
            " i.attachment_key, i.fulltext_source"
            " FROM passages_fts"
            " JOIN passages p ON p.id = passages_fts.rowid"
            " JOIN items i ON i.item_key = p.item_key"
            " WHERE passages_fts MATCH ?"
        )
        params: list[Any] = [match]
        if group_id is not None:
            sql += " AND i.group_id = ?"
            params.append(int(group_id))
        sql += " ORDER BY rank LIMIT ? OFFSET ?"

        results: list[dict[str, Any]] = []
        per_key: dict[str, int] = {}
        # Rows are fetched in pages: grouping by item can discard most of a
        # page when one long book matches everywhere.
        page_size = max(50, limit * per_item * 5)
        offset = 0
        try:
            while len(results) < limit:
                rows = self._con.execute(sql, [*params, page_size, offset]).fetchall()
                for row in rows:
                    key = row["item_key"]
                    if per_key.get(key, 0) >= per_item:
                        continue
                    per_key[key] = per_key.get(key, 0) + 1
                    results.append(self._hit(row))
                    if len(results) >= limit:
                        break
                if len(rows) < page_size or offset > 50 * page_size:
                    break
                offset += page_size
        except sqlite3.OperationalError as e:
            # The query is built so this cannot be a syntax error; anything
            # here is the file itself (locked, damaged).
            raise KeywordIndexError(f"Keyword search failed on {self.path}: {e}") from e
        return {"match": match, "results": results}

    @staticmethod
    def _hit(row: sqlite3.Row) -> dict[str, Any]:
        creators = row["creators"]
        try:
            creators = json.loads(creators) if creators else []
        except ValueError:
            creators = []
        hit: dict[str, Any] = {
            "item_key": row["item_key"],
            "score": -float(row["rank"]),
            "chunk_index": row["chunk_index"],
            "n_chunks": row["n_passages"],
            "char_start": row["char_start"],
            "char_end": row["char_end"],
            "text": row["text"],
            "snippet": (row["snippet"] or "").strip(),
            "group_id": row["group_id"],
            "item_type": row["item_type"],
            "title": row["title"],
            "creators": creators,
            "date": row["date"],
        }
        for optional in ("page", "page_end", "section", "attachment_key", "fulltext_source"):
            if row[optional] is not None:
                hit[optional] = row[optional]
        return hit
