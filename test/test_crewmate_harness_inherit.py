"""What a Crew bot with no pin of its own runs on, and where that comes from.

Resolution, for a Crew bot's own chat and the workers it opens:

1. the Crew bot's pin ``agents.<name>.acp_backend`` (refuses when it cannot start, H17);
2. an explicitly stored ``agent.member_acp_backend`` (legacy, keeps working);
3. ``agent.acp_backend`` (Settings -> Agent Harness), Kiro CLI reading as KAS;
4. KAS, with a logged reason (H3) and a bell note, when Crew bots cannot run on 3.

The answer is the loaded ``agent.member_acp_backend`` (``_read_backend_choice``), so
every reader of the Crew bot backend gets the same one; the governance gate and the
enterprise KAS floor still decide what is selectable.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from kiro_crew import acp_backends
from kiro_crew.agent_sdk import backends as registry
from kiro_crew.config import loader_builders
from kiro_crew.config.loader_builders import _build_agent_config

#: Every harness the tests choose between, Kiro CLI (``""``) included.
SELECTABLE = {"", "kas", "claude", "codex", "goose", "opencode", "strands", "pi", "deepseek"}


@pytest.fixture(autouse=True)
def _registry(_floor_monkeypatch):
    """A fixed selectable set, and a fresh once-per-process log for each test."""
    _floor_monkeypatch.setattr(registry, "_baseline", set(SELECTABLE))
    _floor_monkeypatch.setattr(registry, "_selectable", set(SELECTABLE))
    _floor_monkeypatch.setattr(loader_builders, "_member_fallback_logged", set())
    from kiro_crew import crewmate_harness

    _floor_monkeypatch.setattr(crewmate_harness, "_fallback_logged", set())
    _floor_monkeypatch.setattr(crewmate_harness, "_VERDICTS", {})
    _floor_monkeypatch.setattr(crewmate_harness, "_REFRESHING", set())
    # An installed claude adapter at or above the session-options floor.
    _floor_monkeypatch.setattr(
        "kiro_crew.agent_sdk.drivers.acp.claude_adapter_honours_session_options", lambda: True
    )


def _member(agent: dict) -> str:
    return _build_agent_config(agent).member_acp_backend


class TestResolver:
    def test_crewmates_follow_a_settings_harness_they_can_run_on(self):
        from kiro_crew.agent_sdk.backends import crewmate_inherited_backend

        for harness in ("claude", "codex", "kas"):
            assert crewmate_inherited_backend(harness) == (harness, False)

    def test_kiro_cli_reads_as_kas_and_is_not_a_fallback(self):
        from kiro_crew.agent_sdk.backends import crewmate_inherited_backend

        assert crewmate_inherited_backend(acp_backends.ACP_BACKEND_KIRO) == ("kas", False)

    @pytest.mark.parametrize(
        "harness",
        [
            "goose",
            "opencode",
            "strands",
            "copilot",
            # A Crew bot chat could run here, but its workers could not carry their
            # kit (``worker_kit_backend_refusal``), so inheriting it would refuse
            # every worker the Crew bot opens.
            "pi",
            "deepseek",
        ],
    )
    def test_a_harness_crewmates_cannot_run_on_falls_back_to_kas(self, harness):
        from kiro_crew.agent_sdk.backends import crewmate_inherited_backend

        assert crewmate_inherited_backend(harness) == ("kas", True)


class TestLoadedField:
    def test_nothing_set_is_kas(self):
        assert _member({}) == "kas"

    @pytest.mark.parametrize("unset", [None, ""])
    def test_an_unset_member_field_follows_the_settings_harness(self, unset):
        agent = {"acp_backend": "claude"}
        if unset is not None:
            agent["member_acp_backend"] = unset
        assert _member(agent) == "claude"

    def test_a_kiro_cli_settings_harness_gives_crewmates_kas(self):
        assert _member({"acp_backend": "kiro"}) == "kas"

    def test_a_kiro_cli_settings_harness_on_the_bundle_route_gives_crewmates_kas(self):
        cfg = _build_agent_config({"acp_backend": "kiro", "kas_bundle": True})
        assert cfg.acp_backend == acp_backends.ACP_BACKEND_KIRO
        assert cfg.member_acp_backend == "kas"

    @pytest.mark.parametrize(
        "harness", ["claude", "kas", "kiro", "codex", "pi", "deepseek", "goose"]
    )
    def test_an_inherited_harness_never_refuses_a_crewmates_worker(self, harness):
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.mate_worker import crewmate_worker_backend, worker_kit_backend_refusal

        cfg = KiroCrewConfig()
        cfg.agent = _build_agent_config({"acp_backend": harness})
        assert worker_kit_backend_refusal(crewmate_worker_backend(cfg)) is None

    def test_an_explicit_legacy_value_wins_over_the_settings_harness(self):
        assert _member({"acp_backend": "claude", "member_acp_backend": "codex"}) == "codex"
        assert _member({"acp_backend": "claude", "member_acp_backend": "kas"}) == "kas"

    def test_a_legacy_kiro_value_still_reads_as_kas(self):
        assert _member({"acp_backend": "claude", "member_acp_backend": "kiro"}) == "kas"

    def test_a_legacy_value_crewmates_could_not_inherit_still_works(self):
        # goose is outside the inheritable set, but an explicit pick keeps working.
        assert _member({"acp_backend": "claude", "member_acp_backend": "goose"}) == "goose"

    def test_an_unrunnable_settings_harness_falls_back_with_a_logged_reason(self, caplog):
        with caplog.at_level(logging.WARNING, logger=loader_builders.logger.name):
            assert _member({"acp_backend": "goose"}) == "kas"
            assert _member({"acp_backend": "goose"}) == "kas"
        lines = [
            r.getMessage() for r in caplog.records if "Crew bots cannot run on" in r.getMessage()
        ]
        assert len(lines) == 1
        assert "'goose'" in lines[0] and "'kas'" in lines[0]

    def test_a_policy_denied_settings_harness_lands_crewmates_on_kas(self, _floor_monkeypatch):
        # Governance narrows the registry: claude is gone, so the Settings harness
        # takes the floor (Kiro CLI) and Crew bots read that as KAS.
        _floor_monkeypatch.setattr(registry, "_selectable", {"", "kas"})
        cfg = _build_agent_config({"acp_backend": "claude"})
        assert cfg.acp_backend == acp_backends.ACP_BACKEND_KIRO
        assert cfg.member_acp_backend == "kas"

    def test_a_policy_denied_legacy_value_still_degrades_at_the_gate(self, _floor_monkeypatch):
        _floor_monkeypatch.setattr(registry, "_selectable", {"", "kas"})
        assert _member({"acp_backend": "kas", "member_acp_backend": "claude"}) != "claude"

    def test_a_denied_kas_takes_the_floor_without_a_fallback_line(self, _floor_monkeypatch, caplog):
        _floor_monkeypatch.setattr(registry, "_selectable", {"", "goose"})
        with caplog.at_level(logging.WARNING, logger=loader_builders.logger.name):
            member = _member({"acp_backend": "claude"})
        assert member == acp_backends.ACP_BACKEND_KIRO


def _cfg(
    main: str, member: str, *, pin: str = "", inherited: bool | None = None
) -> SimpleNamespace:
    """A config as the readers see it; *inherited* is the loader's mark (absent: None)."""
    from kiro_crew.config.loader import KiroCrewAgentConfig

    agent = SimpleNamespace(acp_backend=main, member_acp_backend=member, kas_bundle=False)
    if inherited is not None:
        loader_builders.mark_member_backend_inherited(agent, inherited)
    return SimpleNamespace(
        agent=agent,
        agents={
            "atlas": KiroCrewAgentConfig(
                member_id="atlas-0123456789ab", kiro_agent="atlas", acp_backend=pin
            )
        },
    )


@pytest.fixture
def declared(_floor_monkeypatch):
    """Whether config.json holds ``agent.member_acp_backend``; False by default."""
    state = {"value": False}
    _floor_monkeypatch.setattr(
        "kiro_crew.config.loader_document.member_acp_backend_declared", lambda: state["value"]
    )
    return state


class TestSource:
    def test_settings(self, declared):
        from kiro_crew.crewmate_harness import SOURCE_SETTINGS, inherited_harness_source

        got = inherited_harness_source(_cfg("claude", "claude"))
        assert (got.harness, got.source, got.settings_harness) == (
            "claude",
            SOURCE_SETTINGS,
            "claude",
        )

    def test_kiro_cli_is_settings_not_fallback(self, declared):
        from kiro_crew.crewmate_harness import SOURCE_SETTINGS, inherited_harness_source

        got = inherited_harness_source(_cfg(acp_backends.ACP_BACKEND_KIRO, "kas"))
        assert (got.harness, got.source, got.settings_harness) == ("kas", SOURCE_SETTINGS, "kiro")

    def test_legacy(self, declared):
        from kiro_crew.crewmate_harness import SOURCE_LEGACY, inherited_harness_source

        declared["value"] = True
        got = inherited_harness_source(_cfg("goose", "codex"))
        assert (got.harness, got.source) == ("codex", SOURCE_LEGACY)

    def test_fallback(self, declared):
        from kiro_crew.crewmate_harness import SOURCE_FALLBACK, inherited_harness_source

        got = inherited_harness_source(_cfg("goose", "kas"))
        assert (got.harness, got.source, got.settings_harness) == ("kas", SOURCE_FALLBACK, "goose")

    def test_a_pin_wins(self, declared):
        from kiro_crew.crewmate_harness import SOURCE_PIN, crewmate_harness_source

        got = crewmate_harness_source(_cfg("goose", "kas", pin="claude"), "atlas")
        assert (got.harness, got.source) == ("claude", SOURCE_PIN)

    def test_an_unstartable_pin_reads_empty_and_still_refuses_at_start(
        self, declared, _floor_monkeypatch
    ):
        from kiro_crew.crewmate_harness import (
            SOURCE_PIN,
            CrewmateHarnessUnavailable,
            crewmate_backend_pin,
            crewmate_harness_source,
        )

        cfg = _cfg("claude", "claude", pin="no-such-harness")
        got = crewmate_harness_source(cfg, "atlas")
        assert (got.harness, got.source) == ("", SOURCE_PIN)
        with pytest.raises(CrewmateHarnessUnavailable):
            crewmate_backend_pin(cfg, "dashboard:member-atlas-0123456789ab")

    def test_the_worker_backend_agrees_with_the_readout(self, declared):
        from kiro_crew.crewmate_harness import inherited_harness_source
        from kiro_crew.mate_worker import crewmate_worker_backend

        cfg = _cfg("claude", "claude")
        assert acp_backends.backend_to_stored(crewmate_worker_backend(cfg)) == (
            inherited_harness_source(cfg).harness
        )


class _State:
    def __init__(self) -> None:
        self.notes: list[tuple] = []

    def notify(self, kind, title, body, *, url=None, channel=None):
        self.notes.append((kind, title, body, url, channel))


class TestAlarm:
    @pytest.fixture(autouse=True)
    def _fresh(self, _floor_monkeypatch):
        from kiro_crew import crewmate_harness_notice

        _floor_monkeypatch.setattr(crewmate_harness_notice, "_announced", set())

    def test_a_fallback_rings_the_bell_once_naming_both_harnesses(self, declared):
        from kiro_crew import crewmate_harness_notice

        state = _State()
        assert asyncio.run(crewmate_harness_notice.deliver(state, _cfg("goose", "kas")))
        assert not asyncio.run(crewmate_harness_notice.deliver(state, _cfg("goose", "kas")))
        assert len(state.notes) == 1
        _kind, title, body, url, channel = state.notes[0]
        assert "Goose" in title or "goose" in title.lower()
        assert "Kiro" in title or "KAS" in title
        assert url == crewmate_harness_notice.NOTE_URL
        assert channel == crewmate_harness_notice.NOTE_CHANNEL
        assert "cannot run on" in body

    def test_no_bell_when_crewmates_follow_the_settings_harness(self, declared):
        from kiro_crew import crewmate_harness_notice

        state = _State()
        assert not asyncio.run(crewmate_harness_notice.deliver(state, _cfg("claude", "claude")))
        assert not asyncio.run(
            crewmate_harness_notice.deliver(state, _cfg(acp_backends.ACP_BACKEND_KIRO, "kas"))
        )
        assert state.notes == []
        # No fallback, so no fallback bell; the once-per-install moved note is owed
        # instead, since these Bots left KAS for Claude Code (RFC section 6).
        assert crewmate_harness_notice.moved_note_owed(_cfg("claude", "claude"), "atlas")
        assert not crewmate_harness_notice.moved_note_owed(
            _cfg(acp_backends.ACP_BACKEND_KIRO, "kas"), "atlas"
        )

    def test_moved_notice_shown_once_when_inherited_differs_from_kas(self, declared):
        from kiro_crew import crewmate_harness_notice

        state = _State()
        cfg = _cfg("claude", "claude")
        assert asyncio.run(crewmate_harness_notice.deliver_moved(state, cfg, "atlas"))
        # Once per install: a second start, or a restart, says nothing.
        assert not asyncio.run(crewmate_harness_notice.deliver_moved(state, cfg, "atlas"))
        assert len(state.notes) == 1
        _kind, title, body, url, channel = state.notes[0]
        assert "Claude Code" in title and "Kiro Agent" in body
        assert "Agent harness in its Profile → Settings" in body
        assert url == "/members?member=atlas" and channel == crewmate_harness_notice.NOTE_CHANNEL

    @pytest.mark.parametrize(
        "cfg",
        [
            pytest.param(lambda: _cfg("kas", "kas"), id="settings-kas"),
            pytest.param(lambda: _cfg("goose", "kas"), id="fallback"),
            pytest.param(lambda: _cfg("claude", "claude", pin="claude"), id="pinned"),
        ],
    )
    def test_no_moved_notice_when_the_bot_did_not_move_by_inherit(self, declared, cfg):
        from kiro_crew import crewmate_harness_notice

        state = _State()
        assert not asyncio.run(crewmate_harness_notice.deliver_moved(state, cfg(), "atlas"))
        assert state.notes == []

    def test_no_moved_notice_when_the_legacy_setting_decides(self, declared):
        from kiro_crew import crewmate_harness_notice

        declared["value"] = True
        assert not crewmate_harness_notice.moved_note_owed(_cfg("claude", "claude"), "atlas")

    def test_no_bell_when_the_legacy_setting_decides(self, declared):
        from kiro_crew import crewmate_harness_notice

        declared["value"] = True
        state = _State()
        assert not asyncio.run(crewmate_harness_notice.deliver(state, _cfg("goose", "kas")))

    def test_moving_away_and_back_rings_again(self, declared):
        from kiro_crew import crewmate_harness_notice

        state = _State()
        asyncio.run(crewmate_harness_notice.deliver(state, _cfg("goose", "kas")))
        asyncio.run(crewmate_harness_notice.deliver(state, _cfg("claude", "claude")))
        asyncio.run(crewmate_harness_notice.deliver(state, _cfg("goose", "kas")))
        assert len(state.notes) == 2

    def test_a_broken_bell_never_raises(self, declared):
        from kiro_crew import crewmate_harness_notice

        class _Broken:
            def notify(self, *a, **k):
                raise RuntimeError("bell down")

        assert not asyncio.run(crewmate_harness_notice.deliver(_Broken(), _cfg("goose", "kas")))
        # Not delivered, so still owed: the next delivery rings the bell.
        state = _State()
        assert asyncio.run(crewmate_harness_notice.deliver(state, _cfg("goose", "kas")))
        assert len(state.notes) == 1


class TestPanelReadout:
    def test_the_agent_harness_panel_reports_the_crewmate_harness(
        self, declared, _floor_monkeypatch
    ):
        from kiro_crew.dashboard.handlers import acp_backend_status

        _floor_monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load",
            staticmethod(lambda *a, **k: _cfg("goose", "kas")),
        )
        assert acp_backend_status._crewmate_harness() == {
            "harness": "kas",
            "source": "fallback",
            "settings_harness": "goose",
        }

    def test_a_failed_read_leaves_the_readout_out(self, _floor_monkeypatch):
        from kiro_crew.dashboard.handlers import acp_backend_status

        def _boom(*a, **k):
            raise OSError("unreadable")

        _floor_monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(_boom)
        )
        assert acp_backend_status._crewmate_harness() is None


DM_KEY = "dashboard:member-atlas-0123456789ab"


@pytest.fixture
def limits(_floor_monkeypatch):
    """What Crew bot atlas's file sets that the inherited harness cannot carry."""
    state: dict = {"value": (), "reads": 0}

    def _fields(member_id, backend):
        from kiro_crew.crewmate_harness import _on_event_loop

        state["reads"] += 1
        state["on_loop"] = state.get("on_loop", 0) + int(_on_event_loop())
        if member_id != "atlas-0123456789ab" or backend == "kas":
            return (), ()
        return tuple(state["value"]), tuple(state.get("items", ()))

    _floor_monkeypatch.setattr("kiro_crew.mate_worker.kas_only_worker_detail", _fields)
    _floor_monkeypatch.setattr(
        "kiro_crew.crewmate_harness._chat_tool_limits",
        lambda crew, backend: (
            tuple(state.get("chat", ())) if crew == "atlas" and backend != "kas" else ()
        ),
    )
    _floor_monkeypatch.setattr(
        "kiro_crew.agent_discovery.agents_dir_revision",
        lambda _dir: state.get("revision", ("r", 0)),
    )
    return state


class TestLoaderMark:
    @pytest.mark.parametrize(
        ("agent", "inherited"),
        [
            ({}, True),
            ({"member_acp_backend": ""}, True),
            ({"member_acp_backend": "claude"}, False),
            ({"member_acp_backend": "kiro"}, False),
        ],
    )
    def test_the_loader_marks_an_inherited_member_backend(self, agent, inherited):
        built = _build_agent_config({"acp_backend": "claude", **agent})
        assert getattr(built, loader_builders.MEMBER_BACKEND_INHERITED_ATTR) is inherited

    def test_the_mark_is_never_written_back(self):
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.agent = _build_agent_config({"acp_backend": "claude"})
        assert loader_builders.MEMBER_BACKEND_INHERITED_ATTR not in cfg.to_dict()["agent"]

    def test_a_degraded_load_marks_the_defaults_inherited(self):
        from kiro_crew.config import loader_pipeline

        assert "mark_member_backend_inherited(cfg.agent, True)" in (
            __import__("inspect").getsource(loader_pipeline.build_config)
        )


class TestWorkerSpecFallback:
    """One Crew bot whose worker spec its inherited harness cannot carry."""

    def test_a_mate_that_narrows_worker_tools_runs_on_kas(self, limits):
        from kiro_crew.crewmate_harness import crewmate_backend_pin, pinned_backend
        from kiro_crew.mate_worker import crewmate_worker_backend

        limits["value"] = ("tool limits",)
        cfg = _cfg("claude", "claude", inherited=True)
        pin = crewmate_backend_pin(cfg, DM_KEY)
        assert pin is not None and pin.backend == "kas" and pin.worker_spec_fallback
        assert pinned_backend(cfg, "atlas", "claude") == "kas"
        assert crewmate_worker_backend(cfg, "atlas") == "kas"

    def test_the_gate_routes_the_fallback_to_kas(self, limits):
        from kiro_crew.crewmate_harness import crewmate_backend_pin
        from kiro_crew.members import select_provider_backend

        limits["value"] = ("tool limits",)
        cfg = _cfg("claude", "claude", inherited=True)
        pin = crewmate_backend_pin(cfg, DM_KEY)
        assert select_provider_backend(DM_KEY, "claude", "claude", crew_backend=pin) == "kas"

    def test_a_mate_without_limits_follows_settings(self, limits):
        from kiro_crew.crewmate_harness import crewmate_backend_pin, pinned_backend

        cfg = _cfg("claude", "claude", inherited=True)
        assert crewmate_backend_pin(cfg, DM_KEY) is None
        assert pinned_backend(cfg, "atlas", "claude") == "claude"

    def test_the_legacy_setting_is_never_moved(self, limits):
        from kiro_crew.crewmate_harness import crewmate_backend_pin, pinned_backend

        limits["value"] = ("tool limits",)
        cfg = _cfg("claude", "claude", inherited=False)
        assert crewmate_backend_pin(cfg, DM_KEY) is None
        assert pinned_backend(cfg, "atlas", "claude") == "claude"
        assert limits["reads"] == 0

    def test_an_explicit_pin_is_never_moved(self, limits):
        from kiro_crew.crewmate_harness import crewmate_backend_pin

        limits["value"] = ("tool limits",)
        cfg = _cfg("claude", "claude", pin="claude", inherited=True)
        pin = crewmate_backend_pin(cfg, DM_KEY)
        assert pin is not None and pin.backend == "claude" and not pin.worker_spec_fallback

    def test_an_inherited_kas_reads_no_file(self, limits):
        from kiro_crew.crewmate_harness import inherited_falls_back

        limits["value"] = ("tool limits",)
        assert not inherited_falls_back(_cfg("kas", "kas", inherited=True), "atlas")
        assert limits["reads"] == 0

    def test_no_fallback_when_kas_is_not_selectable(self, limits, _floor_monkeypatch):
        from kiro_crew.crewmate_harness import inherited_falls_back

        _floor_monkeypatch.setattr(registry, "_selectable", {"", "claude"})
        limits["value"] = ("tool limits",)
        assert not inherited_falls_back(_cfg("claude", "claude", inherited=True), "atlas")

    def test_the_fallback_is_logged_once(self, limits, caplog):
        from kiro_crew import crewmate_harness

        limits["value"] = ("tool limits",)
        cfg = _cfg("claude", "claude", inherited=True)
        with caplog.at_level(logging.WARNING, logger=crewmate_harness.logger.name):
            assert crewmate_harness.inherited_falls_back(cfg, "atlas")
            assert crewmate_harness.inherited_falls_back(cfg, "atlas")
        lines = [r.getMessage() for r in caplog.records if "for its workers" in r.getMessage()]
        assert len(lines) == 1 and "'atlas'" in lines[0] and "'claude'" in lines[0]
        assert "tool limits" in lines[0]

    @pytest.mark.parametrize("fields", [("hooks",), ("permissions",), ("toolsSettings",)])
    def test_worker_spec_policy_alone_runs_the_mate_on_kas(self, limits, fields):
        from kiro_crew.crewmate_harness import crewmate_backend_pin, pinned_backend

        limits["value"] = fields
        cfg = _cfg("claude", "claude", inherited=True)
        assert crewmate_backend_pin(cfg, DM_KEY).worker_spec_fallback
        assert pinned_backend(cfg, "atlas", "claude") == "kas"

    def test_a_readout_of_a_pinned_mate_logs_nothing(self, limits, declared, caplog):
        from kiro_crew import crewmate_harness

        limits["value"] = ("hooks",)
        cfg = _cfg("claude", "claude", pin="claude", inherited=True)
        with caplog.at_level(logging.WARNING, logger=crewmate_harness.logger.name):
            crewmate_harness.crewmate_inherited_source(cfg, "atlas")
        assert not [r for r in caplog.records if "for its workers" in r.getMessage()]

    def test_the_event_loop_reads_memory_only(self, limits):
        import threading as _threading

        from kiro_crew import crewmate_harness

        limits["value"] = ("hooks",)
        cfg = _cfg("claude", "claude", inherited=True)

        async def _on_loop():
            return crewmate_harness.worker_spec_kas_fields(cfg, "atlas")

        # Nothing held yet: the loop answers nothing and never reads the file itself.
        assert asyncio.run(_on_loop()) == ()
        # The background refresh fills the memory.
        for thread in _threading.enumerate():
            if thread.name == "crewmate-worker-spec-verdict":
                thread.join(5)
        assert asyncio.run(_on_loop()) == ("hooks",)
        assert limits["reads"] == 1 and limits.get("on_loop", 0) == 0

    def test_the_refresh_reads_every_mate_off_the_loop(self, limits):
        from kiro_crew import crewmate_harness

        limits["value"] = ("permissions",)
        cfg = _cfg("claude", "claude", inherited=True)
        crewmate_harness.refresh_worker_spec_verdicts(cfg)
        assert limits["reads"] == 1

        async def _on_loop():
            return crewmate_harness.worker_spec_kas_fields(cfg, "atlas")

        assert asyncio.run(_on_loop()) == ("permissions",)
        assert limits["reads"] == 1

    def test_the_file_read_is_memoized_on_the_folder_revision(self, limits):
        from kiro_crew import crewmate_harness

        limits["revision"] = ("r", 1)
        limits["value"] = ("tool limits",)
        cfg = _cfg("claude", "claude", inherited=True)
        assert crewmate_harness.inherited_falls_back(cfg, "atlas")
        assert crewmate_harness.inherited_falls_back(cfg, "atlas")
        assert limits["reads"] == 1
        # The Crew bot's file changed: read again.
        limits["revision"] = ("r", 2)
        limits["value"] = ()
        assert not crewmate_harness.inherited_falls_back(cfg, "atlas")
        assert limits["reads"] == 2

    def test_the_readout_names_the_mates_real_harness(self, limits, declared):
        from kiro_crew.crewmate_harness import (
            SOURCE_PIN,
            SOURCE_WORKER_SPEC,
            crewmate_harness_source,
            crewmate_inherited_source,
        )

        limits["value"] = ("tool limits",)
        got = crewmate_harness_source(_cfg("claude", "claude", inherited=True), "atlas")
        assert (got.harness, got.source, got.settings_harness) == (
            "kas",
            SOURCE_WORKER_SPEC,
            "claude",
        )
        # With a pin set, Inherited still says what clearing it would give.
        pinned = _cfg("claude", "claude", pin="claude", inherited=True)
        assert crewmate_harness_source(pinned, "atlas").source == SOURCE_PIN
        assert crewmate_inherited_source(pinned, "atlas").source == SOURCE_WORKER_SPEC

    def test_the_model_is_judged_on_kas_for_the_fallback(self, limits):
        from kiro_crew.dashboard.handlers.agents import _pin_entitlement_backend

        cfg = _cfg("claude", "claude", inherited=True)
        assert _pin_entitlement_backend(cfg, "atlas") == "claude"
        limits["value"] = ("tool limits",)
        limits["revision"] = ("r", 1)
        assert _pin_entitlement_backend(cfg, "atlas") == "kas"
        assert _pin_entitlement_backend(cfg, "atlas", for_workers=True) == "kas"


class TestModelSpelling:
    def test_spelling_follows_the_effective_backend(self):
        from kiro_crew.crewmate_harness import kiro_spelling_applies

        assert kiro_spelling_applies("kas")
        assert kiro_spelling_applies(acp_backends.ACP_BACKEND_KIRO)
        assert kiro_spelling_applies(None)
        assert not kiro_spelling_applies("claude")

    def test_an_inherited_claude_alias_is_not_refused_as_a_kiro_spelling(self, _floor_monkeypatch):
        from kiro_crew.dashboard.handlers import agents

        _floor_monkeypatch.setattr(agents.model_registry, "acp_id_correction", lambda _m: "x")
        cfg = _cfg("claude", "claude", inherited=True)
        reason = agents._model_pin_rejected(
            "sonnet", None, "", backend=agents._pin_entitlement_backend(cfg, "atlas")
        )
        assert reason is None or "kiro-cli serves" not in reason

    def test_a_kas_harness_still_gets_the_spelling_check(self, _floor_monkeypatch):
        from kiro_crew.dashboard.handlers import agents

        _floor_monkeypatch.setattr(agents.model_registry, "acp_id_correction", lambda _m: "x")
        reason = agents._model_pin_rejected("claude-sonnet-4-5", None, "", backend="kas")
        assert reason is not None and "kiro-cli serves" in reason


class TestMateAlarm:
    @pytest.fixture(autouse=True)
    def _fresh(self, _floor_monkeypatch):
        from kiro_crew import crewmate_harness_notice

        _floor_monkeypatch.setattr(crewmate_harness_notice, "_announced", set())

    def test_a_mate_fallback_rings_once_and_opens_its_page(self, limits, declared):
        from kiro_crew import crewmate_harness_notice

        limits["value"] = ("tool limits",)
        cfg = _cfg("claude", "claude", inherited=True)
        state = _State()
        assert asyncio.run(crewmate_harness_notice.deliver(state, cfg)) == 1
        assert asyncio.run(crewmate_harness_notice.deliver(state, cfg)) == 0
        _kind, title, body, url, _channel = state.notes[0]
        assert title.startswith("atlas runs on") and "tool limits" in body
        assert url == "/members?member=atlas"

    def test_the_note_names_exactly_the_entries_to_change(self, limits, declared):
        from kiro_crew import crewmate_harness_notice

        limits["value"] = ("tool limits", "hooks")
        limits["items"] = (
            ("turned off", "github/create_issue"),
            ("turned off", "fs/delete"),
            ("hooks", "preToolUse"),
        )
        state = _State()
        asyncio.run(
            crewmate_harness_notice.deliver(state, _cfg("claude", "claude", inherited=True))
        )
        body = state.notes[0][2]
        assert (
            "turn these tools back on or remove these entries in the bot's file or its "
            "Capabilities: turned off: `github/create_issue`, `fs/delete`; hooks: `preToolUse`."
        ) in body
        assert "more" not in body and crewmate_harness_notice.ROUTE in body

    def test_a_long_list_is_capped(self):
        from kiro_crew.crewmate_harness_notice import MAX_ITEMS, _items_line

        line = _items_line(tuple(("turned off", f"s/t{i}") for i in range(MAX_ITEMS + 3)))
        assert line.count("`s/t") == MAX_ITEMS and line.endswith("(+3 more)")

    def test_a_name_with_markup_is_escaped(self):
        from kiro_crew.crewmate_harness_notice import MAX_ITEM_CHARS, _items_line

        line = _items_line((("hooks", "a`<img src=x>\n[x](http://e)\x1b"), ("hooks", "y" * 99)))
        assert line == (
            "hooks: `a<img src=x> [x](http://e)`, `" + "y" * (MAX_ITEM_CHARS - 1) + "…`"
        )

    def test_a_mate_that_stops_falling_back_is_told_again_later(self, limits, declared):
        from kiro_crew import crewmate_harness_notice

        cfg = _cfg("claude", "claude", inherited=True)
        state = _State()
        limits["value"] = ("tool limits",)
        asyncio.run(crewmate_harness_notice.deliver(state, cfg))
        limits["value"], limits["revision"] = (), ("r", 1)
        asyncio.run(crewmate_harness_notice.deliver(state, cfg))
        limits["value"], limits["revision"] = ("tool limits",), ("r", 2)
        asyncio.run(crewmate_harness_notice.deliver(state, cfg))
        assert len(state.notes) == 2


class TestKasOnlyWorkerFields:
    @pytest.fixture
    def spec(self, _floor_monkeypatch):
        state: dict = {"spec": {}}
        _floor_monkeypatch.setattr("kiro_crew.mate_worker.mate_file_for_member", lambda _m: "atlas")
        _floor_monkeypatch.setattr(
            "kiro_crew.agent_sdk.agent_specs.load_agent_spec", lambda _d, _n: dict(state["spec"])
        )
        return state

    def test_every_field_only_kas_carries_is_named(self, spec):
        from kiro_crew.mate_worker import kas_only_worker_fields

        spec["spec"] = {
            "excludedTools": ["use_aws"],
            "hooks": {"preToolUse": [{"command": "x"}]},
            "permissions": {"allow": ["read"]},
            "toolsSettings": {"shell": {"allowedCommands": ["ls"]}},
        }
        assert kas_only_worker_fields("atlas-0123456789ab", "claude") == (
            "tool limits",
            "hooks",
            "permissions",
            "toolsSettings",
        )
        assert kas_only_worker_fields("atlas-0123456789ab", "kas") == ()

    def test_a_plain_spec_names_nothing(self, spec):
        from kiro_crew.mate_worker import kas_only_worker_fields

        spec["spec"] = {"tools": ["read"], "hooks": {}}
        assert kas_only_worker_fields("atlas-0123456789ab", "claude") == ()

    def test_the_detail_names_each_switched_off_tool_and_hook(self, spec):
        from kiro_crew.mate_worker import kas_only_worker_detail

        spec["spec"] = {
            "mcpServers": {
                "github": {"command": "gh", "disabledTools": ["create_issue"]},
                "fs": {"command": "fs", "disabledTools": ["delete"]},
            },
            "hooks": {"preToolUse": [{"command": "x"}]},
        }
        assert kas_only_worker_detail("atlas-0123456789ab", "codex") == (
            ("tool limits", "hooks"),
            (
                ("turned off", "github/create_issue"),
                ("turned off", "fs/delete"),
                ("hooks", "preToolUse"),
            ),
        )

    def test_a_deny_host_is_told_only_the_entries_it_cannot_carry(self, spec):
        from kiro_crew.mate_worker import kas_only_worker_detail

        spec["spec"] = {
            "mcpServers": {"github": {"command": "gh", "disabledTools": ["create_issue"]}},
            "excludedTools": ["shell", "use_aws", "@github/x"],
        }
        assert kas_only_worker_detail("atlas-0123456789ab", "claude")[1] == (
            ("excludedTools", "use_aws"),
        )


class TestChatToolLimits:
    def test_a_saved_spec_with_chat_tool_limits_runs_the_mate_on_kas(self, limits):
        from kiro_crew.crewmate_harness import (
            crewmate_backend_pin,
            worker_spec_kas_detail,
            worker_spec_kas_fields,
        )

        limits["chat"] = ("github/search",)
        cfg = _cfg("claude", "claude", inherited=True)
        assert worker_spec_kas_fields(cfg, "atlas") == ("chat tool limits",)
        assert worker_spec_kas_detail(cfg, "atlas")[1] == (
            ("turned off in Capabilities", "github/search"),
        )
        assert crewmate_backend_pin(cfg, DM_KEY).backend == "kas"

    def test_the_real_check_skips_a_harness_that_carries_chat_limits(self, _floor_monkeypatch):
        from kiro_crew import crewmate_harness

        def _boom(_crew):
            raise AssertionError("must not read")

        _floor_monkeypatch.setattr(
            "kiro_crew.agent_capabilities.prepare_member_capabilities", _boom
        )
        assert not crewmate_harness._chat_tool_limits("atlas", "kas")

    @pytest.mark.parametrize(
        ("prepared", "expected"),
        [({"tool_limited_servers": ("github",)}, True), ({"tool_limited_servers": ()}, False)],
    )
    def test_the_real_check_reads_the_saved_spec(self, _floor_monkeypatch, prepared, expected):
        from kiro_crew import crewmate_harness

        _floor_monkeypatch.setattr(
            "kiro_crew.agent_capabilities.prepare_member_capabilities", lambda _crew: prepared
        )
        assert bool(crewmate_harness._chat_tool_limits("atlas", "codex")) is expected

    def test_an_unreadable_saved_spec_is_left_to_the_start(self, _floor_monkeypatch):
        from kiro_crew import crewmate_harness

        def _refuse(_crew):
            raise RuntimeError("materialization_pending")

        _floor_monkeypatch.setattr(
            "kiro_crew.agent_capabilities.prepare_member_capabilities", _refuse
        )
        assert not crewmate_harness._chat_tool_limits("atlas", "claude")


class TestWatchDocument:
    def test_removing_a_stored_value_equal_to_settings_is_a_change(self):
        from kiro_crew.config.live import diff_config_docs, watch_document
        from kiro_crew.config.loader import KiroCrewConfig

        stored, inherited = KiroCrewConfig(), KiroCrewConfig()
        stored.agent = _build_agent_config(
            {"acp_backend": "claude", "member_acp_backend": "claude"}
        )
        inherited.agent = _build_agent_config({"acp_backend": "claude"})
        assert stored.to_dict()["agent"] == inherited.to_dict()["agent"]
        changed = diff_config_docs(watch_document(stored), watch_document(inherited))
        assert "agent.member_acp_backend" in changed

    def test_the_watch_document_never_changes_to_dict(self):
        from kiro_crew.config.live import watch_document
        from kiro_crew.config.loader import KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.agent = _build_agent_config({"acp_backend": "claude"})
        watch_document(cfg)
        assert cfg.to_dict()["agent"]["member_acp_backend"] == "claude"


class TestPrivateCopyFallback:
    def test_a_project_scoped_spec_is_read_from_the_private_copy(self, _floor_monkeypatch):
        from kiro_crew import crewmate_harness

        def _refuse(_crew):
            raise RuntimeError("project_identity_changed")

        _floor_monkeypatch.setattr(
            "kiro_crew.agent_capabilities.prepare_member_capabilities", _refuse
        )
        _floor_monkeypatch.setattr(
            crewmate_harness, "_private_copy_tool_limits", lambda c: ("github/search",)
        )
        assert crewmate_harness._chat_tool_limits("atlas", "codex") == ("github/search",)


class TestColdStartRefresh:
    """A Bot cold start reads its files fresh, so an edit made outside Crew counts."""

    @pytest.fixture(autouse=True)
    def _fresh(self, _floor_monkeypatch):
        from kiro_crew import crewmate_harness_notice

        _floor_monkeypatch.setattr(crewmate_harness_notice, "_announced", set())

    def test_an_external_hooks_edit_moves_the_next_cold_start_to_kas(
        self, limits, declared, _floor_monkeypatch
    ):
        from kiro_crew import crewmate_harness

        cfg = _cfg("claude", "claude", inherited=True)
        state = _State()
        _floor_monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda *a, **k: cfg)
        )
        _floor_monkeypatch.setattr("kiro_crew.slack.handler.get_dashboard_state", lambda: state)

        async def _pin():
            return crewmate_harness.crewmate_backend_pin(cfg, DM_KEY)

        # A plain Bot: the answer is held, and it follows Settings.
        crewmate_harness.refresh_worker_spec_verdicts(cfg)
        assert asyncio.run(_pin()) is None
        # Its worker file gains hooks in an editor: the folder revision moves, and
        # no in-process writer bumps the spec generation.
        limits["value"], limits["revision"] = ("hooks",), ("r", 9)
        assert asyncio.run(_pin()) is None  # the held hint alone is stale
        # The cold start awaits a fresh read first, so the factory picks KAS.
        asyncio.run(crewmate_harness.refresh_session_verdict(DM_KEY))
        pin = asyncio.run(_pin())
        assert pin is not None and pin.backend == "kas" and pin.worker_spec_fallback
        # And the bell says so.
        assert len(state.notes) == 1 and "hooks" in state.notes[0][2]

    def test_a_plain_bot_cold_start_sends_one_moved_note(
        self, limits, declared, _floor_monkeypatch
    ):
        """The wiring RFC section 6 needs: the cold start itself rings the moved note."""
        from kiro_crew import crewmate_harness, crewmate_harness_notice

        cfg = _cfg("claude", "claude", inherited=True)
        state = _State()
        _floor_monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda *a, **k: cfg)
        )
        _floor_monkeypatch.setattr("kiro_crew.slack.handler.get_dashboard_state", lambda: state)

        asyncio.run(crewmate_harness.refresh_session_verdict(DM_KEY))
        # A second cold start, as after a restart, says nothing more.
        asyncio.run(crewmate_harness.refresh_session_verdict(DM_KEY))
        assert len(state.notes) == 1
        _kind, title, _body, url, channel = state.notes[0]
        assert "Claude Code" in title and url == "/members?member=atlas"
        assert channel == crewmate_harness_notice.NOTE_CHANNEL

    def test_a_pinned_bot_is_not_read(self, limits, _floor_monkeypatch):
        from kiro_crew import crewmate_harness

        cfg = _cfg("claude", "claude", pin="claude", inherited=True)
        _floor_monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda *a, **k: cfg)
        )
        asyncio.run(crewmate_harness.refresh_session_verdict(DM_KEY))
        assert limits["reads"] == 0

    def test_a_failed_read_never_raises(self, limits, declared, _floor_monkeypatch):
        """A failed read returns quietly and leaves the held answer in place."""
        from kiro_crew import crewmate_harness

        cfg = _cfg("claude", "claude", inherited=True)
        state = _State()
        _floor_monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda *a, **k: cfg)
        )
        _floor_monkeypatch.setattr("kiro_crew.slack.handler.get_dashboard_state", lambda: state)

        async def _pin():
            return crewmate_harness.crewmate_backend_pin(cfg, DM_KEY)

        # The held answer: the worker file sets hooks, so the Bot runs on KAS.
        limits["value"], limits["revision"] = ("hooks",), ("r", 1)
        asyncio.run(crewmate_harness.refresh_session_verdict(DM_KEY))
        assert asyncio.run(_pin()).backend == "kas"
        notes_before = list(state.notes)

        def _boom(*a, **k):
            raise OSError("unreadable")

        _floor_monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(_boom)
        )
        assert asyncio.run(crewmate_harness.refresh_session_verdict(DM_KEY)) is None
        assert asyncio.run(_pin()).backend == "kas"
        assert state.notes == notes_before

    def test_the_factory_seam_awaits_the_refresh(self):
        import inspect

        from kiro_crew.session_allocation_parts import get_or_create

        source = inspect.getsource(get_or_create._get_or_create_impl)
        refresh = source.index("await refresh_session_verdict(key)")
        assert refresh < source.index("provider = factory(")


class TestUnrunnablePin:
    """A pin to a harness Bots cannot run on runs the Bot on KAS, logged and rung."""

    @pytest.fixture(autouse=True)
    def _fresh(self, _floor_monkeypatch):
        from kiro_crew import crewmate_harness, crewmate_harness_notice

        _floor_monkeypatch.setattr(crewmate_harness_notice, "_announced", set())
        _floor_monkeypatch.setattr(crewmate_harness, "_pin_fallback_logged", set())

    @pytest.mark.parametrize("pinned", ["goose", "opencode", "strands", "pi", "deepseek"])
    def test_the_pin_runs_on_kas(self, pinned):
        from kiro_crew.crewmate_harness import crewmate_backend_pin, pinned_backend

        cfg = _cfg("claude", "claude", pin=pinned, inherited=True)
        pin = crewmate_backend_pin(cfg, DM_KEY)
        assert pin is not None and pin.backend == "kas" and pin.harness_fallback
        assert pinned_backend(cfg, "atlas", "claude") == "kas"

    @pytest.mark.parametrize("pinned", ["claude", "codex", "kas"])
    def test_a_pin_bots_can_run_on_is_kept(self, pinned):
        from kiro_crew.crewmate_harness import crewmate_backend_pin

        pin = crewmate_backend_pin(_cfg("claude", "claude", pin=pinned, inherited=True), DM_KEY)
        assert pin.backend == pinned and not pin.harness_fallback

    def test_a_pin_that_cannot_start_still_refuses(self):
        from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable, crewmate_backend_pin

        with pytest.raises(CrewmateHarnessUnavailable):
            crewmate_backend_pin(_cfg("claude", "claude", pin="no-such-harness"), DM_KEY)

    def test_it_is_logged_once_as_an_error(self, caplog):
        from kiro_crew import crewmate_harness

        cfg = _cfg("claude", "claude", pin="goose", inherited=True)
        with caplog.at_level(logging.ERROR, logger=crewmate_harness.logger.name):
            crewmate_harness.crewmate_backend_pin(cfg, DM_KEY)
            crewmate_harness.crewmate_backend_pin(cfg, DM_KEY)
        lines = [r for r in caplog.records if "is pinned to" in r.getMessage()]
        assert len(lines) == 1 and lines[0].levelno == logging.ERROR

    def test_the_readout_names_kas(self, declared):
        from kiro_crew.crewmate_harness import SOURCE_PIN, crewmate_harness_source

        got = crewmate_harness_source(
            _cfg("claude", "claude", pin="goose", inherited=True), "atlas"
        )
        assert (got.harness, got.source) == ("kas", SOURCE_PIN)

    def test_the_bell_says_so_as_an_error(self, declared):
        from kiro_crew import crewmate_harness_notice

        state = _State()
        cfg = _cfg("claude", "claude", pin="goose", inherited=True)
        assert asyncio.run(crewmate_harness_notice.deliver(state, cfg)) == 1
        assert asyncio.run(crewmate_harness_notice.deliver(state, cfg)) == 0
        kind, title, _body, url, _channel = state.notes[0]
        assert kind == "error" and title.startswith("atlas runs on") and url.endswith("atlas")


class TestModelFollowsTheHarness:
    """The Bot's model is kept where its harness serves it, else that harness's default."""

    CODEX_PIN = "openai.gpt-5.4"

    @pytest.fixture(autouse=True)
    def _warm(self, _floor_monkeypatch):
        from kiro_crew import model_registry as mr

        _floor_monkeypatch.setattr(
            mr,
            "_ADVERTISED_MODELS",
            {
                "codex": ["openai.gpt-5.4[xhigh]"],
                "claude_code": ["claude-opus-5[1m]"],
                "acp": ["claude-opus-4.8"],
            },
        )

    def _config(self, *, pin: str):
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

        cfg = KiroCrewConfig()
        cfg.agent = _build_agent_config({"acp_backend": "claude", "model": "auto"})
        cfg.agents["atlas"] = KiroCrewAgentConfig(
            member_id="atlas-0123456789ab",
            kiro_agent="atlas",
            acp_backend=pin,
            model=self.CODEX_PIN,
        )
        return cfg

    def _model(self, cfg, _floor_monkeypatch):
        from kiro_crew.session_parts.policy import _session_model

        _floor_monkeypatch.setattr(type(cfg), "_resolve_agent_model", lambda self: "")
        return _session_model(cfg, "atlas", crew_agent="atlas", session_key=DM_KEY)

    def test_the_bots_model_is_kept_where_its_harness_serves_it(self, _floor_monkeypatch):
        assert self._model(self._config(pin="codex"), _floor_monkeypatch) == self.CODEX_PIN

    def test_a_fallback_to_kas_drops_a_model_kas_cannot_serve(self, _floor_monkeypatch):
        assert self._model(self._config(pin="goose"), _floor_monkeypatch) != self.CODEX_PIN

    def test_an_inherited_harness_drops_a_model_it_cannot_serve(self, _floor_monkeypatch):
        assert self._model(self._config(pin=""), _floor_monkeypatch) != self.CODEX_PIN


class TestPinFallbackNeedsKas:
    def test_a_denied_kas_lands_on_the_floor_and_names_it(
        self, declared, _floor_monkeypatch, caplog
    ):
        from kiro_crew import crewmate_harness, crewmate_harness_notice

        _floor_monkeypatch.setattr(crewmate_harness_notice, "_announced", set())
        _floor_monkeypatch.setattr(registry, "_selectable", {"", "claude", "pi"})
        cfg = _cfg("claude", "claude", pin="pi", inherited=True)
        with caplog.at_level(logging.ERROR, logger=crewmate_harness.logger.name):
            pin = crewmate_harness.crewmate_backend_pin(cfg, DM_KEY)
        floor = acp_backends.resolve_selected_backend(acp_backends.ACP_BACKEND_KAS)
        assert floor != "kas" and pin.backend == floor and pin.harness_fallback
        line = [r.getMessage() for r in caplog.records if "is pinned to" in r.getMessage()][0]
        assert repr(acp_backends.backend_to_stored(floor)) in line
        state = _State()
        asyncio.run(crewmate_harness_notice.deliver(state, cfg))
        errors = [n for n in state.notes if n[0] == "error"]
        assert len(errors) == 1 and "Kiro Agent" not in errors[0][1]
        assert crewmate_harness.kas_pin_landing() == acp_backends.backend_to_stored(floor)

    def test_kas_pin_harnesses_lists_the_pins_that_run_on_kas(self):
        from kiro_crew.crewmate_harness import kas_pin_harnesses

        listed = kas_pin_harnesses()
        assert {"goose", "opencode", "pi", "deepseek", "strands"} <= set(listed)
        assert not {"claude", "codex", "kas", "kiro"} & set(listed)


class TestUnpinnedModelJudgedOnTheBotsHarness:
    """An unpinned Bot's model is judged on the harness it runs on, never the install's."""

    def test_a_kiro_cli_install_judges_a_bot_on_kas(self, _floor_monkeypatch):
        from kiro_crew.dashboard.handlers import agents

        cfg = _cfg(acp_backends.ACP_BACKEND_KIRO, "kas", inherited=True)
        assert agents._pin_entitlement_backend(cfg, "atlas") == "kas"
        assert agents._pin_entitlement_backend(cfg) == "kas"


class TestModelSaveRefresh:
    def test_an_external_edit_is_read_before_a_model_save(self, limits, _floor_monkeypatch):
        from kiro_crew import crewmate_harness
        from kiro_crew.dashboard.handlers import agents

        cfg = _cfg("claude", "claude", inherited=True)
        _floor_monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda *a, **k: cfg)
        )
        limits["value"], limits["revision"] = ("hooks",), ("r", 1)
        crewmate_harness.refresh_worker_spec_verdicts(cfg)

        async def _judged():
            return agents._pin_entitlement_backend(cfg, "atlas")

        assert asyncio.run(_judged()) == "kas"
        # The hooks are removed in an editor: the held hint is stale until refreshed.
        limits["value"], limits["revision"] = (), ("r", 2)
        asyncio.run(crewmate_harness.refresh_crew_verdict("atlas"))
        assert asyncio.run(_judged()) == "claude"

    def test_the_update_handler_awaits_the_refresh(self):
        import inspect

        from kiro_crew.dashboard.agent_admin import crew_update

        source = inspect.getsource(crew_update)
        assert source.index("await refresh_crew_verdict(name)") < source.index(
            "pending_reason = await _revalidate_crew_pin("
        )

    def test_a_pinned_bot_is_refreshed_too(self, limits, _floor_monkeypatch):
        """A save that clears the pin is judged on the fresh Inherited answer."""
        from kiro_crew import crewmate_harness

        cfg = _cfg("claude", "claude", pin="kas", inherited=True)
        _floor_monkeypatch.setattr(
            "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda *a, **k: cfg)
        )
        limits["value"], limits["revision"] = ("hooks",), ("r", 1)
        crewmate_harness.refresh_worker_spec_verdicts(cfg)
        limits["value"], limits["revision"] = (), ("r", 2)
        asyncio.run(crewmate_harness.refresh_crew_verdict("atlas"))

        async def _held():
            return crewmate_harness.worker_spec_kas_fields(cfg, "atlas")

        assert asyncio.run(_held()) == ()


class TestCarriedMcpToolLimits:
    @pytest.fixture
    def spec(self, _floor_monkeypatch):
        state: dict = {"spec": {}}
        _floor_monkeypatch.setattr("kiro_crew.mate_worker.mate_file_for_member", lambda _m: "atlas")
        _floor_monkeypatch.setattr(
            "kiro_crew.agent_sdk.agent_specs.load_agent_spec", lambda _d, _n: dict(state["spec"])
        )
        return state

    def test_claude_carries_a_switched_off_mcp_tool(self, spec):
        from kiro_crew.mate_worker import kas_only_worker_fields

        spec["spec"] = {
            "mcpServers": {"github": {"command": "gh", "disabledTools": ["create_issue"]}}
        }
        assert kas_only_worker_fields("atlas-0123456789ab", "claude") == ()
        assert kas_only_worker_fields("atlas-0123456789ab", "codex") == ("tool limits",)

    def test_a_native_tool_limit_no_built_in_covers_still_needs_kas(self, spec):
        from kiro_crew.mate_worker import kas_only_worker_fields

        spec["spec"] = {"excludedTools": ["use_aws"]}
        assert kas_only_worker_fields("atlas-0123456789ab", "claude") == ("tool limits",)
        spec["spec"] = {"excludedTools": ["shell"]}
        assert kas_only_worker_fields("atlas-0123456789ab", "claude") == ()

    def test_a_native_tool_limit_needs_kas_below_the_adapter_options_floor(
        self, spec, _floor_monkeypatch
    ):
        from kiro_crew.mate_worker import kas_only_worker_fields

        _floor_monkeypatch.setattr(
            "kiro_crew.agent_sdk.drivers.acp.claude_adapter_honours_session_options",
            lambda: False,
        )
        spec["spec"] = {"excludedTools": ["shell"]}
        assert kas_only_worker_fields("atlas-0123456789ab", "claude") == ("tool limits",)
