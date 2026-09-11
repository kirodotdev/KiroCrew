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
from chat_test_helpers import _make_state, stub_readonly_spec_publisher

from kiro_crew import context as context_module
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers.side import (
    _run_side_turn,
    api_side_close,
    api_side_open,
    api_side_turn,
)
from kiro_crew.dashboard.side_prompts import SIDE_BOUNDARY_PROMPT
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
        prerequisite_service if prerequisite_service is not None else _READY_KIRO_PREREQUISITE
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


@pytest.fixture(autouse=True)
def _published_readonly_spec(monkeypatch):
    """Stand in for the derived-spec publisher on every side turn in this file;
    see ``chat_test_helpers.stub_readonly_spec_publisher``. Returns the recorded
    ``(base_name, project_dir)`` calls. The tests here pin what the turn does
    with the name (binds the session to it) and with a refusal (never runs the
    base)."""
    return stub_readonly_spec_publisher(monkeypatch)


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
    # One vocabulary for the boundary: these phrases are the footer's
    # (``context_only_tools_unavailable``), so a drift between the two shows here.
    assert "read-only" in last_content
    assert "lookups work here, but changes don't" in last_content
    assert "Use the main chat to take action." in last_content
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
        lambda: MagicMock(agent=MagicMock(acp_backend="")),
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

    # The resolved kiro agent is the BASE of the derived read-only spec the
    # session is bound to; an unresolved alias would surface as "default--readonly".
    assert captured["agent"] == "kirocrew--readonly", (
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
        lambda: MagicMock(agent=MagicMock(acp_backend="")),
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

    assert (
        captured["cwd"] == parent.project
    ), f"side session created without the slot's project cwd: {captured.get('cwd')!r}"


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
    stream_mock = AsyncMock(return_value="ok")
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", stream_mock)

    await _run_side_turn(state, parent, "run-1", "q", is_first_turn=True)

    # With the config unloadable the harness is unknown, so the read-only
    # allowance is not granted: the raw slot.agent runs under REJECT_ALL and
    # no derived spec is involved — fail closed, never the base agent with tools.
    from kiro_crew.llm_helpers import ToolApprovalPolicy

    assert (
        captured["agent"] == "kirocrew"
    ), f"fallback did not use raw slot.agent: {captured.get('agent')!r}"
    assert stream_mock.await_args.kwargs["approval_policy"] is ToolApprovalPolicy.REJECT_ALL


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
async def test_side_turn_refuses_to_substitute_the_default_for_an_app_agent(tmp_path, monkeypatch):
    """An app slot whose agent never materialized must not answer as the default.

    An app's agents live only in ``~/.kiro/agents/<app>--<agent>.json``, so
    ``resolve_agent_bindings`` can honor them only through the materialized-agent
    snapshot -- COLD on the event loop until a warm lands. A cold read falls back
    to the default agent with ``requested_resolved=False``, and dispatching that
    silently runs the generic assistant with none of the app's MCP tools.

    The main chat handles this; the side path is the counted
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
        "kiro_crew.dashboard.handlers.side.KiroCrewConfig.load",
        lambda: MagicMock(agent=MagicMock(acp_backend="")),
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
    assert (
        "notes--assistant" in errors[0]
    ), "the error does not name the agent the user asked for: %r" % (errors[0],)
    assert (
        "see server logs" not in errors[0]
    ), "the actionable message was flattened into the generic failure text"


@pytest.mark.asyncio
async def test_side_turn_self_heals_a_cold_app_agent_and_then_dispatches_it(tmp_path, monkeypatch):
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
        "kiro_crew.dashboard.handlers.side.KiroCrewConfig.load",
        lambda: MagicMock(agent=MagicMock(acp_backend="")),
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

    assert dispatched == [
        "notes--assistant--readonly"
    ], "the healed app agent did not reach get_or_create: %r" % (dispatched,)
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
        "kiro_crew.dashboard.handlers.side.KiroCrewConfig.load",
        lambda: MagicMock(agent=MagicMock(acp_backend="")),
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

    assert dispatched == ["kirocrew--readonly"], "a non-app slot stopped dispatching: %r" % (
        dispatched,
    )
    assert warms == [], "a non-app slot paid for the app-only snapshot rescan"
    assert not _side_errors(events), "a non-app slot was refused by the app-only guard"


@pytest.mark.asyncio
async def test_side_turn_streams_under_read_only_policy(tmp_path, monkeypatch):
    """The side turn must run READ_ONLY (Reads-mode semantics, reject fallback)
    with the gateway's ONE live hook gate and the side session's own identity —
    not REJECT_ALL, and never AUTO_APPROVE. The gate is ``context_builder.hooks``
    by identity: that is the object Settings > Security hot-reloads and the one
    the main chat consults, so a deny added mid-turn binds the side turn too. A
    per-turn manager would freeze the opt-out state at turn start and re-read the
    keystone file on the event loop."""
    from types import SimpleNamespace

    from kiro_crew.hooks import HookManager
    from kiro_crew.llm_helpers import ToolApprovalPolicy

    live_gate = HookManager()
    state = _make_state(tmp_path, context_builder=SimpleNamespace(hooks=live_gate))
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user(_SIDE_QUESTION)
    parent._side.last_run_id = "run-ro"
    parent._side.is_complete = False

    mock_provider = MagicMock()

    async def _fake_get_or_create(key, **kwargs):
        return mock_provider, True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    stream_mock = AsyncMock(return_value=_SIDE_ANSWER)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect",
        stream_mock,
    )

    await _run_side_turn(
        state,
        parent,
        "run-ro",
        _SIDE_QUESTION,
        is_first_turn=True,
    )

    assert stream_mock.await_count == 1
    kwargs = stream_mock.await_args.kwargs
    assert kwargs["approval_policy"] is ToolApprovalPolicy.READ_ONLY
    assert kwargs["hooks"] is live_gate
    # Gate identity: the SIDE session's key, so SEL rows and the governance
    # profile lookup describe the side surface rather than the parent slot.
    assert kwargs["session_key"] == f"side:parent:{parent._side.gen}"
    assert kwargs["agent"]


@pytest.mark.asyncio
async def test_side_turn_without_a_live_gate_passes_no_hooks(tmp_path, monkeypatch):
    """No context builder means no gate. The turn then hands READ_ONLY
    ``hooks=None``, which the policy rejects everything under
    (``read_only_policy_no_hooks``) — never a default-constructed manager, which
    would classify reads against none of the operator's opt-outs or deny rules."""
    from kiro_crew.llm_helpers import ToolApprovalPolicy

    state = _make_state(tmp_path)
    assert state.context_builder is None
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user(_SIDE_QUESTION)
    parent._side.last_run_id = "run-nogate"
    parent._side.is_complete = False

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    stream_mock = AsyncMock(return_value=_SIDE_ANSWER)
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect",
        stream_mock,
    )

    await _run_side_turn(state, parent, "run-nogate", _SIDE_QUESTION, is_first_turn=True)

    kwargs = stream_mock.await_args.kwargs
    assert kwargs["approval_policy"] is ToolApprovalPolicy.READ_ONLY
    assert kwargs["hooks"] is None


def test_dashboard_bound_profile_governs_a_side_turn(tmp_path, monkeypatch):
    """A governance profile bound to ``surface: dashboard`` must refuse, on a
    side turn, the tool it forbids on the parent slot's turns.

    The side turn hands the gate its own key, ``side:<slot>``. Before
    ``sel._infer_source`` classified that prefix, the key matched no
    ``dashboard:``/messaging branch and fell through to the ``slack`` fallback,
    so ``resolve_active_scope`` looked up the slack binding and a
    dashboard-scoped profile governed nothing on the side chat — its read-only
    classifier then auto-approved a tool the operator had forbidden, on a turn
    with no approver. The gate is exercised exactly as ``_resolve_permission``
    calls it for READ_ONLY: classifier-only, with the host-trusted built-in
    identity, so the deny below is the PROFILE's and nothing else's.
    """
    import json

    from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY, HookManager
    from kiro_crew.platform import governance_profiles as gp

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    try:
        gate = HookManager()

        def _side_call(session_key: str):
            return gate.on_tool_call(
                "web_fetch",
                session_key=session_key,
                agent="kirocrew",
                tool_kind="fetch",
                mcp_tool_name="web_fetch",
                mcp_identity_trusted=True,
                classifier_only=True,
            )

        # Positive control: with no profile bound, the host-known read tool is
        # the classifier's own auto-approve — so a deny below is governance's.
        unbound = _side_call("side:parent")
        assert unbound.action == TOOL_AUTO_APPROVE and unbound.read_only

        (profiles / "dashboard-reads.json").write_text(
            json.dumps(
                {
                    "name": "dashboard-reads",
                    "bind": {"type": "surface", "id": "dashboard"},
                    "tools": {"mode": "allow", "allow": ["fs_read"]},
                }
            )
        )
        gp.reset_store()

        # The parent slot's own turns are governed by the binding...
        assert _side_call("dashboard:parent").action == TOOL_DENY
        # ...and so is the side turn, by the SAME profile: the side key
        # classifies as the dashboard surface rather than falling to "slack".
        side = _side_call("side:parent")
        assert side.action == TOOL_DENY, (
            "a dashboard-bound profile skipped the side turn — `side:*` fell "
            "through _infer_source to the slack fallback"
        )
        assert "governance" in (side.reason or "").lower()
    finally:
        gp.reset_store()


@pytest.mark.asyncio
async def test_side_turn_binds_its_session_to_the_derived_readonly_agent(
    tmp_path, monkeypatch, _published_readonly_spec
):
    """The side session is created with ``<agent>--readonly``, never the base.

    The base agent's ``allowedTools`` are approved by kiro-cli before the READ_ONLY
    gate sees them, so a side session bound to the base spec would run the user's
    main-chat grants unattended. The derived name is what makes every tool call
    raise a permission request the gate can judge.
    """
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user(_SIDE_QUESTION)
    parent._side.last_run_id = "run-derived"
    parent._side.is_complete = False

    created: list[tuple[str, dict]] = []

    async def _fake_get_or_create(key, **kwargs):
        created.append((key, kwargs))
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect",
        AsyncMock(return_value=_SIDE_ANSWER),
    )

    await _run_side_turn(state, parent, "run-derived", _SIDE_QUESTION, is_first_turn=True)

    assert _published_readonly_spec, "the turn never asked for a derived spec"
    ((base_name, _project),) = _published_readonly_spec
    assert base_name and not base_name.endswith("--readonly")
    assert len(created) == 1
    _key, kwargs = created[0]
    assert kwargs["agent"] == f"{base_name}--readonly"
    assert kwargs["agent"] != base_name


@pytest.mark.asyncio
async def test_side_turn_refuses_when_the_readonly_spec_cannot_be_derived(tmp_path, monkeypatch):
    """Fail closed: no derived spec, no side turn — and never the base agent.

    The refusal is coded (``ReadOnlySpecError.code``) so the user-facing message
    and the log name the cause, and no session is created, so nothing runs.
    """
    from kiro_crew.dashboard.side_readonly_spec import ReadOnlySpecError

    def _refuse(base_name: str, project_dir: str | None = None) -> str:
        raise ReadOnlySpecError("base_spec_missing", f"no agent spec declares {base_name!r}")

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.publish_readonly_spec", _refuse)
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user(_SIDE_QUESTION)
    parent._side.last_run_id = "run-refused"
    parent._side.is_complete = False

    state.sessions.get_or_create = AsyncMock(
        side_effect=AssertionError("must not create a session")
    )
    stream_mock = AsyncMock(return_value=_SIDE_ANSWER)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", stream_mock)

    await _run_side_turn(state, parent, "run-refused", _SIDE_QUESTION, is_first_turn=True)

    state.sessions.get_or_create.assert_not_awaited()
    assert stream_mock.await_count == 0
    errors = [d for t, d in events if d.get("role") == "assistant" and d.get("is_error")]
    assert errors, "the refusal must reach the panel as an error frame"
    # The user sees plain words; the mechanism name and the code stay in the log.
    assert errors[-1]["content"] == "Side Chat couldn't start. Try again, or use the main chat."
    assert "base_spec_missing" not in errors[-1]["content"]
    assert "spec" not in errors[-1]["content"].lower()
    assert errors[-1].get("final") is True
    assert parent._side.is_complete is True


@pytest.mark.asyncio
async def test_side_turn_rebinds_a_retained_session_whose_binding_changed(
    tmp_path, monkeypatch, _published_readonly_spec
):
    """``get_or_create`` reuses a live session for the side key whatever agent or
    cwd is asked for, and kiro-cli read the spec at spawn. So a retained session
    bound under another agent, project or derived-spec content is destroyed
    before the turn acquires one; a session whose binding matches is kept."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent.project = str(tmp_path / "proj-b")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user(_SIDE_QUESTION)
    parent._side.last_run_id = "run-rebind"
    parent._side.is_complete = False
    # A session created for project A, under the same derived agent.
    parent._side.binding = ("kirocrew--readonly", str(tmp_path / "proj-a"), "d" * 64)

    state.sessions.get_provider = MagicMock(return_value=MagicMock(name="retained provider"))
    state.sessions.destroy = AsyncMock()
    order: list[str] = []

    async def _fake_destroy(key):
        order.append(f"destroy:{key}")

    created: list[str] = []

    async def _fake_get_or_create(key, **kwargs):
        order.append(f"create:{key}:{kwargs['agent']}:{kwargs['cwd']}")
        # Cold start on the first acquisition of a key, a live reuse afterwards.
        is_new = key not in created
        created.append(key)
        return MagicMock(), is_new, False

    state.sessions.destroy = AsyncMock(side_effect=_fake_destroy)
    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    stream_mock = AsyncMock(return_value=_SIDE_ANSWER)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", stream_mock)

    await _run_side_turn(state, parent, "run-rebind", _SIDE_QUESTION, is_first_turn=False)

    assert order == [
        f"destroy:side:parent:{parent._side.gen}",
        f"create:side:parent:{parent._side.gen}:kirocrew--readonly:{tmp_path / 'proj-b'}",
    ], order
    # The new binding is recorded, so the NEXT turn with the same project keeps it.
    assert parent._side.binding == ("kirocrew--readonly", str(tmp_path / "proj-b"), "d" * 64)
    # The cold-started process has no framing: it gets the full envelope, not
    # the bare follow-up a live session would — the sidecar's transcript alone
    # says "not the first turn", but the session behind it is brand new.
    cold_message = stream_mock.await_args.args[1]
    assert SIDE_BOUNDARY_PROMPT in cold_message
    assert cold_message.endswith(f"User: {_SIDE_QUESTION}")

    order.clear()
    parent._side.last_run_id = "run-rebind-2"
    parent._side.is_complete = False
    await _run_side_turn(state, parent, "run-rebind-2", _SIDE_QUESTION, is_first_turn=False)
    assert order == [
        f"create:side:parent:{parent._side.gen}:kirocrew--readonly:{tmp_path / 'proj-b'}"
    ], order
    # A live session keeps its framing: the follow-up is the bare question.
    assert stream_mock.await_args.args[1] == _SIDE_QUESTION


@pytest.mark.asyncio
async def test_a_close_during_the_rebind_destroy_acquires_nothing(
    tmp_path, monkeypatch, _published_readonly_spec
):
    """The rebind's destroy suspends the task; a close landing meanwhile has
    destroyed the same key. Acquiring it again would create a session no sidecar
    owns, so the turn stops there."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    old = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    old.append_user(_SIDE_QUESTION)
    old.last_run_id = "run-old"
    old.is_complete = False
    old.binding = ("kirocrew--readonly", "", "stale" * 16)
    parent._side = old

    async def _destroy_then_close(key):
        parent._side = None  # the close lands while the destroy is in flight

    state.sessions.get_provider = MagicMock(return_value=MagicMock(name="retained provider"))
    state.sessions.destroy = AsyncMock(side_effect=_destroy_then_close)
    state.sessions.get_or_create = AsyncMock(side_effect=AssertionError("must not acquire"))
    state.sessions.release = MagicMock()
    stream_mock = AsyncMock(return_value=_SIDE_ANSWER)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", stream_mock)

    await _run_side_turn(state, parent, "run-old", _SIDE_QUESTION, is_first_turn=False)

    state.sessions.destroy.assert_awaited_once_with(f"side:parent:{old.gen}")
    state.sessions.get_or_create.assert_not_awaited()
    state.sessions.release.assert_not_called()
    assert stream_mock.await_count == 0


@pytest.mark.asyncio
async def test_a_project_change_during_derivation_does_not_split_check_and_spawn(
    tmp_path, monkeypatch
):
    """The shadow check runs against a project's ``.kiro/agents`` and kiro-cli
    resolves ``--agent`` against the spawn cwd's. Both must be the project the
    turn read once: a change landing during the off-loop derivation must not
    have the check run in A and the spawn happen in B, whose file the check
    never saw. B is picked up by the next turn's binding."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    proj_a, proj_b = str(tmp_path / "proj-a"), str(tmp_path / "proj-b")
    parent.project = proj_a
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user(_SIDE_QUESTION)
    parent._side.last_run_id = "run-switch"
    parent._side.is_complete = False
    derived_for: list[str | None] = []

    def _publish_then_switch(base_name: str, project_dir: str | None = None):
        from kiro_crew.dashboard.side_readonly_spec import PublishedSpec

        derived_for.append(project_dir)
        parent.project = proj_b  # the project changes while the derivation is off-loop
        return PublishedSpec(name=f"{base_name}--readonly", digest="d" * 64)

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.publish_readonly_spec", _publish_then_switch
    )
    created: list[dict] = []

    async def _fake_get_or_create(key, **kwargs):
        created.append(kwargs)
        return MagicMock(), True, False

    state.sessions.get_provider = MagicMock(return_value=None)
    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect",
        AsyncMock(return_value=_SIDE_ANSWER),
    )

    await _run_side_turn(state, parent, "run-switch", _SIDE_QUESTION, is_first_turn=True)

    assert derived_for == [proj_a]
    assert created[0]["cwd"] == proj_a
    assert parent._side.binding == ("kirocrew--readonly", proj_a, "d" * 64)


@pytest.mark.asyncio
async def test_a_close_during_acquisition_destroys_the_acquired_session(
    tmp_path, monkeypatch, _published_readonly_spec
):
    """``get_or_create`` suspends the task; a close landing meanwhile destroyed
    the key, so the session it hands back belongs to no sidecar. It is destroyed
    and the turn streams nothing."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    old = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    old.append_user(_SIDE_QUESTION)
    old.last_run_id = "run-old"
    old.is_complete = False
    parent._side = old

    async def _create_then_close(key, **kwargs):
        parent._side = None  # the close lands while the creation is in flight
        return MagicMock(name="ownerless provider"), True, False

    state.sessions.get_provider = MagicMock(return_value=None)
    state.sessions.get_or_create = _create_then_close
    state.sessions.destroy = AsyncMock()
    state.sessions.release = MagicMock()
    stream_mock = AsyncMock(return_value=_SIDE_ANSWER)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", stream_mock)

    await _run_side_turn(state, parent, "run-old", _SIDE_QUESTION, is_first_turn=True)

    state.sessions.destroy.assert_awaited_once_with(f"side:parent:{old.gen}")
    state.sessions.release.assert_called_once_with(f"side:parent:{old.gen}")
    assert stream_mock.await_count == 0
    assert old.binding is None


@pytest.mark.asyncio
async def test_side_turn_destroys_a_live_session_no_binding_vouches_for(
    tmp_path, monkeypatch, _published_readonly_spec
):
    """A live side session with no recorded binding cannot be shown to run under
    the derived spec, so it is cold-started rather than trusted."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user(_SIDE_QUESTION)
    parent._side.last_run_id = "run-unvouched"
    parent._side.is_complete = False
    assert parent._side.binding is None

    state.sessions.get_provider = MagicMock(return_value=MagicMock(name="retained provider"))
    state.sessions.destroy = AsyncMock()

    async def _fake_get_or_create(key, **kwargs):
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.stream_and_collect",
        AsyncMock(return_value=_SIDE_ANSWER),
    )

    await _run_side_turn(state, parent, "run-unvouched", _SIDE_QUESTION, is_first_turn=False)

    state.sessions.destroy.assert_awaited_once_with(f"side:parent:{parent._side.gen}")
    assert parent._side.binding == ("kirocrew--readonly", "", "d" * 64)


@pytest.mark.asyncio
async def test_a_turn_from_a_replaced_sidecar_never_touches_the_replacement_session(
    tmp_path, monkeypatch, _published_readonly_spec
):
    """Close+reopen while a turn is deriving its spec: the old task must not
    destroy, acquire or release anything. Its own generation's session was
    destroyed by the close, and the replacement lives under another key."""
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    old = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    old.append_user(_SIDE_QUESTION)
    old.last_run_id = "run-old"
    old.is_complete = False
    parent._side = old
    replacement = SideState(open=True, created_at="2026-01-01T00:00:01Z")
    assert replacement.gen != old.gen

    def _publish_then_replace(base_name: str, project_dir: str | None = None):
        from kiro_crew.dashboard.side_readonly_spec import PublishedSpec

        # The close+reopen lands while the derivation is off-loop.
        parent._side = replacement
        return PublishedSpec(name=f"{base_name}--readonly", digest="d" * 64)

    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.side.publish_readonly_spec", _publish_then_replace
    )
    state.sessions.get_provider = MagicMock(return_value=MagicMock(name="replacement provider"))
    state.sessions.destroy = AsyncMock()
    state.sessions.get_or_create = AsyncMock(side_effect=AssertionError("must not acquire"))
    state.sessions.release = MagicMock()
    stream_mock = AsyncMock(return_value=_SIDE_ANSWER)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", stream_mock)

    await _run_side_turn(state, parent, "run-old", _SIDE_QUESTION, is_first_turn=True)

    state.sessions.destroy.assert_not_awaited()
    state.sessions.get_or_create.assert_not_awaited()
    state.sessions.release.assert_not_called()
    assert stream_mock.await_count == 0
    assert replacement.binding is None and replacement.messages == []


@pytest.mark.asyncio
async def test_side_close_destroys_the_closing_generations_session(tmp_path):
    state = _make_state(tmp_path)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    closing_gen = parent._side.gen
    state.sessions.destroy = AsyncMock()
    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots/parent/side/close")
        assert resp.status == 200
    state.sessions.destroy.assert_awaited_once_with(f"side:parent:{closing_gen}")
    assert parent._side is None


def _configure_backend(monkeypatch, backend: str) -> None:
    """Make ``KiroCrewConfig.load()`` in the side handler report *backend*."""
    from kiro_crew.dashboard.handlers import side as side_mod

    real_load = side_mod.KiroCrewConfig.load

    def _load(*args, **kwargs):
        cfg = real_load(*args, **kwargs)
        cfg.agent.acp_backend = backend
        return cfg

    monkeypatch.setattr(side_mod.KiroCrewConfig, "load", staticmethod(_load))


@pytest.mark.asyncio
async def test_side_turn_grants_read_only_tools_only_on_the_kiro_backend(
    tmp_path, monkeypatch, _published_readonly_spec
):
    """On kiro-cli (``ACP_BACKENDS_SIDE_READONLY``) the turn derives the read-only
    spec and streams READ_ONLY; the allowance is positive membership, never a
    negation of some other backend."""
    from kiro_crew.acp_backends import ACP_BACKEND_KIRO
    from kiro_crew.llm_helpers import ToolApprovalPolicy

    _configure_backend(monkeypatch, ACP_BACKEND_KIRO)
    state = _make_state(tmp_path)
    _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user(_SIDE_QUESTION)
    parent._side.last_run_id = "run-kiro"
    parent._side.is_complete = False
    created: list[dict] = []

    async def _fake_get_or_create(key, **kwargs):
        created.append(kwargs)
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    stream_mock = AsyncMock(return_value=_SIDE_ANSWER)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", stream_mock)

    await _run_side_turn(state, parent, "run-kiro", _SIDE_QUESTION, is_first_turn=True)

    assert _published_readonly_spec, "kiro-cli turns derive the read-only spec"
    assert created[0]["agent"].endswith("--readonly")
    assert stream_mock.await_args.kwargs["approval_policy"] is ToolApprovalPolicy.READ_ONLY
    assert "lookups work here, but changes don't" in stream_mock.await_args.args[1]


@pytest.mark.asyncio
async def test_side_turn_on_another_backend_runs_no_tools_and_derives_nothing(
    tmp_path, monkeypatch, _published_readonly_spec
):
    """Off the capability set (claude-agent-acp here) the harness has its own
    pre-approval surface the gate cannot see, so the turn keeps the pre-allowance
    posture: the base agent under REJECT_ALL, no derived spec, and a prompt and
    fallback that say tools are unavailable."""
    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE
    from kiro_crew.llm_helpers import ToolApprovalPolicy

    _configure_backend(monkeypatch, ACP_BACKEND_CLAUDE)
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user(_SIDE_QUESTION)
    parent._side.last_run_id = "run-claude"
    parent._side.is_complete = False
    created: list[dict] = []

    async def _fake_get_or_create(key, **kwargs):
        created.append(kwargs)
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    # An empty answer exercises the fallback copy for this branch.
    stream_mock = AsyncMock(return_value="")
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", stream_mock)

    await _run_side_turn(state, parent, "run-claude", _SIDE_QUESTION, is_first_turn=True)

    assert _published_readonly_spec == [], "no derived spec on a non-kiro backend"
    assert not (created[0]["agent"] or "").endswith("--readonly")
    assert stream_mock.await_args.kwargs["approval_policy"] is ToolApprovalPolicy.REJECT_ALL
    prompt = stream_mock.await_args.args[1]
    assert "tools are unavailable here" in prompt
    assert "lookups work here, but changes don't" not in prompt
    last = [d for t, d in events if d.get("role") == "assistant" and d.get("final")][-1]
    assert "can't use tools on this agent backend" in last["content"]
    assert parent._side.binding == (created[0]["agent"] or "", "", "reject_all")
