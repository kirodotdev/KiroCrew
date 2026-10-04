"""A crew's own ACP backend pin (``agents.<name>.acp_backend``).

The pin is a new FIRST input to the one backend-selection gate,
``members.select_provider_backend``: the crew's pin, then the member-DM route
(``agent.member_acp_backend``), then the configured default (``agent.acp_backend``).
It adds no gate of its own (harness-parity H3/H13), so what these tests hold is
that the existing gate still answers correctly with it:

(a) no pin (``None``) -- every crew that sets none -- leaves both routes
    exactly as they were, for a member DM slot and for a plain slot;
(b) a set pin wins over both routes, and ``""`` is a set pin (of kiro-cli), the
    blank rule ``agents.<name>.acp_backend`` documents: null inherits the session
    default, an empty string selects kiro-cli explicitly;
(c) a pin this build cannot select (unknown, or denied by policy) degrades to
    kiro-cli with the resolver's own warning, and does NOT fall through to a
    route, which would hide that the pin was refused;
(d) the pin round-trips through the loader RAW: ``None`` and ``""`` stay
    distinct through a load and a full-document save, an unselectable string
    survives the load so the gate can refuse it with its reason, a non-string
    collapses to inherit.

Then the places that must name the backend a session will actually get, each of
which would otherwise disagree with the gate: the provider factory, the warm
pool, the hot-reload trigger (including a deferred edit's retry), the
dashboard's slot readout, the crews API's model-pin check, and the member
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
#: The resolver's refusal line, naming the crew key the operator must fix rather
#: than the global one. Asserted rather than paraphrased.
_REFUSED = "Ignoring agents.<name>.acp_backend {!r} (not selectable in this build)"


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


def _cfg(*, member_backend: str = "kas", default: str = "", **pins: str | None) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agent.provider = "acp"
    cfg.agent.member_acp_backend = member_backend
    cfg.agent.acp_backend = default
    for name, pin in pins.items():
        cfg.agents[name] = KiroCrewAgentConfig(kiro_agent="kirocrew", acp_backend=pin)
    return cfg


class TestANullPinLeavesBothRoutes:
    def test_a_member_slot_keeps_the_member_route(self):
        assert select_provider_backend(MEMBER_KEY, "kas", "claude", crew_backend=None) == "kas"

    def test_a_plain_slot_keeps_the_configured_default(self):
        assert select_provider_backend(PLAIN_KEY, "kas", "claude", crew_backend=None) == "claude"

    def test_omitting_the_pin_is_no_pin(self):
        """Every caller that predates the pin passes three arguments."""
        assert select_provider_backend(MEMBER_KEY, "kas", "claude") == "kas"
        assert select_provider_backend(PLAIN_KEY, "kas", "claude") == "claude"


class TestBAPinWinsOverBothRoutes:
    def test_over_the_member_route(self):
        assert select_provider_backend(MEMBER_KEY, "kas", "", crew_backend="claude") == "claude"

    def test_over_the_configured_default(self):
        assert select_provider_backend(PLAIN_KEY, "kas", "codex", crew_backend="claude") == "claude"

    def test_an_empty_pin_keeps_a_member_on_kiro_over_a_non_kiro_member_route(self):
        """Truthiness would read ``""`` as no pin and route this DM to kas: the
        one value that keeps a crew on kiro-cli would be unexpressible."""
        assert select_provider_backend(MEMBER_KEY, "kas", "codex", crew_backend="") == ""

    def test_an_empty_pin_keeps_a_plain_session_on_kiro_over_a_non_kiro_default(self):
        assert select_provider_backend(PLAIN_KEY, "kas", "codex", crew_backend="") == ""


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

    def test_a_credential_shaped_pin_is_refused_without_logging_the_secret(self, caplog):
        """The pin is kept raw at load and the config view masks it, so the log
        lines the gate writes must not print it either."""
        caplog.set_level(logging.INFO)
        pin = "token=s3cr3t-value"
        assert select_provider_backend(PLAIN_KEY, "kas", "codex", crew_backend=pin) == ""
        assert "Ignoring agents.<name>.acp_backend" in caplog.text
        assert "s3cr3t-value" not in caplog.text

    def test_a_refused_member_route_names_its_own_key(self, caplog):
        """The member arm resolves agent.member_acp_backend, so its refusal must
        send the operator to that key, not the global one."""
        caplog.set_level(logging.WARNING)
        assert select_provider_backend(MEMBER_KEY, "nope", "codex") == ""
        assert "Ignoring agent.member_acp_backend 'nope'" in caplog.text
        assert "running on kiro-cli instead" in caplog.text

    def test_redaction_never_alters_a_valid_backend_id(self):
        """The gate resolves a redacted copy of the pin, which is only safe if
        no selectable id is something the redactor would rewrite."""
        from kiro_crew.external_text import redact_external_text

        for backend in {*acp_backends.selectable_backends(), "", "kas", "claude"}:
            assert redact_external_text(backend) == backend

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
        assert KiroCrewConfig.load().agents["reviewer"].acp_backend is None

    @pytest.mark.parametrize("pin", [None, ""], ids=["null-inherits", "empty-pins-kiro"])
    def test_null_and_empty_stay_distinct_through_load(self, config_file, pin):
        self._write_raw(config_file, {"kiro_agent": "kirocrew", "acp_backend": pin})
        loaded = KiroCrewConfig.load()
        assert loaded.agents["reviewer"].acp_backend == pin
        assert loaded.crew_acp_backend(None, "reviewer") == pin

    @pytest.mark.parametrize("pin", [None, ""], ids=["null-inherits", "empty-pins-kiro"])
    def test_null_and_empty_stay_distinct_through_a_full_document_save(self, config_file, pin):
        """A full-document save writes every crew record whole, so with a ``""``
        default it would pin every crew that sets nothing to kiro-cli."""
        self._write_raw(config_file, {"kiro_agent": "kirocrew", "acp_backend": pin})
        KiroCrewConfig.load().save()

        saved = json.loads(config_file.read_text(encoding="utf-8"))
        assert saved["agents"]["reviewer"]["acp_backend"] == pin
        assert KiroCrewConfig.load().agents["reviewer"].acp_backend == pin

    def test_a_crew_added_in_memory_saves_as_null(self, config_file):
        cfg = KiroCrewConfig.load()
        cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
        saved = json.loads(config_file.read_text(encoding="utf-8"))
        assert saved["agents"]["reviewer"]["acp_backend"] is None

    def test_an_unselectable_string_is_kept_for_the_gate_to_refuse(self, config_file):
        """Normalizing at load would turn a refused pin into ``""`` -- a kiro-cli
        pin -- and the refusal would never be logged."""
        self._write_raw(config_file, {"kiro_agent": "kirocrew", "acp_backend": "nope"})
        loaded = KiroCrewConfig.load()
        assert loaded.agents["reviewer"].acp_backend == "nope"
        assert loaded.crew_acp_backend(None, "reviewer") == "nope"

    @pytest.mark.parametrize("junk", [7, ["kas"], {"kas": True}, False])
    def test_a_non_string_loads_as_inherit(self, config_file, junk):
        """Inherit, never ``""``: collapsing junk to the empty string would pin
        the crew to kiro-cli on a value nobody chose."""
        self._write_raw(config_file, {"kiro_agent": "kirocrew", "acp_backend": junk})
        assert KiroCrewConfig.load().agents["reviewer"].acp_backend is None


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
        assert _cfg(reviewer="claude").crew_acp_backend("reviewer", "") is None

    def test_an_unknown_crew_pins_nothing(self):
        assert _cfg(reviewer="claude").crew_acp_backend(None, "ghost") is None


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
        cfg = _cfg(default="codex", reviewer=None)
        assert self._backend(cfg, session_key=MEMBER_KEY, crew_agent="reviewer") == "kas"
        assert self._backend(cfg, session_key="cron:nightly", crew_agent="reviewer") == "codex"

    def test_an_empty_pin_runs_the_crew_on_kiro_over_both_routes(self):
        cfg = _cfg(default="codex", reviewer="")
        assert self._backend(cfg, session_key=MEMBER_KEY, crew_agent="reviewer") == ""
        assert self._backend(cfg, session_key="cron:nightly", crew_agent="reviewer") == ""

    def test_a_pooled_child_is_built_on_the_default_not_the_pool_agents_pin(self):
        """Pinned-crew sessions bypass the pool, so its children are claimed only
        by no-crew or unpinned sessions; one built on the pool agent's pin would
        hand those sessions a harness nobody routed them to."""
        cfg = _cfg(default="codex", reviewer="claude")
        assert self._backend(cfg, session_key="", agent="reviewer", pooled=True) == "codex"
        assert self._backend(cfg, session_key="", agent="reviewer") == "claude"

    def test_a_pooled_child_still_carries_the_pool_agents_effort(self):
        """Only the backend pin is dropped: the child is still built as the pool
        agent's crew, so its effort resolves for that crew as before the pin."""
        cfg = _cfg(default="codex", reviewer="claude")
        cfg.agents["reviewer"].reasoning_effort = "high"
        seen: list[tuple] = []
        real = cfg.resolve_session_effort

        def _spy(agent, crew_agent=None):
            seen.append((agent, crew_agent))
            return real(agent, crew_agent)

        cfg.resolve_session_effort = _spy  # type: ignore[method-assign]
        self._backend(cfg, session_key="", agent="reviewer", pooled=True)
        assert seen == [("reviewer", "reviewer")]
        assert real("reviewer", "reviewer") == "high"

    def test_the_pool_fill_marks_its_children_pooled(self):
        """Structural, like the pool-decision check: the fill runs only inside a
        live pool loop, and dropping the flag is a silent wrong-harness claim."""
        import inspect

        from kiro_crew import session_pool

        src = inspect.getsource(session_pool.WarmSessionPool._fill_warm_pool_loop)
        assert "pooled=True" in src

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
    async def test_true_for_an_empty_pin(self):
        """A pooled child runs on the default backend, which need not be kiro-cli."""
        assert await self._service(_cfg(reviewer=""))._crew_pins_backend(None, "reviewer")

    @pytest.mark.asyncio
    async def test_false_for_an_unpinned_crew(self):
        service = self._service(_cfg(reviewer=None))
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
    async def test_an_unreadable_config_cold_starts(self):
        """The factory may still hold a pin this read cannot see; a pooled child
        on the default backend would then take the pinned crew's prompt."""
        from kiro_crew.session_allocation import SessionAllocationService

        service = SessionAllocationService.__new__(SessionAllocationService)
        service._deps = MagicMock()

        def _boom():
            raise OSError("config unreadable")

        service._deps.load_config = _boom
        assert await service._crew_pins_backend(None, "reviewer") is True

    @pytest.mark.asyncio
    async def test_a_torn_config_read_cold_starts(self):
        """A torn read is filled with defaults, so it shows no pin at all."""
        from dataclasses import replace

        from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

        torn = replace(_cfg(), _degraded_sections=frozenset({DEGRADED_WHOLE_CONFIG}))
        assert await self._service(torn)._crew_pins_backend(None, "reviewer") is True

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
    must rebuild it -- and an edit that touches no pin must not drain the pool."""

    def _manager(self, in_force: KiroCrewConfig):
        from kiro_crew.session import SessionManager

        mgr = SessionManager.__new__(SessionManager)
        mgr._cfg = in_force
        return mgr

    def _changed(self, old: dict, new: dict, *changed: str) -> bool:
        change = ConfigChange(old=_cfg(**old), new=_cfg(**new), changed=frozenset(changed))
        return self._manager(change.old)._crew_backend_pin_changed(change)

    @pytest.mark.parametrize(
        ("old", "new", "changed"),
        [
            ({"reviewer": None}, {"reviewer": "claude"}, ("agents.reviewer.acp_backend",)),
            ({"reviewer": "claude"}, {"reviewer": None}, ("agents.reviewer.acp_backend",)),
            ({"reviewer": None}, {"reviewer": ""}, ("agents.reviewer.acp_backend",)),
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
            ({}, {"reviewer": ""}, ("agents.reviewer.kiro_agent", "agents.reviewer.acp_backend")),
            # The named cost of the path rule: a crew that pins nothing still
            # moves its acp_backend leaf when its record appears.
            ({}, {"reviewer": None}, ("agents.reviewer.kiro_agent", "agents.reviewer.acp_backend")),
        ],
        ids=[
            "set",
            "cleared",
            "pinned-to-kiro",
            "pinned-crew-added",
            "pinned-crew-removed",
            "kiro-pinned-crew-added",
            "unpinned-crew-added",
        ],
    )
    def test_a_pin_leaf_in_the_change_is_a_factory_input(self, old, new, changed):
        assert self._changed(old, new, *changed) is True

    @pytest.mark.parametrize(
        ("old", "new", "changed"),
        [
            ({"reviewer": "claude"}, {"reviewer": "claude"}, ("agents.reviewer.description",)),
            ({"reviewer": None}, {"reviewer": "claude"}, ("agent.model",)),
        ],
        ids=["unrelated-crew-edit", "no-agents-path"],
    )
    def test_an_edit_that_touches_no_pin_is_not(self, old, new, changed):
        assert self._changed(old, new, *changed) is False

    @pytest.mark.parametrize(
        "changed",
        ["agents.reviewer.acp_backend", "agents.reviewer", "agents"],
        ids=["leaf", "record", "section"],
    )
    def test_a_retry_whose_old_is_new_still_sees_the_pin(self, changed):
        """The watcher's retry builds ``ConfigChange(old=cfg, new=cfg)``, and a
        bind-time replay delivers bare section prefixes, so neither ``change.old``
        nor the path's spelling can carry the edit. The manager's in-force
        snapshot does."""
        pinned = _cfg(reviewer="claude")
        retry = ConfigChange(old=pinned, new=pinned, changed=frozenset({changed}))
        assert self._manager(_cfg(reviewer=None))._crew_backend_pin_changed(retry) is True

    @pytest.mark.parametrize("fold_in", [False, True], ids=["retry", "fold-in"])
    def test_a_redelivered_pin_leaf_refreshes_even_when_the_pin_is_in_force(self, fold_in):
        """``refresh_defaults`` adopts the config before it restarts the pool, so
        a refresh that failed after adoption has the pin in force and is still
        unfinished, and its redelivery's ``old`` already carries the pin."""
        pinned = _cfg(reviewer="claude")
        redelivery = ConfigChange(
            old=_cfg(reviewer="claude") if fold_in else pinned,
            new=pinned,
            changed=frozenset({"agents.reviewer.acp_backend", "agents.reviewer.description"}),
        )
        assert self._manager(pinned)._crew_backend_pin_changed(redelivery) is True

    @pytest.mark.parametrize(
        ("leaf", "fires"),
        [("agents.reviewer.reasoning_effort", True), ("agents.reviewer.description", False)],
        ids=["reasoning-effort", "unrelated-leaf"],
    )
    def test_a_retried_effort_leaf_is_the_twin_factory_input(self, leaf, fires):
        """``reasoning_effort`` is the other per-crew value the factory reads off
        the config it captured, so its retry must rebuild the factory the same
        way; an unrelated leaf on the same retry must not."""
        cfg = _cfg(reviewer=None)
        cfg.agents["reviewer"].reasoning_effort = "high"
        retry = ConfigChange(old=cfg, new=cfg, changed=frozenset({leaf}))
        assert self._manager(cfg)._crew_backend_pin_changed(retry) is fires

    @pytest.mark.parametrize("changed", ["agents.reviewer", "agents"], ids=["record", "section"])
    def test_a_replay_of_a_pin_already_in_force_rebuilds_nothing(self, changed):
        """A bind-time replay delivers section prefixes for a config the manager
        may already run; with no pin leaf named, only a real difference counts."""
        pinned = _cfg(reviewer="claude")
        replay = ConfigChange(old=pinned, new=pinned, changed=frozenset({changed}))
        assert self._manager(pinned)._crew_backend_pin_changed(replay) is False

    @pytest.mark.asyncio
    async def test_the_applier_refreshes_on_a_pin_change(self):
        from kiro_crew.session import SessionManager

        mgr = self._manager(_cfg(reviewer=None))
        mgr.refresh_defaults = AsyncMock()  # type: ignore[method-assign]
        change = ConfigChange(
            old=_cfg(reviewer=None),
            new=_cfg(reviewer="claude"),
            changed=frozenset({"agents.reviewer.acp_backend"}),
        )
        await SessionManager._on_config_change(mgr, change)
        mgr.refresh_defaults.assert_awaited_once_with(cfg=change.new)


class TestADeferredPinEditIsRetriedThroughTheWatcher:
    """The maintainer's case, end to end through ``ConfigWatch``: the first
    delivery of a pin edit fails, and the watcher redelivers it with an ``old``
    that already carries the pin -- by retry (``old is new``) or folded into the
    next file change. The redelivery must still rebuild the factory, not wait
    for a restart."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("adopted", [False, True], ids=["before-adoption", "after-adoption"])
    @pytest.mark.parametrize("redelivery", ["retry", "fold-in"])
    async def test_the_redelivery_rebuilds_the_factory(self, tmp_path, redelivery, adopted):
        """``after-adoption`` fails the way a pool restart inside
        ``refresh_defaults`` would: the config and factory are swapped in, the
        refresh is not finished, and only the redelivered path still says so."""
        from _hot_reload_helpers import write_config

        from kiro_crew.config import live
        from kiro_crew.config.live import ConfigWatch
        from kiro_crew.session import SessionManager

        cfg_file = tmp_path / "config.json"
        doc = {
            "session": {"pool_size": 0},
            "agents": {"reviewer": {"kiro_agent": "kirocrew"}},
        }
        write_config(cfg_file, doc)
        with (
            patch("kiro_crew.config.loader.config_path", return_value=cfg_file),
            patch("kiro_crew.config.loader.config_local_path", return_value=tmp_path / "l.json"),
            patch("kiro_crew.config.live._WATCH", ConfigWatch(poll_interval_secs=0.05)),
            patch("kiro_crew.session.default_project_dir", return_value="/ws"),
        ):
            watch = live.watch()
            boot = KiroCrewConfig.load()
            mgr = SessionManager(boot, provider_factory=MagicMock())
            watch.prime(boot)
            pins: list[str | None] = []

            async def _refresh(cfg):
                pins.append(cfg.agents["reviewer"].acp_backend)
                if len(pins) == 1:
                    if adopted:
                        mgr._cfg = cfg
                    raise RuntimeError("refresh failed")
                mgr._cfg = cfg

            mgr.refresh_defaults = _refresh  # type: ignore[method-assign]

            doc["agents"]["reviewer"]["acp_backend"] = "claude"
            write_config(cfg_file, doc)
            await watch.refresh_now()
            assert pins == ["claude"], "the first delivery must have reached the factory"

            if redelivery == "fold-in":
                doc["agents"]["reviewer"]["description"] = "an unrelated edit"
                write_config(cfg_file, doc)
            await watch.refresh_now()

        assert pins == ["claude", "claude"]
        assert mgr._cfg.agents["reviewer"].acp_backend == "claude"


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
        assert self._readout(monkeypatch, bindings) is None

    def test_an_unresolvable_selection_reads_the_pre_pin_route(self, monkeypatch):
        """``None``, not ``""``: an empty answer would read as a kiro-cli pin."""
        assert self._readout(monkeypatch, LookupError("selection unavailable")) is None

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

        assert _member_backend_can_dispatch(_cfg(member_backend="kas", reviewer=None), "reviewer")

    def test_an_empty_pin_reads_kiro_and_withholds_the_block(self):
        """kiro-cli v2 exposes no per-session channel, so a member pinned to it
        with ``""`` runs as plain chat even under a dispatch-capable route."""
        from kiro_crew.context import _member_backend_can_dispatch

        cfg = _cfg(member_backend="kas", reviewer="")
        assert _member_backend_can_dispatch(cfg, "reviewer") is False

    def test_a_pin_outside_the_dispatch_set_withholds_the_block(self):
        from kiro_crew.context import _member_backend_can_dispatch

        assert "deepseek" not in acp_backends.ACP_BACKENDS_MEMBER_DISPATCH
        cfg = _cfg(member_backend="kas", reviewer="deepseek")
        assert _member_backend_can_dispatch(cfg, "reviewer") is False

    def test_a_credential_shaped_pin_is_not_logged(self, caplog):
        """Same rule as the gate: the resolver logs a refused pin, and the pin is
        raw agent-written text."""
        from kiro_crew.context import _member_backend_can_dispatch

        caplog.set_level(logging.DEBUG)
        cfg = _cfg(member_backend="kas", reviewer="token=s3cr3t-value")
        assert _member_backend_can_dispatch(cfg, "reviewer") is False
        assert "Ignoring agents.reviewer.acp_backend" in caplog.text
        assert "s3cr3t-value" not in caplog.text

    def test_a_pin_inside_the_dispatch_set_grants_it(self):
        from kiro_crew.context import _member_backend_can_dispatch

        cfg = _cfg(member_backend="", reviewer="claude")
        assert _member_backend_can_dispatch(cfg, "reviewer") is True
