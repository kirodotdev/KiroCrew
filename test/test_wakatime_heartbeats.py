"""Tests for the WakaTime heartbeat send side.

No network: ``build_client`` is patched to a stub whose ``send_heartbeats``
records the chunks it was handed, or raises to prove a failure is swallowed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest

from kiro_crew.wakatime import heartbeats


@dataclass
class _WakaCfg:
    enabled: bool = True
    send_heartbeats: bool = True
    api_base_url: str = ""


@dataclass
class _Cfg:
    wakatime: _WakaCfg = field(default_factory=_WakaCfg)


class _StubClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.batches: list[list[dict[str, Any]]] = []
        self.closed = False
        self._fail = fail

    async def send_heartbeats(self, batch: list[dict[str, Any]]) -> int:
        if self._fail:
            raise RuntimeError("backend down")
        self.batches.append(batch)
        return len(batch)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _clean_state() -> Any:
    heartbeats._reset_for_tests()
    yield
    heartbeats._reset_for_tests()


def test_is_coding_tool_only_matches_write_and_shell() -> None:
    assert heartbeats.is_coding_tool("fs_write")
    assert heartbeats.is_coding_tool("execute_bash")
    assert not heartbeats.is_coding_tool("fs_read")
    assert not heartbeats.is_coding_tool("grep")


def test_entity_is_project_basename_never_full_path() -> None:
    entity = heartbeats._entity_for_project("/Users/someone/secret-dir/my-repo")
    assert entity == "my-repo"


def test_entity_falls_back_to_a_stable_label_when_no_project() -> None:
    assert heartbeats._entity_for_project(None) == "kirocrew-session"
    assert heartbeats._entity_for_project("") == "kirocrew-session"


def test_entity_redacts_a_credential_shaped_basename() -> None:
    # A project directory whose basename looks like a credential must be
    # scrubbed before it can leave for WakaTime, not POSTed verbatim.
    entity = heartbeats._entity_for_project("/home/u/AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in entity
    cfg = _Cfg()
    heartbeats.note_coding_activity("/home/u/AKIAIOSFODNN7EXAMPLE", config=cfg)
    hb = heartbeats._buffer[0]
    assert "AKIAIOSFODNN7EXAMPLE" not in hb["entity"]
    assert "AKIAIOSFODNN7EXAMPLE" not in hb["project"]


def test_disabled_integration_enqueues_nothing() -> None:
    cfg = _Cfg(wakatime=_WakaCfg(enabled=False, send_heartbeats=True))
    heartbeats.note_coding_activity("/tmp/repo", config=cfg)
    assert heartbeats._buffer == []


def test_send_flag_off_enqueues_nothing() -> None:
    cfg = _Cfg(wakatime=_WakaCfg(enabled=True, send_heartbeats=False))
    heartbeats.note_coding_activity("/tmp/repo", config=cfg)
    assert heartbeats._buffer == []


def test_classifying_a_coding_tool_does_not_enqueue_by_itself() -> None:
    # Classification and enqueue are separate: recognizing a write/shell tool
    # as coding activity does not put anything on the buffer. Only an explicit
    # note_coding_activity call does. This is what keeps a denied tool safe —
    # the reject path classifies the request but never calls note_coding_activity,
    # so no heartbeat is ever produced for work that did not execute.
    assert heartbeats.is_coding_event("fs_write", "", False)
    assert heartbeats.is_coding_event("execute_bash", "execute", True)
    assert heartbeats._buffer == []


def test_enabled_and_opted_in_enqueues_one_heartbeat() -> None:
    cfg = _Cfg()
    heartbeats.note_coding_activity(
        "/tmp/my-repo",
        ai_input_tokens=1200,
        ai_output_tokens=340,
        ai_line_changes=42,
        config=cfg,
    )
    assert len(heartbeats._buffer) == 1
    hb = heartbeats._buffer[0]
    assert hb["entity"] == "my-repo"
    assert hb["project"] == "my-repo"
    assert hb["category"] == "ai coding"
    assert hb["type"] == "app"
    assert hb["ai_input_tokens"] == 1200
    assert hb["ai_output_tokens"] == 340
    assert hb["ai_line_changes"] == 42
    # No session identifier is ever sent: the session key encodes a messaging
    # DM peer's platform id, so it must not egress to WakaTime.
    assert "ai_session" not in hb


def test_zero_ai_fields_are_dropped_not_sent_as_zero() -> None:
    cfg = _Cfg()
    heartbeats.note_coding_activity("/tmp/my-repo", config=cfg)
    hb = heartbeats._buffer[0]
    assert "ai_input_tokens" not in hb
    assert "ai_output_tokens" not in hb
    assert "ai_line_changes" not in hb
    assert "ai_session" not in hb
    # But the AI-coding category is always set — that is what routes Kiro Crew
    # activity into WakaTime's AI lane.
    assert hb["category"] == "ai coding"


def test_line_changes_counts_added_and_removed() -> None:
    changes = [
        {"content": "a\nb\nc\n", "after": "a\nB\nc\nd\n"},  # 1 replace + 1 insert
    ]
    # replace of line "b"->"B" counts 2 (one removed, one added), insert "d" counts 1.
    assert heartbeats.line_changes_from_file_changes(changes) == 3


def test_line_changes_handles_missing_after_as_zero() -> None:
    # A before-only entry (no resolved after) contributes nothing rather than a guess.
    assert heartbeats.line_changes_from_file_changes([{"content": "a\nb\n"}]) == 0


def test_line_changes_is_zero_for_malformed_input() -> None:
    assert heartbeats.line_changes_from_file_changes(None) == 0
    assert heartbeats.line_changes_from_file_changes([]) == 0
    assert heartbeats.line_changes_from_file_changes(["not-a-dict"]) == 0


@pytest.mark.asyncio
async def test_delayed_flush_sends_a_buffered_row_within_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubClient()
    monkeypatch.setattr(heartbeats, "build_client", lambda cfg: stub)
    monkeypatch.setattr(heartbeats.KiroCrewConfig, "load", staticmethod(lambda: _Cfg()))
    # Shrink the interval so the delayed flush fires within the test.
    monkeypatch.setattr(heartbeats, "_FLUSH_INTERVAL_SECONDS", 0.05)
    cfg = _Cfg()
    # First activity flushes immediately (interval elapsed from a zeroed clock).
    heartbeats.note_coding_activity("/tmp/repo", config=cfg)
    for _ in range(20):
        await asyncio.sleep(0.02)
        if stub.batches:
            break
    assert len(stub.batches) == 1
    # A second, immediate activity is within the interval: it must arm a delayed
    # flush rather than sit buffered forever.
    heartbeats.note_coding_activity("/tmp/repo", config=cfg)
    assert len(heartbeats._buffer) == 1
    for _ in range(30):
        await asyncio.sleep(0.02)
        if len(stub.batches) == 2:
            break
    assert len(stub.batches) == 2
    assert heartbeats._buffer == []


@pytest.mark.asyncio
async def test_flush_rechecks_opt_in_at_send_time(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubClient()
    monkeypatch.setattr(heartbeats, "build_client", lambda cfg: stub)
    # The opt-in was revoked between scheduling and firing: reloading config at
    # flush time must find it off and send nothing.
    monkeypatch.setattr(
        heartbeats.KiroCrewConfig,
        "load",
        staticmethod(lambda: _Cfg(wakatime=_WakaCfg(enabled=True, send_heartbeats=False))),
    )
    await heartbeats._flush([heartbeats._make_heartbeat("/tmp/repo")])
    assert stub.batches == []


@pytest.mark.asyncio
async def test_flush_chunks_to_the_api_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubClient()
    monkeypatch.setattr(heartbeats, "build_client", lambda cfg: stub)
    monkeypatch.setattr(heartbeats.KiroCrewConfig, "load", staticmethod(lambda: _Cfg()))
    batch = [heartbeats._make_heartbeat("/tmp/repo") for _ in range(60)]
    await heartbeats._flush(batch)
    # 60 rows at cap 25 -> 25 + 25 + 10.
    assert [len(b) for b in stub.batches] == [25, 25, 10]
    assert stub.closed


@pytest.mark.asyncio
async def test_flush_swallows_a_send_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubClient(fail=True)
    monkeypatch.setattr(heartbeats, "build_client", lambda cfg: stub)
    monkeypatch.setattr(heartbeats.KiroCrewConfig, "load", staticmethod(lambda: _Cfg()))
    # Must not raise.
    await heartbeats._flush([heartbeats._make_heartbeat("/tmp/repo")])
    assert stub.closed


@pytest.mark.asyncio
async def test_flush_is_a_noop_when_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(heartbeats, "build_client", lambda cfg: None)
    monkeypatch.setattr(heartbeats.KiroCrewConfig, "load", staticmethod(lambda: _Cfg()))
    await heartbeats._flush([heartbeats._make_heartbeat("/tmp/repo")])


def test_is_coding_event_trusts_shell_and_kind_before_name() -> None:
    # Claude-style frame: empty tool_name but is_shell set.
    assert heartbeats.is_coding_event("", "", True)
    # tool_kind carries the mutating kind even when the name is unknown.
    assert heartbeats.is_coding_event("code", "edit", False)
    assert heartbeats.is_coding_event("", "execute", False)
    # Name fallback still works.
    assert heartbeats.is_coding_event("fs_write", "", False)
    # A read-only frame with no trusted signal is not coding.
    assert not heartbeats.is_coding_event("grep", "read", False)
    assert not heartbeats.is_coding_event("", "", False)


def test_line_changes_skips_entry_with_no_after() -> None:
    # An unresolved after must not be diffed against "" (which would count a
    # full-file deletion); the caller drops such entries, and a stray one here
    # (missing 'after') contributes nothing.
    assert heartbeats.line_changes_from_file_changes([{"content": "a\nb\nc\n"}]) == 0


@pytest.mark.asyncio
async def test_flush_fires_after_the_interval_elapses(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubClient()
    monkeypatch.setattr(heartbeats, "build_client", lambda cfg: stub)
    monkeypatch.setattr(heartbeats.KiroCrewConfig, "load", staticmethod(lambda: _Cfg()))
    cfg = _Cfg()
    # First activity: interval has "elapsed" from a zeroed last-flush, so it flushes.
    heartbeats.note_coding_activity("/tmp/repo", config=cfg)
    # A second, immediate activity is buffered but does not flush again (within interval).
    heartbeats.note_coding_activity("/tmp/repo", config=cfg)
    await asyncio.sleep(0)  # let the scheduled flush task run
    for _ in range(20):
        await asyncio.sleep(0.02)
        if stub.batches:
            break
    assert len(stub.batches) == 1
    assert len(stub.batches[0]) == 1
    assert len(heartbeats._buffer) == 1


def test_buffer_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    # No running loop here, so no flush is scheduled; the buffer just accumulates
    # and must stay capped.
    cfg = _Cfg()
    for _ in range(heartbeats._MAX_BUFFERED + 50):
        heartbeats.note_coding_activity("/tmp/repo", config=cfg)
    assert len(heartbeats._buffer) <= heartbeats._MAX_BUFFERED
