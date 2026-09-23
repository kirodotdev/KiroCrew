"""Tests for the WakaTime heartbeat send side.

No network: ``WakaTimeClient`` is patched to a stub whose ``send_heartbeats``
records the rows it was handed, or raises to prove a failure is swallowed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, cast

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


def _install_client(
    monkeypatch: pytest.MonkeyPatch,
    stub: _StubClient,
    *,
    api_key: str = "test-key",
) -> None:
    monkeypatch.setattr(heartbeats, "resolve_api_key", lambda: api_key)
    monkeypatch.setattr(
        heartbeats,
        "WakaTimeClient",
        lambda *, api_key, api_base: stub,
    )


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
    credential = "AKIAIOSFODNN7EXAMPLE"
    heartbeat = heartbeats._make_heartbeat(f"/home/u/{credential}")
    assert credential not in heartbeat["entity"]
    assert credential not in heartbeat["project"]


def test_entity_redacts_before_bounding_a_long_project_label() -> None:
    credential = "AKIAIOSFODNN7EXAMPLE"
    prefix = "x" * (heartbeats._MAX_ENTITY_CHARS - 10)
    long_label = f"{prefix}{credential}-{'y' * 50}"
    project = f"/home/u/{long_label}"

    entity = heartbeats._entity_for_project(project)
    assert len(entity) == heartbeats._MAX_ENTITY_CHARS
    # If truncation moved before redaction, the first ten credential characters
    # would survive at the end of the capped entity as an unrecognised fragment.
    assert credential[:10] not in entity

    heartbeat = heartbeats._make_heartbeat(project)
    assert heartbeat["entity"] == entity
    assert heartbeat["project"] == entity


def test_disabled_integration_schedules_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduled: list[Any] = []
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: scheduled)
    cfg = _Cfg(wakatime=_WakaCfg(enabled=False, send_heartbeats=True))
    heartbeats.note_coding_activity("/tmp/repo", config=cast(Any, cfg))
    assert scheduled == []


def test_send_flag_off_schedules_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    scheduled: list[Any] = []
    monkeypatch.setattr(asyncio, "get_running_loop", lambda: scheduled)
    cfg = _Cfg(wakatime=_WakaCfg(enabled=True, send_heartbeats=False))
    heartbeats.note_coding_activity("/tmp/repo", config=cast(Any, cfg))
    assert scheduled == []


def test_classifying_a_coding_tool_does_not_send_by_itself() -> None:
    # Classification and sending are separate. This keeps denied tools safe:
    # recognizing a write request does not produce a heartbeat unless the turn
    # later calls note_coding_activity after execution was approved.
    assert heartbeats.is_coding_event("fs_write", "", False)
    assert heartbeats.is_coding_event("execute_bash", "execute", True)


@pytest.mark.asyncio
async def test_enabled_and_opted_in_sends_one_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubClient()
    _install_client(monkeypatch, stub)
    monkeypatch.setattr(heartbeats.KiroCrewConfig, "load", staticmethod(lambda: _Cfg()))

    heartbeats.note_coding_activity(
        "/tmp/my-repo",
        ai_input_tokens=1200,
        ai_output_tokens=340,
        ai_line_changes=42,
        config=cast(Any, _Cfg()),
    )
    for _ in range(20):
        await asyncio.sleep(0)
        if stub.batches:
            break

    assert len(stub.batches) == 1
    assert len(stub.batches[0]) == 1
    hb = stub.batches[0][0]
    assert hb["entity"] == "my-repo"
    assert hb["project"] == "my-repo"
    assert hb["category"] == "ai coding"
    assert hb["type"] == "app"
    assert hb["ai_input_tokens"] == 1200
    assert hb["ai_output_tokens"] == 340
    assert hb["ai_line_changes"] == 42
    assert "ai_session" not in hb
    assert stub.closed


@pytest.mark.asyncio
async def test_each_note_schedules_one_fire_and_forget_send(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started: list[dict[str, Any]] = []
    release = asyncio.Event()

    async def _blocked_send(hb: dict[str, Any]) -> None:
        started.append(hb)
        await release.wait()

    monkeypatch.setattr(heartbeats, "send_heartbeat", _blocked_send)
    heartbeats.note_coding_activity("/tmp/one", config=cast(Any, _Cfg()))
    heartbeats.note_coding_activity("/tmp/two", config=cast(Any, _Cfg()))

    # The hot path returned before either send coroutine started or completed.
    assert started == []
    await asyncio.sleep(0)
    assert len(started) == 2
    assert [hb["project"] for hb in started] == ["one", "two"]

    release.set()
    await asyncio.sleep(0)


def test_zero_ai_fields_are_dropped_not_sent_as_zero() -> None:
    hb = heartbeats._make_heartbeat("/tmp/my-repo")
    assert "ai_input_tokens" not in hb
    assert "ai_output_tokens" not in hb
    assert "ai_line_changes" not in hb
    assert "ai_session" not in hb
    assert hb["category"] == "ai coding"


def test_line_changes_counts_added_and_removed() -> None:
    changes = [
        {"content": "a\nb\nc\n", "after": "a\nB\nc\nd\n"},
    ]
    # Replacing b with B counts one removal and one addition; d adds one more.
    assert heartbeats.line_changes_from_file_changes(changes) == 3


def test_line_changes_handles_missing_after_as_zero() -> None:
    assert heartbeats.line_changes_from_file_changes([{"content": "a\nb\n"}]) == 0


def test_line_changes_is_zero_for_malformed_input() -> None:
    assert heartbeats.line_changes_from_file_changes(None) == 0
    assert heartbeats.line_changes_from_file_changes([]) == 0
    assert heartbeats.line_changes_from_file_changes(["not-a-dict"]) == 0


@pytest.mark.asyncio
async def test_send_rechecks_opt_in_at_send_time(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _StubClient()
    _install_client(monkeypatch, stub)
    monkeypatch.setattr(
        heartbeats.KiroCrewConfig,
        "load",
        staticmethod(lambda: _Cfg(wakatime=_WakaCfg(enabled=True, send_heartbeats=False))),
    )

    await heartbeats.send_heartbeat(heartbeats._make_heartbeat("/tmp/repo"))

    assert stub.batches == []


@pytest.mark.asyncio
async def test_send_resolves_destination_once_inside_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _Cfg()
    stub = _StubClient()
    calls = {"config": 0, "base": 0, "key": 0}

    def _load() -> _Cfg:
        calls["config"] += 1
        return cfg

    def _base(loaded: _Cfg) -> str:
        assert loaded is cfg
        calls["base"] += 1
        return "https://wakatime.example/api/v1"

    def _key() -> str:
        calls["key"] += 1
        return "single-snapshot-key"

    monkeypatch.setattr(heartbeats.KiroCrewConfig, "load", staticmethod(_load))
    monkeypatch.setattr(heartbeats, "resolve_base_url", _base)
    monkeypatch.setattr(heartbeats, "resolve_api_key", _key)
    monkeypatch.setattr(
        heartbeats,
        "WakaTimeClient",
        lambda *, api_key, api_base: stub,
    )

    await heartbeats.send_heartbeat(heartbeats._make_heartbeat("/tmp/repo"))

    assert calls == {"config": 1, "base": 1, "key": 1}
    assert len(stub.batches) == 1
    assert stub.closed


@pytest.mark.asyncio
async def test_send_swallows_failure_and_closes_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubClient(fail=True)
    _install_client(monkeypatch, stub)
    monkeypatch.setattr(heartbeats.KiroCrewConfig, "load", staticmethod(lambda: _Cfg()))

    await heartbeats.send_heartbeat(heartbeats._make_heartbeat("/tmp/repo"))

    assert stub.closed


@pytest.mark.asyncio
async def test_send_is_a_noop_when_no_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = _Cfg()
    monkeypatch.setattr(heartbeats, "resolve_api_key", lambda: "")
    monkeypatch.setattr(heartbeats.KiroCrewConfig, "load", staticmethod(lambda: cfg))
    monkeypatch.setattr(
        heartbeats,
        "WakaTimeClient",
        lambda **_kwargs: pytest.fail("client must not be constructed without an API key"),
    )

    await heartbeats.send_heartbeat(heartbeats._make_heartbeat("/tmp/repo"))


def test_is_coding_event_trusts_shell_and_kind_before_name() -> None:
    assert heartbeats.is_coding_event("", "", True)
    assert not heartbeats.is_coding_event("", "execute", False)
    assert heartbeats.is_coding_event("", "execute", True)
    assert heartbeats.is_coding_event("code", "edit", False)
    # Native name fallback still classifies an empty-kind fs_write frame.
    assert heartbeats.is_coding_event("fs_write", "", False, "")
    assert not heartbeats.is_coding_event("grep", "read", False)
    assert not heartbeats.is_coding_event("", "", False)


def test_mcp_tool_name_fallback_requires_native_identity() -> None:
    # An MCP server controls its own names, so a server tool literally named
    # write is not trusted as coding activity on name alone.
    assert not heartbeats.is_coding_event("write", "", False, "third-party-mcp")
    # Provider-resolved edit kind is trusted for MCP and native tools alike.
    assert heartbeats.is_coding_event("anything", "edit", False, "third-party-mcp")
