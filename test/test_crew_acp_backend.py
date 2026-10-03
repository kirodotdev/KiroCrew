"""A crew's own ACP backend pin (``agents.<name>.acp_backend``).

The pin is a new FIRST input to the one backend-selection gate,
``members.select_provider_backend``: the crew's pin, then the member-DM route
(``agent.member_acp_backend``), then the configured default (``agent.acp_backend``).
It adds no gate of its own (harness-parity H3/H13), so what these tests hold is
that the existing gate still answers correctly with it:

(a) a blank pin -- every crew that sets none -- leaves both routes exactly as
    they were, for a member DM slot and for a plain slot;
(b) a set pin wins over both routes;
(c) a pin this build cannot select (unknown, or denied by policy) degrades to
    kiro-cli with the resolver's own warning, and does NOT fall through to a
    route, which would hide that the pin was refused;
(d) the pin round-trips through the loader RAW: an unselectable string survives
    the load so the gate can refuse it with its reason, a non-string collapses
    to inherit.

Then the places that must name the backend a session will actually get, each of
which would otherwise disagree with the gate: the provider factory, the warm
pool, the hot-reload trigger, the dashboard's slot readout, and the member
operating-mode block.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.agent_sdk import backends as acp_backends
from kiro_crew.config.live import ConfigChange
from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.members import select_provider_backend

#: The canonical session-map alias of a member DM slot -- what reaches the factory.
MEMBER_KEY = "dashboard:member-reviewer"
PLAIN_KEY = "dashboard:abc"
#: The resolver's refusal line. Asserted rather than paraphrased: (c) is about the
#: EXISTING warning reaching the operator, not about some new message.
_REFUSED = "Ignoring agent.acp_backend {!r} (not selectable in this build)"


@pytest.fixture(autouse=True)
def _restore_selectable():
    """The denial tests narrow the process-wide registry; put it back after each test."""
    baseline = set(acp_backends._baseline)
    selectable = set(acp_backends._selectable)
    yield
    acp_backends._baseline.clear()
    acp_backends._baseline.update(baseline)
    acp_backends._selectable.clear()
    acp_backends._selectable.update(selectable)


def _cfg(*, member_backend: str = "kas", default: str = "", **pins: str) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agent.provider = "acp"
    cfg.agent.member_acp_backend = member_backend
    cfg.agent.acp_backend = default
    for name, pin in pins.items():
        cfg.agents[name] = KiroCrewAgentConfig(kiro_agent="kirocrew", acp_backend=pin)
    return cfg


class TestABlankPinLeavesBothRoutes:
    def test_a_member_slot_keeps_the_member_route(self):
        assert select_provider_backend(MEMBER_KEY, "kas", "claude", crew_backend="") == "kas"

    def test_a_plain_slot_keeps_the_configured_default(self):
        assert select_provider_backend(PLAIN_KEY, "kas", "claude", crew_backend="") == "claude"

    def test_omitting_the_pin_is_the_blank_pin(self):
        """Every caller that predates the pin passes three arguments."""
        assert select_provider_backend(MEMBER_KEY, "kas", "claude") == "kas"
        assert select_provider_backend(PLAIN_KEY, "kas", "claude") == "claude"


class TestBAPinWinsOverBothRoutes:
    def test_over_the_member_route(self):
        assert select_provider_backend(MEMBER_KEY, "kas", "", crew_backend="claude") == "claude"

    def test_over_the_configured_default(self):
        assert select_provider_backend(PLAIN_KEY, "kas", "codex", crew_backend="claude") == "claude"


class TestCARefusedPinDegradesToKiro:
    def test_an_unknown_pin(self, caplog):
        caplog.set_level(logging.WARNING)
        assert select_provider_backend(PLAIN_KEY, "kas", "codex", crew_backend="nope") == ""
        assert _REFUSED.format("nope") in caplog.text

    def test_a_policy_denied_pin(self, caplog):
        """Denial narrows the registry the resolver reads, so a pin that was
        selectable a moment ago is refused the same way an unknown one is."""
        acp_backends.apply_selectable_denials({"claude"})
        caplog.set_level(logging.WARNING)
        assert select_provider_backend(PLAIN_KEY, "kas", "codex", crew_backend="claude") == ""
        assert _REFUSED.format("claude") in caplog.text

    def test_a_refused_pin_does_not_fall_through_to_the_member_route(self):
        """H3: an unselectable persisted backend degrades to kiro WITH a logged
        reason. Running the crew on the member route instead would look like a
        working pin to anyone reading which engine the thread is on."""
        assert select_provider_backend(MEMBER_KEY, "kas", "", crew_backend="nope") == ""


class TestDThePinRoundTripsThroughTheLoader:
    @pytest.fixture
    def config_file(self, tmp_path, monkeypatch):
        from kiro_crew.config import loader as loader_module

        path = tmp_path / "config.json"
        monkeypatch.setattr(loader_module, "config_path", lambda: path)
        return path

    def _write_raw(self, path, record: dict) -> None:
        """Write the crew the way a human editing ``config.json`` would."""
        path.write_text(json.dumps({"agents": {"reviewer": record}}), encoding="utf-8")

    def test_a_selectable_pin_round_trips_through_save_and_load(self, config_file):
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew", acp_backend="claude")
        cfg.save()

        saved = json.loads(config_file.read_text(encoding="utf-8"))
        assert saved["agents"]["reviewer"]["acp_backend"] == "claude"
        assert KiroCrewConfig.load().agents["reviewer"].acp_backend == "claude"

    def test_a_record_written_before_the_field_existed_loads_as_inherit(self, config_file):
        self._write_raw(config_file, {"kiro_agent": "kirocrew"})
        assert KiroCrewConfig.load().agents["reviewer"].acp_backend == ""

    def test_an_unselectable_string_is_kept_for_the_gate_to_refuse(self, config_file):
        """Normalizing at load would turn a refused pin into ``""`` -- inherit --
        and the crew would quietly run on a route with nothing logged."""
        self._write_raw(config_file, {"kiro_agent": "kirocrew", "acp_backend": "nope"})
        loaded = KiroCrewConfig.load()
        assert loaded.agents["reviewer"].acp_backend == "nope"
        assert loaded.crew_acp_backend(None, "reviewer") == "nope"

    @pytest.mark.parametrize("junk", [7, ["kas"], {"kas": True}, None])
    def test_a_non_string_loads_as_inherit(self, config_file, junk):
        self._write_raw(config_file, {"kiro_agent": "kirocrew", "acp_backend": junk})
        assert KiroCrewConfig.load().agents["reviewer"].acp_backend == ""


class TestCrewAcpBackend:
    """Keyed on resolve_crew_identity, exactly like the effort pin."""

    def test_reads_the_crew_named_by_crew_agent(self):
        assert _cfg(reviewer="claude").crew_acp_backend(None, "reviewer") == "claude"

    def test_reads_a_crew_named_by_agent_alone(self):
        """Slack threads, cron jobs and spawned agents pass a CREW name as
        ``agent`` with no ``crew_agent``; those unattended sessions get the pin too."""
        assert _cfg(reviewer="claude").crew_acp_backend("reviewer") == "claude"

    def test_an_empty_crew_agent_means_no_crew(self):
        """A template-bound session passes ``""``, so a template name that matches
        a crew key does not borrow that crew's engine."""
        assert _cfg(reviewer="claude").crew_acp_backend("reviewer", "") == ""

    def test_an_unknown_crew_pins_nothing(self):
        assert _cfg(reviewer="claude").crew_acp_backend(None, "ghost") == ""


class TestTheFactoryRoutesThePin:
    """The factory is the one construction path, so the pin must reach it there."""

    def _backend(self, cfg: KiroCrewConfig, **call) -> str:
        with patch("kiro_crew.providers.acp.AcpProvider") as mock_provider:
            mock_provider.return_value = MagicMock()
            cfg.create_provider_factory()(**call)
            assert mock_provider.called, "factory did not construct AcpProvider"
            return mock_provider.call_args.kwargs["acp_backend"]

    def test_a_scheduled_turn_runs_on_its_crews_pin(self):
        cfg = _cfg(reviewer="claude")
        assert self._backend(cfg, session_key="cron:nightly", crew_agent="reviewer") == "claude"

    def test_a_member_dm_runs_on_its_crews_pin(self):
        cfg = _cfg(reviewer="claude")
        assert self._backend(cfg, session_key=MEMBER_KEY, crew_agent="reviewer") == "claude"

    def test_an_unpinned_crew_keeps_both_routes(self):
        cfg = _cfg(default="codex", reviewer="")
        assert self._backend(cfg, session_key=MEMBER_KEY, crew_agent="reviewer") == "kas"
        assert self._backend(cfg, session_key="cron:nightly", crew_agent="reviewer") == "codex"

    def test_a_template_session_ignores_a_same_named_crews_pin(self):
        cfg = _cfg(**{"kirocrew-worker": "claude"})
        backend = self._backend(cfg, session_key=PLAIN_KEY, agent="kirocrew-worker", crew_agent="")
        assert backend == ""


class TestWarmPoolBypassesAPinnedCrew:
    """A pooled child was spawned on the DEFAULT backend with no crew, so a warm
    hit would run a pinned crew on an engine it did not pin."""

    def _service(self, cfg: KiroCrewConfig):
        from kiro_crew.session_allocation import SessionAllocationService

        service = SessionAllocationService.__new__(SessionAllocationService)
        service._deps = MagicMock()
        service._deps.load_config = lambda: cfg
        return service

    @pytest.mark.asyncio
    async def test_true_for_a_pinned_crew(self):
        assert await self._service(_cfg(reviewer="claude"))._crew_pins_backend(None, "reviewer")

    @pytest.mark.asyncio
    async def test_true_for_a_pin_the_gate_will_refuse(self):
        """The refusal is logged and degraded by the factory's gate, so a refused
        pin must reach the factory too rather than be served from the pool."""
        assert await self._service(_cfg(reviewer="nope"))._crew_pins_backend(None, "reviewer")

    @pytest.mark.asyncio
    async def test_false_for_an_unpinned_crew(self):
        service = self._service(_cfg(reviewer=""))
        assert await service._crew_pins_backend(None, "reviewer") is False

    @pytest.mark.asyncio
    async def test_false_when_no_crew_resolves(self):
        service = self._service(_cfg(reviewer="claude"))
        assert await service._crew_pins_backend("reviewer", "") is False

    @pytest.mark.asyncio
    async def test_a_non_string_crew_agent_is_not_passed_through(self):
        service = self._service(_cfg(reviewer="claude"))
        assert await service._crew_pins_backend(None, object()) is False

    @pytest.mark.asyncio
    async def test_an_unreadable_config_pools_as_before(self):
        from kiro_crew.session_allocation import SessionAllocationService

        service = SessionAllocationService.__new__(SessionAllocationService)
        service._deps = MagicMock()

        def _boom():
            raise OSError("config unreadable")

        service._deps.load_config = _boom
        assert await service._crew_pins_backend(None, "reviewer") is False

    def test_the_pool_decision_consults_the_probe(self):
        """Structural, like the effort arm's: the decision is unreachable from a
        unit test without a real pool, and its absence is a silent no-op."""
        import inspect

        from kiro_crew import session_allocation

        src = inspect.getsource(session_allocation.SessionAllocationService._get_or_create_impl)
        assert "_crew_pins_backend" in src
        assert 'pool_decision = "bypass_backend"' in src


class TestAPinEditRebuildsTheFactory:
    """The factory reads the pin off the config it captured, so an edit to a pin
    must rebuild it -- and an edit that changes no pin must not drain the pool."""

    def _change(self, old: dict[str, str], new: dict[str, str], *changed: str) -> ConfigChange:
        return ConfigChange(old=_cfg(**old), new=_cfg(**new), changed=frozenset(changed))

    @pytest.mark.parametrize(
        ("old", "new", "changed"),
        [
            ({"reviewer": ""}, {"reviewer": "claude"}, ("agents.reviewer.acp_backend",)),
            ({"reviewer": "claude"}, {"reviewer": ""}, ("agents.reviewer.acp_backend",)),
            (
                {},
                {"reviewer": "claude"},
                ("agents.reviewer.kiro_agent", "agents.reviewer.acp_backend"),
            ),
            (
                {"reviewer": "claude"},
                {},
                ("agents.reviewer.kiro_agent", "agents.reviewer.acp_backend"),
            ),
        ],
        ids=["set", "cleared", "pinned-crew-added", "pinned-crew-removed"],
    )
    def test_a_pin_that_changes_value_is_a_factory_input(self, old, new, changed):
        from kiro_crew.session import SessionManager

        assert SessionManager._crew_backend_pin_changed(self._change(old, new, *changed)) is True

    @pytest.mark.parametrize(
        ("old", "new", "changed"),
        [
            ({}, {"reviewer": ""}, ("agents.reviewer.kiro_agent", "agents.reviewer.acp_backend")),
            ({"reviewer": ""}, {"reviewer": ""}, ("agents.reviewer.acp_backend",)),
            ({"reviewer": "claude"}, {"reviewer": "claude"}, ("agents.reviewer.description",)),
        ],
        ids=["unpinned-crew-added", "default-written-out", "unrelated-crew-edit"],
    )
    def test_an_edit_that_changes_no_pin_is_not(self, old, new, changed):
        from kiro_crew.session import SessionManager

        assert SessionManager._crew_backend_pin_changed(self._change(old, new, *changed)) is False

    @pytest.mark.asyncio
    async def test_the_applier_refreshes_on_a_pin_change(self):
        from kiro_crew.session import SessionManager

        mgr = SessionManager.__new__(SessionManager)
        mgr.refresh_defaults = AsyncMock()  # type: ignore[method-assign]
        change = self._change(
            {"reviewer": ""}, {"reviewer": "claude"}, "agents.reviewer.acp_backend"
        )
        await SessionManager._on_config_change(mgr, change)
        mgr.refresh_defaults.assert_awaited_once_with(cfg=change.new)


class TestTheSlotReadoutNamesTheFactorysCrew:
    """The dashboard's readers of the gate (cold-slot capabilities, set_model)
    derive the crew the way the turn path hands it to the factory."""

    def _readout(self, monkeypatch, bindings) -> str:
        from kiro_crew import session_agent_selection as sas
        from kiro_crew.dashboard import chat_utils

        def _resolve(*_a, **_k):
            if isinstance(bindings, Exception):
                raise bindings
            return bindings

        monkeypatch.setattr(sas, "resolve_session_agent_bindings", _resolve)
        return chat_utils.session_crew_acp_backend(_cfg(reviewer="claude"), MEMBER_KEY, "reviewer")

    def test_a_crew_selection_reads_that_crews_pin(self, monkeypatch):
        bindings = SimpleNamespace(selection_kind="member", resolved_alias="reviewer")
        assert self._readout(monkeypatch, bindings) == "claude"

    def test_a_template_selection_runs_as_no_crew(self, monkeypatch):
        """Its fallback alias supplies defaults, not an identity -- the turn path
        hands the factory ``crew_agent=""`` for it."""
        bindings = SimpleNamespace(selection_kind="template", resolved_alias="reviewer")
        assert self._readout(monkeypatch, bindings) == ""

    def test_an_unresolvable_selection_reads_the_pre_pin_route(self, monkeypatch):
        assert self._readout(monkeypatch, LookupError("selection unavailable")) == ""

    @pytest.mark.asyncio
    async def test_the_cold_slot_readout_hands_the_pin_to_the_gate(self, monkeypatch):
        from kiro_crew.dashboard import chat_handlers

        monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=_cfg))
        monkeypatch.setattr(chat_handlers, "session_crew_acp_backend", lambda *_a: "claude")
        slot = SimpleNamespace(
            key="member-reviewer", linked_session_key="", agent="reviewer", project=""
        )
        assert await chat_handlers._configured_backend_for_slot(slot) == "claude"


class TestTheMemberBlockFollowsThePin:
    """The member operating-mode block teaches dispatch tools that only a
    dispatch-capable engine mounts, so it must ask about the engine the member
    will actually run on."""

    def test_an_unpinned_member_reads_the_member_route(self):
        from kiro_crew.context import _member_backend_can_dispatch

        assert _member_backend_can_dispatch(_cfg(member_backend="kas", reviewer=""), "reviewer")

    def test_a_pin_outside_the_dispatch_set_withholds_the_block(self):
        from kiro_crew.context import _member_backend_can_dispatch

        assert "deepseek" not in acp_backends.ACP_BACKENDS_MEMBER_DISPATCH
        cfg = _cfg(member_backend="kas", reviewer="deepseek")
        assert _member_backend_can_dispatch(cfg, "reviewer") is False

    def test_a_pin_inside_the_dispatch_set_grants_it(self):
        from kiro_crew.context import _member_backend_can_dispatch

        cfg = _cfg(member_backend="", reviewer="claude")
        assert _member_backend_can_dispatch(cfg, "reviewer") is True
