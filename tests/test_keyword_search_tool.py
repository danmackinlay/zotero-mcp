"""`zotero_keyword_search` and `zotero-cli search --mode keyword` over a built index."""

import json
from unittest.mock import MagicMock, patch

import pytest
from conftest import DummyContext

from zotero_mcp import keyword_index
from zotero_mcp.cli_standalone import build_parser, cmd_search
from zotero_mcp.keyword_index import KeywordIndex
from zotero_mcp.tools import search as search_module

pytestmark = pytest.mark.skipif(
    not keyword_index.fts5_available(), reason="this Python's SQLite has no FTS5"
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A home directory whose config points the keyword index into tmp_path."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    config_dir = tmp_path / ".config" / "zotero-mcp"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(json.dumps({
        "semantic_search": {"keyword_index": {"enabled": True, "path": str(tmp_path / "kw.sqlite3")}}
    }))
    monkeypatch.setattr(search_module._client, "get_active_group_id", lambda: 0)
    return tmp_path


@pytest.fixture
def built(home):
    with KeywordIndex(home / "kw.sqlite3") as idx:
        idx.replace_item(
            "BOOKKEY1",
            {"group_id": 0, "item_type": "book", "title": "Real Analysis", "date": "1999",
             "creators": [{"lastName": "Folland", "firstName": "Gerald", "creatorType": "author"}],
             "attachment_key": "ATTKEY01", "fulltext": "yes", "fulltext_source": "pdf"},
            [{"chunk_index": 0, "text": "Preface", "char_start": 0, "char_end": 7, "page": 1, "page_end": 1},
             {"chunk_index": 1, "text": "Theorem 5.13 (Banach-Steinhaus). A pointwise bounded family "
                                        "of bounded operators is uniformly bounded.",
              "char_start": 7, "char_end": 120, "page": 168, "page_end": 169, "section": "Results"}],
        )
        idx.replace_item(
            "GROUPKEY",
            {"group_id": 7, "item_type": "journalArticle", "title": "Group paper", "creators": [],
             "fulltext": "none"},
            [{"chunk_index": 0, "text": "Banach-Steinhaus in a group library", "char_start": 0, "char_end": 35}],
        )
        idx.commit()
    return home


def test_a_hit_names_the_item_the_attachment_and_the_pages(built):
    out = search_module.keyword_search(query="Banach-Steinhaus uniformly bounded", ctx=DummyContext())

    assert "# Keyword Search Results" in out
    assert "**Item Key:** BOOKKEY1" in out
    assert "**Authors:** Folland, Gerald" in out
    assert "Results, pp. 168-169, passage 2/2" in out
    assert 'zotero_read_pdf_pages(item_key="ATTKEY01", start_page=168, end_page=169)' in out
    assert "uniformly bounded" in out
    assert 'Searched for: "Banach Steinhaus"' in out
    # The active library only.
    assert "GROUPKEY" not in out


def test_search_all_libraries_and_library_id(built):
    everywhere = search_module.keyword_search(query="Banach", search_all_libraries=True, ctx=DummyContext())
    assert "BOOKKEY1" in everywhere and "GROUPKEY" in everywhere
    assert "**Library:** group 7" in everywhere

    group = search_module.keyword_search(query="Banach", library_id=7, ctx=DummyContext())
    assert "GROUPKEY" in group and "BOOKKEY1" not in group
    # No pages for this one, so no read suggestion either.
    assert "zotero_read_pdf_pages" not in group

    both = search_module.keyword_search(query="Banach", library_id=7, search_all_libraries=True,
                                        ctx=DummyContext())
    assert both.startswith("Error:")


def test_no_index_gives_the_command_that_builds_one(home):
    out = search_module.keyword_search(query="Banach", ctx=DummyContext())
    assert out.startswith("Error:")
    assert "update-db --keyword-only" in out


def test_an_empty_index_says_so(home):
    KeywordIndex(home / "kw.sqlite3").close()
    out = search_module.keyword_search(query="Banach", ctx=DummyContext())
    assert out.startswith("Error:") and "empty" in out


@pytest.mark.parametrize("query", ["", "   "])
def test_empty_query_is_an_error(built, query):
    assert search_module.keyword_search(query=query, ctx=DummyContext()).startswith("Error:")


def test_a_query_with_no_words_says_what_to_send(built):
    out = search_module.keyword_search(query="* ^ ()", ctx=DummyContext())
    assert out.startswith("Error:") and "distinctive words" in out


def test_no_match_suggests_what_to_try(built):
    out = search_module.keyword_search(query="Radon Nikodym", ctx=DummyContext())
    assert out.startswith("No passages found")
    assert "zotero_semantic_search" in out


def _cli(argv, built):
    args = build_parser().parse_args(argv)
    client = MagicMock()
    client.get_active_group_id.return_value = 0
    with patch("zotero_mcp.cli_standalone.setup_zotero_environment"), \
         patch("zotero_mcp.cli_standalone._import_tools",
               return_value=(search_module, MagicMock(), MagicMock(), MagicMock(), client)):
        cmd_search(args)


def test_cli_json_returns_passages_not_item_records(built, capsys):
    _cli(["--json", "search", "--mode", "keyword", "Banach-Steinhaus", "--limit", "5"], built)
    payload = json.loads(capsys.readouterr().out)

    assert payload["ok"] is True
    data = payload["data"]
    assert data["mode"] == "keyword" and data["count"] == 1
    (hit,) = data["passages"]
    assert hit["item_key"] == "BOOKKEY1"
    assert hit["attachment_key"] == "ATTKEY01"
    assert (hit["page"], hit["page_end"]) == (168, 169)
    assert "Banach" in hit["passage"]
    assert hit["text"].startswith("Theorem 5.13 (Banach-Steinhaus). A pointwise bounded family")


def test_cli_json_all_libraries(built, capsys):
    _cli(["--json", "search", "--mode", "keyword", "Banach", "--all-libraries"], built)
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["count"] == 2
    assert {hit["group_id"] for hit in data["passages"]} == {0, 7}


def test_cli_json_without_an_index_is_an_error_envelope(home, capsys):
    with pytest.raises(SystemExit) as exit_info:
        _cli(["--json", "search", "--mode", "keyword", "Banach"], home)
    assert exit_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False and "update-db --keyword-only" in payload["error"]["message"]


def test_cli_markdown(built, capsys):
    _cli(["search", "--mode", "keyword", "Banach-Steinhaus"], built)
    out = capsys.readouterr().out
    assert "**Item Key:** BOOKKEY1" in out
    # The next step is given as a command the CLI user can run.
    assert "zotero-cli read ATTKEY01 --start-page 168 --end-page 169" in out
    assert "zotero_read_pdf_pages" not in out


def test_update_db_accepts_keyword_only():
    args = build_parser().parse_args(["db", "update", "--keyword-only"])
    assert args.keyword_only is True
