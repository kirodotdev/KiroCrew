"""Session start reads the kiro-cli transcript files off the event loop.

``_initialize_session`` checks that a resumed session's ``<id>.json`` exists and
reads the size of its ``<id>.jsonl``, so the tool-result reader starts at the
end. Both now run through ``asyncio.to_thread``; ``_jsonl_size`` keeps the old
answers: the file's size, or 0 when it is missing or cannot be read.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from kiro_crew.acp import client_start
from kiro_crew.acp.client_start import _jsonl_size


def test_jsonl_size_is_the_file_size(tmp_path: Path) -> None:
    path = tmp_path / "s.jsonl"
    path.write_bytes(b"x" * 123)
    assert _jsonl_size(path) == 123


def test_a_missing_transcript_reads_as_zero(tmp_path: Path) -> None:
    assert _jsonl_size(tmp_path / "absent.jsonl") == 0


def test_an_unreadable_transcript_reads_as_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "s.jsonl"
    path.write_bytes(b"x")

    def _refuse(self: Path, *args: object, **kwargs: object) -> object:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "stat", _refuse)
    assert _jsonl_size(path) == 0


def _initialize_session_calls() -> list[str]:
    tree = ast.parse(Path(client_start.__file__).read_text(encoding="utf-8"))
    fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_initialize_session"
    )
    return [ast.unparse(node) for node in ast.walk(fn) if isinstance(node, ast.Call)]


def test_session_start_hands_both_transcript_reads_to_a_thread() -> None:
    calls = _initialize_session_calls()
    assert any(c.startswith("asyncio.to_thread(Path(session_file).exists") for c in calls)
    assert any(c.startswith("asyncio.to_thread(_jsonl_size") for c in calls)
    assert not any(c.endswith(".exists()") or c.endswith(".stat()") for c in calls)
