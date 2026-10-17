"""A changed ``agent.default_agent`` reaches the channels without a gateway restart.

The default agent is what a new thread in Slack runs when nothing names one. The
handler kept it in ``_cached_default_agent``, loaded once and written only by the
local ``!agent`` command, so a write from the dashboard, ``kirocrew config set``
or an editor never reached a running gateway.

Every test moves the value through a real ``ConfigWatch`` reload and the appliers
the gateway registered (their own prefixes and callbacks, not copies). The reload
tests rewrite a real ``config.json``. The tests for a discarded section hand the
watcher the loader's answer for one, which is the defaults plus the degraded flag.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Sequence
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from _hot_reload_helpers import point_loader_at, sel_outcomes, write_config

import kiro_crew.slack.gateway as gw
from kiro_crew.config.live import ConfigWatch, Subscription
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.slack import handler as slack_handler
from kiro_crew.slack.gateway import GatewayOrchestrator

_AUTHORIZATION = "slack.authorization_config_change"


def _orchestrator() -> GatewayOrchestrator:
    """A Slack-less orchestrator: its appliers are registered, nothing connects."""
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        return GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)


def _covers(sub: Subscription, path: str) -> bool:
    return any(path == prefix or path.startswith(prefix + ".") for prefix in sub.prefixes)


def _watch_of(orch: GatewayOrchestrator, *paths: str) -> tuple[ConfigWatch, list[Subscription]]:
    """A watcher of its own carrying the orchestrator's subscriptions that cover *paths*.

    Each keeps the prefixes and the applier the gateway registered, and no other
    subscriber (the session manager's) runs."""
    watch = ConfigWatch(poll_interval_secs=0.05)
    subs = [
        watch.subscribe(*registered.prefixes, callback=registered.callback(), name=registered.name)
        for registered in orch._config_subs
        if any(_covers(registered, path) for path in paths)
    ]
    return watch, subs


def _covering(subs: Sequence[Subscription], path: str) -> Subscription:
    found = [sub for sub in subs if _covers(sub, path)]
    assert found, f"no subscription of the gateway covers {path}"
    return found[0]


def _config(
    *,
    open_channels: Sequence[str] = (),
    default_agent: str = "",
    discarded: Iterable[str] = (),
) -> KiroCrewConfig:
    """What the loader answers: the two settings these tests move, and what it discarded."""
    base = KiroCrewConfig()
    return replace(
        base,
        slack=replace(base.slack, open_channels=list(open_channels)),
        agent=replace(base.agent, default_agent=default_agent),
        _degraded_sections=frozenset(discarded),
    )


class _Rig:
    """The gateway's Slack and default-agent appliers, fed one load per cycle."""

    def __init__(self, orch: GatewayOrchestrator) -> None:
        self.orch = orch
        self.watch, self.subs = _watch_of(orch, "slack.open_channels", "agent.default_agent")
        self.served = _config(open_channels=["C_OLD"], default_agent="alpha")
        self.pushed: list[set[str]] = []
        self.audited: list[dict] = []
        self.watch.prime(self.served)

    @property
    def slack(self) -> Subscription:
        return _covering(self.subs, "slack.open_channels")

    @property
    def agent(self) -> Subscription:
        return _covering(self.subs, "agent.default_agent")

    @property
    def authorizations(self) -> int:
        return len(sel_outcomes(self.audited, _AUTHORIZATION))

    def stale(self, sub: Subscription) -> frozenset[str]:
        """The paths the watcher holds as not yet adopted by *sub*."""
        held = self.watch._stale.get(id(sub))
        return held[1] if held else frozenset()

    async def reload(self, cfg: KiroCrewConfig) -> None:
        """One watcher cycle whose load answers *cfg*."""
        self.served = cfg
        await asyncio.wait_for(self.watch.refresh_now(), timeout=30)


@pytest.fixture
def rig(tmp_path, monkeypatch) -> _Rig:
    point_loader_at(tmp_path, monkeypatch, {})
    monkeypatch.setattr(slack_handler, "_cached_default_agent", "alpha")
    orch = _orchestrator()
    orch._tracking_channels = set()
    orch._open_channels = set()
    gateway_rig = _Rig(orch)
    monkeypatch.setattr(
        slack_handler, "set_open_channels", lambda ids: gateway_rig.pushed.append(set(ids))
    )
    monkeypatch.setattr(slack_handler, "adopt_slack_config", lambda fresh: None)
    monkeypatch.setattr(
        gw,
        "sel",
        lambda: SimpleNamespace(log_api_access=lambda **kw: gateway_rig.audited.append(kw)),
    )
    monkeypatch.setattr(ConfigWatch, "_load", staticmethod(lambda: gateway_rig.served))
    return gateway_rig


@pytest.mark.asyncio
async def test_a_default_agent_written_while_running_reaches_the_next_slack_thread(
    tmp_path, monkeypatch
):
    cfg_path = point_loader_at(tmp_path, monkeypatch, {"agent": {"default_agent": "alpha"}})
    monkeypatch.setattr(slack_handler, "_cached_default_agent", None)
    orch = _orchestrator()
    watch, _ = _watch_of(orch, "agent.default_agent")
    watch.prime(KiroCrewConfig.load())

    assert slack_handler._get_agent_for_session("slack:T1:1") == "alpha"

    write_config(cfg_path, {"agent": {"default_agent": "beta"}})
    reload = await asyncio.wait_for(watch.refresh_now(), timeout=30)

    assert reload is not None and reload.touched("agent.default_agent")
    assert slack_handler._get_agent_for_session("slack:T2:2") == "beta"


@pytest.mark.asyncio
async def test_a_thread_override_still_wins_over_a_changed_default(tmp_path, monkeypatch):
    cfg_path = point_loader_at(tmp_path, monkeypatch, {"agent": {"default_agent": "alpha"}})
    monkeypatch.setattr(slack_handler, "_cached_default_agent", None)
    monkeypatch.setitem(slack_handler._thread_agents, "slack:T3:3", "pinned")
    orch = _orchestrator()
    watch, _ = _watch_of(orch, "agent.default_agent")
    watch.prime(KiroCrewConfig.load())

    assert slack_handler._get_agent_for_session("slack:T0:0") == "alpha"
    write_config(cfg_path, {"agent": {"default_agent": "beta"}})
    await asyncio.wait_for(watch.refresh_now(), timeout=30)

    assert slack_handler._get_agent_for_session("slack:T3:3") == "pinned"
    assert slack_handler._get_agent_for_session("slack:T4:4") == "beta"


@pytest.mark.asyncio
async def test_a_discarded_agent_section_keeps_the_default_and_defers_only_its_own_path(rig):
    """A discarded section holds DEFAULTS; adopting them would drop the configured agent.

    The ``slack.*`` change that arrived in the same reload is still applied once, and
    only the agent path is handed back to the watcher to retry."""
    await rig.reload(_config(open_channels=["C_NEW"], discarded={"agent"}))

    assert slack_handler._cached_default_agent == "alpha"
    assert rig.pushed == [{"C_NEW"}]
    assert rig.stale(rig.agent) == {"agent.default_agent"}
    assert rig.stale(rig.slack) == frozenset()


@pytest.mark.asyncio
@pytest.mark.parametrize("repaired_first", ["slack", "agent"])
async def test_each_discarded_section_is_retried_on_its_own(rig, repaired_first):
    """Repairing one discarded section applies it once and leaves the other's paths alone.

    One reload discards both sections, so both are deferred. One is then repaired while
    the other stays discarded. A deferral the watcher merges into the repaired side's
    stale paths would keep them stale, and its work (for Slack, an authorization audit
    record and the enterprise-id reload) would run again on every tick until the other
    section validates."""
    slack_first = repaired_first == "slack"
    first = (
        _config(open_channels=["C_NEW"], discarded={"agent"})
        if slack_first
        else _config(default_agent="beta", discarded={"slack"})
    )
    repaired, discarded = (rig.slack, rig.agent) if slack_first else (rig.agent, rig.slack)

    await rig.reload(_config(discarded={"slack", "agent"}))

    assert slack_handler._cached_default_agent == "alpha"
    assert rig.pushed == [] and rig.authorizations == 0
    assert rig.stale(rig.slack) >= {"slack.open_channels"}
    assert rig.stale(rig.agent) >= {"agent.default_agent"}

    for _ in range(3):  # the repair, then two quiet ticks
        await rig.reload(first)

    assert rig.pushed == ([{"C_NEW"}] if slack_first else [])
    assert rig.authorizations == (1 if slack_first else 0)
    assert slack_handler._cached_default_agent == ("alpha" if slack_first else "beta")
    assert rig.stale(repaired) == frozenset()
    assert rig.stale(discarded) == (
        {"agent.default_agent"} if slack_first else {"slack.open_channels"}
    )

    await rig.reload(_config(open_channels=["C_NEW"], default_agent="beta"))

    assert rig.watch._stale == {}
    assert slack_handler._cached_default_agent == "beta"
    assert rig.pushed == [{"C_NEW"}]
    assert rig.authorizations == 1


@pytest.mark.asyncio
async def test_an_unrelated_agent_change_leaves_the_default_alone(tmp_path, monkeypatch):
    cfg_path = point_loader_at(tmp_path, monkeypatch, {"agent": {"default_agent": "beta"}})
    monkeypatch.setattr(slack_handler, "_cached_default_agent", "alpha")
    orch = _orchestrator()
    watch, _ = _watch_of(orch, "agent.default_agent")
    watch.prime(KiroCrewConfig.load())

    write_config(cfg_path, {"agent": {"default_agent": "beta", "max_channels": 2}})
    reload = await asyncio.wait_for(watch.refresh_now(), timeout=30)

    assert reload is not None and reload.touched("agent.max_channels")
    assert slack_handler._cached_default_agent == "alpha"
