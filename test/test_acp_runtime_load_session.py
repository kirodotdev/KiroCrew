"""``AcpRuntime`` session creation and resume.

The member-dispatch mount on both paths, ``load_session`` against
``AcpClient``'s resume path, and ``create_session``'s failure and OAuth handling.
"""

import asyncio
import json
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import (
    _await_pending,
    _feed,
    _make_runtime,
    _replying_send,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp.runtime import AcpRuntimeError, AcpSessionHandle
from kiro_crew.acp.types import (
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    METHOD_SESSION_TERMINATE,
    METHOD_SET_MODE,
)
from kiro_crew.mcp_gateway.claim import STUB_SESSION_TOKEN_ENV
from kiro_crew.testing.wait import await_until


def _identity_tokens(elements):
    """The per-session token carried by each of *elements*, in order.

    A missing pair yields ``""`` for that element, so a caller can tell "every
    element carries one" from "one of them stopped".
    """
    out = []
    for element in elements:
        env = element.get("env")
        pairs = env if isinstance(env, list) else []
        value = ""
        for pair in pairs:
            if isinstance(pair, dict) and pair.get("name") == STUB_SESSION_TOKEN_ENV:
                value = str(pair.get("value") or "")
        out.append(value)
    return out


def _without_identity_env(elements):
    """*elements* with the per-session identity VALUE dropped from each.

    Lets a comparison assert on the stub SET — names, commands, args, and every
    other env pair — without asserting that two different sessions were given the
    same session token, which they must not be.

    Only the VALUE is excluded, never the pair's presence: the pair is rewritten to
    a fixed placeholder rather than removed, so an element that stops carrying a
    token at all still differs from one that carries a different token. Removing it
    outright is what made the ratchet blind to a token disappearing — the
    comparison alone cannot see presence, so :func:`_identity_tokens` is asserted
    beside it.
    """
    out = []
    for element in elements:
        shaped = dict(element)
        env = shaped.get("env")
        if isinstance(env, list):
            shaped["env"] = [
                (
                    {"name": STUB_SESSION_TOKEN_ENV, "value": "<per-session>"}
                    if isinstance(pair, dict) and pair.get("name") == STUB_SESSION_TOKEN_ENV
                    else pair
                )
                for pair in env
            ]
        out.append(shaped)
    return out


class TestRuntimeMemberDispatchDisabled:
    """A switched-off dashboard server is not mounted on EITHER runtime path.

    ``AcpRuntime`` composes the array for an ``ACP_BACKENDS_ACP_RUNTIME`` host, and the
    member entry it appends is outside every rule that array is filtered by -- the
    ``tools`` allowlist that keeps a disabled server out of a projected array never
    sees an element appended after it. ``disabled`` also has no per-tool or per-call
    spelling, so no harness can refuse a call to a server it was handed: not mounting
    it is the only place the operator's switch-off can be honoured.

    Both paths are pinned because ``session/load`` RE-INITIALIZES the session's
    servers, so a resume that did not ask would re-mount a server the original
    ``session/new`` withheld.
    """

    MEMBER_KEY = "dashboard_member-autofix"

    @staticmethod
    def _switch_off(monkeypatch, tmp_path, *, disabled: bool):
        """Write the switch where the dashboard's own MCP action writes it."""
        import kiro_crew.agent as agent_mod
        from kiro_crew.members import MEMBER_DISPATCH_SERVER

        settings = tmp_path / "mcp.json"
        entry = {"disabled": True} if disabled else {}
        settings.write_text(
            json.dumps({"mcpServers": {MEMBER_DISPATCH_SERVER: entry}}), encoding="utf-8"
        )
        monkeypatch.setattr(agent_mod, "_KIRO_MCP_JSON", settings)
        return MEMBER_DISPATCH_SERVER

    @pytest.mark.asyncio
    @pytest.mark.parametrize("disabled", [True, False])
    async def test_create_session_asks_before_mounting(self, monkeypatch, tmp_path, disabled):
        server = self._switch_off(monkeypatch, tmp_path, disabled=disabled)
        rt, _, _ = _make_runtime()
        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_NEW:
                return {"sessionId": "sid-new", "modes": {"currentModeId": "kirocrew"}}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        await rt.create_session(cwd="/work", agent="kirocrew", member_session_key=self.MEMBER_KEY)
        params = next(p for m, p in sent if m == METHOD_SESSION_NEW)
        names = [e["name"] for e in params["mcpServers"]]
        assert (server in names) is (not disabled), names

    @pytest.mark.asyncio
    @pytest.mark.parametrize(("member", "disabled"), [(True, True), (True, False), (False, False)])
    async def test_the_handle_records_the_mount(self, monkeypatch, tmp_path, member, disabled):
        """The context builder teaches the session_* tools off this flag, so it
        must carry exactly the answer the array got -- withheld, mounted, or a
        non-member session that never had the entry."""
        self._switch_off(monkeypatch, tmp_path, disabled=disabled)
        rt, _, _ = _make_runtime()

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_NEW:
                return {"sessionId": "sid-new", "modes": {"currentModeId": "kirocrew"}}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        handle = await rt.create_session(
            cwd="/work",
            agent="kirocrew",
            member_session_key=self.MEMBER_KEY if member else "",
        )
        assert handle.member_dispatch_mounted is (member and not disabled)

    def test_the_resume_path_records_the_mount_on_its_handle(self):
        """The load half, pinned on its source for the reason the grant pin below gives:
        driving a resume needs the re-attach bracket this fake transport does not answer."""
        import inspect

        from kiro_crew.acp.runtime import AcpRuntime

        source = inspect.getsource(AcpRuntime.load_session)
        assert "member_mounted = member_entry is not None" in source
        assert "handle.member_dispatch_mounted = member_mounted" in source

    def test_the_scope_comes_from_the_one_decider(self):
        """The switch-off has to be read where this host resolves its agent.

        ``session_mcp`` resolves a spec project-nearest and does NOT fall back, so on a
        host that runs the USER-level agent (KAS) a same-named file in the checkout would
        decide the answer for a session that never reads it: a disable written where that
        session's agent actually lives would read as "not disabled" and the withdrawn
        server would mount. Asserted against ``overlay_project_scope`` rather than
        against a literal, because that function is the decider the array's own
        projection uses and these two must not come apart.
        """
        from kiro_crew.acp.runtime import _disable_check_scope
        from kiro_crew.agent_sdk.backends import overlay_project_scope

        for backend in ("kas", "codex", "kiro", "opencode", "claude"):
            assert _disable_check_scope(backend, "/work") == overlay_project_scope(
                backend, "/work"
            ).get("work_dir"), backend
        # The case the mismatch would hide: KAS reads no checkout at all.
        assert _disable_check_scope("kas", "/work") is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("disabled", [True, False])
    async def test_the_kas_grant_follows_the_withhold(self, monkeypatch, tmp_path, disabled):
        """Withholding the mount has to withhold the GRANT with it.

        ``member_dispatch=True`` widens the KAS agent payload: the dashboard server joins
        ``tools`` and the member verbs join ``allowedTools``, which is an approval-free
        path. A grant that outlived the withhold would leave a switched-off server both
        named and pre-approved on the very session that is not mounting it -- worse than
        an unguarded mount, because nothing would even ask.

        Read off the flag reaching ``_kas_custom_agents`` rather than off a KAS wire
        payload: ``create_session`` enters that seam for every host (it answers None for a
        non-KAS one), so the decision is observable without a KAS session to drive.
        """
        from kiro_crew.acp.harness.base import SessionExtras

        self._switch_off(monkeypatch, tmp_path, disabled=disabled)
        rt, _, _ = _make_runtime()
        seen: list[bool] = []

        async def _capture(_agent, *, member_dispatch=False, crew_panel=False, session_key=""):
            seen.append(member_dispatch)
            return SessionExtras(custom_agents=None, derived_spec_snapshot=None)

        monkeypatch.setattr(rt, "_kas_custom_agents", _capture)

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_NEW:
                return {"sessionId": "sid-new", "modes": {"currentModeId": "kirocrew"}}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        await rt.create_session(cwd="/work", agent="kirocrew", member_session_key=self.MEMBER_KEY)
        assert seen == [not disabled], (seen, disabled)

    def test_the_resume_path_carries_the_same_grant_expression(self):
        """The resume half, pinned where it can be: its own source.

        The seam is entered only for KAS on that path -- by design, so the kiro resume
        reaches a comparison and stops -- and driving a KAS resume needs the whole
        re-attach and activation bracket this suite's fake transport does not answer. A
        source pin still fails if the term is dropped from one path and kept on the other,
        which is the drift that matters: the two halves of one feature disagreeing.
        """
        import inspect

        from kiro_crew.acp.runtime import AcpRuntime

        for method in (AcpRuntime.create_session, AcpRuntime.load_session):
            source = inspect.getsource(method)
            assert (
                "member_dispatch=bool(member_session_key) and not member_withheld" in source
            ), method.__name__

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["create", "load"])
    async def test_both_paths_pass_that_scope(self, monkeypatch, path):
        """Not just the helper: the value each path actually hands the reader."""
        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.members import MEMBER_DISPATCH_SERVER, MEMBER_PANEL_SERVER

        seen: list[tuple[str, object]] = []

        def _capture(name, _agent, *, work_dir=None):
            seen.append((name, work_dir))
            return False

        # The per-tool reader is the THIRD read on these paths and takes the same
        # scope, so it is captured here too: left real it would be handed the
        # sentinel below and fail on it, and the property under test is that every
        # reader gets the decider's answer.
        tool_scopes: list[object] = []

        def _capture_tools(_agent, *, work_dir=None):
            tool_scopes.append(work_dir)
            return frozenset()

        monkeypatch.setattr(runtime_mod, "session_mcp_server_is_disabled", _capture)
        monkeypatch.setattr(runtime_mod, "session_mcp_disabled_tools", _capture_tools)
        rt, _, _ = _make_runtime()
        rt._can_load_session = True

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_NEW:
                return {"sessionId": "sid-new", "modes": {"currentModeId": "kirocrew"}}
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        # A sentinel rather than a checkout path: the default runtime backend is not
        # user-level-only, so its scope and the raw session checkout are the SAME
        # string, and a call site that skipped the decider would pass that assertion.
        # Identity against the decider's answer cannot be reached any other way.
        scope = object()
        monkeypatch.setattr(runtime_mod, "_disable_check_scope", lambda _backend, _wd: scope)
        if path == "create":
            await rt.create_session(
                cwd="/work", agent="kirocrew", member_session_key=self.MEMBER_KEY
            )
        else:
            await rt.load_session(
                "/home/u/.kiro/sessions/cli/sid-123.json",
                "sid-123",
                cwd="/work",
                agent="kirocrew",
                member_session_key=self.MEMBER_KEY,
            )
        assert {name for name, _ in seen} == {
            MEMBER_DISPATCH_SERVER,
            MEMBER_PANEL_SERVER,
        }, seen
        # EVERY call, not just the first: all three reads on these paths take the
        # same switch scope, and one of them resolving its own is the mismatch
        # this pins shut.
        assert all(work_dir is scope for _, work_dir in seen), seen
        assert tool_scopes, "the per-tool switch must be read too"
        assert all(work_dir is scope for work_dir in tool_scopes), tool_scopes

    @pytest.mark.asyncio
    @pytest.mark.parametrize("disabled", [True, False])
    async def test_load_session_asks_again_on_resume(self, monkeypatch, tmp_path, disabled):
        server = self._switch_off(monkeypatch, tmp_path, disabled=disabled)
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        await rt.load_session(
            "/home/u/.kiro/sessions/cli/sid-123.json",
            "sid-123",
            cwd="/work",
            agent="kirocrew",
            member_session_key=self.MEMBER_KEY,
        )
        params = next(p for m, p in sent if m == METHOD_SESSION_LOAD)
        names = [e["name"] for e in params["mcpServers"]]
        assert (server in names) is (not disabled), names


class TestAcpRuntimeLoadSession:
    """load_session() must mirror AcpClient._initialize_session's resume path:
    issue session/load DIRECTLY (no session/new first) under the ORIGINAL sid,
    with the same cwd + mcpServers (pooled stubs and managed direct tools)
    + _kiro.dev/session_file _meta. The double-session
    drift it replaces produced stopReason='refusal'."""

    @pytest.mark.asyncio
    async def test_load_session_sends_direct_session_load_params(self, monkeypatch):
        rt, _, _ = _make_runtime()
        rt._can_load_session = True

        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            # session/load echoes "modes"; set_mode echoes nothing meaningful.
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)

        handle = await rt.load_session(
            "/home/u/.kiro/sessions/cli/sid-123.json",
            "sid-123",
            cwd="/work",
            agent="kirocrew",
        )

        # No session/new was issued — the first RPC is session/load itself.
        methods = [m for m, _ in sent]
        assert METHOD_SESSION_NEW not in methods
        assert methods[0] == METHOD_SESSION_LOAD

        load_params = sent[0][1]
        servers = load_params["mcpServers"]
        assert [entry["name"] for entry in servers] == [
            "kirocrew-core",
            "kirocrew-cron",
            "kirocrew-guide",
            "kirocrew-mate",
        ]
        assert all(_identity_tokens(servers))
        assert load_params == {
            "sessionId": "sid-123",
            "cwd": "/work",
            "mcpServers": servers,
            "_meta": {"_kiro.dev/session_file": "/home/u/.kiro/sessions/cli/sid-123.json"},
        }
        # Handle adopts the ORIGINAL sid and its queue is registered.
        assert handle.session_id == "sid-123"
        assert "sid-123" in rt._session_queues
        # set_mode ran for the resumed session (mirrors AcpClient step 4).
        assert METHOD_SET_MODE in methods

    @pytest.mark.asyncio
    async def test_load_session_moves_a_resumed_session_off_an_unserved_default(self, monkeypatch):
        """The resume path is the second half of the served-default check.

        session/load echoes ``currentModelId`` like session/new does, and a
        session persisted before the served list changed can come back on a
        default the account does not serve. load_session must run
        ``ensure_served_default`` after storing the response, exactly as
        create_session does, so the first prompt after a resume cannot fail with
        "no access to model".
        """
        from kiro_crew.acp.types import ACP_BACKEND_KIRO, METHOD_SET_MODEL

        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        rt._acp_backend = ACP_BACKEND_KIRO

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_LOAD:
                return {
                    "modes": {"currentModeId": "kirocrew"},
                    "models": {
                        "currentModelId": "auto",
                        "availableModels": [{"modelId": "gpt-5.6-sol"}, {"modelId": "glm-5"}],
                    },
                }
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        # set_model goes through the routed send and waits for the adapter's reply.
        routed = _replying_send(lambda params: rt._session_queues[params["sessionId"]])
        monkeypatch.setattr(rt, "send_request", routed)

        handle = await rt.load_session("/f.json", "sid-resume", agent="kirocrew")

        set_model_calls = [c for c in routed.await_args_list if c.args[0] == METHOD_SET_MODEL]
        assert len(set_model_calls) == 1, routed.await_args_list
        assert set_model_calls[0].args[1] == {"sessionId": "sid-resume", "modelId": "gpt-5.6-sol"}
        assert handle.served_model == "gpt-5.6-sol"
        # The intent is untouched: the resumed session still INHERITS.
        assert handle.model == ""

    @pytest.mark.asyncio
    async def test_load_session_does_not_bill_the_restored_ledger(self, monkeypatch):
        """A resumed session's first cumulative cost carries its lifetime spend."""
        rt, _, _ = _make_runtime()
        rt._can_load_session = True

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        handle = await rt.load_session("/f.json", "sid-cost", agent="kirocrew")

        handle.last_prompt_stats.apply_cost_cumulative(5.00)
        assert handle.last_prompt_stats.cost_usd == 0.0
        handle.last_prompt_stats.apply_cost_cumulative(5.04)
        assert handle.last_prompt_stats.cost_usd == pytest.approx(0.04)

    @pytest.mark.asyncio
    async def test_load_session_raises_when_capability_absent(self):
        rt, _, _ = _make_runtime()
        rt._can_load_session = False
        with pytest.raises(AcpRuntimeError):
            await rt.load_session("/f.json", "sid-x")
        # No queue leaked on the guard path.
        assert "sid-x" not in rt._session_queues

    @pytest.mark.asyncio
    async def test_load_session_without_modes_raises_and_unregisters(self, monkeypatch):
        rt, _, _ = _make_runtime()
        rt._can_load_session = True

        async def _fake_send(method, params, timeout=None):
            return {}  # no "modes" → load did not actually restore state

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)

        with pytest.raises(AcpRuntimeError):
            await rt.load_session("/f.json", "sid-y", agent="kirocrew")
        # The queue registered before the send must be cleaned up on failure.
        assert "sid-y" not in rt._session_queues

    @pytest.mark.asyncio
    async def test_load_session_reinjects_the_kas_agent_definition(self, monkeypatch):
        """Resume must re-send the agent, for the same reason session/new sends it.

        KAS registers client agents PER SESSION and has no ``--agent`` flag, so a
        resumed session that is not handed them again advertises only the modes it
        can find on disk — and that set is not a superset of what session/new had,
        because KAS skips an agent profile written for kiro-cli. Omitting this made
        the requested mode genuinely absent on resume, and the mode guard then
        refused the load rather than silently running the backend default.
        """
        from kiro_crew.acp._dispatch import build_session_new_params

        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        async def _fake_agents(agent, *, member_dispatch=False, crew_panel=False, session_key=""):
            from kiro_crew.acp.harness import SessionExtras

            return SessionExtras(custom_agents=[{"id": agent, "prompt": "p", "tools": []}])

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)
        rt._acp_backend = ACP_BACKEND_KAS

        await rt.load_session("", "sid-kas", cwd="/work", agent="kirocrew")

        load_params = sent[0][1]
        assert load_params["_meta"]["kiro"]["customAgents"] == [
            {"id": "kirocrew", "prompt": "p", "tools": []}
        ]
        # Same envelope as session/new, because both go through one builder. Two
        # hand-built copies of this nesting would be free to drift, and a resumed
        # session that got a subtly different shape would fail the same way the
        # missing injection did: mode absent, load refused.
        assert (
            load_params["_meta"]["kiro"]
            == build_session_new_params(
                "/work", kas_custom_agents=[{"id": "kirocrew", "prompt": "p", "tools": []}]
            )["_meta"]["kiro"]
        )

    @pytest.mark.asyncio
    async def test_the_kas_resume_report_reads_the_hoisted_array(self, monkeypatch):
        """The session report must name the array the resume SENT, post-hoist.

        ``hoist_servers_for_session`` moves a managed server out of the agent definition
        and into the session array, so on KAS the array the wire carries is not the
        one the roster was bound from. The report and the stall diagnostic read that
        binding, so a resume that re-assigned only the request param would describe
        servers it did not send -- the pre-hoist roster -- while the session ran on
        the hoisted one.
        """
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        async def _fake_agents(agent, *, member_dispatch=False, crew_panel=False, session_key=""):
            from kiro_crew.acp.harness import SessionExtras

            return SessionExtras(custom_agents=[{"id": agent, "prompt": "p", "tools": []}])

        def _hoist(agents, agent, servers, **_kw):
            # The shape the real hoist produces: a managed server appears on the
            # array that was not in the roster the caller passed in.
            return agents, list(servers) + [{"name": "hoisted", "command": "/bin/h"}]

        import kiro_crew.acp.runtime as runtime_mod

        reported: list[list] = []
        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)
        monkeypatch.setattr(runtime_mod, "hoist_servers_for_session", _hoist)
        monkeypatch.setattr(
            rt,
            "_guard_unresolved_mcp_refs",
            lambda handle, spec, agent, wire: reported.append(wire),
        )
        rt._acp_backend = ACP_BACKEND_KAS

        await rt.load_session("", "sid-hoist", cwd="/work", agent="kirocrew")

        load_params = sent[0][1]
        names = [e.get("name") for e in load_params["mcpServers"]]
        assert "hoisted" in names, "the hoisted server never reached the wire"
        assert reported, "the wire roster was never handed to the report/guard"
        assert [e.get("name") for e in reported[-1]] == names, (
            "the report reads a different array than the resume sent; rebind "
            "wire_servers at the hoist rather than only load_params['mcpServers']"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("resume", [False, True], ids=["new", "load"])
    async def test_kas_hoisted_control_plane_carries_the_session_token(self, monkeypatch, resume):
        """On KAS the managed servers reach the wire through the hoist, token included.

        The runtime stamps the per-session token onto its array BEFORE
        ``hoist_servers_for_session`` runs, and on KAS that array is empty: every
        managed server arrives through the hoist. A hoisted ``kirocrew-core``
        without the token reads its tool policy unattested and refuses every call
        as ``identity_unattested`` -- memory, logs, spawn_run -- on every session.
        """
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            if method == METHOD_SESSION_NEW:
                return {"sessionId": "sid-kas-new"}
            return {}

        async def _fake_agents(agent, *, member_dispatch=False, crew_panel=False, session_key=""):
            from kiro_crew.acp.harness import SessionExtras

            return SessionExtras(
                custom_agents=[
                    {
                        "id": agent,
                        "prompt": "p",
                        "tools": ["@kirocrew-core", "@external"],
                        "mcpServers": {
                            "kirocrew-core": {
                                "command": "kc",
                                "args": ["mcp-core"],
                                "env": {"KIROCREW_SESSION_KEY": session_key},
                            },
                            "external": {"command": "ext"},
                        },
                    }
                ]
            )

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)
        # Readiness is its own concern (see the kas_readiness_wire tests in
        # test_acp_runtime.py); this one is about what the request carried, so the
        # wait returns at once.
        monkeypatch.setattr(AcpSessionHandle, "wait_mcp_ready", AsyncMock(return_value=None))
        rt._acp_backend = ACP_BACKEND_KAS

        if resume:
            handle = await rt.load_session(
                "", "sid-kas-load", cwd="/work", agent="kirocrew", session_key="dashboard:chat-1"
            )
            method = METHOD_SESSION_LOAD
        else:
            handle = await rt.create_session(
                cwd="/work", agent="kirocrew", session_key="dashboard:chat-1"
            )
            method = METHOD_SESSION_NEW
        params = next(p for m, p in sent if m == method)
        core = [e for e in params["mcpServers"] if e.get("name") == "kirocrew-core"]
        assert len(core) == 1, "kirocrew-core must travel in the session-level array"
        (token,) = _identity_tokens(core)
        assert token, "the hoisted kirocrew-core was launched without the session token"
        assert token == handle.stub_session_token, "the element must carry THIS session's token"
        # A third-party server stays in the agent block, and never gets the token.
        (agent_block,) = params["_meta"]["kiro"]["customAgents"]
        assert STUB_SESSION_TOKEN_ENV not in json.dumps(agent_block)

    @pytest.mark.asyncio
    async def test_load_session_keeps_the_transcript_path_alongside_the_agents(self, monkeypatch):
        """Merged, not assigned: a third _meta writer must not drop an earlier one.

        The two envelopes belong to different backends today (a transcript path is
        kiro-cli-only), so in practice they do not collide — which is exactly why a
        plain assignment would survive review and then lose a field later.
        """
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        async def _fake_agents(agent, *, member_dispatch=False, crew_panel=False, session_key=""):
            from kiro_crew.acp.harness import SessionExtras

            return SessionExtras(custom_agents=[{"id": agent, "prompt": "p", "tools": []}])

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)
        rt._acp_backend = ACP_BACKEND_KAS

        await rt.load_session("/t.json", "sid-both", cwd="/work", agent="kirocrew")

        meta = sent[0][1]["_meta"]
        assert meta["_kiro.dev/session_file"] == "/t.json"
        assert "kiro" in meta

    @pytest.mark.asyncio
    async def test_the_kiro_resume_path_never_reaches_the_adapter(self, monkeypatch):
        """harness-parity H13: the kiro construction path must not change at all.

        Relying on ``_kas_custom_agents`` to answer ``None`` would leave the kiro
        resume awaiting an adapter coroutine — working, but changed, and free to
        grow a failure mode later. The backend guard is what makes the kiro path
        reach a comparison and stop, so this asserts the adapter is never called.
        """
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        sent: list[tuple[str, dict]] = []
        calls: list[str] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {"currentModeId": "kirocrew"}, "models": []}
            return {}

        async def _fake_agents(agent, *, member_dispatch=False, crew_panel=False, session_key=""):
            from kiro_crew.acp.harness import SessionExtras

            calls.append(agent)
            return SessionExtras(custom_agents=[{"id": agent, "prompt": "p", "tools": []}])

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        monkeypatch.setattr(rt, "_kas_custom_agents", _fake_agents)

        await rt.load_session("/t.json", "sid-kiro", cwd="/work", agent="kirocrew")

        assert calls == []
        assert sent[0][1]["_meta"] == {"_kiro.dev/session_file": "/t.json"}

    @pytest.mark.asyncio
    async def test_load_session_params_match_acp_client(self, monkeypatch):
        """Drift guard: the kiro (non-claude) session/load payload built here
        must equal the one AcpClient._initialize_session builds, so the two
        resume paths never diverge. Compares the field set explicitly."""
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        captured: dict = {}

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_LOAD:
                captured.update(params)
                return {"modes": {}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        await rt.load_session("/k/sid.json", "sid", cwd="/w", agent="kirocrew")

        # Mirror of AcpClient's kiro-branch load_params (client_start.py step 2).
        # Direct managed tools retain per-session caller attribution on resume;
        # the pooled case is covered by test_load_session_redeclares_pooled_stubs.
        servers = captured["mcpServers"]
        assert [entry["name"] for entry in servers] == [
            "kirocrew-core",
            "kirocrew-cron",
            "kirocrew-guide",
            "kirocrew-mate",
        ]
        assert all(_identity_tokens(servers))
        expected = {
            "sessionId": "sid",
            "cwd": "/w",
            "mcpServers": servers,
            "_meta": {"_kiro.dev/session_file": "/k/sid.json"},
        }
        assert captured == expected

    @pytest.mark.asyncio
    async def test_load_session_unregisters_queue_when_set_mode_fails(self, monkeypatch):
        """A set_mode failure AFTER the queue is registered must TERMINATE the
        resumed session on kiro-cli (session/load already restored it there, so
        a plain unregister leaks it) and drop the local queue, so the caller's
        create_session() fallback doesn't leave the reader routing late
        transcript-replay frames to an abandoned resume_sid queue."""
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        methods: list[str] = []

        async def _fake_send(method, params, timeout=None):
            methods.append(method)
            if method == METHOD_SESSION_LOAD:
                return {"modes": {}, "models": []}  # load succeeds, queue registers
            if method == METHOD_SET_MODE:
                raise AcpRuntimeError("set_mode boom")
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)

        with pytest.raises(AcpRuntimeError):
            await rt.load_session("/k/sid.json", "sid-z", cwd="/w", agent="kirocrew")
        assert METHOD_SESSION_TERMINATE in methods
        assert "sid-z" not in rt._session_queues

    @pytest.mark.asyncio
    async def test_load_session_redeclares_pooled_stubs(self, tmp_path, monkeypatch):
        """A resumed session must re-declare the pooled broker
        stubs. session/load re-initializes the session's MCP servers, so a []
        sent here applies: the stubs stop shadowing the
        agent spec's same-named entries and kiro-cli spawns its own copy of
        every pooled server, silently un-pooling the session for life.

        Asserts on the EMITTED mcpServers of both requests: load_session must
        carry exactly the entries create_session injects for the same agent +
        overlay. Mutating the fix back to [] fails the first assert."""
        from kiro_crew.mcp_gateway.rewriter import _WRAPPER_MARKER

        overlay = tmp_path / "agents"
        overlay.mkdir()
        (overlay / "kirocrew.json").write_text(
            json.dumps(
                {
                    "name": "kirocrew",
                    "mcpServers": {
                        "builder-mcp": {
                            _WRAPPER_MARKER: True,
                            "command": "/data/mcp-gateway/stubs/mc-mcp-stub-wrapper.sh",
                            "args": ["--target-command=builder-mcp"],
                            "env": {},
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        rt._mcp_gateway_overlay = str(overlay)

        sent: list[tuple[str, dict]] = []

        async def _fake_send(method, params, timeout=None):
            sent.append((method, params))
            if method == METHOD_SESSION_LOAD:
                return {"modes": {}, "models": []}
            if method == METHOD_SESSION_NEW:
                return {"sessionId": "sid-new"}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)

        await rt.load_session("/k/sid.json", "sid-r", cwd="/w", agent="kirocrew")
        load_params = next(p for m, p in sent if m == METHOD_SESSION_LOAD)
        assert [e["name"] for e in load_params["mcpServers"]] == [
            "builder-mcp",
            "kirocrew-core",
            "kirocrew-cron",
            "kirocrew-guide",
            "kirocrew-mate",
        ]

        # Parity with create_session for the same agent + overlay: the two
        # injection paths must never diverge.
        #
        # The per-session token's VALUE is the one part that legitimately differs:
        # each session start mints its own (``_own_stub_session``) and these are two
        # different sessions. So the value is normalised and everything else --
        # ``command``, ``args``, every other env pair -- is compared byte-for-byte,
        # because the parity this guards is the stub SET, and re-declaring a
        # different one is what silently un-pools a resumed session.
        #
        # PRESENCE is asserted separately and is not part of the normalisation: a
        # comparison that dropped the pair from both sides would pass just as
        # happily if one path stopped carrying a token at all, which is the
        # regression this file is the only guard for.
        await rt.create_session(cwd="/w", agent="kirocrew")
        new_params = next(p for m, p in sent if m == METHOD_SESSION_NEW)
        load_tokens = _identity_tokens(load_params["mcpServers"])
        new_tokens = _identity_tokens(new_params["mcpServers"])
        assert all(load_tokens) and all(new_tokens), (
            "every re-declared element must carry this session's identity token; "
            f"load={load_tokens} new={new_tokens}"
        )
        assert load_tokens != new_tokens, "two different sessions must not share a token"
        assert _without_identity_env(load_params["mcpServers"]) == _without_identity_env(
            new_params["mcpServers"]
        )

    @pytest.mark.asyncio
    async def test_load_session_resolves_stubs_off_the_event_loop(self, monkeypatch):
        """The overlay lookup stats and reads files; like create_session it must
        run via asyncio.to_thread, not on the loop thread."""
        import threading

        import kiro_crew.acp.runtime as rt_mod

        rt, _, _ = _make_runtime()
        rt._can_load_session = True
        rt._mcp_gateway_overlay = "/nonexistent-overlay"

        loop_thread = threading.current_thread()
        seen: list[threading.Thread] = []

        def _recording_pooled(overlay_dir, agent, channel_id=None, **_kw):
            # ``**_kw`` so the double keeps mirroring the real signature, which
            # takes the session's checkout as ``work_dir``.
            seen.append(threading.current_thread())
            return []

        monkeypatch.setattr(rt_mod, "pooled_session_servers", _recording_pooled)

        async def _fake_send(method, params, timeout=None):
            if method == METHOD_SESSION_LOAD:
                return {"modes": {}, "models": []}
            return {}

        monkeypatch.setattr(rt, "_send_and_await", _fake_send)
        await rt.load_session("/k/sid.json", "sid-t", cwd="/w", agent="kirocrew")

        assert seen, "load_session never consulted pooled_session_servers"
        assert all(t is not loop_thread for t in seen)

    def test_every_session_request_builder_consults_pooled_servers(self):
        """The stub injection lives at multiple call sites in
        two files, and a call site silently sending [] is the failure this guards.
        Enumerate every function that issues session/new or session/load and
        assert each one consults the pooled-stub resolution (either
        pooled_session_servers directly or the _pooled_mcp_servers hook), so a
        fourth builder — or a regression in an existing one — fails here
        instead of shipping another silent un-pooling path."""
        import ast
        import inspect

        import kiro_crew.acp.client as client_mod
        import kiro_crew.acp.runtime as rt_mod

        _SEND_FUNCS = {"_send_request", "_send_and_await"}
        # All THREE session-creating verbs. ``session/resume`` is the standard
        # alternative to ``session/load`` for an agent that keeps sessions without
        # implementing full loading, and a resumed session re-declares its whole MCP
        # surface exactly as a loaded one does -- so a builder sending it must consult
        # the pooled stubs for the same reason, and leaving it out would exempt the
        # newest restore path from this ratchet.
        _SESSION_METHODS = {
            "METHOD_SESSION_NEW",
            "METHOD_SESSION_LOAD",
            "METHOD_SESSION_RESUME",
        }

        # Every session-method constant one expression can evaluate to, so a builder
        # that CHOOSES its verb is read as naming both.
        def _method_names(node: ast.AST) -> set:
            if isinstance(node, ast.Name):
                return {node.id} & _SESSION_METHODS
            if isinstance(node, ast.IfExp):
                return _method_names(node.body) | _method_names(node.orelse)
            return set()

        def _builders(module) -> dict[str, str]:
            src = inspect.getsource(module)
            out: dict[str, str] = {}
            for node in ast.walk(ast.parse(src)):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                # Locals bound to one of those constants, so ``restore_method = A if
                # ... else B`` followed by a send of ``restore_method`` is still seen.
                # Without this the scan sees only a constant named AT the call, and one
                # variable makes a builder invisible -- which is exactly the silent
                # un-pooling this ratchet exists to catch.
                aliases = set()
                for inner in ast.walk(node):
                    if not isinstance(inner, ast.Assign) or not _method_names(inner.value):
                        continue
                    for target in inner.targets:
                        if isinstance(target, ast.Name):
                            aliases.add(target.id)
                for call in ast.walk(node):
                    if not (
                        isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Attribute)
                        and call.func.attr in _SEND_FUNCS
                        and call.args
                    ):
                        continue
                    first = call.args[0]
                    aliased = isinstance(first, ast.Name) and first.id in aliases
                    if _method_names(first) or aliased:
                        out[node.name] = ast.get_source_segment(src, node) or ""
                        break
            return out

        builders = {**_builders(rt_mod), **_builders(client_mod)}
        for owner in client_mod._CLIENT_OWNERS:
            builders.update(_builders(sys.modules[owner]))
        # runtime.py's owner modules (runtime_*.py) hold code that was its own.
        for owner in rt_mod._RUNTIME_OWNERS:
            builders.update(_builders(sys.modules[owner]))
        # Exempt: builders whose session exists only to read the session/new
        # response and is terminated before any prompt. Such a session never
        # calls a tool, so pooled broker stubs would add per-probe MCP boot
        # churn without pooling anything. Everything a REAL conversation runs
        # through must stay in the ratchet.
        _NEVER_PROMPTS = {"probe_advertised_models"}
        for name in _NEVER_PROMPTS:
            assert name in builders, f"{name} no longer issues session/new — remove its exemption"
            builders.pop(name)
        # The four known builders; a new one is included automatically.
        _EXPECTED = {
            "create_session",
            "load_session",
            "_new_session_following_substitution",
            "_initialize_session",
        }
        # Names what is MISSING first and the found set second, so a reader chasing
        # this failure looks up the name that is absent rather than one that is
        # present.
        assert _EXPECTED <= builders.keys(), (
            f"expected builders missing from scan: {sorted(_EXPECTED - builders.keys())} "
            f"(found: {sorted(builders)})"
        )
        for name, body in builders.items():
            assert "pooled_session_servers" in body or "_pooled_mcp_servers" in body, (
                f"{name} issues session/new or session/load but never consults "
                "the pooled broker stubs — it would un-pool its sessions (#3528)"
            )


@pytest.mark.asyncio
async def test_create_session_terminates_session_when_set_mode_fails(monkeypatch):
    """A set_mode failure AFTER session/new succeeded must TERMINATE the session
    on kiro-cli — session/new already created it in the shared process, so a
    plain local unregister would leak it there (RSS growth). terminate_session
    also unregisters the local queue, so the abandoned-queue routing is closed
    too. Mirrors the same cleanup load_session() performs."""
    rt, _, _ = _make_runtime()
    methods: list[str] = []

    async def _fake_send(method, params, timeout=None):
        methods.append(method)
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-new"}  # session/new succeeds → queue registers
        if method == METHOD_SET_MODE:
            raise AcpRuntimeError("set_mode boom")
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    with pytest.raises(AcpRuntimeError):
        await rt.create_session(cwd="/w", agent="kirocrew")
    # kiro-cli was told to evict the just-created session, and the local queue
    # registered before set_mode is cleaned up on failure.
    assert METHOD_SESSION_TERMINATE in methods
    assert "sid-new" not in rt._session_queues


@pytest.mark.asyncio
async def test_create_session_registers_queue_on_success(monkeypatch):
    """Happy path: a successful create_session keeps the session queue
    registered so the returned handle receives its frames."""
    rt, _, _ = _make_runtime()

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            return {"sessionId": "sid-ok"}
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    handle = await rt.create_session(cwd="/w", agent="kirocrew")
    assert handle.session_id == "sid-ok"
    assert "sid-ok" in rt._session_queues


@pytest.mark.asyncio
async def test_create_session_buffers_oauth_emitted_before_response():
    """OAuth emitted during session/new survives until the provider can drain it."""
    from kiro_crew.acp.session_provider import AcpSessionProvider

    rt, reader, _ = _make_runtime()
    reader_task = await _start_reader(rt)
    create_task = asyncio.create_task(rt.create_session(cwd="/w"))
    try:
        request_id = await _await_pending(rt)
        oauth_url = "https://mcp.linear.app/authorize?client_id=shared"
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {
                    "sessionId": "sid-new",
                    "serverName": "linear",
                    "oauthUrl": oauth_url,
                },
            },
        )
        _feed(reader, {"id": request_id, "result": {"sessionId": "sid-new"}})

        handle = await await_until(create_task, what="the session being created")
        provider = AcpSessionProvider(handle, rt)
        assert provider.pop_pending_oauth_requests() == [
            {"serverName": "linear", "oauthUrl": oauth_url}
        ]
        assert provider.pop_pending_oauth_requests() == []
    finally:
        if not create_task.done():
            create_task.cancel()
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_failed_session_init_oauth_does_not_leak_to_reused_id():
    """A failed init cannot leave an approval URL for a later shared session."""
    rt, reader, _ = _make_runtime()
    reader_task = await _start_reader(rt)
    failed_task = asyncio.create_task(rt.create_session(cwd="/w"))
    fresh_task = None
    try:
        failed_request_id = await _await_pending(rt)
        _feed(
            reader,
            {
                "method": METHOD_MCP_OAUTH_REQUEST,
                "params": {
                    "sessionId": "sid-reused",
                    "serverName": "linear",
                    "oauthUrl": "https://mcp.linear.app/authorize?client_id=stale",
                },
            },
        )
        _feed(
            reader,
            {
                "id": failed_request_id,
                "error": {"code": -32603, "message": "session init failed"},
            },
        )
        with pytest.raises(AcpRuntimeError, match="session init failed"):
            await await_until(failed_task, what="the failed session start settling")
        assert not rt._pending_init_notifications

        fresh_task = asyncio.create_task(rt.create_session(cwd="/w"))
        fresh_request_id = await _await_pending(rt, exclude={failed_request_id})
        _feed(reader, {"id": fresh_request_id, "result": {"sessionId": "sid-reused"}})
        handle = await await_until(fresh_task, what="the fresh session being created")
        assert handle.pop_pending_oauth_requests() == []
    finally:
        for task in (failed_task, fresh_task):
            if task is not None and not task.done():
                task.cancel()
        await _stop_reader(reader_task)


@pytest.mark.asyncio
async def test_kiro_create_session_never_feeds_or_persists_the_advertised_model_cache(
    monkeypatch,
):
    """The kiro path gains no call for an adapter-only feature (harness-parity H13).

    The advertised-model feed and persist are gated at the call site on the
    backends whose served list is the authority. kiro is not one of them, so
    creating a kiro session must never reach either, even though session/new
    carries models.
    """
    rt, _, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KIRO
    feed = MagicMock()
    monkeypatch.setattr(AcpSessionHandle, "_feed_advertised_model_cache", feed)
    spy = AsyncMock()
    monkeypatch.setattr(AcpSessionHandle, "_persist_advertised_models_if_changed", spy)

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_NEW:
            return {
                "sessionId": "sid-kiro",
                "models": {
                    "availableModels": [{"modelId": "claude-opus-4.8", "name": "Opus"}],
                    "currentModelId": "claude-opus-4.8",
                },
            }
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    handle = await rt.create_session(cwd="/w", agent="kirocrew")

    assert handle.session_id == "sid-kiro"
    feed.assert_not_called()
    spy.assert_not_awaited()


@pytest.mark.asyncio
async def test_kiro_load_session_never_feeds_or_persists_the_advertised_model_cache(
    monkeypatch,
):
    """The resume path carries the same kiro gate as create (harness-parity H13).

    session/load echoes the model list like session/new does, so the kiro resume
    must skip both the feed and the persist for the same reason the kiro create
    does.
    """
    rt, _, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_KIRO
    rt._can_load_session = True
    feed = MagicMock()
    monkeypatch.setattr(AcpSessionHandle, "_feed_advertised_model_cache", feed)
    spy = AsyncMock()
    monkeypatch.setattr(AcpSessionHandle, "_persist_advertised_models_if_changed", spy)

    async def _fake_send(method, params, timeout=None):
        if method == METHOD_SESSION_LOAD:
            return {
                "modes": {"currentModeId": "kirocrew"},
                "models": {
                    "availableModels": [{"modelId": "claude-opus-4.8", "name": "Opus"}],
                    "currentModelId": "claude-opus-4.8",
                },
            }
        return {}

    monkeypatch.setattr(rt, "_send_and_await", _fake_send)

    handle = await rt.load_session(
        "/home/u/.kiro/sessions/cli/sid-123.json",
        "sid-123",
        cwd="/work",
        agent="kirocrew",
    )

    assert handle.session_id == "sid-123"
    feed.assert_not_called()
    spy.assert_not_awaited()
