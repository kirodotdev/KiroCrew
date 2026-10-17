"""The keystone save's guards on PUT /api/computer-use/config.

An app token cannot write the keystone, its target lists or its budgets, and a
mixed state and limits save is all-or-nothing, the keystone written after the
limits. Flipping the enable restarts sessions and rebuilds the agent spec under
the config lock; a no-op or limits-only save does neither, and a failed restart
or rebuild never fails the save. A malformed keystone still renders the settings
GET, reports the policy unreadable, and keeps refusing the action path. The
shared fixtures and app builders live in ``computer_use_api_helpers.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from computer_use_api_helpers import (  # noqa: F401 -- fixtures are requested by name
    _client,
    _reset_ctx,
    config_file_fixture,
    home_fixture,
    mock_sel_fixture,
    no_agent_spec_rewrite,
    no_real_probe,
    profiles_dir_fixture,
    state_file_fixture,
)

from kiro_crew.computer_use import enable_state
from kiro_crew.computer_use.types import STATE_KEY_ENABLED, PolicyStateError
from kiro_crew.dashboard.handlers import computer_use as cu_api


def _app_token_middleware(app_name: str):
    """Stand in for ``token_auth_middleware``'s app-token grant.

    The real middleware sets BOTH ``request["user"]`` and ``request["app"]`` for an
    app-scoped token — which is the trap: ``request["user"]`` is truthy, so a handler
    that only checks it treats an app exactly like the dashboard operator.
    """

    @web.middleware
    async def _mw(request: web.Request, handler):
        request["user"] = "dashboard"
        request["app"] = app_name
        request["internal_auth"] = True
        return await handler(request)

    return _mw


def _app_client(app_name: str = "pwn") -> TestClient:
    app = web.Application(middlewares=[_app_token_middleware(app_name)])
    app.router.add_get("/api/computer-use/config", cu_api.api_computer_use_config_get)
    app.router.add_put("/api/computer-use/config", cu_api.api_computer_use_config_save)
    return TestClient(TestServer(app))


class TestAnAppTokenCannotWriteTheKeystone:
    """The one write the keystone design exists to prevent (reviewer finding).

    ``enable_state.save_state`` deliberately bypasses ``is_sensitive_path`` — that is
    what lets the operator's own Settings panel write a file the agent cannot read or
    write with a tool. So this handler is the only thing standing between an
    App-Kit-scoped token and ``enabled: true``. Checking nothing is unsafe:
    ``request["user"]`` is truthy for an app token too, and an app whose manifest
    declares ``permissions.api: ["/api/computer-use"]`` passes
    ``app_token_path_allowed`` (verified: a bare ``/api/computer-use`` pattern matches
    ``/api/computer-use/config`` on the path boundary). An agent that can author an app
    manifest could therefore mint a token and turn on its own desktop automation.
    """

    @pytest.mark.asyncio
    async def test_an_app_token_put_is_refused_and_the_keystone_is_untouched(
        self, home, state_file, mock_sel
    ):
        async with _app_client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
        assert resp.status == 403
        # THE assertion: refused BEFORE any write — the keystone never appears.
        assert (
            not state_file.exists()
            or json.loads(state_file.read_text()).get(STATE_KEY_ENABLED) is not True
        )
        # A security-boundary denial is audited (backend-security-controls).
        assert mock_sel.log_api_access.called

    @pytest.mark.asyncio
    async def test_an_app_token_cannot_edit_the_target_lists_either(self, home, state_file):
        """``allowed_apps`` widening is the same boundary as the enable."""
        async with _app_client() as client:
            resp = await client.put(
                "/api/computer-use/config", json={"allowed_apps": ["com.apple.Terminal"]}
            )
        assert resp.status == 403
        assert not state_file.exists()

    @pytest.mark.asyncio
    async def test_an_app_token_cannot_shrink_a_budget_either(self, home, config_file):
        """Refused at the door, before the widen/narrow distinction is reached.

        The 409 ceiling logic distinguishes widening from narrowing; this gate does
        not, deliberately — an app token has no business writing ANY of the
        operator's computer-use configuration, and a narrow-only exception would be a
        second code path to keep correct for no user benefit.
        """
        async with _app_client() as client:
            resp = await client.put("/api/computer-use/config", json={"max_tree_nodes": 200})
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_the_dashboard_user_is_still_allowed(self, home, state_file):
        """The gate must key on ``app``, not break the operator's own panel."""
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
        assert resp.status == 200
        assert json.loads(state_file.read_text(encoding="utf-8"))[STATE_KEY_ENABLED] is True


class TestMixedSaveIsAllOrNothing:
    """A mixed state+limits PUT must not half-apply (reviewer finding).

    The keystone write lands first, so a corrupt ``config.json`` can leave the
    SECURITY state applied — the feature enabled, or the real-pointer opt-in set —
    while the response told the operator the save had failed. The ceiling had moved
    and nothing said so.
    """

    @pytest.mark.asyncio
    async def test_a_corrupt_config_leaves_the_keystone_untouched(self, home, state_file):
        (home / "config.json").write_text("{not json")
        async with _client() as client:
            resp = await client.put(
                "/api/computer-use/config",
                json={"enabled": True, "max_tree_nodes": 900},
            )
        assert resp.status == 500
        # THE assertion: the enable did not survive the failed save.
        assert (
            not state_file.exists()
            or json.loads(state_file.read_text()).get(STATE_KEY_ENABLED) is not True
        )

    @pytest.mark.asyncio
    async def test_a_state_only_save_still_works_with_no_config_file(self, home, state_file):
        """The preflight must not turn an absent config.json into a failure."""
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
        assert resp.status == 200
        assert json.loads(state_file.read_text())[STATE_KEY_ENABLED] is True

    @pytest.mark.asyncio
    async def test_a_healthy_mixed_save_applies_both(self, home, state_file, config_file):
        async with _client() as client:
            resp = await client.put(
                "/api/computer-use/config",
                json={"enabled": True, "max_tree_nodes": 900},
            )
        assert resp.status == 200
        assert json.loads(state_file.read_text())[STATE_KEY_ENABLED] is True
        assert json.loads(config_file.read_text())["computer_use"]["max_tree_nodes"] == 900

    @pytest.mark.asyncio
    async def test_a_write_time_OSError_can_never_leave_the_CEILING_applied(
        self, home, state_file, config_file, monkeypatch
    ):
        """The preflight cannot rule out a write-time failure — so ordering must.

        A full disk (or a mode change between the check and the write) raises from
        one writer after the other already landed. Two files are not atomic here,
        but the DIRECTION of a partial apply is a choice: limits first, keystone
        last, so the only reachable half-state is "budget knob moved, ceiling
        untouched" and a reported failure never hides an enable.
        """

        def boom(_patch):
            raise OSError("no space left on device")

        monkeypatch.setattr(cu_api, "_write_state", boom)
        async with _client() as client:
            resp = await client.put(
                "/api/computer-use/config",
                json={"enabled": True, "max_tree_nodes": 900},
            )
        assert resp.status == 500
        # THE assertion: the security state is untouched.
        assert (
            not state_file.exists()
            or json.loads(state_file.read_text()).get(STATE_KEY_ENABLED) is not True
        )

    @pytest.mark.asyncio
    async def test_the_keystone_is_written_after_the_limits(self, home, monkeypatch):
        """Pins the ORDER itself, so a refactor cannot quietly swap the two back."""
        order: list[str] = []
        real_state = cu_api._write_state
        real_limits = cu_api._write_limits

        def state(patch):
            order.append("state")
            real_state(patch)

        def limits(patch):
            order.append("limits")
            real_limits(patch)

        monkeypatch.setattr(cu_api, "_write_state", state)
        monkeypatch.setattr(cu_api, "_write_limits", limits)
        async with _client() as client:
            resp = await client.put(
                "/api/computer-use/config",
                json={"enabled": True, "max_tree_nodes": 900},
            )
        assert resp.status == 200
        assert order == ["limits", "state"]


class TestEnableRestartsSessions:
    """Flipping the enable must restart sessions, the way an MCP change does.

    kiro-cli caches ``tools/list`` for the LIFETIME of a session and ACP has no
    ``tools/list_changed`` notification, so without this the operator enables the
    feature, sees "0 tools" in the chat they are sitting in, and concludes it is
    broken. ``POST /api/mcp/sync`` already resets sessions for exactly this reason;
    this pins that the enable does the same.

    The three properties that matter are all about NOT restarting more than needed:
    a budget knob is read per call, and re-saving the same value must not tear down
    the user's session.
    """

    @staticmethod
    def _spy(monkeypatch) -> list:
        """Record calls to ``_reset_all_sessions`` without touching real sessions.

        The spec rebuild the enable flip performs is stubbed module-wide by the
        ``no_agent_spec_rewrite`` autouse fixture, so it needs no handling here.
        """
        calls: list = []
        import kiro_crew.dashboard.handlers.sessions as sessions_mod

        async def _fake(request):
            calls.append(request)
            return 3

        monkeypatch.setattr(sessions_mod, "_reset_all_sessions", _fake)
        return calls

    @pytest.mark.asyncio
    async def test_turning_it_ON_restarts_and_reports_the_count(self, state_file, monkeypatch):
        calls = self._spy(monkeypatch)
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
            body = await resp.json()
        assert resp.status == 200
        assert len(calls) == 1
        # Reported so the panel can explain the restart rather than leaving the
        # operator to guess why their session reset.
        assert body["sessions_reset"] == 3

    @pytest.mark.asyncio
    async def test_turning_it_OFF_also_restarts(self, state_file, monkeypatch):
        """Symmetric: a disabled feature must stop being callable in a live session."""
        state_file.write_text(json.dumps({STATE_KEY_ENABLED: True}), encoding="utf-8")
        calls = self._spy(monkeypatch)
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": False})
        assert resp.status == 200
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_a_NO_OP_resave_does_not_restart(self, state_file, monkeypatch):
        """Re-saving the same value must not kill the operator's session."""
        state_file.write_text(json.dumps({STATE_KEY_ENABLED: True}), encoding="utf-8")
        calls = self._spy(monkeypatch)
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
            body = await resp.json()
        assert resp.status == 200
        assert calls == []
        assert body["sessions_reset"] == 0

    @pytest.mark.asyncio
    async def test_a_LIMITS_only_save_does_not_restart(self, config_file, monkeypatch):
        """Budget knobs are read per call — restarting for one would be gratuitous."""
        calls = self._spy(monkeypatch)
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"max_tree_nodes": 400})
        assert resp.status == 200
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_failed_restart_does_not_fail_the_SAVE(self, state_file, monkeypatch):
        """The write already landed; reporting failure would be a lie.

        Worst case is the pre-existing behaviour — the new tool surface appears on
        the next cold session — which is strictly better than telling the operator
        their save failed when the keystone did move.
        """
        import kiro_crew.dashboard.handlers.sessions as sessions_mod

        async def _boom(request):
            raise RuntimeError("no session manager on this app")

        monkeypatch.setattr(sessions_mod, "_reset_all_sessions", _boom)
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
            body = await resp.json()
        assert resp.status == 200
        assert body["enabled"] is True
        assert body["sessions_reset"] == 0
        assert json.loads(state_file.read_text())[STATE_KEY_ENABLED] is True

    @pytest.mark.asyncio
    async def test_the_flip_REBUILDS_the_agent_spec(self, state_file, monkeypatch):
        """The enable is a spec-emission gate, so the reset needs a fresh spec.

        ``agent._computer_use_spec_gate`` keeps ``kirocrew-computer`` out of the
        emitted spec while the keystone is off, so restarting sessions without
        rebuilding would restart them into a spec that still omits the server —
        the operator enables the feature and the tools appear only after the next
        gateway start.
        """
        import kiro_crew.agent as agent_mod

        self._spy(monkeypatch)
        built: list = []
        monkeypatch.setattr(
            agent_mod,
            "rebuild_agent_config",
            lambda **k: (built.append(k), Path("/dev/null"))[1],
        )
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
        assert resp.status == 200
        assert len(built) == 1

    @pytest.mark.asyncio
    async def test_a_NO_OP_resave_does_not_rebuild(self, state_file, monkeypatch):
        """Same narrowness as the reset: no transition, no work."""
        import kiro_crew.agent as agent_mod

        state_file.write_text(json.dumps({STATE_KEY_ENABLED: True}), encoding="utf-8")
        self._spy(monkeypatch)
        built: list = []
        monkeypatch.setattr(
            agent_mod,
            "rebuild_agent_config",
            lambda **k: (built.append(k), Path("/dev/null"))[1],
        )
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
        assert resp.status == 200
        assert built == []

    @pytest.mark.asyncio
    async def test_the_spec_rebuild_HOLDS_the_config_lock(self, state_file, monkeypatch):
        """**The rebuild must not escape the lock that serialises keystone writes.**

        The rebuild READS the keystone and WRITES the spec. Outside the lock, two
        overlapping PUTs interleave: an enable's slower rebuild can land its spec
        AFTER a later disable's, leaving a spec that mounts — and therefore spawns
        — the server the keystone now forbids. Holding the lock makes
        read-decide-write atomic against every keystone writer, so whichever
        rebuild finishes last is the one that read the final state.

        Asserted on the lock's own state at rebuild time rather than on source
        order: a lock acquired and released before the call would read the same in
        the source and fix nothing.
        """
        import kiro_crew.agent as agent_mod
        from kiro_crew.dashboard.handlers.agents import _get_config_lock

        self._spy(monkeypatch)
        # Resolved on the loop: ``_get_config_lock`` needs a running loop, and the
        # rebuild itself runs in a worker thread where there is none.
        lock = _get_config_lock()
        held: list[bool] = []
        monkeypatch.setattr(
            agent_mod,
            "rebuild_agent_config",
            lambda **_: (held.append(lock.locked()), Path("/dev/null"))[1],
        )
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
        assert resp.status == 200
        assert held == [True], "the spec rebuild ran outside the config lock"

    @pytest.mark.asyncio
    async def test_no_test_here_can_reach_the_REAL_spec_rebuild(self, state_file, monkeypatch):
        """Ratchet for the ``no_agent_spec_rewrite`` autouse fixture.

        Without this, deleting that fixture fails nothing: every test would still
        pass while quietly rewriting the developer's ``~/.kiro/agents``. The
        assertion is behavioural — it flips the enable for real and checks that
        what the handler called was the stub, not the module's own function.
        """
        import kiro_crew.agent as agent_mod

        reached: list[str] = []
        current = agent_mod.rebuild_agent_config
        if getattr(current, "__name__", "") != "<lambda>":
            # The guard is gone; install a tripwire so the call is observable.
            monkeypatch.setattr(
                agent_mod,
                "rebuild_agent_config",
                lambda **_: (reached.append("real"), Path("/dev/null"))[1],
            )
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
        assert resp.status == 200
        assert reached == [], (
            "the real rebuild_agent_config was reachable — the no_agent_spec_rewrite "
            "autouse fixture is missing or no longer autouse"
        )

    @pytest.mark.asyncio
    async def test_patching_the_agent_module_REACHES_the_handler(self, state_file, monkeypatch):
        """The guard above only works because the import resolves at CALL time.

        ``api_computer_use_config_save`` imports ``rebuild_agent_config`` inside the
        function. Hoisting it to module scope would bind the name at import time,
        so patching ``kiro_crew.agent`` would not reach this call site — and
        the guard above would keep passing while every enable-flipping test wrote
        the operator's real ``~/.kiro/agents`` again.

        Asserted POSITIVELY (the stub must have run), because "the real one did not
        run" is also true when nothing ran at all.
        """
        import kiro_crew.agent as agent_mod

        ran: list[str] = []
        monkeypatch.setattr(
            agent_mod,
            "rebuild_agent_config",
            lambda **_: (ran.append("stub"), Path("/dev/null"))[1],
        )
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
        assert resp.status == 200
        assert ran == ["stub"], (
            "patching kiro_crew.agent no longer reaches the handler's call site — the "
            "import was hoisted to module scope, which defeats no_agent_spec_rewrite"
        )

    @pytest.mark.asyncio
    async def test_a_failed_rebuild_does_not_fail_the_SAVE(self, state_file, monkeypatch):
        """Same rule the reset already followed: the write landed and was audited.

        The fallback is the pre-existing behaviour — the tool surface appears on
        the next gateway start — and the sessions are still reset, because a stale
        ``tools/list`` is a separate problem from a stale spec.
        """
        import kiro_crew.agent as agent_mod

        calls = self._spy(monkeypatch)

        def _boom(**_):
            raise OSError("read-only filesystem")

        monkeypatch.setattr(agent_mod, "rebuild_agent_config", _boom)
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
            body = await resp.json()
        assert resp.status == 200
        assert body["enabled"] is True
        assert len(calls) == 1
        assert json.loads(state_file.read_text())[STATE_KEY_ENABLED] is True
        assert body["agent_spec_rewritten"] is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("rebuild", "expected"),
        [
            pytest.param(
                lambda **kw: (kw["_wrote_out"].append(True), Path("/dev/null"))[1],
                True,
                id="wrote",
            ),
            # The shared-home guard refused: the path comes back and nothing is written.
            pytest.param(lambda **_: Path("/dev/null"), False, id="declined"),
        ],
    )
    async def test_the_save_reports_whether_the_spec_was_rewritten(
        self, state_file, monkeypatch, rebuild, expected
    ):
        import kiro_crew.agent as agent_mod

        self._spy(monkeypatch)
        monkeypatch.setattr(agent_mod, "rebuild_agent_config", rebuild)
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
            body = await resp.json()
        assert resp.status == 200
        assert body["enabled"] is True
        assert body["agent_spec_rewritten"] is expected

    @pytest.mark.asyncio
    async def test_a_save_without_an_enable_change_reports_no_rebuild(
        self, state_file, monkeypatch
    ):
        state_file.write_text(json.dumps({STATE_KEY_ENABLED: True}), encoding="utf-8")
        self._spy(monkeypatch)
        async with _client() as client:
            resp = await client.put("/api/computer-use/config", json={"enabled": True})
            body = await resp.json()
        assert resp.status == 200
        assert "agent_spec_rewritten" not in body


class TestAMalformedKeystoneStillRendersSettings:
    """GPT 5.6 BLOCKING, confirmed: a hand-edited keystone 500'd the config GET.

    ``load_policy_config`` raises on a present-but-malformed ``allowed_apps`` **by
    design** — refusing is the safe direction for a value clearly meant to narrow
    something, because coercing it to the empty tuple would silently convert a
    restriction into no restriction at all. That is right on the ACTION path.

    On the READ path it was a self-inflicted lockout: the exception escaped
    ``_snapshot()``, the Settings GET returned HTTP 500, and the only UI that can
    repair the file became unreachable. The page has to render precisely BECAUSE the
    file is broken.
    """

    @pytest.mark.asyncio
    async def test_the_GET_renders_instead_of_500ing(self, state_file: Path):
        # A bare string where a list belongs — the realistic hand-edit.
        state_file.write_text(json.dumps({"enabled": True, "allowed_apps": "Preview"}))
        async with _client() as client:
            resp = await client.get("/api/computer-use/config")
            body = await resp.json()
        assert resp.status == 200
        # And it reports the enable truthfully rather than degrading to "off", which
        # would misrepresent the machine's actual state.
        assert body["enabled"] is True

    @pytest.mark.asyncio
    async def test_the_payload_SAYS_the_policy_is_unreadable(self, state_file: Path):
        """An empty list must not read as "no restriction configured".

        Without this the panel would show a confidently empty allow-list for a file
        that actually contains one — the operator would see the restriction they
        wrote silently absent, with nothing to explain why.
        """
        state_file.write_text(json.dumps({"enabled": True, "allowed_apps": "Preview"}))
        async with _client() as client:
            body = await (await client.get("/api/computer-use/config")).json()
        assert body["allowed_apps"] == []
        assert "allowed_apps" in body["policy_error"]

    @pytest.mark.asyncio
    async def test_a_LIST_of_malformed_entries_is_handled_too(self, state_file: Path):
        """``[{"name": "Preview"}]`` is a list, so the container check passes and every
        item is dropped — the same widening bug one level down, and the same 500."""
        state_file.write_text(json.dumps({"enabled": True, "allowed_apps": [{"name": "Preview"}]}))
        async with _client() as client:
            resp = await client.get("/api/computer-use/config")
        assert resp.status == 200

    @pytest.mark.asyncio
    async def test_a_HEALTHY_policy_reports_no_error(self, state_file: Path):
        """Inverse guard: the fallback must not fire on a well-formed file, or every
        real allow-list would render as unreadable."""
        state_file.write_text(json.dumps({"enabled": True, "allowed_apps": ["Preview", "Notes"]}))
        async with _client() as client:
            body = await (await client.get("/api/computer-use/config")).json()
        assert body["allowed_apps"] == ["preview", "notes"]
        assert body["policy_error"] == ""

    def test_the_ACTION_path_still_refuses(self, state_file: Path):
        """THE assertion that keeps this a rendering fix and not a ceiling change.

        Making the GET fail soft must not make the dispatcher fail soft: a malformed
        allow-list still has to refuse the action, or the fix would have converted an
        operator's restriction into no restriction — exactly the bug the strict mode
        exists to prevent.
        """
        state_file.write_text(json.dumps({"enabled": True, "allowed_apps": "Preview"}))
        with pytest.raises(PolicyStateError):
            enable_state.load_policy_config()
