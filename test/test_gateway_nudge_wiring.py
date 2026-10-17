"""The AutoNudge wiring ``gateway_runtime/nudge_wiring.py`` hands the service.

``_init_autonudge`` builds the closures ``AutoNudgeService`` and the monitor
controller call back into: the fire and monitor dispatchers that route a loop to
its channel's adapter, the observer, the monitor tick, and the wake judge's
evidence collector. These tests drive each closure through the arguments the
service is constructed with, so they pin what a loop's key routes to and what
the collector reads, wherever the code lives.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any, NamedTuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import autonudge_judge
from kiro_crew.autonudge import NudgeLoop
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import session_control
from kiro_crew.monitoring.models import MonitorDispatchResult
from kiro_crew.slack import gateway as gw


def _orchestrator() -> Any:
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        return gw.GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)


def _loop(key: str, **kwargs: Any) -> NudgeLoop:
    return NudgeLoop(id=kwargs.pop("id", "loop-1"), slot_key=key, message="keep checking", **kwargs)


class _Wired(NamedTuple):
    service_kwargs: dict[str, Any]
    observer: Any
    monitor_dispatch: Any
    controller: MagicMock


async def _wire(orch: Any) -> _Wired:
    """Run ``_init_autonudge`` against a recording service and controller."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch("kiro_crew.slack.gateway.autonudge_enabled", return_value=True))
        service_cls = stack.enter_context(patch("kiro_crew.slack.gateway.AutoNudgeService"))
        controller_cls = stack.enter_context(
            patch("kiro_crew.monitoring.controller.MonitorController")
        )
        service = MagicMock()
        service.start = AsyncMock()
        service.list_all.return_value = []
        service_cls.return_value = service
        controller = MagicMock()
        controller.tick = AsyncMock()
        controller_cls.return_value = controller
        await orch._init_autonudge()
    return _Wired(
        service_cls.call_args.kwargs,
        service.subscribe.call_args.args[0],
        controller_cls.call_args.args[1],
        controller,
    )


# ── the dispatchers route by the loop's key ──────────────────────────────────


@pytest.mark.asyncio
async def test_a_webex_loop_fires_through_the_webex_adapter() -> None:
    orch = _orchestrator()
    orch._fire_webex_nudge = AsyncMock(return_value=True)
    wired = await _wire(orch)
    loop = _loop("webex:room-1")

    assert await wired.service_kwargs["on_fire"](loop) is True
    orch._fire_webex_nudge.assert_awaited_once_with(loop)


@pytest.mark.parametrize(
    ("key", "adapter", "who"),
    [
        ("slack:111.222", "_fire_slack_nudge", "Slack"),
        ("discord:kirocrew:direct:U9", "_fire_discord_nudge", "Discord"),
        ("chat-1", "_fire_dashboard_nudge", "Dashboard"),
    ],
)
@pytest.mark.asyncio
async def test_an_untyped_monitor_dispatch_answers_unavailable(
    key: str, adapter: str, who: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A dispatcher that answers a bare bool is reported, never trusted as dispatched."""
    orch = _orchestrator()
    setattr(orch, adapter, AsyncMock(return_value=True))
    wired = await _wire(orch)

    with caplog.at_level("ERROR", logger="kiro_crew.slack.gateway"):
        result = await wired.monitor_dispatch(_loop(key), "envelope")

    assert result is MonitorDispatchResult.UNAVAILABLE
    getattr(orch, adapter).assert_awaited_once()
    assert f"{who} monitor dispatcher returned an untyped result" in caplog.text


@pytest.mark.asyncio
async def test_a_channel_monitor_with_no_dispatcher_is_unavailable() -> None:
    orch = _orchestrator()
    wired = await _wire(orch)
    result = await wired.monitor_dispatch(_loop("webex:room-1"), "envelope")
    assert result is MonitorDispatchResult.UNAVAILABLE


# ── the observer and the monitor tick ────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_observer_tells_the_user_a_loop_is_held() -> None:
    orch = _orchestrator()
    orch._notify_nudge_held = MagicMock(return_value=True)
    wired = await _wire(orch)
    loop = _loop("chat-1")

    wired.observer("held", loop)

    orch._notify_nudge_held.assert_called_once_with(loop)


@pytest.mark.asyncio
async def test_the_monitor_tick_reaches_the_controller() -> None:
    orch = _orchestrator()
    wired = await _wire(orch)
    loop = _loop("chat-1")

    await wired.service_kwargs["on_monitor_tick"](loop)

    wired.controller.tick.assert_awaited_once()
    assert wired.controller.tick.await_args.args == (loop,)


# ── the wake judge's evidence collector ──────────────────────────────────────


@pytest.mark.asyncio
async def test_the_collector_reads_nothing_without_a_dashboard() -> None:
    orch = _orchestrator()
    orch.dashboard_state = None
    wired = await _wire(orch)

    assert await wired.service_kwargs["collect_judge_evidence"](_loop("chat-1")) == ([], 0, {})


def _judge_seams(monkeypatch: pytest.MonkeyPatch, **overrides: Any) -> dict[str, Any]:
    """Pin the judge helpers the collector calls, and record what its readers return."""
    seen: dict[str, Any] = {}

    async def _collect(targets, *, read_session, read_pr, cursors):  # type: ignore[no-untyped-def]
        seen["targets"] = list(targets)
        seen["cursors_in"] = dict(cursors)
        seen["session"] = await read_session("chat-9", 4)
        seen["pr"] = await read_pr("org/repo#7")
        return [{"evidence": True}], 1

    def _parse_targets(spec: Any, message: str, *, watched: str) -> list[str]:
        return ["chat-9", "org/repo#7"]

    defaults: dict[str, Any] = {
        "parse_targets": _parse_targets,
        "spec_of": lambda loop: {},
        "watched_pr_subject": lambda loop: "",
        "collect_evidence": _collect,
        "pr_target_is_unread": lambda observed: False,
        "pr_observation_is_about": lambda target, **kw: True,
        "take_pr_bodies": lambda loop_id: ({}, False),
        "payload_for_judge": lambda observed, stashed, dropped: dict(observed),
    }
    defaults.update(overrides)
    for name, value in defaults.items():
        monkeypatch.setattr(autonudge_judge, name, value)
    return seen


@pytest.mark.asyncio
async def test_the_collector_reads_the_session_page_the_judge_retains(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    orch = _orchestrator()
    orch.dashboard_state = MagicMock()
    calls: list[dict[str, Any]] = []

    def _read_messages(state: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append({"state": state, **kwargs})
        return {"messages": [{"role": "assistant", "content": "done"}], "next_since": 9, "total": 3}

    monkeypatch.setattr(session_control, "read_messages", _read_messages)
    seen = _judge_seams(monkeypatch)
    monitor = SimpleNamespace(
        last_observation={"status": "open"}, kind="pr", target="org/repo#7", last_observed_at=120.5
    )
    loop = _loop("chat-1", monitor=monitor, judge_cursors={"chat-9": 4, "departed": 2})
    wired = await _wire(orch)

    evidence, dropped, cursors = await wired.service_kwargs["collect_judge_evidence"](loop)

    assert (evidence, dropped) == ([{"evidence": True}], 1)
    # Cursors are pruned to the targets this tick reads, and returned, not stored.
    assert cursors == {"chat-9": 4}
    assert seen["cursors_in"] == {"chat-9": 4}
    assert seen["session"] == ([{"role": "assistant", "content": "done"}], 9, 3)
    assert calls == [
        {
            "state": orch.dashboard_state,
            "caller_session_key": "chat-1",
            "target": "chat-9",
            "limit": autonudge_judge.MAX_ROWS_PER_TARGET,
            "since": 4,
        }
    ]
    # The pull-request reading is the monitor's own, aged by its sibling field.
    assert seen["pr"] == {"status": "open", "observed_at": 120.5}


@pytest.mark.parametrize(
    ("overrides", "monitor"),
    [
        ({}, None),
        ({}, SimpleNamespace(last_observation=None, kind="pr", target="t")),
        ({"pr_target_is_unread": lambda observed: True}, "observed"),
        ({"pr_observation_is_about": lambda target, **kw: False}, "observed"),
        ({"payload_for_judge": lambda observed, stashed, dropped: None}, "observed"),
    ],
    ids=["no-monitor", "no-reading", "unread", "other-subject", "bodies-lost"],
)
@pytest.mark.asyncio
async def test_the_pr_reader_counts_an_unusable_reading_as_unread(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, Any], monitor: Any
) -> None:
    orch = _orchestrator()
    orch.dashboard_state = MagicMock()
    monkeypatch.setattr(
        session_control, "read_messages", lambda state, **kw: {"messages": [], "next_since": None}
    )
    seen = _judge_seams(monkeypatch, **overrides)
    if monitor == "observed":
        monitor = SimpleNamespace(
            last_observation={"status": "open"}, kind="pr", target="org/repo#7", last_observed_at=0
        )
    wired = await _wire(orch)

    await wired.service_kwargs["collect_judge_evidence"](_loop("chat-1", monitor=monitor))

    assert seen["pr"] is None
    # An empty page keeps the reader's own cursor and reports no total.
    assert seen["session"] == ([], 4, None)
