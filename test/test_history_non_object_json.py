"""History readers treat JSON that is not an object like JSON that does not parse.

Each reader survives a line or file that is not JSON, and treats JSON that parses to
something other than an object (``[1]``, ``42``) the same way, rather than letting
``data.get(...)`` raise ``AttributeError`` and fail the whole read:

* ``ChannelHistory`` restoring an observe channel skips the line, as it skips a corrupt
  one, and keeps every good line.
* ``ConversationLog.get_cached_summary`` reports no cache, as it does for an unreadable
  sidecar.

``ConversationLog.mark_consolidated`` and ``advance_consolidation_sub_offset`` are
writers that rewrite line 0, and they already treat a damaged first line the same
whatever the damage: they raise without writing, into the consolidator's
``except Exception`` that charges the attempt and backs off. A silent skip there would
leave the marker unmoved and re-run the same span on every pass, so the last tests pin
that a non-object first line raises and leaves the file byte-for-byte unchanged.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from kiro_crew.channel_history import ChannelHistory
from kiro_crew.history import ConversationLog


@pytest.mark.parametrize("bad", ["[1]", "42", '"text"', "null"])
def test_an_observe_channel_skips_a_non_object_line_and_keeps_the_rest(
    tmp_path: Path, bad: str
) -> None:
    now = time.time()
    good = {"user": "bob", "text": "kept", "thread_ts": None, "ts": now}
    (tmp_path / "C1.jsonl").write_text(f"{bad}\n{json.dumps(good)}\n", encoding="utf-8")

    history = ChannelHistory(history_dir=tmp_path)
    history.set_observe("C1")

    assert history.entry_count("C1") == 1
    assert "kept" in history.context_for("C1")


@pytest.mark.parametrize("bad", ["[1]", "42", '"text"', "null"])
def test_a_non_object_summary_cache_reads_as_no_cache(tmp_path: Path, bad: str) -> None:
    log = ConversationLog(base_dir=tmp_path)
    log.append("k", "user", "hello")
    sig = log.session_mtime("k")
    assert sig is not None
    log.set_cached_summary("k", "A summary", sig, log.rotation_generation("k"))
    assert log.get_cached_summary("k") == "A summary"

    log._summary_cache_path("k").write_text(bad, encoding="utf-8")

    assert log.get_cached_summary("k") is None


def _log_with_head(tmp_path: Path, head: str) -> tuple[ConversationLog, Path, str]:
    log = ConversationLog(base_dir=tmp_path)
    for i in range(3):
        log.append("k", "user", f"m{i}")
    path = log._path("k")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    lines[0] = head + "\n"
    body = "".join(lines)
    path.write_text(body, encoding="utf-8")
    return log, path, body


@pytest.mark.parametrize("head", ["[1]", "not json"])
def test_mark_consolidated_refuses_a_damaged_head_without_writing(
    tmp_path: Path, head: str
) -> None:
    log, path, body = _log_with_head(tmp_path, head)
    with pytest.raises((AttributeError, ValueError)):
        log.mark_consolidated("k", 2, log.rotation_generation("k"))
    assert path.read_text(encoding="utf-8") == body


@pytest.mark.parametrize("head", ["[1]", "not json"])
def test_the_sub_offset_writer_refuses_a_damaged_head_without_writing(
    tmp_path: Path, head: str
) -> None:
    log, path, body = _log_with_head(tmp_path, head)
    with pytest.raises((AttributeError, ValueError)):
        log.advance_consolidation_sub_offset("k", 0, 5, 0)
    assert path.read_text(encoding="utf-8") == body
