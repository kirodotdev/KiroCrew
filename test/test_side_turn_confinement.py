"""The read-only confinement of a /side turn, from its approval policy to its backend's tools.

A side turn streams under READ_ONLY with the gateway's live hook gate, or with no hooks when
there is none, and a dashboard-bound governance profile governs the ``side:`` key, a claude
WebSearch included. On kiro-cli and KAS the session is bound to the derived
``<agent>--readonly`` spec, and a turn whose spec cannot be derived is refused without a
session. A retained session whose binding changed, or that no binding vouches for, is
destroyed before the turn acquires one; a close landing mid-turn leaves no ownerless
session, a turn from a replaced sidecar never touches the replacement's session, a project
change during derivation keeps the shadow check and the spawn on the project the turn read,
and ``/side/close`` destroys the closing generation's session. claude runs read-only on a
confined session and goose on its own goose home; an unverified claude adapter and a backend
outside the read-only set run no tools, and ``/api/chat/side/tools`` reports what a turn
would run. The /side conversation invariants, the app-agent refusal and self-heal, and
``/side/stop`` are pinned in ``test_side.py``; the shared constants, app factory, broadcast
recorder and autouse spec-publisher stub live in ``side_helpers.py``.
"""

from __future__ import annotations

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

from kiro_crew.dashboard.handlers.side import _run_side_turn
from kiro_crew.dashboard.side_prompts import SIDE_BOUNDARY_PROMPT
from kiro_crew.dashboard.side_state import SideState


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


def test_dashboard_bound_profile_governs_a_claude_side_read(tmp_path, monkeypatch):
    """A profile that denies ``web_search`` refuses claude's WebSearch on a side
    turn too. claude states no ``_meta.kiro`` identity and its title is prose
    (``Search "…"``), so only the mapped ``harness_builtin_tool`` can meet the
    rule; without it the read-only proof would approve the denied search."""
    import json

    from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY, HookManager
    from kiro_crew.platform import governance_profiles as gp

    profiles = tmp_path / "profiles"
    profiles.mkdir()
    monkeypatch.setattr(gp, "_PROFILES_DIR", profiles)
    gp.reset_store()
    try:
        gate = HookManager()

        def _claude_search():
            return gate.on_tool_call(
                'Search "release notes"',
                session_key="side:parent",
                agent="kirocrew",
                tool_kind="fetch",
                harness_builtin_tool="web_search",
                classifier_only=True,
            )

        unbound = _claude_search()
        assert unbound.action == TOOL_AUTO_APPROVE and unbound.read_only

        (profiles / "no-web-search.json").write_text(
            json.dumps(
                {
                    "name": "no-web-search",
                    "bind": {"type": "surface", "id": "dashboard"},
                    "tools": {"mode": "deny", "deny": ["web_search"]},
                }
            )
        )
        gp.reset_store()

        denied = _claude_search()
        assert denied.action == TOOL_DENY
        assert "governance" in (denied.reason or "").lower()
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
@pytest.mark.parametrize("backend", ["", "kas"], ids=["kiro", "kas"])
async def test_side_turn_grants_read_only_tools_on_a_member_backend(
    tmp_path, monkeypatch, _published_readonly_spec, backend
):
    """On a member of ``ACP_BACKENDS_SIDE_READONLY`` (kiro-cli, KAS) the turn
    derives the read-only spec and streams READ_ONLY; the allowance is positive
    membership, never a negation of some other backend."""
    from kiro_crew.acp_backends import ACP_BACKENDS_SIDE_READONLY
    from kiro_crew.llm_helpers import ToolApprovalPolicy

    assert backend in ACP_BACKENDS_SIDE_READONLY
    _configure_backend(monkeypatch, backend)
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


def _side_turn_fixture(tmp_path, run_id: str):
    """A state whose parent slot has an open sidecar with one pending question,
    plus the list the fake ``get_or_create`` records each call's kwargs into."""
    state = _make_state(tmp_path)
    events = _capture_broadcasts(state)
    parent = state.get_or_create_slot("parent")
    parent._side = SideState(open=True, created_at="2026-01-01T00:00:00Z")
    parent._side.append_user(_SIDE_QUESTION)
    parent._side.last_run_id = run_id
    parent._side.is_complete = False
    created: list[dict] = []

    async def _fake_get_or_create(key, **kwargs):
        created.append(kwargs)
        return MagicMock(), True, False

    state.sessions.get_or_create = _fake_get_or_create
    state.sessions.release = MagicMock()
    return state, parent, events, created


@pytest.mark.asyncio
async def test_side_turn_on_another_backend_runs_no_tools_and_derives_nothing(
    tmp_path, monkeypatch, _published_readonly_spec
):
    """Off the capability set (opencode here) the harness has its own
    pre-approval surface the gate cannot see, so the turn keeps the pre-allowance
    posture: the base agent under REJECT_ALL, no derived spec, and a prompt and
    fallback that say tools are unavailable."""
    from kiro_crew.acp_backends import ACP_BACKEND_OPENCODE
    from kiro_crew.llm_helpers import ToolApprovalPolicy

    _configure_backend(monkeypatch, ACP_BACKEND_OPENCODE)
    state, parent, events, created = _side_turn_fixture(tmp_path, "run-opencode")
    # An empty answer exercises the fallback copy for this branch.
    stream_mock = AsyncMock(return_value="")
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", stream_mock)

    await _run_side_turn(state, parent, "run-opencode", _SIDE_QUESTION, is_first_turn=True)

    assert _published_readonly_spec == [], "no derived spec on a non-kiro backend"
    assert not (created[0]["agent"] or "").endswith("--readonly")
    assert "side_read_only" not in created[0]
    assert stream_mock.await_args.kwargs["approval_policy"] is ToolApprovalPolicy.REJECT_ALL
    prompt = stream_mock.await_args.args[1]
    assert "tools are unavailable here" in prompt
    assert "lookups work here, but changes don't" not in prompt
    last = [d for t, d in events if d.get("role") == "assistant" and d.get("final")][-1]
    assert "can't use tools on this agent backend" in last["content"]
    assert parent._side.binding == (created[0]["agent"] or "", "", "reject_all")


@pytest.mark.asyncio
async def test_side_turn_on_claude_runs_read_only_on_a_confined_session(
    tmp_path, monkeypatch, _published_readonly_spec
):
    """On claude the confinement is the session's own options, not a kiro agent
    spec: nothing is derived, the base agent's session is requested with
    ``side_read_only`` (which the claude client turns into the read-only shape),
    the turn streams READ_ONLY, and the prompt names no shell, because the
    session has none."""
    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE
    from kiro_crew.dashboard.handlers import side as side_mod
    from kiro_crew.llm_helpers import ToolApprovalPolicy

    _configure_backend(monkeypatch, ACP_BACKEND_CLAUDE)
    monkeypatch.setattr(side_mod, "claude_adapter_honours_session_options", lambda: True)
    state, parent, _events, created = _side_turn_fixture(tmp_path, "run-claude")
    stream_mock = AsyncMock(return_value=_SIDE_ANSWER)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", stream_mock)

    await _run_side_turn(state, parent, "run-claude", _SIDE_QUESTION, is_first_turn=True)

    assert _published_readonly_spec == [], "claude reads no kiro agent spec"
    assert created[0]["side_read_only"] is True
    assert not (created[0]["agent"] or "").endswith("--readonly")
    assert stream_mock.await_args.kwargs["approval_policy"] is ToolApprovalPolicy.READ_ONLY
    prompt = stream_mock.await_args.args[1]
    assert "lookups work here, but changes don't" in prompt
    assert "There is no shell here" in prompt
    assert "git status" not in prompt
    assert parent._side.binding == (created[0]["agent"] or "", "", "claude_read_only")


@pytest.mark.asyncio
async def test_side_turn_on_goose_runs_read_only_on_its_own_goose_home(
    tmp_path, monkeypatch, _published_readonly_spec
):
    """On goose the confinement is the separate goose home its launch builds for
    ``side_read_only``: nothing is derived, the turn streams READ_ONLY, and the
    prompt names neither a shell nor a file-text read, because there is neither."""
    from kiro_crew.acp_backends import ACP_BACKEND_GOOSE
    from kiro_crew.dashboard.handlers import side as side_mod
    from kiro_crew.dashboard.side_prompts import SIDE_BOUNDARY_PROMPT_LISTING
    from kiro_crew.llm_helpers import ToolApprovalPolicy

    _configure_backend(monkeypatch, ACP_BACKEND_GOOSE)
    monkeypatch.setattr(side_mod, "goose_side_home_supported", lambda: True)
    state, parent, _events, created = _side_turn_fixture(tmp_path, "run-goose")
    stream_mock = AsyncMock(return_value=_SIDE_ANSWER)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", stream_mock)

    await _run_side_turn(state, parent, "run-goose", _SIDE_QUESTION, is_first_turn=True)

    assert _published_readonly_spec == [], "goose reads no kiro agent spec"
    assert created[0]["side_read_only"] is True
    assert not (created[0]["agent"] or "").endswith("--readonly")
    assert stream_mock.await_args.kwargs["approval_policy"] is ToolApprovalPolicy.READ_ONLY
    prompt = stream_mock.await_args.args[1]
    assert SIDE_BOUNDARY_PROMPT_LISTING in prompt
    assert "git status" not in prompt
    assert parent._side.binding == (created[0]["agent"] or "", "", "goose_read_only")


@pytest.mark.asyncio
async def test_side_turn_on_claude_with_an_unverified_adapter_runs_no_tools(
    tmp_path, monkeypatch, _published_readonly_spec
):
    """An adapter below the verified options floor may ignore the read-only
    shape, so the turn runs REJECT_ALL on an ordinary session instead."""
    from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE
    from kiro_crew.dashboard.handlers import side as side_mod
    from kiro_crew.llm_helpers import ToolApprovalPolicy

    _configure_backend(monkeypatch, ACP_BACKEND_CLAUDE)
    monkeypatch.setattr(side_mod, "claude_adapter_honours_session_options", lambda: False)
    state, parent, _events, created = _side_turn_fixture(tmp_path, "run-old-claude")
    stream_mock = AsyncMock(return_value=_SIDE_ANSWER)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.side.stream_and_collect", stream_mock)

    await _run_side_turn(state, parent, "run-old-claude", _SIDE_QUESTION, is_first_turn=True)

    assert "side_read_only" not in created[0]
    assert stream_mock.await_args.kwargs["approval_policy"] is ToolApprovalPolicy.REJECT_ALL
    assert "tools are unavailable here" in stream_mock.await_args.args[1]
    assert parent._side.binding == (created[0]["agent"] or "", "", "reject_all")


def _claude_adapter_remedy() -> dict[str, str]:
    """The floor and update step the outdated-adapter footer names, from the
    adapter's own constants rather than restated digits."""
    from kiro_crew.acp.client import CLAUDE_ACP_NPM_PKG, CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION

    return {
        "claude_adapter_min_version": ".".join(map(str, CLAUDE_ACP_SETTING_SOURCES_MIN_VERSION)),
        "claude_adapter_update_command": f"npm i -g {CLAUDE_ACP_NPM_PKG}",
    }


_CLAUDE_ADAPTER_REMEDY = _claude_adapter_remedy()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("backend", "adapter_verified", "read_only_tools", "claude_adapter_outdated"),
    [
        ("kiro", False, True, False),
        ("claude", True, True, False),
        ("claude", False, False, True),
        ("opencode", True, False, False),
        ("goose", False, True, False),
    ],
)
async def test_side_tools_answers_what_a_side_turn_runs(
    tmp_path, monkeypatch, backend, adapter_verified, read_only_tools, claude_adapter_outdated
):
    """The footer reads this, so it must give the turn's answer, adapter floor
    included: an old claude adapter must not show a footer that promises lookups,
    and is named as the reason so the footer can say to update it."""
    from kiro_crew.acp_backends import (
        ACP_BACKEND_CLAUDE,
        ACP_BACKEND_GOOSE,
        ACP_BACKEND_KIRO,
        ACP_BACKEND_OPENCODE,
    )
    from kiro_crew.dashboard.handlers import side as side_mod

    backends = {
        "goose": ACP_BACKEND_GOOSE,
        "kiro": ACP_BACKEND_KIRO,
        "claude": ACP_BACKEND_CLAUDE,
        "opencode": ACP_BACKEND_OPENCODE,
    }
    _configure_backend(monkeypatch, backends[backend])
    monkeypatch.setattr(
        side_mod, "claude_adapter_honours_session_options", lambda: adapter_verified
    )
    monkeypatch.setattr(side_mod, "goose_side_home_supported", lambda: True)
    async with TestClient(TestServer(_make_side_app(_make_state(tmp_path)))) as client:
        resp = await client.get("/api/chat/side/tools")
        assert resp.status == 200
        assert await resp.json() == {
            "read_only_tools": read_only_tools,
            "claude_adapter_outdated": claude_adapter_outdated,
            **_CLAUDE_ADAPTER_REMEDY,
            "config_unavailable": False,
        }


@pytest.mark.asyncio
async def test_side_tools_is_false_when_the_config_does_not_load(tmp_path, monkeypatch):
    """A turn whose config cannot load runs no tools, so the footer says so too."""
    from kiro_crew.dashboard.handlers import side as side_mod

    def _unreadable(*args, **kwargs):
        raise OSError("config unreadable")

    monkeypatch.setattr(side_mod.KiroCrewConfig, "load", staticmethod(_unreadable))
    async with TestClient(TestServer(_make_side_app(_make_state(tmp_path)))) as client:
        resp = await client.get("/api/chat/side/tools")
        assert await resp.json() == {
            "read_only_tools": False,
            "claude_adapter_outdated": False,
            **_CLAUDE_ADAPTER_REMEDY,
            "config_unavailable": True,
        }
