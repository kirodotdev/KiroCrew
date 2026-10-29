"""A policy-provider apply that changes nothing does not restart the gateway.

`_check_for_updates_via_provider` runs the check again after ``apply_command``
exits 0. When the check still offers the same version, the install did not
change, and a restart would boot into the same check and apply on every cycle.
Both apply sites (the min_version floor and the voluntary auto-update) report it
and keep running; any other re-check answer lets the restart go ahead.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _make_orchestrator, _mock_dashboard_state

_SNAPSHOT = "kiro_crew.platform.wheel_apply.snapshot_memory_before_update"


async def _run(monkeypatch, *, mandatory: bool, recheck):
    import kiro_crew.dashboard.handlers as handlers
    import kiro_crew.platform.update_governance as gov
    from kiro_crew.platform.update_capability import AUTO_ROUTE_PROVIDER, AutoUpdateEffect
    from kiro_crew.platform.update_provider import CommandProvider, UpdateCheckResult

    orch = _make_orchestrator()
    orch.dashboard_state = _mock_dashboard_state()
    effect = "mandatory" if mandatory else "install"
    orch._auto_update_effect = AsyncMock(return_value=AutoUpdateEffect(effect, AUTO_ROUTE_PROVIDER))
    orch._prepare_auto_update_apply = AsyncMock(return_value=True)
    orch._finish_auto_update_apply = AsyncMock()
    orch._restart_after_update = AsyncMock()

    provider = CommandProvider(check_command="c", apply_command="a")
    provider.check = AsyncMock(  # type: ignore[method-assign]
        side_effect=[UpdateCheckResult(available=True, remote_version="9.9.9"), recheck]
    )
    provider.apply = AsyncMock(return_value=True)  # type: ignore[method-assign]

    original = dict(handlers._update_info)
    monkeypatch.setattr(gov, "update_required", lambda _v: mandatory)
    monkeypatch.setattr(gov, "min_version", lambda: "9.0.0")
    cfg = MagicMock()
    cfg.auto_update = True
    try:
        handlers._update_info.clear()
        with (
            patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg),
            patch(_SNAPSHOT, return_value=(1, "")),
        ):
            await orch._check_for_updates_via_provider(provider)
    finally:
        handlers._update_info.clear()
        handlers._update_info.update(original)
    return orch, provider


@pytest.mark.parametrize("mandatory", [True, False], ids=["min_version-floor", "auto-update"])
@pytest.mark.asyncio
async def test_an_apply_the_check_still_offers_again_does_not_restart(monkeypatch, mandatory):
    from kiro_crew.platform.update_provider import UpdateCheckResult

    orch, provider = await _run(
        monkeypatch,
        mandatory=mandatory,
        recheck=UpdateCheckResult(available=True, remote_version="9.9.9"),
    )

    provider.apply.assert_awaited_once()
    assert provider.check.await_count == 2
    orch._restart_after_update.assert_not_awaited()
    orch._finish_auto_update_apply.assert_awaited_once()
    ds = orch.dashboard_state
    failed = [c for c in ds.push_update_progress.call_args_list if c.args[0] == "failed"]
    assert failed and "did not change" in failed[-1].args[1]
    ds.notify.assert_called_once()
    assert ds.notify.call_args.args[1] == "Kiro Crew update changed nothing"


@pytest.mark.parametrize("mandatory", [True, False], ids=["min_version-floor", "auto-update"])
@pytest.mark.parametrize(
    "recheck",
    [
        pytest.param({"available": False}, id="installed"),
        pytest.param({"available": True, "remote_version": "10.0.0"}, id="newer-offered"),
        pytest.param({"error": "check_command timed out"}, id="recheck-errored"),
    ],
)
@pytest.mark.asyncio
async def test_an_apply_that_changed_the_install_restarts(monkeypatch, mandatory, recheck):
    from kiro_crew.platform.update_provider import UpdateCheckResult

    orch, provider = await _run(
        monkeypatch, mandatory=mandatory, recheck=UpdateCheckResult(**recheck)
    )

    provider.apply.assert_awaited_once()
    orch._restart_after_update.assert_awaited_once()
    orch.dashboard_state.notify.assert_not_called()
