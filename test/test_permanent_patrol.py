"""What ``monitor_start`` bounds a loop with when the model picks none.

A gated loop arms with no cycle cap and the operator's runtime ceiling (7 days as
shipped) as its one runaway backstop; a loop that re-injects every interval keeps
the default cap. Explicit bounds are honoured, except on a ``watch="work-ledger"``
patrol, which takes none from the model (rfc-permanent-patrol.md).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew import conductor_patrol, mcp_core
from kiro_crew.mcp_tools import control
from kiro_crew.mcp_tools._limits import _MONITOR_DEFAULT_MAX_CYCLES
from kiro_crew.monitoring.limits import runtime_ceiling_secs


@pytest.fixture()
def emitted(monkeypatch) -> list[dict[str, Any]]:
    monkeypatch.setattr(mcp_core, "_autonudge_binding_key", lambda sk: "chat-1-123")
    monkeypatch.setattr(mcp_core, "_current_session_key", lambda: "dashboard:1", raising=False)
    payloads: list[dict[str, Any]] = []

    def _capture(kind: str, args: dict[str, Any], human: str) -> str:
        payloads.append(args)
        return human

    monkeypatch.setattr(control, "_emit_directive", _capture)
    return payloads


def test_a_work_ledger_patrol_ignores_the_bounds_it_was_given(emitted):
    out = control.monitor_start(
        "monitor_start",
        {
            "message": "Patrol the ledger.",
            "interval_secs": 600,
            "max_cycles": 24,
            "max_runtime_secs": 3600,
            "watch": "work-ledger",
        },
    )
    (payload,) = emitted
    assert payload["max_cycles"] == 0
    assert payload["max_runtime_secs"] == runtime_ceiling_secs()
    assert "with NO cycle cap" in out


@pytest.mark.parametrize("interval", [None, 90, 900])
def test_a_work_ledger_patrol_arms_at_the_one_patrol_interval(emitted, interval):
    """A self-armed patrol matches the gateway's default one: the model's interval,
    or none, arms at ``PATROL_INTERVAL_SECS`` (300 s)."""
    args: dict[str, Any] = {"message": "Patrol the ledger.", "watch": "work-ledger"}
    if interval is not None:
        args["interval_secs"] = interval
    out = control.monitor_start("monitor_start", args)
    (payload,) = emitted
    assert payload["idle_secs"] == conductor_patrol.PATROL_INTERVAL_SECS == 300
    assert "every 300s" in out


def test_a_loop_without_the_ledger_watch_keeps_its_own_interval(emitted):
    control.monitor_start("monitor_start", {"message": PR, "interval_secs": 90})
    (payload,) = emitted
    assert payload["idle_secs"] == 90


PR = "Watch https://github.com/acme/widgets/pull/42 for CI."


def test_a_gated_loop_with_no_bounds_has_no_cap_and_the_ceiling_runtime(emitted):
    control.monitor_start("monitor_start", {"message": PR})
    (payload,) = emitted
    assert payload["gate"] is True
    assert payload["max_cycles"] == 0
    assert payload["max_runtime_secs"] == runtime_ceiling_secs()


def test_explicit_bounds_are_still_honoured(emitted):
    control.monitor_start(
        "monitor_start", {"message": PR, "max_cycles": 30, "max_runtime_secs": 3600}
    )
    (payload,) = emitted
    assert payload["max_cycles"] == 30
    assert payload["max_runtime_secs"] == 3600


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"message": PR, "gate": False}, id="gate-false"),
        pytest.param({"message": "Refresh the heartbeat file."}, id="nothing-to-observe"),
    ],
)
def test_a_loop_that_wakes_every_interval_keeps_the_default_cap(emitted, args):
    control.monitor_start("monitor_start", args)
    (payload,) = emitted
    assert payload["max_cycles"] == _MONITOR_DEFAULT_MAX_CYCLES
    assert payload["max_runtime_secs"] == runtime_ceiling_secs()
    # Only a gated default leaves the cap for the applier to reconsider.
    assert payload.get("max_cycles_default", False) is (args.get("gate", True) is True)


@pytest.mark.asyncio
@pytest.mark.parametrize(("observed", "cap"), [(object(), 0), (None, _MONITOR_DEFAULT_MAX_CYCLES)])
async def test_the_applier_drops_a_default_cap_when_the_loop_is_gated_after_all(observed, cap):
    """The tool's replay reads no session log, so a bare ``PR <n>`` reaches the
    applier with the default cap; the applier re-reads the log and decides."""
    from kiro_crew.dashboard import session_directive_apply as sda

    authz = AsyncMock(return_value=(SimpleNamespace(id="loop-1"), None, 200))
    with (
        patch.object(sda, "_binding", return_value="chat-9-1"),
        patch("kiro_crew.autonudge.get_instance", return_value=MagicMock()),
        patch("kiro_crew.autonudge.read_session_texts", return_value=["acme/widgets#42"]),
        patch("kiro_crew.autonudge.infer_monitor", return_value=observed),
        patch("kiro_crew.autonudge_authz.authorize_and_add_nudge", authz),
    ):
        await sda._monitor_start(
            MagicMock(),
            "chat-9-1",
            {"message": "Watch PR 42.", "max_cycles": 24, "max_cycles_default": True},
            producer_is_channel=False,
        )
    assert authz.await_args.kwargs["max_cycles"] == cap


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "patch_in", "idle"),
    [
        pytest.param("", {"watch": "work-ledger", "idle_secs": 1800}, 300, id="adds-the-watch"),
        pytest.param("work-ledger", {"idle_secs": 900}, 300, id="retimes-a-ledger-loop"),
        pytest.param("", {"idle_secs": 900}, 900, id="plain-loop-keeps-its-own"),
    ],
)
async def test_monitor_update_holds_a_ledger_patrol_at_the_one_interval(kind, patch_in, idle):
    """The repair the prompt prescribes, ``monitor_update(watch="work-ledger")``, and a
    later interval edit on a ledger loop both land at ``PATROL_INTERVAL_SECS``."""
    from kiro_crew.dashboard import session_directive_apply as sda

    loop = SimpleNamespace(
        id="loop-1",
        active=True,
        gate=True,
        goal_token="",
        cycle_count=0,
        max_cycles=0,
        monitor=SimpleNamespace(kind=kind) if kind else None,
    )
    svc = MagicMock()
    svc.get_by_slot.return_value = loop
    authz = AsyncMock(return_value=(loop, None, 200))
    with (
        patch.object(sda, "_binding", return_value="chat-9-1"),
        patch("kiro_crew.autonudge.get_instance", return_value=svc),
        patch("kiro_crew.autonudge_authz.authorize_and_update_nudge", authz),
    ):
        await sda._monitor_update(MagicMock(_slots={}), "chat-9-1", {"patch": dict(patch_in)})
    assert authz.await_args.kwargs["idle_secs"] == idle
