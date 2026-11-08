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
