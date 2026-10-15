"""``AcpRuntime`` session start.

The served-default check after ``set_mode``, the ``availableModes`` guard and the
launch-agent verification around mode activation, and the session-start timeout
budget handed to ``session/new``, ``session/load`` and ``set_mode``.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import (
    _feed,
    _make_runtime,
    _register,
    _replying_send,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp.runtime import (
    _REQUEST_TIMEOUT,
    _SESSION_NEW_TIMEOUT,
    AcpRuntime,
    AcpRuntimeError,
    AcpSessionHandle,
)
from kiro_crew.acp.types import (
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_TERMINATE,
    METHOD_SET_MODE,
)
from kiro_crew.testing.wait import default_timeout


class TestServedDefaultAfterSetMode:
    """The served-default check must also judge the model ``set_mode`` applies.

    Both session-start paths run ``ensure_served_default`` right after storing the
    session/new or session/load response, which is before the mode is activated.
    kiro-cli's ``set_mode`` puts the session on the model the activated agent's
    spec pins, undoing that earlier switch, and reports that model only in the
    ``_kiro.dev/agent/switched`` frame it sends before answering ``{}``. With
    ``agent.model`` set to a model the account cannot run (it is copied into
    ``kirocrew.json``), the first prompt failed with "no access to model".
    """

    UNSERVED = "probe-unserved-model"
    ADVERTISED = [{"modelId": "auto"}, {"modelId": "gpt-5.6-sol"}, {"modelId": "glm-5"}]

    def _rig(self, monkeypatch, *, current, switched):
        """*switched* is the ``params`` of the frame set_mode sends, or None for none."""
        from kiro_crew.acp.types import ACP_BACKEND_KIRO

        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        rt._acp_backend = ACP_BACKEND_KIRO

        async def _fake_send(method, params, timeout=None):
            if method in (METHOD_SESSION_NEW, METHOD_SESSION_LOAD):
                return {
                    "sessionId": "sid-new",
                    "modes": {"currentModeId": "kirocrew"},
                    "models": {"currentModelId": current, "availableModels": self.ADVERTISED},
                }
            if method == METHOD_SET_MODE and switched is not None:
                # What the reader loop does with the frame kiro-cli sends first.
                rt._note_switched_model(params["sessionId"], switched)
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        routed = _replying_send(lambda params: rt._session_queues[params["sessionId"]])
        monkeypatch.setattr(rt, "send_request", routed)
        return rt, routed

    @staticmethod
    def _set_models(routed) -> list[str]:
        from kiro_crew.acp.types import METHOD_SET_MODEL

        return [
            c.args[1]["modelId"] for c in routed.await_args_list if c.args[0] == METHOD_SET_MODEL
        ]

    async def _start(self, rt, path):
        if path == "create":
            return await rt.create_session(cwd="/work", agent="kirocrew")
        return await rt.load_session("/f.json", "sid-new", cwd="/work", agent="kirocrew")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["create", "load"])
    async def test_an_unserved_activated_model_is_switched_off(self, monkeypatch, path):
        rt, routed = self._rig(
            monkeypatch,
            current="gpt-5.6-sol",
            switched={"agentName": "kirocrew", "model": self.UNSERVED},
        )

        handle = await self._start(rt, path)

        assert self._set_models(routed) == ["auto"]
        assert handle.served_model == "auto"
        # The intent is untouched: the session still INHERITS.
        assert handle.model == ""
        assert "sid-new" not in rt._switched_models

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["create", "load"])
    async def test_an_unserved_default_is_switched_off_twice(self, monkeypatch, path):
        # session/new hands out the unserved id, and set_mode puts it back.
        rt, routed = self._rig(
            monkeypatch, current=self.UNSERVED, switched={"model": self.UNSERVED}
        )

        handle = await self._start(rt, path)

        assert self._set_models(routed) == ["auto", "auto"]
        assert handle.served_model == "auto"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["create", "load"])
    async def test_a_served_activated_model_is_adopted_without_a_switch(self, monkeypatch, path):
        rt, routed = self._rig(monkeypatch, current="gpt-5.6-sol", switched={"model": "glm-5"})

        handle = await self._start(rt, path)

        assert self._set_models(routed) == []
        assert handle.served_model == "glm-5"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "switched",
        [None, {"agentName": "kirocrew"}, {"model": "  "}, {"model": None}],
        ids=["no-frame", "no-model", "blank", "null"],
    )
    async def test_no_reported_model_is_no_evidence(self, monkeypatch, switched):
        rt, routed = self._rig(monkeypatch, current="gpt-5.6-sol", switched=switched)

        handle = await self._start(rt, "create")

        assert self._set_models(routed) == []
        assert handle.served_model == "gpt-5.6-sol"

    @pytest.mark.asyncio
    async def test_an_earlier_frame_is_not_judged_as_this_activation(self, monkeypatch):
        # A record left by an earlier switch on the same session id names a model
        # this activation never applied.
        rt, routed = self._rig(monkeypatch, current="gpt-5.6-sol", switched=None)
        rt._switched_models["sid-new"] = self.UNSERVED

        handle = await self._start(rt, "load")

        assert self._set_models(routed) == []
        assert handle.served_model == "gpt-5.6-sol"

    @pytest.mark.asyncio
    async def test_the_reader_records_the_switched_frames_model(self):
        rt, reader, _ = _make_runtime()
        queues = _register(rt, "sA")
        task = await _start_reader(rt)
        try:
            frame = {
                "jsonrpc": "2.0",
                "method": "_kiro.dev/agent/switched",
                "params": {
                    "sessionId": "sA",
                    "agentName": "kirocrew",
                    "previousAgentName": "kiro_default",
                    "welcomeMessage": None,
                    "model": self.UNSERVED,
                },
            }
            _feed(reader, frame)
            msg = await asyncio.wait_for(queues["sA"].get(), timeout=default_timeout())
            # Recorded no later than queued, and the frame still reaches the session.
            assert rt._switched_models == {"sA": self.UNSERVED}
            assert msg.method == "_kiro.dev/agent/switched"
        finally:
            await _stop_reader(task)
        rt.unregister_session("sA")
        assert rt._switched_models == {}


# ── set_mode availableModes guard (regression: "Mode '<agent>' not found") ──


def _new_resp(modes: dict | None) -> dict:
    r: dict = {"sessionId": "s1"}
    if modes is not None:
        r["modes"] = modes
    return r


@pytest.mark.asyncio
async def test_create_session_sets_mode_when_agent_is_advertised():
    """Happy path: the requested agent is in availableModes → set_mode fires."""
    rt, _, _ = _make_runtime()
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    resp = _new_resp(
        {"currentModeId": "kirocrew", "availableModes": [{"id": "kirocrew"}, {"id": "ops"}]}
    )
    rt._send_and_await = AsyncMock(side_effect=[resp, {}])  # type: ignore[method-assign]
    with patch.object(AcpSessionHandle, "drain_init", AsyncMock()):
        handle = await rt.create_session(agent="ops", mcp_servers=[])
    methods = [c.args[0] for c in rt._send_and_await.call_args_list]
    assert methods == [METHOD_SESSION_NEW, METHOD_SET_MODE]
    assert rt._send_and_await.call_args_list[1].args[1] == {
        "sessionId": "s1",
        "modeId": "ops",
    }
    assert handle.session_id == "s1"


@pytest.mark.asyncio
async def test_create_session_fails_closed_when_agent_not_advertised():
    """Guard (A): modes advertised but the agent is absent → FAIL CLOSED
    (terminate + raise), never silently run the backend default. Substituting a
    broader default for a requested restricted agent would be a privilege
    escalation."""
    rt, _, _ = _make_runtime()
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    resp = _new_resp({"currentModeId": "default", "availableModes": [{"id": "default"}]})
    # session/new response, then the terminate roundtrip from the fail-closed path
    rt._send_and_await = AsyncMock(side_effect=[resp, {}])  # type: ignore[method-assign]
    with patch.object(AcpSessionHandle, "drain_init", AsyncMock()):
        with pytest.raises(AcpRuntimeError, match="not available") as exc:
            await rt.create_session(agent="kirocrew", mcp_servers=[])
    # An ordinary agent keeps the materialize hint: setup does write that file.
    assert "kirocrew setup --agent-only" in str(exc.value)
    methods = [c.args[0] for c in rt._send_and_await.call_args_list]
    assert METHOD_SET_MODE not in methods  # never activated the wrong mode
    assert METHOD_SESSION_TERMINATE in methods  # created session cleaned up
    assert "s1" not in rt._session_queues  # unregistered


@pytest.mark.asyncio
async def test_create_session_refusal_explains_a_derived_readonly_spec(tmp_path, monkeypatch):
    """The side turn's ``<agent>--readonly`` spec is written by the side turn,
    never by ``kirocrew setup``, and kiro-cli lists agents only at process start.
    The refusal must say that and name the base agent -- the setup hint sent the
    user to a command that cannot create the file. The spec is published by the
    real publisher, because the owner marker on it is what the wording keys on."""
    from kiro_crew import agent as agent_mod
    from kiro_crew.dashboard import side_readonly_spec as srs

    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", tmp_path)
    monkeypatch.setattr(srs, "_refresh_materialized_snapshot", lambda: None)
    (tmp_path / "scout.json").write_text(json.dumps({"name": "scout"}), encoding="utf-8")
    assert srs.publish_readonly_spec("scout").name == "scout--readonly"
    rt, _, _ = _make_runtime()
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    resp = _new_resp({"currentModeId": "kirocrew", "availableModes": [{"id": "kirocrew"}]})
    rt._send_and_await = AsyncMock(side_effect=[resp, {}])  # type: ignore[method-assign]
    with patch.object(AcpSessionHandle, "drain_init", AsyncMock()):
        with pytest.raises(AcpRuntimeError, match="not available") as exc:
            await rt.create_session(agent="scout--readonly", mcp_servers=[])
    message = str(exc.value)
    assert "kirocrew setup --agent-only" not in message
    assert "is likely missing" not in message
    assert "read-only spec Kiro Crew derives from 'scout'" in message
    assert "name 'scout'" in message
    assert "Refusing to run the backend default mode kirocrew in its place" in message


@pytest.mark.asyncio
async def test_create_session_fails_closed_when_available_modes_empty():
    """An explicitly-empty `availableModes: []` is
    ADVERTISED (not absent), so it must fail closed — not be treated as
    "no modes → attempt" and then fault with "Mode not found"."""
    rt, _, _ = _make_runtime()
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    resp = _new_resp({"currentModeId": "kirocrew", "availableModes": []})
    rt._send_and_await = AsyncMock(side_effect=[resp, {}])  # type: ignore[method-assign]
    with patch.object(AcpSessionHandle, "drain_init", AsyncMock()):
        with pytest.raises(AcpRuntimeError, match="not available"):
            await rt.create_session(agent="kirocrew", mcp_servers=[])
    methods = [c.args[0] for c in rt._send_and_await.call_args_list]
    assert METHOD_SET_MODE not in methods
    assert METHOD_SESSION_TERMINATE in methods


@pytest.mark.asyncio
async def test_create_session_fails_closed_when_spawn_agent_not_advertised():
    """Guard (A2): the agent `--agent` selected is absent from the advertised
    modes, so its spec never loaded (missing, or rejected wholesale on an unknown
    field) and kiro-cli silently fell back to its own default agent. That default
    mounts none of Kiro Crew's control plane while the global provider mcp.json
    stays merged, so third-party servers keep working and every Crew tool the
    injected prompt names -- learn_add among them -- answers "does not exist".
    No override is passed, which is exactly why Guard (A) cannot catch it."""
    rt, _, _ = _make_runtime()
    rt._agent = "kirocrew"
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    resp = _new_resp({"currentModeId": "default", "availableModes": [{"id": "default"}]})
    # session/new response, then the terminate roundtrip from the fail-closed path
    rt._send_and_await = AsyncMock(side_effect=[resp, {}])  # type: ignore[method-assign]
    with patch.object(AcpSessionHandle, "drain_init", AsyncMock()):
        with pytest.raises(AcpRuntimeError, match="spawned with --agent"):
            await rt.create_session(mcp_servers=[])
    methods = [c.args[0] for c in rt._send_and_await.call_args_list]
    assert METHOD_SET_MODE not in methods  # never activated the wrong mode
    assert METHOD_SESSION_TERMINATE in methods  # created session cleaned up
    assert "s1" not in rt._session_queues  # unregistered


@pytest.mark.asyncio
async def test_create_session_admits_spawn_agent_when_advertised():
    """Guard (A2) admits the ordinary case: the spawn agent IS advertised, so the
    session stands and no set_mode fires (nothing to switch to)."""
    rt, _, _ = _make_runtime()
    rt._agent = "kirocrew"
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    resp = _new_resp(
        {"currentModeId": "kirocrew", "availableModes": [{"id": "kirocrew"}, {"id": "ops"}]}
    )
    rt._send_and_await = AsyncMock(side_effect=[resp, {}])  # type: ignore[method-assign]
    with patch.object(AcpSessionHandle, "drain_init", AsyncMock()):
        handle = await rt.create_session(mcp_servers=[])
    methods = [c.args[0] for c in rt._send_and_await.call_args_list]
    assert METHOD_SET_MODE not in methods
    assert METHOD_SESSION_TERMINATE not in methods
    assert handle.session_id == "s1"


@pytest.mark.asyncio
async def test_create_session_fails_closed_when_current_mode_names_another_agent():
    """A `currentModeId`-only response naming a DIFFERENT agent is positive evidence
    of the substitution, so it fails closed even with no advertised list. Admitting
    a current-mode mismatch would let exactly this response through the
    compatibility escape: `_mode_available` returns True whenever no list was
    advertised, so the substituted agent would run with none of Kiro Crew's tools.
    A live probe of kiro-cli pins the meaning of the field -- a spec that loads is
    reported as the current mode, and a spec the backend refuses reports the
    backend's own default instead."""
    rt, _, _ = _make_runtime()
    rt._agent = "kirocrew"
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    # No availableModes at all -> parse_session_modes reports advertised=False.
    resp = _new_resp({"currentModeId": "kiro_default"})
    rt._send_and_await = AsyncMock(side_effect=[resp, {}])  # type: ignore[method-assign]
    with patch.object(AcpSessionHandle, "drain_init", AsyncMock()):
        with pytest.raises(AcpRuntimeError, match="not the agent this session is running"):
            await rt.create_session(mcp_servers=[])
    methods = [c.args[0] for c in rt._send_and_await.call_args_list]
    assert METHOD_SET_MODE not in methods
    assert METHOD_SESSION_TERMINATE in methods
    assert "s1" not in rt._session_queues


@pytest.mark.asyncio
async def test_create_session_spawn_agent_guard_admits_when_no_modes_advertised():
    """Guard (A2) is judged on the same evidence as Guard (A): a backend that
    advertises no `modes` list at all (older kiro-cli / offline fake backend) is
    no evidence of substitution, so the session is admitted."""
    rt, _, _ = _make_runtime()
    rt._agent = "kirocrew"
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    rt._send_and_await = AsyncMock(side_effect=[_new_resp(None), {}])  # type: ignore[method-assign]
    with patch.object(AcpSessionHandle, "drain_init", AsyncMock()):
        handle = await rt.create_session(mcp_servers=[])
    methods = [c.args[0] for c in rt._send_and_await.call_args_list]
    assert METHOD_SESSION_TERMINATE not in methods
    assert handle.session_id == "s1"


@pytest.mark.asyncio
async def test_create_session_admits_spawn_agent_named_as_current_mode():
    """Guard (A2) admits an agent the response names as the CURRENT mode even when
    the advertised list omits it. The guard asks "did my agent load", not "is the
    advertised list complete", so a backend that reports the active mode without
    listing it must not have its session torn down over that gap."""
    rt, _, _ = _make_runtime()
    rt._agent = "kirocrew"
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    resp = _new_resp({"currentModeId": "kirocrew", "availableModes": [{"id": "other"}]})
    rt._send_and_await = AsyncMock(side_effect=[resp, {}])  # type: ignore[method-assign]
    with patch.object(AcpSessionHandle, "drain_init", AsyncMock()):
        handle = await rt.create_session(mcp_servers=[])
    methods = [c.args[0] for c in rt._send_and_await.call_args_list]
    assert METHOD_SESSION_TERMINATE not in methods
    assert handle.session_id == "s1"


@pytest.mark.asyncio
async def test_activate_mode_bracketed_allows_the_launched_agent_every_start():
    """The session-start bracket activates the launched agent (``self._agent``)
    even with no prepared view, keyed on ``self._agent`` -- NOT on a stored
    ``spawn_agent_name`` (the shared runtime never sets one). A shared runtime
    starts many sessions as the same agent over its life, so the allowance holds
    on every start and nothing is consumed."""
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    rt, _, _ = _make_runtime()
    rt._agent = "kirocrew"
    # No spawn_agent_name on the shared runtime: the allowance is keyed on
    # self._agent alone.
    rt._native_skill_projection = NativeSkillProjection(aliases={})
    refreshed = NativeSkillProjection(aliases={})
    captured: dict = {}

    async def _send(method, params, *, timeout=None, **kwargs):
        captured["method"] = method
        captured["params"] = params
        captured["kwargs"] = kwargs
        return {}

    rt._send_and_await = AsyncMock(side_effect=_send)  # type: ignore[method-assign]

    with patch(
        "kiro_crew.acp.skill_projection.prepare_native_skill_projection",
        return_value=refreshed,
    ):
        await rt._activate_mode_bracketed(
            "s1", "kirocrew", budget=30.0, payload_snapshot=None, wire_registered=True
        )

    # The launched agent's authored name goes on the wire (translate=False), with
    # no spawn_agent_name needed or set anywhere.
    assert rt._native_skill_projection is refreshed
    assert captured["method"] == METHOD_SET_MODE
    assert captured["params"]["modeId"] == "kirocrew"
    assert captured["kwargs"].get("translate") is False
    assert refreshed.spawn_agent_name == ""


@pytest.mark.asyncio
async def test_activate_mode_bracketed_refresh_still_rejects_a_foreign_mode():
    """The launched-agent allowance is narrow: only ``self._agent`` passes with no
    view. A session start naming some OTHER unprepared mode still raises through the
    strict resolver -- an agent cannot escape its launch scope."""
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    rt, _, _ = _make_runtime()
    rt._agent = "kirocrew"
    rt._native_skill_projection = NativeSkillProjection(aliases={}, spawn_agent_name="kirocrew")
    refreshed = NativeSkillProjection(aliases={})
    # The strict resolver rejects a foreign mode BEFORE any send, so this must not run.
    rt._send_and_await = AsyncMock()  # type: ignore[method-assign]
    rt.terminate_session = AsyncMock()  # type: ignore[method-assign]

    with patch(
        "kiro_crew.acp.skill_projection.prepare_native_skill_projection",
        return_value=refreshed,
    ):
        with pytest.raises(ValueError, match="no prepared skill discovery view"):
            await rt._activate_mode_bracketed(
                "s1",
                "intruder",
                budget=30.0,
                payload_snapshot=None,
                wire_registered=True,
            )
    rt._send_and_await.assert_not_called()
    # The shared runtime does NOT set spawn_agent_name, and recognise() does not
    # carry it, so the refreshed projection carries NO launch name -- the bracket
    # allows the launched agent purely via self._agent, not a carried exemption.
    assert refreshed.spawn_agent_name == ""


@pytest.mark.asyncio
async def test_two_shared_sessions_both_start_as_the_launched_agent():
    """A shared runtime starts MANY sessions as self._agent. Each start brackets its
    own set_mode, so the launched-agent allowance must hold for the second session
    exactly as for the first: the allowance is per-start, not consumed by the first
    start. Both back-to-back starts send set_mode for the launched agent, with no
    prepared view, and neither raises."""
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    rt, _, _ = _make_runtime()
    rt._agent = "kirocrew"
    # The shared runtime does NOT set spawn_agent_name: the bracket allows the
    # launched agent purely via self._agent. Projections carry no launch name.
    rt._native_skill_projection = NativeSkillProjection(aliases={})
    rt._spawn_skill_projection = rt._native_skill_projection
    refreshed = NativeSkillProjection(aliases={})
    sent: list[dict] = []

    async def _send(method, params, *, timeout=None, **kwargs):
        if method == METHOD_SET_MODE:
            sent.append(params)
        return {}

    rt._send_and_await = AsyncMock(side_effect=_send)  # type: ignore[method-assign]

    with patch(
        "kiro_crew.acp.skill_projection.prepare_native_skill_projection",
        return_value=refreshed,
    ):
        for session_id in ("s1", "s2"):
            await rt._activate_mode_bracketed(
                session_id,
                "kirocrew",
                budget=30.0,
                payload_snapshot=None,
                wire_registered=True,
            )

    # Both sessions sent set_mode for the launched agent; the second did NOT fail,
    # and no spawn_agent_name was ever needed on the shared runtime.
    assert [p["modeId"] for p in sent] == ["kirocrew", "kirocrew"]
    assert refreshed.spawn_agent_name == ""


def test_resolve_start_alias_keeps_the_launch_agent_and_stays_strict_otherwise():
    """``_resolve_start_alias`` is the single launch-agent-aware resolver used at
    the initial resolution AND both supersession re-checks. It keeps the launched
    agent's authored name with no prepared view, and takes the strict resolver for
    every other mode (which raises for an unprepared one)."""
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    rt, _, _ = _make_runtime()
    rt._agent = "kirocrew"
    projection = NativeSkillProjection(aliases={})

    # The launched agent passes with no view, keeping its authored name.
    assert rt._resolve_start_alias(projection, "kirocrew") == "kirocrew"
    # A foreign unprepared mode is rejected by the strict resolver.
    with pytest.raises(ValueError, match="no prepared skill discovery view"):
        rt._resolve_start_alias(projection, "intruder")


def test_refuse_if_view_superseded_keeps_an_unchanged_launch_agent():
    """A concurrent no-view start must not be treated as superseded.

    When a concurrent no-view start adopts a newer EMPTY projection, a post-send
    supersession check that recomputes the current alias with the STRICT resolver
    finds no entry for the no-view launch agent, so ``newest != sent_alias`` and the
    start would raise, terminating a session that was never superseded. The
    launch-agent-aware resolver keeps the unchanged launch agent here: the launched
    agent's authored name is still its name in the newer view, so nothing is
    superseded and no raise occurs."""
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    rt, _, _ = _make_runtime()
    rt._agent = "kirocrew"
    # A newer (empty) projection is adopted at a higher generation than the start
    # used -- the concurrent-start condition under test.
    rt._native_skill_projection = NativeSkillProjection(aliases={})
    rt._skill_projection_generation = 7
    rt._skill_projection_unadopted = 0

    # sent_alias is the launch agent's authored name; used_generation is older than
    # the adopted one, forcing the supersession branch. With the launch-agent-aware
    # resolver, newest == sent_alias == "kirocrew", so this does NOT raise.
    rt._refuse_if_view_superseded("kirocrew", "kirocrew", used_generation=3)

    # A genuinely superseded FOREIGN agent (strict, no view in the newer projection)
    # still fails closed -- the fix is scoped to the launch agent.
    with pytest.raises(AcpRuntimeError):
        rt._refuse_if_view_superseded("intruder", "intruder-alias", used_generation=3)


@pytest.mark.asyncio
async def test_create_session_spawn_agent_guard_skipped_on_kas_backend():
    """Guard (A2) is restricted to the backend whose argv carries `--agent`. On KAS
    the agent travels over the wire and is activated by set_mode, which Guard (A)
    covers, so an advertised list without the runtime agent must not fail closed
    here."""
    from kiro_crew.acp.types import ACP_BACKEND_KAS

    rt, _, _ = _make_runtime()
    rt._agent = "kirocrew"
    rt._acp_backend = ACP_BACKEND_KAS
    assert (
        await rt._verify_spawn_agent_active(
            "s1",
            _new_resp({"currentModeId": "kas", "availableModes": [{"id": "kas"}]}),
            override=None,
        )
        is None
    )


@pytest.mark.asyncio
async def test_verify_spawn_agent_active_accepts_a_projected_agent_via_the_framed_response():
    """Guard (A2) accepts a projected agent because framing normalises its alias.

    The spawn argv forwards the view ALIAS for a projected agent, but every inbound
    frame passes through ``projection.frame`` before this guard reads it, and frame
    maps ``currentModeId`` from the alias back to the declared name. So the response
    this guard sees names the DECLARED name, and the plain ``current == spawn_agent``
    comparison accepts it -- no alias-matching set in the guard is needed.
    """
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    rt, _, _ = _make_runtime()
    rt._agent = "kirocrew"
    projection = NativeSkillProjection(aliases={"kirocrew": "native-alias-abc"})
    rt._native_skill_projection = projection
    # The backend reports the alias; framing maps it back to the declared name
    # before the guard, which is exactly what the guard then compares against.
    framed = projection.frame(
        {"currentModeId": "native-alias-abc", "availableModes": [{"id": "native-alias-abc"}]}
    )
    assert await rt._verify_spawn_agent_active("s1", _new_resp(framed), override=None) is None


@pytest.mark.asyncio
async def test_verify_spawn_agent_active_accepts_a_no_view_launch_agent_under_its_own_name():
    """Guard (A2) accepts a launch agent with NO prepared view spawned under its name.

    This PR lets a launch identity with no projected view spawn and activate under
    its own name (``spawn_agent`` returns it unchanged). The availability check
    must mirror that exemption independently of the direct-client switch exemption
    (``spawn_agent_name``, empty on the shared runtime): an unprojected launch
    agent the backend advertises under its own name is a valid session, not a
    substitution.
    """
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    rt, _, _ = _make_runtime()
    rt._agent = "kiro_default"
    # Projection holds a view for a DIFFERENT agent; kiro_default has none.
    rt._native_skill_projection = NativeSkillProjection(aliases={"other": "other-alias"})
    assert (
        await rt._verify_spawn_agent_active(
            "s1",
            _new_resp(
                {"currentModeId": "kiro_default", "availableModes": [{"id": "kiro_default"}]}
            ),
            override=None,
        )
        is None
    )


@pytest.mark.asyncio
async def test_verify_spawn_agent_active_still_fails_closed_on_substitution_with_a_projected_agent():
    """A backend default still fails closed under the plain declared-name comparison.

    When the backend ran its OWN default agent after refusing the spec, the
    reported current mode matches neither the declared name nor anything framing
    maps to it, so the guard still terminates and raises.
    """
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    rt, _, _ = _make_runtime()
    rt._agent = "kirocrew"
    rt._native_skill_projection = NativeSkillProjection(aliases={"kirocrew": "native-alias-abc"})
    rt.terminate_session = AsyncMock()  # type: ignore[method-assign]
    with pytest.raises(AcpRuntimeError, match="spawned with --agent"):
        await rt._verify_spawn_agent_active(
            "s1",
            _new_resp(
                {"currentModeId": "kiro_default", "availableModes": [{"id": "kiro_default"}]}
            ),
            override=None,
        )
    rt.terminate_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_verify_spawn_agent_active_still_fails_closed_when_declared_name_is_absent():
    """A genuine substitution still fails closed under the declared-name comparison.

    When the backend reports a DIFFERENT current mode than the launch identity, the
    guard still terminates and raises -- the plain comparison does not loosen the
    substitution check.
    """
    from kiro_crew.acp.skill_projection import NativeSkillProjection

    rt, _, _ = _make_runtime()
    rt._agent = "custom"
    rt._native_skill_projection = NativeSkillProjection(aliases={"custom": "native-alias"})
    rt.terminate_session = AsyncMock()  # type: ignore[method-assign]
    with pytest.raises(AcpRuntimeError, match="spawned with --agent"):
        await rt._verify_spawn_agent_active(
            "s1",
            _new_resp({"currentModeId": "default", "availableModes": [{"id": "default"}]}),
            override=None,
        )
    rt.terminate_session.assert_awaited_once()


@pytest.mark.asyncio
async def test_load_session_fails_closed_when_spawn_agent_not_advertised():
    """Guard (A2) on the resume path. `load_session`'s own check reads `agent`, and
    its sole caller passes `agent=agent or None`, so a no-override resume reached no
    check at all — yet a fresh runtime re-reads the spec from disk, so the spawn
    agent can fail to load on resume exactly as on a cold start."""
    rt, _, _ = _make_runtime()
    rt._agent = "kirocrew"
    rt._can_load_session = True
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    resp = {"modes": {"currentModeId": "default", "availableModes": [{"id": "default"}]}}
    rt._send_and_await = AsyncMock(side_effect=[resp, {}])  # type: ignore[method-assign]
    with patch.object(AcpSessionHandle, "drain_init", AsyncMock()):
        with pytest.raises(AcpRuntimeError, match="spawned with --agent"):
            await rt.load_session("/home/u/.kiro/sessions/cli/sid-9.json", "sid-9", cwd="/w")
    methods = [c.args[0] for c in rt._send_and_await.call_args_list]
    assert METHOD_SET_MODE not in methods
    assert METHOD_SESSION_TERMINATE in methods


@pytest.mark.asyncio
async def test_create_session_sets_mode_when_no_modes_advertised():
    """Backward compat: a backend that omits `modes` (older kiro-cli / fake
    backend) still gets set_mode attempted."""
    rt, _, _ = _make_runtime()
    rt._finish_session_init = MagicMock(return_value=[])  # type: ignore[method-assign]
    resp = _new_resp(None)
    rt._send_and_await = AsyncMock(side_effect=[resp, {}])  # type: ignore[method-assign]
    with patch.object(AcpSessionHandle, "drain_init", AsyncMock()):
        await rt.create_session(agent="kirocrew", mcp_servers=[])
    methods = [c.args[0] for c in rt._send_and_await.call_args_list]
    assert METHOD_SET_MODE in methods


def test_mode_available_helper():
    """Unit: the guard predicate. Empty modes ⇒ attempt (True); advertised ⇒
    membership test."""
    from kiro_crew.acp.runtime import AcpRuntime

    assert AcpRuntime._mode_available("kirocrew", _new_resp(None)) is True
    assert (
        AcpRuntime._mode_available("kirocrew", _new_resp({"availableModes": [{"id": "kirocrew"}]}))
        is True
    )
    assert (
        AcpRuntime._mode_available("kirocrew", _new_resp({"availableModes": [{"id": "default"}]}))
        is False
    )
    # Present-but-empty availableModes → advertised, agent absent → fail closed.
    assert AcpRuntime._mode_available("kirocrew", _new_resp({"availableModes": []})) is False
    # A modes dict WITHOUT an availableModes list → not advertised → attempt.
    assert AcpRuntime._mode_available("kirocrew", _new_resp({"currentModeId": "x"})) is True


def test_advertised_mode_origin_reads_the_v3_stamp_and_nothing_else():
    """The v3 engine stamps ``_meta.kiro.resource.source.origin`` per mode;
    ``client`` is a definition Crew sent, ``bundled`` the engine's own. Any
    other shape -- no stamp, an older engine, an odd entry -- reads as ''."""
    from kiro_crew.acp._dispatch import advertised_mode_origin

    def stamped(mode_id, origin):
        return {
            "id": mode_id,
            "_meta": {
                "kiro": {"resource": {"resourceType": "agent", "source": {"origin": origin}}}
            },
        }

    resp = _new_resp(
        {"availableModes": [stamped("plan", "bundled"), stamped("kirocrew", "client")]}
    )
    assert advertised_mode_origin(resp, "plan") == "bundled"
    assert advertised_mode_origin(resp, "kirocrew") == "client"
    assert advertised_mode_origin(resp, "absent") == ""
    assert advertised_mode_origin(_new_resp({"availableModes": [{"id": "plan"}]}), "plan") == ""
    assert advertised_mode_origin(_new_resp(None), "plan") == ""
    assert advertised_mode_origin({}, "plan") == ""
    odd = _new_resp({"availableModes": [{"id": "plan", "_meta": {"kiro": {"resource": "x"}}}]})
    assert advertised_mode_origin(odd, "plan") == ""


def test_activation_refusal_is_a_harness_seam_kas_reads_the_stamp_kiro_answers_none(caplog):
    """Guard (C): the runtime asks the harness, never a backend identity. The KAS
    harness refuses only the MEASURED built-in stamp (``bundled``) on the
    requested id, in plain words naming the remedy; a stamp it has never
    measured is logged and let through (kiro-cli is the operator's install,
    so an unmeasured value must not deny every crewmate); the spawn-time
    hosts answer None for every response, so the Kiro path carries no
    branch."""
    from kiro_crew.acp.harness.kas import KasHarness
    from kiro_crew.acp.harness.kiro import KiroHarness

    def stamped(origin):
        return _new_resp(
            {
                "availableModes": [
                    {"id": "plan", "_meta": {"kiro": {"resource": {"source": {"origin": origin}}}}}
                ]
            }
        )

    kas = KasHarness()
    with caplog.at_level("WARNING", logger="kiro_crew.acp.harness.kas"):
        refusal = kas.activation_refusal("plan", stamped("bundled"))
    assert refusal and refusal.startswith("Rename this mate's template: “plan” is reserved")
    assert " -- " not in refusal
    assert "Agent Template tab" in refusal and "KAS" not in refusal
    # The raw stamp is logged: the refusal blames the name, so the log is the
    # only place a changed engine stamping would show as the real cause.
    assert any(
        "origin='bundled'" in r.getMessage() and "'plan'" in r.getMessage() for r in caplog.records
    )
    # An unmeasured positive stamp is NOT a refusal: logged once, activated.
    caplog.clear()
    with caplog.at_level("WARNING", logger="kiro_crew.acp.harness.kas"):
        assert kas.activation_refusal("plan", stamped("custom")) is None
    assert any(
        "unmeasured" in r.getMessage() and "origin='custom'" in r.getMessage()
        for r in caplog.records
    )
    caplog.clear()
    assert kas.activation_refusal("plan", stamped("client")) is None
    assert not caplog.records
    assert kas.activation_refusal("plan", stamped("client")) is None
    assert kas.activation_refusal("plan", _new_resp({"availableModes": [{"id": "plan"}]})) is None
    assert kas.activation_refusal("absent", stamped("bundled")) is None
    assert kas.activation_refusal("plan", _new_resp(None)) is None
    kiro = KiroHarness()
    assert kiro.activation_refusal("plan", stamped("bundled")) is None


def test_parse_session_modes_shapes():
    """The shared parser: absent/odd `modes` ⇒ ([], '', False); a present
    availableModes list ⇒ advertised=True (even when empty); id read from
    id → modeId → value fallbacks."""
    from kiro_crew.acp._dispatch import parse_session_modes

    assert parse_session_modes({}) == ([], "", False)
    assert parse_session_modes({"modes": "nonsense"}) == ([], "", False)
    # modes dict but no availableModes list → not advertised (attempt path).
    assert parse_session_modes({"modes": {"currentModeId": "x"}}) == ([], "x", False)
    # present but empty → advertised True (fail-closed path).
    assert parse_session_modes({"modes": {"availableModes": []}}) == ([], "", True)
    ids, current, advertised = parse_session_modes(
        {
            "modes": {
                "currentModeId": "kirocrew",
                "availableModes": [
                    {"id": "kirocrew"},
                    {"modeId": "ops"},
                    {"value": "code-reviewer"},
                    {"name": "no-id-dropped"},
                    "not-a-dict",
                ],
            }
        }
    )
    assert ids == ["kirocrew", "ops", "code-reviewer"]
    assert current == "kirocrew"
    assert advertised is True


# ── Session-start timeout budget ──
#
# kiro-cli blocks the session/new (and session/load) response while it
# initializes the session's MCP servers; a remote server pending OAuth holds
# that for its full 30s authorization wait. These tests lock in the CALL SITE
# — the timeout actually handed to _send_and_await — not just the constant,
# because dropping the ``timeout=`` argument silently reverts to the generic
# 30s _REQUEST_TIMEOUT, which is the exact regression.


@pytest.mark.asyncio
async def test_session_new_call_site_passes_budget_above_request_timeout(monkeypatch):
    """create_session must hand _send_and_await an explicit session/new timeout
    that exceeds _REQUEST_TIMEOUT — the generic default equals the backend's
    30s OAuth wait, turning session start into a race the client loses."""
    rt, _, _ = _make_runtime()
    rt._expect_mcp_reports = False  # skip the MCP drain wait — not under test
    seen: dict[str, object] = {}

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            seen["timeout"] = timeout
            return {"sessionId": "sid-budget"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    await rt.create_session(cwd="/w", mcp_servers=[])

    assert seen["timeout"] == _SESSION_NEW_TIMEOUT
    assert isinstance(seen["timeout"], float)
    assert seen["timeout"] > _REQUEST_TIMEOUT


@pytest.mark.asyncio
async def test_session_load_call_site_passes_budget_above_request_timeout(monkeypatch):
    """load_session is gated by the same MCP re-initialization (kiro-cli
    re-initializes servers on load; oauth_request frames are staged while
    either request is in flight), so it must carry the same budget."""
    rt, _, _ = _make_runtime()
    rt._can_load_session = True
    rt._expect_mcp_reports = False  # skip the MCP drain wait — not under test
    seen: dict[str, object] = {}

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_LOAD:
            seen["timeout"] = timeout
            return {"modes": {"currentModeId": "kirocrew"}}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    await rt.load_session("/home/u/.kiro/sessions/cli/sid-9.json", "sid-9", cwd="/w")

    assert seen["timeout"] == _SESSION_NEW_TIMEOUT
    assert seen["timeout"] > _REQUEST_TIMEOUT


@pytest.mark.asyncio
async def test_create_set_mode_call_site_passes_budget_above_request_timeout(
    monkeypatch,
):
    """create_session's set_mode must carry the session-start budget, not the
    generic _REQUEST_TIMEOUT. Switching to an agent boots THAT agent's MCP
    servers (the same (re-)initialization session/new gets 90s for); a
    switched-to server pending OAuth holds the response for its full 30s wait,
    so the generic 30s budget races it exactly as it would session start.
    set_mode fires here because the session/new response advertises
    no `modes` list (older/fake backend -> attempt)."""
    rt, _, _ = _make_runtime()
    seen: dict[str, object] = {}

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-setmode"}  # no `modes` -> set_mode attempts
        if method == METHOD_SET_MODE:
            seen["timeout"] = timeout
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    await rt.create_session(cwd="/w", agent="kirocrew")

    assert seen["timeout"] == _SESSION_NEW_TIMEOUT
    assert isinstance(seen["timeout"], float)
    assert seen["timeout"] > _REQUEST_TIMEOUT


@pytest.mark.asyncio
async def test_load_set_mode_call_site_passes_budget_above_request_timeout(
    monkeypatch,
):
    """The resume path's set_mode is gated by the same switched-to-agent MCP
    (re-)initialization as create_session's, so it must carry session/load's
    budget rather than the generic _REQUEST_TIMEOUT. The session/load
    response echoes `modes` (a genuine resume), which is also what makes
    _mode_available admit the switch."""
    rt, _, _ = _make_runtime()
    rt._can_load_session = True
    seen: dict[str, object] = {}

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_LOAD:
            return {
                "modes": {
                    "currentModeId": "kiro",
                    "availableModes": [{"id": "kirocrew"}],
                }
            }
        if method == METHOD_SET_MODE:
            seen["timeout"] = timeout
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    await rt.load_session(
        "/home/u/.kiro/sessions/cli/sid-9.json", "sid-9", agent="kirocrew", cwd="/w"
    )

    assert seen["timeout"] == _SESSION_NEW_TIMEOUT
    assert isinstance(seen["timeout"], float)
    assert seen["timeout"] > _REQUEST_TIMEOUT


@pytest.mark.asyncio
async def test_session_start_budget_follows_config(monkeypatch):
    """agent.session_start_timeout_secs raises the session/new budget: the
    configured value is resolved lazily (off-loop, on first session start —
    never in __init__, where a config cache miss would block the event loop)
    and handed to _send_and_await, so a large agent whose MCP fleet needs
    longer than the 90s default (many servers, sandboxed per-server
    launchers, loaded hosts) can extend the budget without patching the
    constant. Resolved once per runtime and cached thereafter."""
    from types import SimpleNamespace

    from kiro_crew.config.loader import KiroCrewConfig

    fake_cfg = SimpleNamespace(agent=SimpleNamespace(session_start_timeout_secs=240))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: fake_cfg))

    rt, _, _ = _make_runtime()
    rt._expect_mcp_reports = False
    # Lazy: construction must not have resolved (or read) config.
    assert rt._session_start_timeout is None
    seen: dict[str, object] = {}

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            seen["timeout"] = timeout
            return {"sessionId": "sid-cfg"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    await rt.create_session(cwd="/w", mcp_servers=[])

    assert seen["timeout"] == 240.0
    # Cached: later session starts on this runtime reuse the snapshot.
    assert rt._session_start_timeout == 240.0
    assert await rt._session_start_budget() == 240.0


def test_runtime_construction_never_touches_config(monkeypatch):
    """AcpRuntime.__init__ runs on the event loop; KiroCrewConfig.load() is a
    synchronous disk read + schema validation on a cache miss, so the budget
    must resolve lazily (off-loop) on first session start — never at
    construction."""
    from kiro_crew.config.loader import KiroCrewConfig

    def _boom(cls):
        raise AssertionError("config must not be consulted at construction")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
    rt = AcpRuntime(work_dir="/tmp")
    assert rt._session_start_timeout is None


def test_resolve_session_start_timeout_floors_and_falls_back(monkeypatch):
    """The resolver never returns below the built-in floor (a budget under the
    backend's 30s OAuth wait recreates the race), and any config-load
    failure degrades to the default instead of breaking runtime construction."""
    from types import SimpleNamespace

    from kiro_crew.acp.runtime import _resolve_session_start_timeout
    from kiro_crew.config.loader import KiroCrewConfig

    # Below-floor value (belt-and-braces: the loader clamp already prevents
    # this on disk, but a degraded load must not shrink the budget either).
    low_cfg = SimpleNamespace(agent=SimpleNamespace(session_start_timeout_secs=10))
    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda cls: low_cfg))
    assert _resolve_session_start_timeout() == _SESSION_NEW_TIMEOUT

    # Config load blowing up falls back to the default.
    def _boom(cls):
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(_boom))
    assert _resolve_session_start_timeout() == _SESSION_NEW_TIMEOUT


@pytest.mark.asyncio
async def test_send_and_await_timeout_error_names_the_budget():
    """The timeout message must carry the budget that elapsed so a 90s
    session-start timeout is distinguishable from a generic 30s one. The
    'timed out' substring is load-bearing (chat_runner matches on it)."""
    rt, _, _ = _make_runtime()

    with pytest.raises(AcpRuntimeError) as exc_info:
        # stdin is mocked and nothing ever responds → wait_for times out.
        await rt._send_and_await("probe/method", {}, timeout=0.01)

    msg = str(exc_info.value)
    assert "timed out" in msg
    assert "0.01s" in msg
    assert "probe/method" in msg
