"""Whether anything on a gateway runs on KAS, so the dashboard offers the Kiro sign-in.

``crewmate_harness.kas_runs_here`` is the one rule the config read's ``kas_in_use``
carries, and the dashboard shows every Kiro sign-in shortcut on it. A Kiro CLI main
harness still runs its Bots on KAS, so it must answer True there too.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew.agent_sdk import backends as registry
from kiro_crew.config import loader_builders
from kiro_crew.config.loader_builders import _build_agent_config

SELECTABLE = {"", "kas", "claude", "codex", "goose"}


@pytest.fixture(autouse=True)
def _registry(_floor_monkeypatch):
    _floor_monkeypatch.setattr(registry, "_baseline", set(SELECTABLE))
    _floor_monkeypatch.setattr(registry, "_selectable", set(SELECTABLE))
    _floor_monkeypatch.setattr(loader_builders, "_member_fallback_logged", set())
    from kiro_crew import crewmate_harness

    _floor_monkeypatch.setattr(crewmate_harness, "_VERDICTS", {})
    _floor_monkeypatch.setattr(crewmate_harness, "_REFRESHING", set())


def _cfg(agent: dict, pins: dict[str, str] | None = None) -> SimpleNamespace:
    from kiro_crew.config.loader import KiroCrewAgentConfig

    agents = {
        name: KiroCrewAgentConfig(
            member_id=f"{name}-0123456789ab", kiro_agent=name, acp_backend=pin
        )
        for name, pin in ({"atlas": ""} if pins is None else pins).items()
    }
    return SimpleNamespace(agent=_build_agent_config(agent), agents=agents)


@pytest.mark.parametrize(
    ("agent", "pins", "expected"),
    [
        # The main harness is KAS.
        ({"acp_backend": "kas"}, None, True),
        # Kiro CLI main harness, Bot harness unset: Bots run on KAS.
        ({"acp_backend": "kiro"}, None, True),
        ({"acp_backend": "kiro", "member_acp_backend": ""}, None, True),
        # A stored Bot harness of KAS, or of Kiro CLI, which Bots run as KAS.
        ({"acp_backend": "claude", "member_acp_backend": "kas"}, None, True),
        ({"acp_backend": "claude", "member_acp_backend": "kiro"}, None, True),
        # A main harness Bots cannot run on sends them to KAS.
        ({"acp_backend": "goose"}, None, True),
        # One Bot pinned to KAS, or to a harness Bots cannot run on.
        (
            {"acp_backend": "claude", "member_acp_backend": "claude"},
            {"atlas": "", "nova": "kas"},
            True,
        ),
        ({"acp_backend": "claude", "member_acp_backend": "claude"}, {"nova": "goose"}, True),
        # Nothing on KAS: the main and Bot harness are Claude, no Bot pins KAS.
        ({"acp_backend": "claude", "member_acp_backend": "claude"}, None, False),
        ({"acp_backend": "claude"}, {"atlas": "", "nova": "codex"}, False),
        ({"acp_backend": "claude", "member_acp_backend": "claude"}, {}, False),
        # The Bot harness loads as KAS, but no Bot exists to inherit it.
        ({"acp_backend": "kiro"}, {}, False),
        ({"acp_backend": "goose"}, {}, False),
        ({"acp_backend": "claude", "member_acp_backend": "kas"}, {}, False),
        # Every Bot pins a harness other than KAS: nothing inherits the Bot harness.
        ({"acp_backend": "kiro"}, {"atlas": "claude"}, False),
    ],
)
def test_kas_runs_here(agent, pins, expected):
    from kiro_crew.crewmate_harness import kas_runs_here

    assert kas_runs_here(_cfg(agent, pins)) is expected


def test_a_pin_that_cannot_start_does_not_count(_floor_monkeypatch):
    from kiro_crew.crewmate_harness import kas_runs_here

    _floor_monkeypatch.setattr(registry, "_selectable", {"", "claude"})
    cfg = _cfg({"acp_backend": "claude", "member_acp_backend": "claude"}, {"nova": "kas"})
    assert kas_runs_here(cfg) is False


def test_nothing_runs_on_kas_where_policy_denies_it(_floor_monkeypatch):
    from kiro_crew.crewmate_harness import kas_runs_here

    _floor_monkeypatch.setattr(registry, "_selectable", {"", "claude", "goose"})
    cfg = _cfg({"acp_backend": "claude", "member_acp_backend": "claude"}, {"nova": "goose"})
    assert kas_runs_here(cfg) is False


def test_a_bot_whose_worker_spec_needs_kas_counts(_floor_monkeypatch):
    from kiro_crew import crewmate_harness

    _floor_monkeypatch.setattr(
        crewmate_harness, "worker_spec_kas_fields", lambda _c, _n: ("hooks",)
    )
    assert crewmate_harness.kas_runs_here(_cfg({"acp_backend": "claude"})) is True


def _get_config(monkeypatch, cfg) -> dict:
    """The body of ``GET /api/config/kirocrew`` for *cfg*, through the real handler."""
    import asyncio
    import json
    from unittest.mock import MagicMock

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.dashboard.handlers import api_kirocrew_config

    monkeypatch.setattr(KiroCrewConfig, "load", classmethod(lambda _cls, *a, **k: cfg))
    # The masking pass reads the platform context, which is not under test here.
    monkeypatch.setattr(
        "kiro_crew.dashboard.handlers.core_config_view._masked_config_dict",
        lambda c: {"agent": {"acp_backend": c.agent.acp_backend}},
    )
    req = MagicMock()
    req.method = "GET"
    return json.loads(asyncio.run(api_kirocrew_config(req)).body)


def test_the_config_read_carries_the_answer(_floor_monkeypatch, monkeypatch):
    from kiro_crew.config.loader import KiroCrewConfig

    cfg = KiroCrewConfig()
    cfg.agent = _build_agent_config({"acp_backend": "kiro"})
    assert _get_config(monkeypatch, cfg)["kas_in_use"] is False  # no Bots
    cfg.agents = _cfg({}, {"atlas": ""}).agents
    assert _get_config(monkeypatch, cfg)["kas_in_use"] is True
    cfg.agent = _build_agent_config({"acp_backend": "claude", "member_acp_backend": "claude"})
    assert _get_config(monkeypatch, cfg)["kas_in_use"] is False


def test_the_config_read_takes_a_fresh_worker_spec_answer(_floor_monkeypatch, monkeypatch):
    """On the event loop the worker-spec answer is only the held one, empty until a
    background refresh lands; the read asks off the loop, where it is fresh."""
    from kiro_crew import crewmate_harness
    from kiro_crew.config.loader import KiroCrewConfig

    _floor_monkeypatch.setattr(crewmate_harness, "_held_verdict", lambda _k: ((), ()))
    _floor_monkeypatch.setattr(crewmate_harness, "_fresh_verdict", lambda _k: (("hooks",), ()))
    cfg = KiroCrewConfig()
    cfg.agent = _build_agent_config({"acp_backend": "claude"})
    cfg.agents = _cfg({}, {"atlas": ""}).agents
    assert _get_config(monkeypatch, cfg)["kas_in_use"] is True


def test_the_config_read_survives_a_failing_answer(_floor_monkeypatch, monkeypatch):
    from kiro_crew import crewmate_harness
    from kiro_crew.config.loader import KiroCrewConfig

    def boom(_cfg):
        raise RuntimeError("unreadable")

    _floor_monkeypatch.setattr(crewmate_harness, "kas_runs_here", boom)
    body = _get_config(monkeypatch, KiroCrewConfig())
    assert "kas_in_use" not in body and body["agent"]
