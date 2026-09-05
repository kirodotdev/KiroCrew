"""/side conversation invariants — one test per load-bearing property.

1. Memory isolation: parent ``build_session_context`` is byte-equal after a
   /side round-trip.
2. Same session: open/turn/close never invokes ``get_or_create_slot``.
3. Non-blocking: ``api_side_turn`` returns before ``_run_side_turn`` finishes.
4. Channel separation: side run_id never appears in main-channel payloads.
5. Tool rejection: empty LLM output produces a visible fallback bubble.
6. Agent resolution: the KiroCrew slot agent name (e.g. "default") is resolved
   to the real kiro-cli agent before get_or_create, so set_mode never rejects
   it with "Mode '<name>' not found".
7. Streaming redaction: a credential split across streaming chunk boundaries is
   never emitted on the wire, and the stored/final text is redacted.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew import context as context_module
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers.side import (
    _run_side_turn,
    api_side_close,
    api_side_open,
    api_side_turn,
)
from kiro_crew.dashboard.side_state import SideState
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader

_SIDE_QUESTION = "what is the difference between TCP and UDP?"
_SIDE_ANSWER = "TCP is connection-oriented and UDP is not."
_MAIN_CHAT_EVENT_TYPES = frozenset({"chat_message", "chat_done", "chat_segment", "chat_status"})


class _ReadyKiroPrerequisiteService(KiroPrerequisiteService):
    async def session_ready(self) -> bool:
        return True


_READY_KIRO_PREREQUISITE = object.__new__(_ReadyKiroPrerequisiteService)


async def _no_audit(**kwargs: Any) -> None:
    del kwargs


def _make_side_app(
    state,
    prerequisite_service: KiroPrerequisiteService | None = None,
) -> web.Application:
    app = web.Application()
    app["state"] = state
    app["kiro_prerequisite_service"] = (
        prerequisite_service
        if prerequisite_service is not None
        else _READY_KIRO_PREREQUISITE
    )
    app.router.add_post("/api/chat/slots/{slot}/side/open", api_side_open)
    app.router.add_post("/api/chat/slots/{slot}/side/turn", api_side_turn)
    app.router.add_post("/api/chat/slots/{slot}/side/close", api_side_close)
    return app


def _capture_broadcasts(state) -> list[tuple[str, Any]]:
    events: list[tuple[str, Any]] = []

    def _record(msg_type, data):
        events.append((msg_type, data))

    # Both channels: side frames are owner-only while main chat events go to every client,
    # and these tests discriminate by event type rather than by audience.
    state.broadcast_ws = _record
    state.broadcast_ws_owners = _record
    return events


def _stub_run_side_turn(monkeypatch, *, answer: str = _SIDE_ANSWER):
    async def _fake_run(state, slot, run_id, question, *, is_first_turn):
        if slot._side is not None and slot._side.open:
            slot._side.append_assistant(answer)

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", _fake_run)


#: What a frozen clock reads. Any fixed instant does; a recognisable one makes an
#: accidental real-clock read obvious in a failure diff.
_FROZEN_NOW = datetime(2026, 1, 2, 3, 4, 5)


class _FrozenClock(datetime):
    """A ``datetime`` whose ``now()`` does not advance.

    Subclassed rather than replaced with a stub: ``context`` happens to call only
    ``now``, but a bare stub would break the moment any other ``datetime`` API is
    used there, and that breakage would read as a defect in the test rather than
    in its double.
    """

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return _FROZEN_NOW.replace(tzinfo=tz)


def _freeze_context_clock(monkeypatch):
    """Pin the clock that ``build_session_context`` renders into its output.

    The rendered context carries a wall-clock read formatted to the MINUTE, so
    comparing two renders byte-for-byte otherwise asserts that both happened
    inside the same clock minute — a property no behaviour under test controls,
    and one that breaks whenever a minute boundary lands between the two calls.
    Freezing the clock keeps the equality total over everything else.
    """
    monkeypatch.setattr(context_module, "datetime", _FrozenClock)


@pytest.mark.asyncio
async def test_memory_isolation_byte_equal_after_round_trip(tmp_path, monkeypatch):
    """Parent build_session_context is byte-equal pre/post a /side round-trip."""
    _stub_run_side_turn(monkeypatch)
    _freeze_context_clock(monkeypatch)
    state = _make_state(tmp_path)
    state.sessions.destroy = AsyncMock()
    parent = state.get_or_create_slot("parent")
    parent.append("user", "hi main", "msg msg-u")
    parent.append("assistant", "hello main", "msg msg-a")
    parent.drain()
    state.conversation_log.append("parent", "user", "hi main")
    state.conversation_log.append("parent", "assistant", "hello main")

    builder = ContextBuilder(
        memory=MemoryStore(workspace=tmp_path / "ws"),
        skills=SkillsLoader(skills_path=tmp_path / "skills", install_builtins=False),
        lessons=LessonStore(base_dir=tmp_path / "lessons"),
        conversation_log=state.conversation_log,
    )
    ctx_before = builder.build_session_context(session_key="parent")
    # Proves the freeze reached the renderer. Without this, a rename or an
    # inlined import in `context` would put the real clock back and hand the
    # equality below its minute-boundary dependency again, silently.
    assert _FROZEN_NOW.strftime("%Y-%m-%d %H:%M") in ctx_before

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": _SIDE_QUESTION},
        )
        await client.post("/api/chat/slots/parent/side/close", json={})

    ctx_after = builder.build_session_context(session_key="parent")
    assert ctx_after == ctx_before, "main context diverged after /side round-trip"
    assert _SIDE_QUESTION not in ctx_after
    assert _SIDE_ANSWER not in ctx_after
    assert parent._side is None


@pytest.mark.asyncio
async def test_side_path_never_creates_a_new_slot(tmp_path, monkeypatch):
    """open/turn/close on the parent must not invoke get_or_create_slot."""
    _stub_run_side_turn(monkeypatch)
    state = _make_state(tmp_path)
    state.get_or_create_slot("parent")
    state.sessions.destroy = AsyncMock()

    seen_keys: list[str] = []
    original = state.get_or_create_slot

    def _spy(*args, **kwargs):
        seen_keys.append(args[0] if args else kwargs.get("name", ""))
        return original(*args, **kwargs)

    monkeypatch.setattr(state, "get_or_create_slot", _spy)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": "ping"},
        )
        await client.post("/api/chat/slots/parent/side/close", json={})

    assert seen_keys == [], f"side path called get_or_create_slot: {seen_keys}"


@pytest.mark.asyncio
async def test_side_turn_returns_before_run_finishes(tmp_path, monkeypatch):
    """api_side_turn must return its 200 before the LLM stream completes."""
    release = asyncio.Event()
    started = asyncio.Event()

    async def _blocking(state, slot, run_id, question, *, is_first_turn):
        started.set()
        await release.wait()

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", _blocking)
    state = _make_state(tmp_path)
    state.get_or_create_slot("parent")
    app = _make_side_app(state)

    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        resp = await asyncio.wait_for(
            client.post(
                "/api/chat/slots/parent/side/turn",
                json={"question": "blocking?"},
            ),
            timeout=5.0,
        )
        assert resp.status == 200
        assert started.is_set(), "_run_side_turn did not start before HTTP return"
        release.set()
        for _ in range(50):
            if not state._background_tasks:
                break
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_stale_not_ready_does_not_reject_a_side_turn(tmp_path):
    """A latched not-ready value is advisory and must not 503 a side turn.

    Readiness is probed at boot and on explicit action only, so a stale value
    would block a turn the CLI would have served; the ACP attempt reports a
    signed-out CLI itself.
    """

    state = _make_state(tmp_path)
    parent = state.get_or_create_slot("parent")
    service = KiroPrerequisiteService(
        platform_name="linux",
        environ={"HOME": str(tmp_path), "PATH": ""},
        home=tmp_path,
        audit_writer=_no_audit,
        clock=lambda: 1.0,
    )
    service._has_probed = True
    service._last_probe_at = 1.0
    assert await service.session_ready() is False
    app = _make_side_app(state, service)

    async with TestClient(TestServer(app)) as client:
        opened = await client.post("/api/chat/slots/parent/side/open", json={})
        assert opened.status == 200
        assert parent._side is not None
        response = await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": _SIDE_QUESTION},
        )
        body = await response.json()

    assert response.status == 200
    assert body.get("code") != "kiro_prerequisite_required"


@pytest.mark.asyncio
async def test_side_turn_surfaces_the_actionable_auth_message(tmp_path, monkeypatch):
    """A signed-out CLI must reach the side panel as its own message.

    The side panel has no other channel to tell the user what to do, so the
    generic "(side conversation failed — see server logs)" is not good enough:
    AcpAuthRequired carries the actionable `kiro-cli login` text.
    """

    from kiro_crew.acp.client import AcpAuthRequired

    state = _make_state(tmp_path)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState()
    parent._side.last_run_id = "run-auth"

    async def exploding_stream(*_a, **_k):
        raise AcpAuthRequired("kiro-cli is not logged in.")
        yield  # pragma: no cover — generator shape only

    client = MagicMock()
    client.stream = exploding_stream
    client.stream_command = exploding_stream
    state.sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    state.sessions.release = MagicMock()

    broadcasts: list[dict] = []
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.broadcast_side_result",
        lambda state, **kw: broadcasts.append(kw),
    )

    await _run_side_turn(state, parent, "run-auth", _SIDE_QUESTION, is_first_turn=True)

    errors = [b for b in broadcasts if b.get("is_error")]
    assert errors, broadcasts
    assert "not logged in" in errors[-1]["content"]
    assert "see server logs" not in errors[-1]["content"]


@pytest.mark.asyncio
async def test_side_run_id_never_leaks_to_main_channels(tmp_path, monkeypatch):
    """Side broadcasts go on chat.side_result; run_id never appears on main channels."""
    side_started = asyncio.Event()
    side_release = asyncio.Event()

    async def _streaming(state, slot, run_id, question, *, is_first_turn):
        from kiro_crew.dashboard.ws import broadcast_side_result

        side_started.set()
        await side_release.wait()
        broadcast_side_result(
            state,
            slot_key=slot.key,
            run_id=run_id,
            role="assistant",
            content="answer",
        )

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", _streaming)
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    state.get_or_create_slot("parent")

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        turn_resp = await client.post(
            "/api/chat/slots/parent/side/turn",
            json={"question": "q"},
        )
        side_run_id = (await turn_resp.json())["run_id"]
        await asyncio.wait_for(side_started.wait(), timeout=5.0)

        state.broadcast_ws("chat_message", {"slot": "parent", "content": "main"})
        state.broadcast_ws("chat_done", {"slot": "parent"})

        side_release.set()
        for _ in range(50):
            if not state._background_tasks:
                break
            await asyncio.sleep(0.01)

    main = [(t, p) for t, p in events if t in _MAIN_CHAT_EVENT_TYPES]
    for etype, payload in main:
        assert side_run_id not in repr(payload), f"side run_id leaked into main {etype}: {payload}"
    side_payloads = [p for t, p in events if t == "chat.side_result"]
    assert any(p.get("run_id") == side_run_id for p in side_payloads)


@pytest.mark.asyncio
async def test_empty_llm_output_produces_visible_fallback(tmp_path, monkeypatch):
    """When stream_and_collect returns empty, /side broadcasts a fallback bubble."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("run ls /tmp")
    parent._side.last_run_id = "run-abc"  # match what api_side_turn would set
    parent._side.is_complete = False

    mock_provider = MagicMock()

    async def _fake_get_or_create(key, **kwargs):
        return mock_provider, True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect",
        AsyncMock(return_value=""),
    )

    await _run_side_turn(
        state,
        parent,
        "run-abc",
        "run ls /tmp",
        is_first_turn=True,
    )

    assistant_broadcasts = [(t, d) for t, d in events if d.get("role") == "assistant"]
    assert assistant_broadcasts, "expected at least one assistant broadcast"
    last_content = assistant_broadcasts[-1][1]["content"]
    assert "intentionally unavailable" in last_content
    assert "main chat" in last_content
    assert "enable" not in last_content.lower()
    assert "rephrase" not in last_content.lower()
    stored = [m for m in parent._side.messages if m["role"] == "assistant"]
    assert stored and stored[-1]["content"] == last_content


@pytest.mark.asyncio
async def test_side_turn_resolves_slot_agent_to_kiro_agent(tmp_path, monkeypatch):
    """slot.agent (a KiroCrew name like "default") is resolved to the real
    kiro-cli agent before get_or_create -> create_session -> set_mode.

    Regression: passing the raw slot name straight through made kiro-cli reject
    it with ``Mode 'default' not found`` (no ~/.kiro/agents/default.json),
    crashing every /side turn. The main chat path resolves bindings for the
    same reason; the side path must too.
    """
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "default"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    captured: dict[str, Any] = {}

    async def _fake_get_or_create(key, **kwargs):
        captured["agent"] = kwargs.get("agent")
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.KiroCrewConfig.load",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.resolve_agent_bindings",
        lambda cfg, agent, project_dir=None: MagicMock(kiro_agent="kirocrew"),
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect",
        AsyncMock(return_value="ok"),
    )

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    assert captured["agent"] == "kirocrew", (
        f"side turn passed an unresolved agent to get_or_create: " f"{captured.get('agent')!r}"
    )


@pytest.mark.asyncio
async def test_side_turn_runs_in_the_slot_project_dir(tmp_path, monkeypatch):
    """The side session is created with ``cwd=slot.project``.

    Regression: the side path resolved project-scope agents (via
    resolve_agent_bindings with slot.project) but then created the session
    without a cwd, so kiro-cli — which resolves --agent against
    $PWD/.kiro/agents — rejected the very mode the resolver just returned.
    """
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "default"
    parent.project = str(tmp_path / "proj")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    captured: dict[str, Any] = {}

    async def _fake_get_or_create(key, **kwargs):
        captured["cwd"] = kwargs.get("cwd")
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.KiroCrewConfig.load",
        lambda: MagicMock(),
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.resolve_agent_bindings",
        lambda cfg, agent, project_dir=None: MagicMock(kiro_agent="kirocrew"),
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect",
        AsyncMock(return_value="ok"),
    )

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    assert captured["cwd"] == parent.project, (
        f"side session created without the slot's project cwd: {captured.get('cwd')!r}"
    )


@pytest.mark.asyncio
async def test_side_turn_agent_resolution_falls_back_on_error(tmp_path, monkeypatch):
    """If binding resolution raises, fall back to the raw slot.agent rather
    than crashing the side turn before it starts."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    captured: dict[str, Any] = {}

    async def _fake_get_or_create(key, **kwargs):
        captured["agent"] = kwargs.get("agent")
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()

    def _boom(*_a, **_k):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.KiroCrewConfig.load", _boom)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect",
        AsyncMock(return_value="ok"),
    )

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    assert (
        captured["agent"] == "kirocrew"
    ), f"fallback did not use raw slot.agent: {captured.get('agent')!r}"


@pytest.mark.asyncio
async def test_side_stream_redacts_credential_split_across_chunks(tmp_path, monkeypatch):
    """A credential split across streaming chunk boundaries must never reach
    the wire, and the stored/final text must be redacted.

    broadcast_side_result redacts each frame, but per-frame redaction alone
    misses a secret split across deltas (``...AKIA`` | ``IOSFODNN7...``). The
    StreamRedactor withholds the trailing credential-class run until it's safe,
    matching the main chat path.
    """
    raw_cred = "AKIAIOSFODNN7EXAMPLE"
    # Long-query URL so redact_exfiltration_urls() actually flags it (bare /
    # short-query URLs are intentionally left alone). The unique payload is what
    # must never survive — the redaction label itself names the host.
    exfil_payload = "leaked" + "Z" * 64
    exfil_url = f"https://attacker.io/c?d={exfil_payload}"
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.agent = "kirocrew"
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("q")
    parent._side.last_run_id = "run-1"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()

    async def _fake_stream(provider, message, *, on_chunk=None, **kwargs):
        # Split the credential across two deltas so a naive per-frame redactor
        # would leak the reassembled token across the stream. Also include an
        # exfiltration URL to prove redact() applies BOTH passes (URLs + creds).
        on_chunk("here is a key AKIA")
        on_chunk(f"IOSFODNN7EXAMPLE see {exfil_url} done")
        return f"here is a key AKIAIOSFODNN7EXAMPLE see {exfil_url} done"

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _fake_stream)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    side_events = [d for t, d in events if t == "chat.side_result"]
    # Concatenation of every streamed delta must not reveal the raw secrets.
    streamed = "".join(d["content"] for d in side_events if not d.get("final"))
    assert raw_cred not in streamed, f"raw credential leaked in stream: {streamed!r}"
    assert exfil_payload not in streamed, f"exfil URL leaked in stream: {streamed!r}"

    final = [d for d in side_events if d.get("final")]
    assert final, "expected a terminal (final) side frame"
    # Both passes must scrub the final frame: credentials AND exfiltration URLs.
    assert raw_cred not in final[-1]["content"]
    assert exfil_payload not in final[-1]["content"]
    assert "[REDACTED" in final[-1]["content"]

    stored = [m for m in parent._side.messages if m["role"] == "assistant"]
    assert stored, "expected the assistant reply to be stored"
    assert raw_cred not in stored[-1]["content"]
    assert exfil_payload not in stored[-1]["content"]
    assert "[REDACTED" in stored[-1]["content"]


def _app_slot(state, *, app: str = "notes", agent: str = "notes--assistant"):
    """A slot owned by an app, with an open sidecar and one pending run."""
    slot = state.get_or_create_slot("app-parent")
    slot._app = app
    slot.agent = agent
    slot._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    slot._side.append_user("q")
    slot._side.last_run_id = "run-1"
    slot._side.is_complete = False
    return slot


def _side_errors(events: list[tuple[str, Any]]) -> list[str]:
    return [
        d.get("content", "")
        for _t, d in events
        if isinstance(d, dict) and d.get("is_error") and d.get("kind") == "side"
    ]


def _dispatch_recorder(state) -> list[str]:
    dispatched: list[str] = []

    async def _fake_get_or_create(key, **kwargs):
        dispatched.append(kwargs.get("agent") or "")
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    return dispatched


@pytest.mark.asyncio
async def test_side_turn_refuses_to_substitute_the_default_for_an_app_agent(
    tmp_path, monkeypatch
):
    """An app slot whose agent never materialized must not answer as the default.

    An app's agents live only in ``~/.kiro/agents/<app>--<agent>.json``, so
    ``resolve_agent_bindings`` can honor them only through the materialized-agent
    snapshot -- COLD on the event loop until a warm lands. A cold read falls back
    to the default agent with ``requested_resolved=False``, and dispatching that
    silently runs the generic assistant with none of the app's MCP tools.

    The main chat closed this in #4995 / #4983; the side path was the counted
    unfixed sibling, carrying none of the three rungs. A side answer is the worse
    place for it: there is no turn card to scrutinise, so it simply reads as this
    app's assistant answering.

    Here BOTH self-heal rungs fail, so the turn must end without ever creating a
    session, and must say why.
    """
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    slot = _app_slot(state)
    dispatched = _dispatch_recorder(state)
    warms: list[int] = []

    async def _recover_fails(cfg, _slot, *, project=None):
        # Mirrors the real coroutine's contract: a recovery failure only logs and
        # hands back the still-cold bindings.
        return MagicMock(kiro_agent="kirocrew", requested_resolved=False)

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.KiroCrewConfig.load", lambda: MagicMock()
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.resolve_agent_bindings",
        lambda cfg, agent, project_dir=None: MagicMock(
            kiro_agent="kirocrew", requested_resolved=False
        ),
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.refresh_materialized_agents",
        lambda: warms.append(1),
        # raising=False so this asserts the CONTRACT rather than where the rescan
        # happens to live. Against a build that has no such rung the test still
        # RUNS, and fails on the dispatch below -- the actual defect -- instead of
        # erroring because a name was missing from the module.
        raising=False,
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner._recover_app_agent_binding",
        _recover_fails,
        raising=False,
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect",
        AsyncMock(return_value="ok"),
    )

    await _run_side_turn(state, slot, "run-1", "q", is_first_turn=True)

    assert dispatched == [], (
        "the side turn dispatched an agent for an app slot whose agent never "
        "resolved -- it would have answered as %r, not the app's agent" % (dispatched,)
    )
    assert warms == [1], "the snapshot-rescan rung did not run exactly once: %r" % (warms,)
    errors = _side_errors(events)
    assert errors, "the refusal was silent; the side panel has no other channel"
    assert "notes--assistant" in errors[0], (
        "the error does not name the agent the user asked for: %r" % (errors[0],)
    )
    assert "see server logs" not in errors[0], (
        "the actionable message was flattened into the generic failure text"
    )


@pytest.mark.asyncio
async def test_side_turn_self_heals_a_cold_app_agent_and_then_dispatches_it(
    tmp_path, monkeypatch
):
    """When a rung succeeds, the turn proceeds on the app's own agent.

    The refusal above must not be the only outcome: a cold snapshot is the common
    case and it is recoverable, so the fix has to heal it rather than only refuse.
    Ordering is deterministic rather than raced -- the rescan flips the resolver
    stub's answer, so the second resolve is guaranteed to see the warm one.
    """
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    slot = _app_slot(state)
    dispatched = _dispatch_recorder(state)
    warmed: list[int] = []
    recovered: list[int] = []

    def _resolve(cfg, agent, project_dir=None):
        if warmed:
            return MagicMock(kiro_agent="notes--assistant", requested_resolved=True)
        return MagicMock(kiro_agent="kirocrew", requested_resolved=False)

    async def _recover(cfg, _slot, *, project=None):
        recovered.append(1)
        return MagicMock(kiro_agent="kirocrew", requested_resolved=False)

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.KiroCrewConfig.load", lambda: MagicMock()
    )
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.resolve_agent_bindings", _resolve)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.refresh_materialized_agents",
        lambda: warmed.append(1),
        raising=False,
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.chat_runner._recover_app_agent_binding", _recover, raising=False
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect",
        AsyncMock(return_value="ok"),
    )

    await _run_side_turn(state, slot, "run-1", "q", is_first_turn=True)

    assert dispatched == ["notes--assistant"], (
        "the healed app agent did not reach get_or_create: %r" % (dispatched,)
    )
    assert recovered == [], (
        "the expensive register-from-source rung ran even though the rescan had "
        "already resolved the agent"
    )


@pytest.mark.asyncio
async def test_side_turn_self_heal_is_scoped_to_app_slots(tmp_path, monkeypatch):
    """A non-app slot never pays for the self-heal, and is never refused.

    ``requested_resolved`` is False for any unknown agent name, not only a cold
    app one. Gating on ``slot._app`` is what keeps an ordinary slot on its
    existing best-effort behaviour -- it still dispatches -- and keeps the common
    path free of the rescan's I/O.
    """
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    slot = state.get_or_create_slot("plain-parent")
    slot.agent = "default"
    slot._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    slot._side.append_user("q")
    slot._side.last_run_id = "run-1"
    slot._side.is_complete = False
    dispatched = _dispatch_recorder(state)
    warms: list[int] = []

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.KiroCrewConfig.load", lambda: MagicMock()
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.resolve_agent_bindings",
        lambda cfg, agent, project_dir=None: MagicMock(
            kiro_agent="kirocrew", requested_resolved=False
        ),
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.refresh_materialized_agents",
        lambda: warms.append(1),
        # raising=False so this asserts the CONTRACT rather than where the rescan
        # happens to live. Against a build that has no such rung the test still
        # RUNS, and fails on the dispatch below -- the actual defect -- instead of
        # erroring because a name was missing from the module.
        raising=False,
    )
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect",
        AsyncMock(return_value="ok"),
    )

    await _run_side_turn(state, slot, "run-1", "q", is_first_turn=True)

    assert dispatched == ["kirocrew"], "a non-app slot stopped dispatching: %r" % (dispatched,)
    assert warms == [], "a non-app slot paid for the app-only snapshot rescan"
    assert not _side_errors(events), "a non-app slot was refused by the app-only guard"
