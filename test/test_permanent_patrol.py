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

from kiro_crew import mcp_core
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
