"""The unattended policy-provider apply copies memory first, or skips the update.

`_check_for_updates_via_provider` calls ``provider.apply()`` at two sites: the
mandatory min_version floor and the voluntary auto-update. Each must take the same
pre-update memory copy `kirocrew update` takes, before the provider rewrites the
install, and a copy that did not land skips the update without applying.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _make_orchestrator, _mock_dashboard_state

_SNAPSHOT = "kiro_crew.platform.wheel_apply.snapshot_memory_before_update"


def _provider(order: list[str]):
    from kiro_crew.platform.update_provider import CommandProvider, UpdateCheckResult

    provider = CommandProvider(check_command="c", apply_command="a")
    provider.check = AsyncMock(  # type: ignore[method-assign]
        return_value=UpdateCheckResult(available=True, remote_version="9.9.9")
    )

    async def _apply() -> bool:
        order.append("apply")
        return True

    provider.apply = AsyncMock(side_effect=_apply)  # type: ignore[method-assign]
    return provider


def _orchestrator(effect: str):
    from kiro_crew.platform.update_capability import AUTO_ROUTE_PROVIDER, AutoUpdateEffect

    orch = _make_orchestrator()
    orch.dashboard_state = _mock_dashboard_state()
    orch._auto_update_effect = AsyncMock(return_value=AutoUpdateEffect(effect, AUTO_ROUTE_PROVIDER))
    orch._prepare_auto_update_apply = AsyncMock(return_value=True)
    orch._finish_auto_update_apply = AsyncMock()
    orch._restart_after_update = AsyncMock()
    return orch


async def _run(monkeypatch, *, mandatory: bool, failure: str, order: list[str]):
    import kiro_crew.dashboard.handlers as handlers
    import kiro_crew.platform.update_governance as gov

    orch = _orchestrator("mandatory" if mandatory else "install")
    provider = _provider(order)
    handlers._update_info.clear()
    monkeypatch.setattr(gov, "update_required", lambda _v: mandatory)
    monkeypatch.setattr(gov, "min_version", lambda: "9.0.0")
    cfg = MagicMock()
    cfg.auto_update = True

    def _copy() -> tuple[int, str]:
        order.append("copy")
        return (0, failure) if failure else (2, "")

    with (
        patch("kiro_crew.config.KiroCrewConfig.load", return_value=cfg),
        patch(_SNAPSHOT, side_effect=_copy),
    ):
        await orch._check_for_updates_via_provider(provider)
    return orch, provider


@pytest.mark.parametrize("mandatory", [True, False], ids=["min_version-floor", "auto-update"])
@pytest.mark.asyncio
async def test_the_memory_copy_runs_before_the_provider_apply(monkeypatch, mandatory) -> None:
    order: list[str] = []
    orch, provider = await _run(monkeypatch, mandatory=mandatory, failure="", order=order)

    assert order == ["copy", "apply"]
    orch._restart_after_update.assert_awaited_once()
    orch._finish_auto_update_apply.assert_awaited_once()


@pytest.mark.parametrize("mandatory", [True, False], ids=["min_version-floor", "auto-update"])
@pytest.mark.asyncio
async def test_a_failed_copy_skips_the_apply_and_says_so(monkeypatch, mandatory) -> None:
    order: list[str] = []
    orch, provider = await _run(
        monkeypatch, mandatory=mandatory, failure="1 store(s) not copied", order=order
    )

    assert order == ["copy"]
    provider.apply.assert_not_awaited()
    orch._restart_after_update.assert_not_awaited()
    # The apply slot is still released, and the gateway keeps running.
    orch._finish_auto_update_apply.assert_awaited_once()
    ds = orch.dashboard_state
    ds.notify.assert_called_once()
    assert "Pre-update memory snapshot failed: 1 store(s) not copied" in ds.notify.call_args[0][2]
    ds.push_update_progress.assert_called_with("failed", ds.notify.call_args[0][2])
