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

The file also pins that a stale readiness value never refuses a side turn and that a
signed-out CLI reaches the panel as its own message, plus the slot project as the session
cwd, the fallback when binding resolution fails, the app-agent refusal and self-heal, and
``/side/stop`` (cancel, settle exactly once, and the races around a stop). The read-only
confinement of a side turn, from its approval policy and session binding to each backend's
tools, is pinned in ``test_side_turn_confinement.py``; the shared question and answer
constants, the always-ready prerequisite service, the ``/side`` app factory, the broadcast
recorder and the autouse spec-publisher stub live in ``side_helpers.py``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from side_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _SIDE_ANSWER,
    _SIDE_QUESTION,
    _capture_broadcasts,
    _make_side_app,
    _published_readonly_spec_fixture,
)

from kiro_crew import context as context_module
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers.side import _run_side_turn
from kiro_crew.dashboard.side_state import SideState
from kiro_crew.kiro_prerequisite import KiroPrerequisiteService
from kiro_crew.learn import LessonStore
from kiro_crew.memory import MemoryStore
from kiro_crew.skills import SkillsLoader
from kiro_crew.testing.wait import async_wait_until, await_until

_MAIN_CHAT_EVENT_TYPES = frozenset({"chat_message", "chat_done", "chat_segment", "chat_status"})


async def _no_audit(**kwargs: Any) -> None:
    del kwargs


def _stub_run_side_turn(monkeypatch, *, answer: str = _SIDE_ANSWER):
    async def _fake_run(state, slot, run_id, question, *, is_first_turn, start_priority=None):
        if slot._side is not None and slot._side.open:
            slot._side.append_assistant(answer)

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", _fake_run)


@pytest.fixture
def _close_skills_loaders(close_skills_loaders):
    """Close each loader and join its catalog worker after the requesting test."""
    return close_skills_loaders


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
async def test_memory_isolation_byte_equal_after_round_trip(
    tmp_path, monkeypatch, _close_skills_loaders
):
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
        # Construction opens the skill search index; the module fixture closes
        # the loader and joins its catalog worker at teardown.
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

    async def _blocking(state, slot, run_id, question, *, is_first_turn, start_priority=None):
        started.set()
        await release.wait()

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", _blocking)
    state = _make_state(tmp_path)
    state.get_or_create_slot("parent")
    app = _make_side_app(state)

    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        resp = await await_until(
            asyncio.ensure_future(
                client.post(
                    "/api/chat/slots/parent/side/turn",
                    json={"question": "blocking?"},
                )
            ),
            timeout=5.0,
        )
        assert resp.status == 200
        assert started.is_set(), "_run_side_turn did not start before HTTP return"
        release.set()
        await async_wait_until(lambda: not state._background_tasks)


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

    async def _streaming(state, slot, run_id, question, *, is_first_turn, start_priority=None):
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
        await await_until(side_started)

        state.broadcast_ws("chat_message", {"slot": "parent", "content": "main"})
        state.broadcast_ws("chat_done", {"slot": "parent"})

        side_release.set()
        await async_wait_until(lambda: not state._background_tasks)

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
async def test_empty_answer_after_text_streamed_still_produces_the_fallback(tmp_path, monkeypatch):
    """A prompt-busy retry starts its text over: chunks from the abandoned attempt
    reached ``on_chunk``, but the answer it returns is the last attempt's, here empty."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user("run ls /tmp")
    parent._side.last_run_id = "run-abc"
    parent._side.is_complete = False

    mock_provider = MagicMock()

    async def _fake_get_or_create(key, **kwargs):
        return mock_provider, True, False

    async def _retried_to_nothing(provider, message, *, on_chunk=None, **kwargs):
        on_chunk("Let me look at /tmp")
        return ""

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", _retried_to_nothing)

    await _run_side_turn(state, parent, "run-abc", "run ls /tmp", is_first_turn=True)

    final = [d for t, d in events if t == "chat.side_result" and d.get("final")][-1]
    assert "lookups work here, but changes don't" in final["content"]
    stored = [m for m in parent._side.messages if m["role"] == "assistant"]
    assert stored and stored[-1]["content"] == final["content"]


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
async def test_side_stop_cancels_in_flight_turn_and_settles(tmp_path, monkeypatch):
    """POST /side/stop cancels the hung turn's task and broadcasts a terminal frame.

    The escape hatch for a hung side turn: /interrupt targets the main
    run, so a side turn — a separate background task — needs its own cancel. The
    stop must cancel the task AND broadcast a terminal frame that clears the
    client's streaming/pending, keyed to the same run so the hung row settles.
    """
    release = asyncio.Event()
    started = asyncio.Event()

    async def _blocking(state, slot, run_id, question, *, is_first_turn, start_priority=None):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Mirror the REAL _run_side_turn finally: on cancellation it clears
            # ``slot._side.task = None`` under its own ``last_run_id == run_id``
            # gate. The handler then awaits this task, so by the time it
            # re-checks identity ``side.task`` is already None — the exact
            # production condition that a strict ``side.task is task`` gate
            # mis-read as "superseded", trapping the panel busy. Reproducing it
            # here is what makes this test exercise the real settle path.
            if slot._side is not None and slot._side.last_run_id == run_id:
                slot._side.is_complete = True
                slot._side.task = None
            raise

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", _blocking)
    state = _make_state(tmp_path)
    state.sessions.destroy = AsyncMock()
    events = _capture_broadcasts(state)
    state.get_or_create_slot("parent")

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        turn = await client.post("/api/chat/slots/parent/side/turn", json={"question": "hangs?"})
        run_id = (await turn.json())["run_id"]
        await await_until(started)

        slot = state._slots["parent"]
        assert slot._side.task is not None and not slot._side.is_complete

        stop = await client.post("/api/chat/slots/parent/side/stop", json={})
        body = await stop.json()
        assert body == {"ok": True}

    # The task was cancelled, the turn marked complete, and the handle cleared so
    # a later stop cannot cancel a finished (or next) turn.
    assert slot._side.is_complete
    assert slot._side.task is None
    # The stopped question is settled in the transcript with an assistant row, so
    # the next turn does not re-read it as an open, unanswered User: line.
    assert slot._side.messages
    last = slot._side.messages[-1]
    assert last["role"] == "assistant"
    assert "stopped" in last["content"]
    # A terminal error frame for THIS run clears streaming/pending on the client.
    terminal = [
        d
        for _t, d in events
        if d.get("role") == "assistant" and d.get("final") and d.get("run_id") == run_id
    ]
    assert terminal, "no terminal frame broadcast on stop"
    assert terminal[-1]["is_error"] is True
    assert "stopped" in terminal[-1]["content"]


@pytest.mark.asyncio
async def test_side_stop_cancels_the_backend_session(tmp_path, monkeypatch):
    """Stop reaches kiro-cli: it awaits the provider's cancel, not just the task.

    task.cancel() only unwinds the local consumer; without a backend cancel the
    stopped turn keeps generating and billing. The handler must await the side
    session provider's cancel before dropping the task handle.
    """
    release = asyncio.Event()
    started = asyncio.Event()

    async def _blocking(state, slot, run_id, question, *, is_first_turn, start_priority=None):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", _blocking)
    state = _make_state(tmp_path)
    state.sessions.destroy = AsyncMock()
    provider = MagicMock(name="side provider")
    provider.cancel = AsyncMock()
    state.sessions.get_provider = MagicMock(return_value=provider)
    _capture_broadcasts(state)
    state.get_or_create_slot("parent")

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        await client.post("/api/chat/slots/parent/side/turn", json={"question": "hangs?"})
        await await_until(started)
        stop = await client.post("/api/chat/slots/parent/side/stop", json={})
        assert (await stop.json()) == {"ok": True}

    # The backend session was cancelled, not just the local task.
    provider.cancel.assert_awaited()


@pytest.mark.asyncio
async def test_side_stop_does_not_clobber_a_replacement_turn(tmp_path, monkeypatch):
    """Identity re-check: a turn that settles during the cancel yield is not erased.

    ``provider.cancel()`` can end the prompt normally, so the cancelled turn's
    own finally may flip ``is_complete`` and ``_drain_side_queue`` dispatch the
    NEXT queued turn onto the same ``side:<slot>:<gen>`` key before the stop
    handler resumes. Without an identity re-check the handler would then run
    ``is_complete = True`` / ``task = None`` / ``append_assistant`` on the
    REPLACEMENT turn — erasing its live handle, marking a streaming turn
    complete, and leaving a stale terminal frame. The handler must instead bail
    once it sees the turn it cancelled was superseded.
    """
    release = asyncio.Event()
    started = asyncio.Event()

    async def _blocking(state, slot, run_id, question, *, is_first_turn, start_priority=None):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", _blocking)
    state = _make_state(tmp_path)
    state.sessions.destroy = AsyncMock()
    state.get_or_create_slot("parent")

    # The replacement turn's live handle, installed DURING the cancel await to
    # simulate a concurrent side-turn request draining the queue mid-stop.
    replacement_task = None

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        turn = await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        run_id = (await turn.json())["run_id"]
        await await_until(started)
        slot = state._slots["parent"]
        side = slot._side

        async def _supersede():
            # Stand in for the replacement turn's state: a new run id and a new,
            # still-running task handle, installed while the stop handler is
            # awaiting the cancel of the turn it snapshotted.
            nonlocal replacement_task
            replacement_task = asyncio.ensure_future(asyncio.sleep(3600))
            side.last_run_id = "run-replacement"
            side.is_complete = False
            side.task = replacement_task

        provider = MagicMock(name="side provider")
        provider.cancel = AsyncMock(side_effect=_supersede)
        state.sessions.get_provider = MagicMock(return_value=provider)

        stop = await client.post("/api/chat/slots/parent/side/stop", json={})
        body = await stop.json()
        # It still reports the stop succeeded, but settles nothing.
        assert body == {"ok": True}

    # The replacement turn is untouched: its handle survives, it is not marked
    # complete, and no "(side response stopped)" row was appended over it.
    assert side.task is replacement_task
    assert side.task is not None and not side.task.done()
    assert side.is_complete is False
    assert side.last_run_id == "run-replacement"
    assert not any(m["role"] == "assistant" and "stopped" in m["content"] for m in side.messages)
    assert run_id != "run-replacement"
    replacement_task.cancel()


@pytest.mark.asyncio
async def test_concurrent_side_stops_settle_the_row_exactly_once(tmp_path, monkeypatch):
    """A double-click (two concurrent stops) appends ONE stopped row, not two.

    GPT fenced finding: both requests pass the pre-await early-out while the
    turn is live, both await the cancel, both reach the settle block — so
    without an atomic claim both append ``(side response stopped)`` and
    broadcast a terminal frame, duplicating the terminal transcript row kiro-cli
    is then fed. The handler claims the turn by clearing the handle
    (``side.task = None``) synchronously before the first await, so the second
    request re-reads ``task is None`` at the early-out and settles nothing.
    """
    release = asyncio.Event()
    started = asyncio.Event()
    in_cancel = asyncio.Event()
    hold_cancel = asyncio.Event()

    async def _blocking(state, slot, run_id, question, *, is_first_turn, start_priority=None):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", _blocking)
    state = _make_state(tmp_path)
    state.sessions.destroy = AsyncMock()
    events = _capture_broadcasts(state)
    state.get_or_create_slot("parent")

    # ``provider.cancel`` parks on the first request inside the await window and
    # releases only once the SECOND stop has been issued, so the two overlap on
    # the exact window the fence is about.
    async def _park_cancel():
        in_cancel.set()
        await hold_cancel.wait()

    provider = MagicMock(name="side provider")
    provider.cancel = AsyncMock(side_effect=_park_cancel)
    state.sessions.get_provider = MagicMock(return_value=provider)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        turn = await client.post("/api/chat/slots/parent/side/turn", json={"question": "hangs?"})
        run_id = (await turn.json())["run_id"]
        await await_until(started)

        # First stop: enters the await window and parks inside provider.cancel.
        first = asyncio.ensure_future(client.post("/api/chat/slots/parent/side/stop", json={}))
        await await_until(in_cancel)
        # Second stop while the first is parked: must hit the claimed early-out.
        second = await client.post("/api/chat/slots/parent/side/stop", json={})
        assert (await second.json()) == {"ok": True}
        # Let the first finish.
        hold_cancel.set()
        first_resp = await await_until(first)
        assert (await first_resp.json()) == {"ok": True}

        slot = state._slots["parent"]

    # Exactly one stopped row in the transcript — not two.
    stopped_rows = [
        m for m in slot._side.messages if m["role"] == "assistant" and "stopped" in m["content"]
    ]
    assert len(stopped_rows) == 1, f"expected 1 stopped row, got {len(stopped_rows)}"
    # Exactly one terminal error frame for this run — not two.
    terminal = [
        d
        for _t, d in events
        if d.get("role") == "assistant" and d.get("final") and d.get("run_id") == run_id
    ]
    assert len(terminal) == 1, f"expected 1 terminal frame, got {len(terminal)}"


@pytest.mark.asyncio
async def test_side_stop_appends_no_row_when_turn_ends_normally(tmp_path, monkeypatch):
    """If ``provider.cancel()`` lets the prompt finish normally, no stopped row.

    Opus FINDING: ``provider.cancel()`` can end the prompt before ``task.cancel()``
    lands, so the turn takes its SUCCESS path (real answer appended + broadcast)
    and ``task.cancelled()`` is False. The handler must NOT then append a second
    ``(side response stopped)`` row and terminal error frame over the delivered
    answer. The settle is gated on ``task.cancelled()``.
    """
    started = asyncio.Event()

    async def _finishing(state, slot, run_id, question, *, is_first_turn, start_priority=None):
        started.set()
        # The turn completes normally (no cancellation): append its real answer
        # and settle, mirroring _run_side_turn's success + finally path.
        if slot._side is not None:
            slot._side.append_assistant("the real answer")
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # Swallow the cancel (do NOT re-raise) so task.cancelled() is False —
            # the "ended normally during the cancel window" condition.
            if slot._side is not None and slot._side.last_run_id == run_id:
                slot._side.is_complete = True
                slot._side.task = None
            return

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", _finishing)
    state = _make_state(tmp_path)
    state.sessions.destroy = AsyncMock()
    events = _capture_broadcasts(state)
    state.get_or_create_slot("parent")

    provider = MagicMock(name="side provider")
    provider.cancel = AsyncMock()
    state.sessions.get_provider = MagicMock(return_value=provider)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        turn = await client.post("/api/chat/slots/parent/side/turn", json={"question": "q?"})
        run_id = (await turn.json())["run_id"]
        await await_until(started)
        stop = await client.post("/api/chat/slots/parent/side/stop", json={})
        assert (await stop.json()) == {"ok": True}
        slot = state._slots["parent"]

    # No "(side response stopped)" row was appended over the delivered answer.
    assert not any(
        m["role"] == "assistant" and "stopped" in m["content"] for m in slot._side.messages
    ), "a stopped row was appended over a normally-completed turn"
    # No terminal error frame for this run from the stop handler.
    stop_terminals = [
        d
        for _t, d in events
        if d.get("role") == "assistant"
        and d.get("final")
        and d.get("run_id") == run_id
        and d.get("is_error")
        and "stopped" in str(d.get("content", ""))
    ]
    assert not stop_terminals, "a spurious stopped terminal frame was broadcast"


@pytest.mark.asyncio
async def test_a_submit_during_the_stop_window_does_not_replace_the_cancelled_turn(
    tmp_path, monkeypatch
):
    """GPT F1 (fenced) / Opus: a concurrent /side/turn mid-stop must NOT dispatch.

    The subtle seam: clearing ``side.task`` alone does not close the race,
    because the cancelled turn's own ``finally`` flips ``is_complete = True``
    synchronously as it unwinds — and that lands DURING the stop handler's
    bounded await of the task cleanup, before the handler resumes to record the
    stopped row. In that window ``is_complete`` reads idle, so a ``POST
    /side/turn`` that finished parsing its body would pass the busy gate,
    dispatch a REPLACEMENT, and the stop would then return ``superseded``,
    leaving the cancelled turn's user line unanswered in the history fed to the
    next turn (security-class). The ``is_stopping`` claim keeps the turn busy
    through cleanup: the busy gate honours it, so the concurrent submit is
    QUEUED (not dispatched) even AFTER the finally flipped ``is_complete``, and
    the cancelled turn settles with its stopped row with no replacement over it.

    This test forces the submit into exactly that post-flip window: the stub's
    cancellation handler flips ``is_complete = True`` (mirroring the real
    ``finally``) and then PARKS, and the concurrent submit is issued while it is
    parked there — so the only thing keeping the busy gate shut is
    ``is_stopping``.
    """
    release = asyncio.Event()
    started = asyncio.Event()
    in_cancel_cleanup = asyncio.Event()
    hold_cancel_cleanup = asyncio.Event()
    dispatched_runs: list[str] = []

    async def _blocking(state, slot, run_id, question, *, is_first_turn, start_priority=None):
        dispatched_runs.append(run_id)
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            # Mirror the real _run_side_turn finally: flip is_complete True
            # SYNCHRONOUSLY as the turn unwinds, THEN park so a concurrent submit
            # can race in while is_complete already reads idle. Only is_stopping
            # keeps the busy gate shut in this window.
            if slot._side is not None and slot._side.last_run_id == run_id:
                slot._side.is_complete = True
                slot._side.task = None
            in_cancel_cleanup.set()
            await hold_cancel_cleanup.wait()
            raise

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", _blocking)
    state = _make_state(tmp_path)
    state.sessions.destroy = AsyncMock()
    events = _capture_broadcasts(state)
    state.get_or_create_slot("parent")

    # provider.cancel returns immediately; the task's own cancel-cleanup is where
    # we park (after the is_complete flip), via the stub above.
    provider = MagicMock(name="side provider")
    provider.cancel = AsyncMock()
    state.sessions.get_provider = MagicMock(return_value=provider)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        turn = await client.post("/api/chat/slots/parent/side/turn", json={"question": "q1"})
        run_id = (await turn.json())["run_id"]
        await await_until(started)
        slot = state._slots["parent"]

        # Stop R1: task.cancel() lands, the stub flips is_complete=True and parks
        # in its cancel-cleanup. The stop handler is now awaiting that cleanup.
        stop = asyncio.ensure_future(client.post("/api/chat/slots/parent/side/stop", json={}))
        await await_until(in_cancel_cleanup)

        # The exact GPT seam: is_complete is already True here, so WITHOUT the
        # is_stopping claim the busy gate would read idle and dispatch q2 as a
        # replacement. is_stopping must keep it QUEUED.
        assert slot._side.is_complete is True, "precondition: finally flipped is_complete"
        assert slot._side.is_stopping is True, "stop must hold the busy claim"
        r2 = await client.post("/api/chat/slots/parent/side/turn", json={"question": "q2"})
        assert r2.status in (200, 202, 409), r2.status

        # Let the stop finish its cancel-cleanup + settle.
        hold_cancel_cleanup.set()
        stop_resp = await await_until(stop)
        assert (await stop_resp.json()) == {"ok": True}

    # Only the original turn was ever dispatched to _run_side_turn — the submit
    # did not run as a replacement over the cancelling turn.
    assert dispatched_runs == [run_id], f"a replacement turn dispatched: {dispatched_runs}"
    # The stop released the busy claim once it settled.
    assert slot._side.is_stopping is False, "is_stopping must be cleared after settle"
    # The cancelled turn settled with its stopped row (not left unanswered).
    assert any(
        m["role"] == "assistant" and "stopped" in m["content"] for m in slot._side.messages
    ), "the cancelled turn's user line was left unanswered"
    terminal = [
        d
        for _t, d in events
        if d.get("role") == "assistant" and d.get("final") and d.get("run_id") == run_id
    ]
    assert terminal, "no terminal frame settled the cancelled turn"


@pytest.mark.asyncio
async def test_side_stop_settles_the_client_when_cleanup_wedges(tmp_path, monkeypatch):
    """Design FINDING: a wedged cleanup (timeout) must still broadcast the frame.

    If the task's finally hangs past ``_STOP_CLEANUP_TIMEOUT`` the awaited
    cleanup times out and the task is still running — ``task.cancelled()`` is
    False, but this is NOT a normal completion. Folding it into the
    completed-normally branch would return without a terminal frame, leaving the
    panel streaming in the exact deep-hang the endpoint exists to escape. The
    handler must settle the stopped row + terminal frame on timeout, mark the
    sidecar complete, and record ``settled=timeout`` in SEL.
    """
    started = asyncio.Event()

    async def _wedged(state, slot, run_id, question, *, is_first_turn, start_priority=None):
        started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            # The finally is WEDGED: swallow the cancel and keep running past the
            # stop handler's cleanup budget (never clears is_complete/task).
            await asyncio.sleep(3600)

    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._run_side_turn", _wedged)
    # Shrink the cleanup budget so the test does not wait the real timeout.
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side._STOP_CLEANUP_TIMEOUT", 0.05)
    state = _make_state(tmp_path)
    state.sessions.destroy = AsyncMock()
    events = _capture_broadcasts(state)
    state.get_or_create_slot("parent")

    provider = MagicMock(name="side provider")
    provider.cancel = AsyncMock()
    state.sessions.get_provider = MagicMock(return_value=provider)

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        turn = await client.post("/api/chat/slots/parent/side/turn", json={"question": "hangs?"})
        run_id = (await turn.json())["run_id"]
        await await_until(started)
        slot = state._slots["parent"]

        stop = await client.post("/api/chat/slots/parent/side/stop", json={})
        assert (await stop.json()) == {"ok": True}

    # Even though the task never finished its cleanup, the panel is settled:
    # is_complete flipped, a stopped row appended, and a terminal frame sent.
    assert slot._side.is_complete, "a wedged cleanup left the sidecar busy"
    assert any(
        m["role"] == "assistant" and "stopped" in m["content"] for m in slot._side.messages
    ), "no stopped row on a wedged cleanup"
    terminal = [
        d
        for _t, d in events
        if d.get("role") == "assistant" and d.get("final") and d.get("run_id") == run_id
    ]
    assert terminal, "a wedged cleanup did not broadcast a terminal frame"
    assert terminal[-1]["is_error"] is True


@pytest.mark.asyncio
async def test_side_stop_is_idempotent_when_idle(tmp_path, monkeypatch):
    """Stop with no turn in flight reports stopped=False rather than erroring.

    A stop pressed just as the turn settles is a race the UI must survive.
    """
    _stub_run_side_turn(monkeypatch)
    state = _make_state(tmp_path)
    state.sessions.destroy = AsyncMock()
    state.get_or_create_slot("parent")

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/chat/slots/parent/side/open", json={})
        # Idle sidecar, no turn ever started.
        stop = await client.post("/api/chat/slots/parent/side/stop", json={})
        assert stop.status == 200
        assert await stop.json() == {"ok": True}


@pytest.mark.asyncio
async def test_side_stop_on_missing_or_closed_side(tmp_path, monkeypatch):
    """Stop on a slot with no open sidecar is a 409, not a 500."""
    _stub_run_side_turn(monkeypatch)
    state = _make_state(tmp_path)
    state.sessions.destroy = AsyncMock()
    state.get_or_create_slot("parent")

    app = _make_side_app(state)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/slots/parent/side/stop", json={})
        assert resp.status == 409
        body = await resp.json()
        assert body["code"] == "side_not_open"
