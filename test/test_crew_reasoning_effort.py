"""A crew's own reasoning-effort pin (``agents.<name>.reasoning_effort``).

The pin is the tier between an explicit per-session override and the configured
default. These tests hold the four properties that make it real rather than
decorative:

1. It is resolved in ONE place (the provider factory), so a crew woken by a
   schedule or a webhook -- which has no dashboard slot to carry an override --
   gets it too.
2. It ranks correctly: below an explicit override, above BOTH defaults.
3. It cannot be served from the warm pool, whose pre-warmed children were built
   under a different effort overlay.
4. Bad input is rejected at the API and coerced at the file-load boundary --
   never carried to the provider, where kiro-cli refuses the whole overlay.
"""

from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig, coerce_effort


@pytest.fixture(autouse=True)
def _owner_caller(monkeypatch):
    """Exercise the agent handlers past their independent owner-auth boundary."""
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request",
        lambda request: True,
    )


class TestCoerceEffort:
    @pytest.mark.parametrize("level", ["low", "medium", "high", "xhigh", "max"])
    def test_a_concrete_level_survives(self, level):
        assert coerce_effort(level) == level

    def test_surrounding_whitespace_is_stripped(self):
        assert coerce_effort("  high  ") == "high"

    @pytest.mark.parametrize("bad", ["", "  ", "ultra", "HIGH", None, 3, {"low": 1}, ["high"]])
    def test_anything_else_collapses_to_inherit(self, bad):
        """A hand-edited config must not be able to stop a session from starting.

        ``"HIGH"`` is in the list on purpose: the levels are lowercase on the
        wire, and silently case-folding here would accept a spelling the API
        rejects, so the two boundaries would disagree about the same value.
        """
        assert coerce_effort(bad) == ""


class TestCrewPinnedEffort:
    """The resolver the factory and the warm-pool probe share."""

    def _cfg(self, **crews: str) -> KiroCrewConfig:
        cfg = KiroCrewConfig()
        for name, effort in crews.items():
            cfg.agents[name] = KiroCrewAgentConfig(kiro_agent="kirocrew", reasoning_effort=effort)
        return cfg

    def test_reads_the_crew_named_by_crew_agent(self):
        cfg = self._cfg(reviewer="max")
        assert cfg.crew_pinned_effort(None, "reviewer") == "max"

    def test_reads_a_crew_named_by_agent_alone(self):
        """Slack threads, cron jobs and spawned agents pass a CREW name as
        ``agent`` with no ``crew_agent`` -- the surface convention
        ``resolve_crew_identity`` documents. Those are exactly the unattended
        sessions the pin exists for, so the fallback must resolve them."""
        cfg = self._cfg(reviewer="high")
        assert cfg.crew_pinned_effort("reviewer") == "high"

    def test_empty_crew_agent_means_no_crew(self):
        """ "" is authoritative, not absent: the dashboard passes it to opt OUT of
        the name fallback, so a kiro template name that happens to match a crew
        key must not inherit that crew's pin."""
        cfg = self._cfg(reviewer="max")
        assert cfg.crew_pinned_effort("reviewer", "") == ""

    def test_an_unknown_crew_pins_nothing(self):
        assert self._cfg(reviewer="max").crew_pinned_effort(None, "ghost") == ""

    def test_a_crew_with_no_pin_returns_empty(self):
        assert self._cfg(reviewer="").crew_pinned_effort(None, "reviewer") == ""

    def test_a_junk_stored_value_is_not_returned(self):
        """Defence in depth: the load path coerces, but a config object built in
        process (an app, a test, a future writer) can still hold anything."""
        cfg = self._cfg(reviewer="max")
        cfg.agents["reviewer"].reasoning_effort = "ultra"
        assert cfg.crew_pinned_effort(None, "reviewer") == ""


class TestResolveSessionEffort:
    """The chain below an explicit override, shared by the factory and the API.

    It exists because two callers need the SAME answer: the pane's job is to say
    what a session will run at, so a second copy of the chain drifting from the
    factory's would report a background-agent crew at the chat default while it
    actually runs at the role effort.
    """

    def _cfg(self, *, chat: str = "", background: str = "", crew_pin: str = "") -> KiroCrewConfig:
        cfg = KiroCrewConfig()
        cfg.agent.reasoning_effort = chat
        if background:
            cfg.agent.role_efforts = {"background": background}
        cfg.agents["worker"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew-lite", reasoning_effort=crew_pin
        )
        return cfg

    def test_a_background_agent_takes_the_role_effort_not_the_chat_default(self):
        cfg = self._cfg(chat="low", background="high")
        assert cfg.resolve_session_effort("kirocrew-lite", "worker") == "high"

    def test_the_role_check_follows_the_bound_template_not_the_passed_name(self):
        """Slack threads, cron jobs and spawned agents pass a CREW name as
        ``agent``. Keying the role check on that raw value put an unpinned crew
        bound to a background worker on the chat default whenever the surface
        named the crew, while the API readout -- which passes the resolved
        kiro_agent -- reported the role effort."""
        cfg = self._cfg(chat="low", background="high")
        # The cron/Slack shape: a CREW name in `agent`, no crew_agent at all.
        assert cfg.resolve_session_effort("worker") == "high"

    def test_a_crew_named_after_a_background_agent_follows_its_own_binding(self):
        """The name collision must not decide it either: a crew that happens to be
        called kirocrew-lite while bound to a normal template runs that template,
        so it takes the chat default."""
        cfg = self._cfg(chat="low", background="high")
        cfg.agents["kirocrew-lite"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        assert cfg.resolve_session_effort("kirocrew-lite") == "low"

    def test_the_bare_background_key_still_resolves_the_role_effort(self):
        """With no crew record to read, ``agent`` IS the agent name -- the
        background and heartbeat session keys pass it directly."""
        cfg = self._cfg(chat="low", background="high")
        assert cfg.resolve_session_effort("kirocrew-heartbeat", "") == "high"

    def test_a_normal_agent_takes_the_chat_default(self):
        cfg = self._cfg(chat="low", background="high")
        assert cfg.resolve_session_effort("kirocrew", "") == "low"

    def test_a_crew_pin_outranks_the_role_effort(self):
        cfg = self._cfg(chat="low", background="high", crew_pin="max")
        assert cfg.resolve_session_effort("kirocrew-lite", "worker") == "max"

    def test_empty_when_no_tier_pins_anything(self):
        assert self._cfg().resolve_session_effort("kirocrew", "") == ""

    def test_the_factory_and_the_resolver_cannot_disagree(self):
        """Pins the sharing itself: the factory must not re-derive the chain.

        A copy inside the factory is what the API readout would silently drift
        from, and the drift is invisible in any test that only exercises one side.
        """
        import inspect

        from kiro_crew.config import loader

        src = inspect.getsource(loader.KiroCrewConfig.create_provider_factory)
        assert "resolve_session_effort" in src
        # The background-agent pair lives in ONE place now.
        assert '"kirocrew-lite"' not in src


class TestBackgroundWorkerAgents:
    def test_names_the_two_role_agents(self):
        from kiro_crew.config.loader import BACKGROUND_WORKER_AGENTS

        assert BACKGROUND_WORKER_AGENTS == ("kirocrew-lite", "kirocrew-heartbeat")


class TestConfigLoadCoercesTheCrewPin:
    def test_a_valid_level_round_trips_through_save_and_load(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew", reasoning_effort="xhigh"
        )
        cfg.save()

        assert KiroCrewConfig.load().agents["reviewer"].reasoning_effort == "xhigh"

    def test_a_hand_edited_junk_level_loads_as_inherit(self):
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        # Write the junk the way a human editing config.json would: past the API,
        # straight into the stored document.
        cfg.agents["reviewer"].reasoning_effort = "ultra"
        cfg.save()

        assert KiroCrewConfig.load().agents["reviewer"].reasoning_effort == ""


class TestFactoryAppliesTheCrewPin:
    """The factory is the single authority, so ONE lookup covers every surface."""

    def _capture(self, *, crews: dict[str, str], config_effort: str = "", **factory_call):
        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"
        cfg.agent.reasoning_effort = config_effort
        for name, effort in crews.items():
            cfg.agents[name] = KiroCrewAgentConfig(kiro_agent="kirocrew", reasoning_effort=effort)
        with patch("kiro_crew.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            factory(**factory_call)
            assert mock_provider.called, "factory did not construct AcpProvider"
            return mock_provider.call_args.kwargs

    def test_crew_pin_reaches_the_provider_with_no_override_present(self):
        kwargs = self._capture(
            crews={"reviewer": "max"},
            session_key="cron:nightly",
            crew_agent="reviewer",
            model_override="claude-opus-4.7",
        )
        assert kwargs.get("effort_per_model") == {"claude-opus-4.7": "max"}

    def test_crew_pin_beats_the_global_default(self):
        kwargs = self._capture(
            crews={"reviewer": "max"},
            config_effort="low",
            session_key="cron:nightly",
            crew_agent="reviewer",
            model_override="claude-opus-4.7",
        )
        assert kwargs.get("effort_per_model") == {"claude-opus-4.7": "max"}

    def test_an_explicit_override_still_beats_the_crew_pin(self):
        """A per-session pick outranks the crew, exactly as it does for model."""
        kwargs = self._capture(
            crews={"reviewer": "low"},
            session_key="dashboard:1",
            crew_agent="reviewer",
            model_override="claude-opus-4.7",
            reasoning_effort_override="max",
        )
        assert kwargs.get("effort_per_model") == {"claude-opus-4.7": "max"}

    def test_a_pin_on_a_background_crew_beats_its_role_default(self):
        """``kirocrew-lite`` resolves the "background" role effort by name. A pin
        the operator typed on that crew is a choice; the role effort is a
        built-in, so the choice wins."""
        cfg = KiroCrewConfig()
        cfg.agent.provider = "acp"
        cfg.agent.role_efforts = {"background": "low"}
        cfg.agents["kirocrew-lite"] = KiroCrewAgentConfig(
            kiro_agent="kirocrew-lite", reasoning_effort="high"
        )
        with patch("kiro_crew.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            factory = cfg.create_provider_factory()
            factory(
                session_key="background",
                agent="kirocrew-lite",
                crew_agent="kirocrew-lite",
                model_override="claude-opus-4.7",
            )
            kwargs = mock_provider.call_args.kwargs
        assert kwargs.get("effort_per_model") == {"claude-opus-4.7": "high"}

    def test_an_unpinned_crew_changes_nothing(self):
        """The no-pin path must be byte-identical to the pre-field behaviour."""
        kwargs = self._capture(
            crews={"reviewer": ""},
            config_effort="high",
            session_key="cron:nightly",
            crew_agent="reviewer",
            model_override="claude-opus-4.7",
        )
        assert kwargs.get("effort_per_model") == {"claude-opus-4.7": "high"}

    def test_crew_pin_is_dropped_on_a_model_that_cannot_use_it(self):
        """Same rule as the global default: a level is never forced onto a model
        that rejects effort, because kiro-cli refuses the whole overlay."""
        kwargs = self._capture(
            crews={"reviewer": "max"},
            session_key="cron:nightly",
            crew_agent="reviewer",
            model_override="claude-haiku-4.5",
        )
        assert kwargs.get("effort_per_model") == {}


class TestWarmPoolBypassesACrewPin:
    """A pooled child was spawned under whatever overlay was current when the
    pool filled, and the claim path re-keys the model but never re-pushes
    effort -- so a warm hit would silently run the crew at the wrong depth."""

    def _service(self, cfg: KiroCrewConfig):
        from kiro_crew.session_allocation import SessionAllocationService

        service = SessionAllocationService.__new__(SessionAllocationService)
        service._deps = MagicMock()
        service._deps.load_config = lambda: cfg
        return service

    def _cfg(self, effort: str) -> KiroCrewConfig:
        cfg = KiroCrewConfig()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew", reasoning_effort=effort)
        return cfg

    @pytest.mark.asyncio
    async def test_true_for_a_pinned_crew(self):
        service = self._service(self._cfg("max"))
        assert await service._crew_pins_effort(None, "reviewer") is True

    @pytest.mark.asyncio
    async def test_false_for_an_unpinned_crew(self):
        service = self._service(self._cfg(""))
        assert await service._crew_pins_effort(None, "reviewer") is False

    @pytest.mark.asyncio
    async def test_false_when_no_crew_resolves(self):
        service = self._service(self._cfg("max"))
        assert await service._crew_pins_effort(None, "") is False

    @pytest.mark.asyncio
    async def test_a_non_string_crew_agent_is_not_passed_through(self):
        """``extra_factory_kwargs`` is untyped, so the probe must narrow before
        handing the value to the resolver -- a non-string would otherwise be
        returned verbatim by ``resolve_crew_identity`` and indexed into
        ``config.agents``."""
        service = self._service(self._cfg("max"))
        assert await service._crew_pins_effort(None, object()) is False

    @pytest.mark.asyncio
    async def test_an_unreadable_config_pools_as_before(self):
        """Failure must not stop a session from starting."""
        from kiro_crew.session_allocation import SessionAllocationService

        service = SessionAllocationService.__new__(SessionAllocationService)
        service._deps = MagicMock()

        def _boom():
            raise OSError("config unreadable")

        service._deps.load_config = _boom
        assert await service._crew_pins_effort(None, "reviewer") is False

    def test_the_pool_decision_consults_the_probe(self):
        """Structural pin, matching the sibling disqualifier in test_effort.py:
        the arm is unreachable from a unit test without standing up a real pool,
        and its absence is exactly the silent-no-op this class exists to stop."""
        import inspect

        from kiro_crew import session_allocation

        src = inspect.getsource(session_allocation.SessionAllocationService.get_or_create)
        assert "_crew_pins_effort" in src
        assert 'pool_decision = "bypass_effort"' in src


class TestConcurrentRefreshesInstallInOrder:
    """Two overlapping refreshes must leave the NEWEST config installed.

    Lives with this feature's tests because this PR is what made the window
    reachable: taking the config load off the event loop (so a crew save does not
    stall the gateway) introduced an await point between READING the config and
    INSTALLING it. Two refreshes could then complete their loads out of order and
    let the older one install last, pinning every new session to stale defaults
    until the next restart -- silently, and specifically for the effort settings
    this PR added. Holding the pool-fill lock across load AND install closes it.
    """

    @pytest.mark.asyncio
    async def test_the_older_load_cannot_install_last(self):
        import threading
        import time
        from unittest.mock import AsyncMock

        from kiro_crew.session import SessionManager

        base = KiroCrewConfig()
        base.session.timeout_secs = 2
        first = copy.deepcopy(base)
        first.agent.reasoning_effort = "low"
        second = copy.deepcopy(base)
        second.agent.reasoning_effort = "max"
        loads = [first, second]

        seen = 0
        guard = threading.Lock()

        def _staggered_load():
            """The load STARTED first finishes LAST -- the inversion under test."""
            nonlocal seen
            with guard:
                index = seen
                seen += 1
            # Blocking sleep on purpose: this runs in the to_thread worker, and it
            # is what makes the completion order the opposite of the start order.
            time.sleep(0.30 if index == 0 else 0.0)
            return loads[min(index, len(loads) - 1)]

        mgr = SessionManager(base, provider_factory=lambda **_kw: AsyncMock())
        with (
            patch.object(mgr, "start_pool", AsyncMock()),
            patch.object(mgr, "_retire_stale_backend_bg_runtime", AsyncMock()),
            patch("kiro_crew.session.build_provider_factory", return_value=MagicMock()),
            patch("kiro_crew.session.KiroCrewConfig.load", side_effect=_staggered_load),
        ):
            await asyncio.gather(mgr.refresh_defaults(), mgr.refresh_defaults())

        assert seen == 2, "both refreshes must have loaded"
        # The second load reflects the newer on-disk state, so it must be what is
        # left installed no matter which thread finished first.
        assert mgr._cfg.agent.reasoning_effort == "max"
        await mgr.close_all()


def _crud_app() -> web.Application:
    from kiro_crew.dashboard.handlers import (
        api_kirocrew_agent_update,
        api_kirocrew_agents_create,
    )

    app = web.Application()
    app.router.add_post("/api/agents", api_kirocrew_agents_create)
    app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
    return app


@pytest.fixture()
def seeded_agent():
    """One stored crew, written through the real config API."""
    cfg = KiroCrewConfig.load()
    cfg.agents["existing"] = KiroCrewAgentConfig(
        kiro_agent="kirocrew", workspace="default", memory_store="default"
    )
    cfg.save()
    return "existing"


class TestApiValidatesTheCrewPin:
    """The API rejects instead of coercing: a typo sent by the form has an author
    on the other end, and a silent "" would read back as "inherits" and look
    like the save was lost."""

    @pytest.mark.asyncio
    async def test_create_stores_a_valid_level(self):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents",
                json={"name": "reviewer", "kiro_agent": "kirocrew", "reasoning_effort": "xhigh"},
            )
            assert resp.status == 200

        assert KiroCrewConfig.load().agents["reviewer"].reasoning_effort == "xhigh"

    @pytest.mark.asyncio
    async def test_create_refuses_an_unknown_level_and_stores_nothing(self):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents",
                json={"name": "reviewer", "kiro_agent": "kirocrew", "reasoning_effort": "ultra"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["code"] == "invalid_reasoning_effort"
            # The message must name the accepted values; "invalid" alone leaves
            # the author guessing at the vocabulary.
            assert "xhigh" in body["error"]

        assert "reviewer" not in KiroCrewConfig.load().agents

    @pytest.mark.asyncio
    async def test_create_without_the_field_defaults_to_inherit(self):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.post(
                "/api/agents", json={"name": "reviewer", "kiro_agent": "kirocrew"}
            )
            assert resp.status == 200

        assert KiroCrewConfig.load().agents["reviewer"].reasoning_effort == ""

    @pytest.mark.asyncio
    async def test_update_stores_a_valid_level(self, seeded_agent):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}", json={"reasoning_effort": "high"}
            )
            assert resp.status == 200

        assert KiroCrewConfig.load().agents[seeded_agent].reasoning_effort == "high"

    @pytest.mark.asyncio
    async def test_update_clears_a_pin_with_the_empty_string(self, seeded_agent):
        async with TestClient(TestServer(_crud_app())) as client:
            assert (
                await client.put(f"/api/agents/{seeded_agent}", json={"reasoning_effort": "high"})
            ).status == 200
            assert (
                await client.put(f"/api/agents/{seeded_agent}", json={"reasoning_effort": ""})
            ).status == 200

        assert KiroCrewConfig.load().agents[seeded_agent].reasoning_effort == ""

    @pytest.mark.asyncio
    async def test_update_refuses_an_unknown_level_and_writes_nothing(self, seeded_agent):
        """The whole request must be a no-op, not just the offending field: the
        validation runs before the first assignment, so a rejected effort cannot
        smuggle a workspace change through with it."""
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"workspace": "other", "reasoning_effort": "ultra"},
            )
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_reasoning_effort"

        stored = KiroCrewConfig.load().agents[seeded_agent]
        assert stored.reasoning_effort == ""
        assert stored.workspace == "default"

    @pytest.mark.asyncio
    async def test_update_refuses_a_non_string(self, seeded_agent):
        async with TestClient(TestServer(_crud_app())) as client:
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"reasoning_effort": 3})
            assert resp.status == 400
            assert (await resp.json())["code"] == "invalid_reasoning_effort"


class TestResolvedEndpointReportsEffort:
    @pytest.mark.asyncio
    async def test_reports_a_crew_pin_as_pinned(self, seeded_agent):
        from kiro_crew.dashboard.handlers import api_kirocrew_agent_resolved_model

        cfg = KiroCrewConfig.load()
        cfg.agents[seeded_agent].reasoning_effort = "max"
        cfg.agent.reasoning_effort = "low"
        cfg.save()

        app = web.Application()
        app.router.add_get("/api/agents/resolved-model", api_kirocrew_agent_resolved_model)
        async with TestClient(TestServer(app)) as client:
            body = await (
                await client.get("/api/agents/resolved-model", params={"agent": seeded_agent})
            ).json()

        assert body["reasoning_effort"] == "max"
        assert body["effort_pinned"] is True

    @pytest.mark.asyncio
    async def test_reports_the_global_default_as_not_pinned(self, seeded_agent):
        from kiro_crew.dashboard.handlers import api_kirocrew_agent_resolved_model

        cfg = KiroCrewConfig.load()
        cfg.agent.reasoning_effort = "low"
        cfg.save()

        app = web.Application()
        app.router.add_get("/api/agents/resolved-model", api_kirocrew_agent_resolved_model)
        async with TestClient(TestServer(app)) as client:
            body = await (
                await client.get("/api/agents/resolved-model", params={"agent": seeded_agent})
            ).json()

        assert body["reasoning_effort"] == "low"
        assert body["effort_pinned"] is False

    @pytest.mark.asyncio
    async def test_reports_the_role_effort_for_a_background_agent_crew(self, seeded_agent):
        """The readout must not report the chat default for a crew that runs at
        the background role effort -- that is the drift the shared resolver
        exists to prevent, and it is invisible to a crew on a normal agent."""
        from kiro_crew.dashboard.handlers import api_kirocrew_agent_resolved_model

        cfg = KiroCrewConfig.load()
        cfg.agents[seeded_agent].kiro_agent = "kirocrew-lite"
        cfg.agent.reasoning_effort = "low"
        cfg.agent.role_efforts = {"background": "high"}
        cfg.save()

        app = web.Application()
        app.router.add_get("/api/agents/resolved-model", api_kirocrew_agent_resolved_model)
        async with TestClient(TestServer(app)) as client:
            body = await (
                await client.get("/api/agents/resolved-model", params={"agent": seeded_agent})
            ).json()

        assert body["reasoning_effort"] == "high"
        assert body["effort_pinned"] is False


class TestSavingAPinRefreshesSessionDefaults:
    """The factory answers from the config it captured at build time, so any change
    to what the effort chain READS off a crew record must invalidate that capture.

    The chain reads two fields -- the pin, and the bound ``kiro_agent`` the role
    default keys on -- so the trigger is a change to `_effort_inputs`, not a list
    of conditions per handler. Three review rounds found the per-condition version
    incomplete one case at a time; these tests pin the invariant instead.
    """

    def _app(self, refreshed: list[str]) -> web.Application:
        from kiro_crew.dashboard.handlers import (
            api_kirocrew_agent_delete,
            api_kirocrew_agent_update,
            api_kirocrew_agents_create,
        )

        class _Sessions:
            async def refresh_defaults(self) -> None:
                refreshed.append("yes")

        app = web.Application()
        app.router.add_post("/api/agents", api_kirocrew_agents_create)
        app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
        app.router.add_delete("/api/agents/{name}", api_kirocrew_agent_delete)
        app["state"] = SimpleNamespace(sessions=_Sessions())
        return app

    @pytest.mark.asyncio
    async def test_update_that_changes_the_pin_refreshes(self, seeded_agent):
        refreshed: list[str] = []
        async with TestClient(TestServer(self._app(refreshed))) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}", json={"reasoning_effort": "high"}
            )
            assert resp.status == 200
        assert refreshed == ["yes"]

    @pytest.mark.asyncio
    async def test_rebinding_kiro_agent_refreshes_even_with_no_pin(self, seeded_agent):
        """The role default reads the BOUND kiro_agent, so re-binding a crew to a
        background worker changes what it resolves to while it pins nothing."""
        refreshed: list[str] = []
        async with TestClient(TestServer(self._app(refreshed))) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}", json={"kiro_agent": "kirocrew-lite"}
            )
            assert resp.status == 200
        assert refreshed == ["yes"]

    @pytest.mark.asyncio
    async def test_update_that_changes_neither_input_does_not_refresh(self, seeded_agent):
        """refresh_defaults drains the warm pool, and the crew form sends
        reasoning_effort on EVERY save (that is what makes clearing possible), so
        refreshing on the field's presence would cost a cold start on every
        unrelated crew edit."""
        cfg = KiroCrewConfig.load()
        cfg.agents[seeded_agent].reasoning_effort = "high"
        cfg.save()

        refreshed: list[str] = []
        async with TestClient(TestServer(self._app(refreshed))) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}",
                json={"reasoning_effort": "high", "triggers": "pager duty"},
            )
            assert resp.status == 200
        assert refreshed == []

    @pytest.mark.asyncio
    async def test_clearing_a_pin_refreshes(self, seeded_agent):
        cfg = KiroCrewConfig.load()
        cfg.agents[seeded_agent].reasoning_effort = "high"
        cfg.save()

        refreshed: list[str] = []
        async with TestClient(TestServer(self._app(refreshed))) as client:
            resp = await client.put(f"/api/agents/{seeded_agent}", json={"reasoning_effort": ""})
            assert resp.status == 200
        assert refreshed == ["yes"]

    @pytest.mark.asyncio
    async def test_create_refreshes_even_without_a_pin(self):
        """A crew APPEARING changes the chain's answer with no pin of its own: the
        captured config cannot read a binding for a crew it does not know, so a
        scheduled session naming it would take the chat default instead of the
        role effort its binding implies."""
        refreshed: list[str] = []
        async with TestClient(TestServer(self._app(refreshed))) as client:
            resp = await client.post(
                "/api/agents", json={"name": "worker", "kiro_agent": "kirocrew-lite"}
            )
            assert resp.status == 200
        assert refreshed == ["yes"]

    @pytest.mark.asyncio
    async def test_delete_refreshes(self, seeded_agent):
        """The other half: the captured config still holds a deleted crew's record,
        so a cron job still naming it would keep resolving the old pin."""
        refreshed: list[str] = []
        async with TestClient(TestServer(self._app(refreshed))) as client:
            resp = await client.delete(f"/api/agents/{seeded_agent}")
            assert resp.status == 200
        assert refreshed == ["yes"]

    def test_the_trigger_is_derived_from_the_resolver_inputs(self):
        """Pins the shape, not the instances: `_effort_inputs` must read exactly
        the fields the chain reads, so a future field is added in one place."""
        from kiro_crew.config.loader import KiroCrewAgentConfig as Crew
        from kiro_crew.dashboard.handlers.agents import _effort_inputs

        assert _effort_inputs(None) is None
        base = Crew(kiro_agent="kirocrew", reasoning_effort="high")
        assert _effort_inputs(base) == ("kirocrew", "high")
        # A junk stored level normalizes, so a no-op save of junk is not a change.
        assert _effort_inputs(Crew(kiro_agent="kirocrew", reasoning_effort="ultra")) == (
            "kirocrew",
            "",
        )
        # A field the chain does NOT read must not trigger a pool drain.
        assert _effort_inputs(
            Crew(kiro_agent="kirocrew", reasoning_effort="high", description="x")
        ) == _effort_inputs(base)

    @pytest.mark.asyncio
    async def test_a_failing_refresh_does_not_fail_the_save(self, seeded_agent):
        """The write is already durable, so the save must not report failure --
        a failed refresh costs one gateway lifetime of staleness, which is what
        the behaviour was before the call existed."""
        from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

        class _Sessions:
            async def refresh_defaults(self) -> None:
                raise RuntimeError("pool wedged")

        app = web.Application()
        app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
        app["state"] = SimpleNamespace(sessions=_Sessions())
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}", json={"reasoning_effort": "high"}
            )
            assert resp.status == 200

        assert KiroCrewConfig.load().agents[seeded_agent].reasoning_effort == "high"

    @pytest.mark.asyncio
    async def test_no_state_on_the_app_is_not_an_error(self, seeded_agent):
        """The handlers are mounted without dashboard state in tests and in the
        CLI-only paths; a missing session manager must be a no-op, not a 500."""
        from kiro_crew.dashboard.handlers import api_kirocrew_agent_update

        app = web.Application()
        app.router.add_put("/api/agents/{name}", api_kirocrew_agent_update)
        async with TestClient(TestServer(app)) as client:
            resp = await client.put(
                f"/api/agents/{seeded_agent}", json={"reasoning_effort": "high"}
            )
            assert resp.status == 200
