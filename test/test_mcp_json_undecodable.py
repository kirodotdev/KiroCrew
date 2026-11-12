"""An ``mcp.json`` whose bytes are not UTF-8 is skipped like one that does not parse.

MCP discovery reads several ``mcp.json`` files (Kiro Crew's own, Kiro's global,
and other tools' globals). A file that does not parse is logged and skipped, so
the servers in the other files are still found. A file whose bytes are not
UTF-8 is the same unusable file, so it is skipped the same way instead of
raising ``UnicodeDecodeError`` out of discovery and hiding every server.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import pytest

from kiro_crew import mcp_discovery

T = TypeVar("T")

# Not UTF-8: 0xff never starts a UTF-8 sequence.
_UNDECODABLE = b'\xff\xfe{"mcpServers": {"bad": {"command": "x"}}}'


def _call(fn: Callable[[], T]) -> T:
    """Run *fn*, failing the test (not erroring it) when it raises on the bytes."""
    try:
        return fn()
    except UnicodeDecodeError as exc:
        pytest.fail(f"an mcp.json that is not UTF-8 raised {exc!r}")


@pytest.fixture
def files(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    bad = tmp_path / "bad" / "mcp.json"
    good = tmp_path / "good" / "mcp.json"
    bad.parent.mkdir()
    good.parent.mkdir()
    bad.write_bytes(_UNDECODABLE)
    good.write_text('{"mcpServers": {"live": {"command": "y"}}}', encoding="utf-8")
    monkeypatch.setattr(mcp_discovery, "_MCP_JSON_PATHS", (bad, good))
    monkeypatch.setattr(mcp_discovery, "_extra_scope_sources", lambda: ())
    return bad, good


def test_discovery_skips_an_mcp_json_that_is_not_utf8_and_keeps_the_rest(files, caplog):
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_discovery"):
        by_source = _call(mcp_discovery._load_mcp_json_by_source)

    assert {name for bucket in by_source.values() for name in bucket} == {"live"}
    assert [r for r in caplog.records if "Failed to load MCP config" in r.message]


def test_the_name_reader_reads_an_mcp_json_that_is_not_utf8_as_empty(files):
    bad, _good = files

    assert _call(lambda: mcp_discovery._mcp_names_from_file(bad)) == set()


# ── The skip names the actionable UTF-16 repair, not the opaque codec error ──


def test_discovery_names_the_utf16_repair_reason_in_the_warning(files, caplog):
    """A UTF-16/UTF-32 mcp.json is skipped with the fixed 're-save as UTF-8' reason.

    The fixture bytes begin with the UTF-16LE mark PowerShell 5.1 writes. Before
    this change the only signal was the opaque ``'utf-8' codec can't decode byte
    0xff`` message; now the warning names the same repair the kiro-cli path names,
    so a user can act on it. The file is still skipped and the other file's server
    is still found (the invariant above).
    """
    with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_discovery"):
        by_source = _call(mcp_discovery._load_mcp_json_by_source)

    assert {name for bucket in by_source.values() for name in bucket} == {"live"}
    msgs = [r.getMessage() for r in caplog.records if "Failed to load MCP config" in r.getMessage()]
    assert any("re-save it as UTF-8" in m and "UTF-16" in m for m in msgs), msgs


def test_load_user_json_object_names_the_utf16_repair_and_reads_empty(tmp_path, caplog):
    """The shared object reader skips a UTF-16 file as empty, naming the repair.

    ``load_user_json_object`` backs the spec-rebuild / Connections leg. A UTF-16
    file reads as ``{}`` (skip-and-keep, nothing raised) with the fixed reason in
    the warning rather than the codec error.
    """
    from kiro_crew.user_json import load_user_json_object, wide_bom_reason

    p = tmp_path / "mcp.json"
    p.write_bytes(b'\xff\xfe{"mcpServers": {"bad": {"command": "x"}}}')
    with caplog.at_level(logging.WARNING, logger="kiro_crew.user_json"):
        assert load_user_json_object(p) == {}
    assert any("re-save it as UTF-8" in r.getMessage() for r in caplog.records)
    # The helper names UTF-16/UTF-32 marks and leaves a plain UTF-8 file alone.
    assert wide_bom_reason(b"\xff\xfe\x00\x00{}") == "it is saved as UTF-32; re-save it as UTF-8"
    assert wide_bom_reason(b'{"ok": true}') is None


# ── UTF-16 without a mark, and naming each skipped file to the dashboard ──

_UTF16 = "it is saved as UTF-16; re-save it as UTF-8"
_DOC = '{"mcpServers": {"bad": {"command": "x"}}}'


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (_DOC.encode("utf-16-le"), _UTF16),
        (_DOC.encode("utf-16-be"), _UTF16),
        # Not this shape, so each keeps the reason it has today.
        (b"\0" * 8, None),
        (b"{\0", None),
        (_DOC.encode("utf-32-le"), None),
        (_DOC.encode("utf-32-be"), None),
        (b'/*\0\0*/{"mcpServers": {}}', None),
    ],
)
def test_utf16_without_a_mark_gets_the_utf16_reason(raw, reason):
    from kiro_crew.user_json import wide_bom_reason

    assert wide_bom_reason(raw) == reason


def test_a_nul_in_a_leading_comment_still_loads(tmp_path):
    from kiro_crew.user_json import load_user_json_object

    p = tmp_path / "mcp.json"
    p.write_bytes(b'/*\0\0*/{"mcpServers": {"live": {}}}')
    assert load_user_json_object(p) == {"mcpServers": {"live": {}}}


@pytest.mark.parametrize(
    "raw",
    [
        b"\xff\xfe" + _DOC.encode("utf-16-le"),
        b"\xfe\xff" + _DOC.encode("utf-16-be"),
        _DOC.encode("utf-16-le"),
    ],
    ids=["utf16le-mark", "utf16be-mark", "utf16le-no-mark"],
)
def test_the_reader_reports_each_file_it_skips_for_its_encoding(files, raw):
    bad, _good = files
    bad.write_bytes(raw)
    skipped: list[tuple[Path, str]] = []

    by_source = mcp_discovery._load_mcp_json_by_source(skipped=skipped)

    assert {name for bucket in by_source.values() for name in bucket} == {"live"}
    assert skipped == [(bad, _UTF16)]


@pytest.mark.asyncio
async def test_the_unreadable_route_names_the_file_from_home_and_the_reason(
    files, tmp_path, monkeypatch
):
    import json

    from aiohttp.test_utils import make_mocked_request

    from kiro_crew.dashboard.handlers import mcp as mcp_handlers

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    resp = await mcp_handlers.api_mcp_unreadable_files(
        make_mocked_request("GET", "/api/mcp/unreadable")
    )

    assert json.loads(resp.body) == {"files": [{"file": "~/bad/mcp.json", "reason": _UTF16}]}
