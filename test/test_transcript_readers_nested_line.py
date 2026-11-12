"""A transcript line nested past the interpreter's limit is skipped, not raised.

``json.loads`` raises ``RecursionError`` on such a line, and it is not a
``ValueError``. The session digest scanners promise "never raises" and the
history search corpus skips unparseable lines, so both skip it and keep reading.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from stray_line_helpers import cyclic_gc_quiesced  # noqa: F401 - fixture
from stray_line_helpers import too_deep_to_decode

from kiro_crew import session_digest
from kiro_crew.history import ConversationLog


def _no_raise(fn: Callable[..., Any], *args: Any) -> Any:
    """*fn*'s result, or a marker string, so a raise fails the assert that names it."""
    try:
        return fn(*args)
    except RecursionError:
        return "raised RecursionError"


@pytest.mark.usefixtures("cyclic_gc_quiesced")
def test_the_digest_scanners_skip_a_line_nested_past_the_limit(tmp_path: Path) -> None:
    nested = too_deep_to_decode()
    transcript = tmp_path / "t.jsonl"
    transcript.write_text(
        nested + "\n" + json.dumps({"role": "user", "content": "hi"}) + "\n", encoding="utf-8"
    )
    cli_log = tmp_path / "c.jsonl"
    prompt = {"kind": "Prompt", "data": {"content": [{"kind": "text", "data": "hello"}]}}
    cli_log.write_text(nested + "\n" + json.dumps(prompt) + "\n", encoding="utf-8")

    assert _no_raise(session_digest._scan_transcript, transcript) == ("hi", 1)
    assert _no_raise(session_digest._scan_cli_log, cli_log) == (1, 0)
    assert _no_raise(session_digest._first_message_from_cli, cli_log) == "hello"


@pytest.mark.usefixtures("cyclic_gc_quiesced")
def test_history_search_skips_a_line_nested_past_the_limit(tmp_path: Path) -> None:
    log = ConversationLog(base_dir=tmp_path)
    log.append("k", "user", "findable text")
    with open(log._path("k"), "a", encoding="utf-8") as handle:
        handle.write(too_deep_to_decode() + "\n")

    assert _no_raise(lambda: list(log._iter_message_texts("k"))) == ["findable text"]
