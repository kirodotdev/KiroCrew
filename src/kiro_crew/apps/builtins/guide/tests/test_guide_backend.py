"""Tests for the Guide app's data layer, search, media lookup, and MCP surface.

The rootdir ``conftest.py`` pins ``KIROCREW_HOME`` to a per-test temp dir, so
``app_data_dir("guide")`` — where the overlay lives — is already isolated; each
test drops its overlay there. ``search.reset_cache()`` runs before every test so
the module-level caches never bleed between tests.

Anchored to real base entries shipped in ``data/guide-base.json`` (129 entries).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

from kiro_crew.apps.builtins.guide.backend import mcp_server, search

# Real base entries (ids are stable public identifiers).
_TERMINAL = "open-a-terminal"
_WINDOWS = "install-windows-unsupported"


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    search.reset_cache()
    yield
    search.reset_cache()


def _overlay_dir() -> Path:
    from kiro_crew.apps.manager import app_data_dir

    d = app_data_dir("guide") / "overlay"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_overlay(obj: object) -> None:
    """Write the overlay verbatim (canonical dict, or a legacy list/entries-only)."""
    (_overlay_dir() / "guide-overlay.json").write_text(
        json.dumps(obj, ensure_ascii=False), encoding="utf-8"
    )


# ── base load + search ────────────────────────────────────────────────────────


def test_base_entries_load() -> None:
    entries = search.all_entries()
    assert len(entries) == 129
    ids = {e["id"] for e in entries}
    assert _TERMINAL in ids
    assert _WINDOWS in ids


def test_search_ranks_relevant_entry_first() -> None:
    results = search.search("terminal", limit=10)
    ids = [r["id"] for r in results]
    assert _TERMINAL in ids  # the relevant entry surfaces for its own keyword
    assert results[0]["fix"]  # the top result resolves a one-line fix


def test_search_empty_query_returns_by_weight() -> None:
    results = search.search("", limit=10)
    assert 1 <= len(results) <= 10
    assert all(r.get("id") for r in results)


def test_search_platform_filter_excludes_nonmatching() -> None:
    # install-windows-unsupported is windows-only; a macos filter must drop it.
    ids = {r["id"] for r in search.search("windows", platform="macos", limit=50)}
    assert _WINDOWS not in ids


def test_search_topic_filter_matches_list_valued_topic() -> None:
    # `topic` is a list in the real data (e.g. ["gateway"]); the filter must
    # match membership, not stringified-list equality.
    results = search.search("terminal", topic="gateway", limit=50)
    assert any(r["id"] == _TERMINAL for r in results)


def test_get_entry_returns_full_text() -> None:
    entry = search.get_entry(_TERMINAL)
    assert entry is not None
    assert entry["crew_prompt"]
    assert isinstance(entry["steps"], list) and entry["steps"]


def test_get_unknown_entry_is_none() -> None:
    assert search.get_entry("nope-not-real") is None


def test_index_lists_ids_and_distinct_facets() -> None:
    idx = search.index()
    assert len(idx["ids"]) == 129
    assert _TERMINAL in idx["ids"] and _WINDOWS in idx["ids"]
    # Distinct, sorted platform/topic values for the filter chips.
    assert "macos" in idx["platforms"] and "windows" in idx["platforms"]
    assert idx["platforms"] == sorted(idx["platforms"])
    assert "gateway" in idx["topics"]
    assert idx["topics"] == sorted(idx["topics"])


# ── overlay: canonical {entries, patches, excluded_both_entries} ───────────────


def test_overlay_patch_replaces_fields_wholesale() -> None:
    # A patch's field value is authoritative: keywords is REPLACED, not merged,
    # so a base keyword must disappear.
    base_entry = search.get_entry(_TERMINAL)
    assert base_entry is not None
    base_kw = base_entry["keywords"]
    assert "terminal" in base_kw
    _write_overlay(
        {
            "version": "test",
            "entries": [],
            "patches": [
                {"id": _TERMINAL, "patch": {"trust": "internal-verified", "keywords": ["kerberos"]}}
            ],
            "excluded_both_entries": [],
        }
    )
    entry = search.get_entry(_TERMINAL)
    assert entry is not None
    assert entry["trust"] == "internal-verified"  # scalar overwritten
    assert entry["keywords"] == ["kerberos"]  # whole-field replace, not append
    assert "terminal" not in entry["keywords"]


def test_overlay_entries_and_excluded_are_appended() -> None:
    _write_overlay(
        {
            "entries": [{"id": "int-only-1", "title": "Internal only", "weight": 9.0}],
            "patches": [],
            "excluded_both_entries": [
                {"id": "excl-1", "title": "Excluded both", "keywords": ["excluded"]}
            ],
        }
    )
    assert search.get_entry("int-only-1") is not None
    assert search.get_entry("excl-1") is not None
    assert any(r["id"] == "excl-1" for r in search.search("excluded", limit=50))
    # Base is still intact alongside the appends.
    assert search.get_entry(_TERMINAL) is not None


def test_overlay_patch_for_unknown_id_is_skipped() -> None:
    _write_overlay(
        {
            "entries": [],
            "patches": [{"id": "ghost-id", "patch": {"trust": "x"}}],
            "excluded_both_entries": [],
        }
    )
    assert search.get_entry("ghost-id") is None
    assert len(search.all_entries()) == 129  # nothing dropped, nothing added


def test_overlay_legacy_bare_list_still_loads() -> None:
    _write_overlay([{"id": "legacy-1", "title": "Legacy entry"}])
    assert search.get_entry("legacy-1") is not None


def test_overlay_lazy_reload_on_mtime_change() -> None:
    assert search.get_entry("late-add") is None
    _write_overlay({"entries": [{"id": "late-add", "title": "Added later"}]})
    assert search.get_entry("late-add") is not None


# ── media two-tier lookup ──────────────────────────────────────────────────────


def test_media_base_lookup_finds_bundled_file() -> None:
    assert search.resolve_media("README.md") is not None


def test_media_overlay_overrides_base() -> None:
    media = _overlay_dir() / "media"
    media.mkdir(parents=True, exist_ok=True)
    (media / "README.md").write_text("overlay copy", encoding="utf-8")
    resolved = search.resolve_media("README.md")
    assert resolved is not None
    assert resolved.read_text(encoding="utf-8") == "overlay copy"


def test_media_traversal_is_rejected() -> None:
    assert search.resolve_media("../app.json") is None
    assert search.resolve_media("/etc/passwd") is None


# ── MCP server ─────────────────────────────────────────────────────────────────


def _call(method: str, params: dict | None = None, req_id: int = 1) -> dict:
    reply = mcp_server.handle(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
    )
    assert reply is not None
    return reply


def test_mcp_tools_list_advertises_both_tools() -> None:
    reply = _call("tools/list")
    names = {t["name"] for t in reply["result"]["tools"]}
    assert names == {"guide_search", "guide_get"}


def test_mcp_guide_search_returns_real_entries() -> None:
    reply = _call(
        "tools/call", {"name": "guide_search", "arguments": {"query": "terminal", "limit": 10}}
    )
    payload = json.loads(reply["result"]["content"][0]["text"])
    ids = [r["id"] for r in payload["results"]]
    assert _TERMINAL in ids


def test_mcp_guide_get_returns_full_entry() -> None:
    reply = _call("tools/call", {"name": "guide_get", "arguments": {"id": _WINDOWS}})
    payload = json.loads(reply["result"]["content"][0]["text"])
    assert payload["id"] == _WINDOWS
    assert payload["steps"]


def test_mcp_guide_get_missing_id_is_invalid_params() -> None:
    reply = _call("tools/call", {"name": "guide_get", "arguments": {}})
    assert reply["error"]["code"] == mcp_server._INVALID_PARAMS


def test_mcp_unknown_tool_is_method_not_found() -> None:
    reply = _call("tools/call", {"name": "nope", "arguments": {}})
    assert reply["error"]["code"] == mcp_server._METHOD_NOT_FOUND
