"""``POST /api/update`` ARMS the update for the next start; it installs nothing.

Maintainer rulings 3 and 4 (decision entry "updates never restart a running
gateway"): clicking Update must not restart the user's gateway, and must not start
the Toolbox (or any) update while the gateway runs. These drive the real handler with
every installer, command and restart replaced by a spy that fails the test.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import update_next_start
from kiro_crew.dashboard.handlers import updates
from kiro_crew.dashboard.state import DashboardState


def _request(tmp_path, monkeypatch, body: object | None) -> tuple[MagicMock, DashboardState]:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = DashboardState(
        sessions=MagicMock(count=0), crons=MagicMock(), lessons=MagicMock(), start_time=0.0
    )
    app = web.Application()
    app["state"] = state
    req = MagicMock()
    req.app = app
    claims = {"app": "", "user": str(getattr(state, "owner_id", "") or "") or "local-app"}
    req.__contains__.side_effect = lambda key: key in claims
    req.__getitem__.side_effect = lambda key: claims[key]
    req.get.side_effect = lambda key, *default: claims.get(key, *default)
    req.can_read_body = body is not None
    req.json = AsyncMock(return_value=body)
    return req, state


@pytest.fixture(autouse=True)
def _verdict():
    from kiro_crew.dashboard.handlers import _update_info

    original = dict(_update_info)
    _update_info.clear()
    _update_info.update(
        {"latest_version": "9.9.9", "update_available": True, "check_status": "succeeded"}
    )
    yield
    _update_info.clear()
    _update_info.update(original)


def _install_shape(monkeypatch, managed_by: str, provider: object | None = None) -> None:
    from types import SimpleNamespace

    from kiro_crew.platform import update_provider

    monkeypatch.setattr(
        "kiro_crew.platform.update_capability.derive_capability",
        lambda **_kw: SimpleNamespace(managed_by=managed_by),
    )
    monkeypatch.setattr(update_provider, "resolve_provider", lambda: provider)


@pytest.fixture(autouse=True)
def _git_checkout(_floor_monkeypatch):
    _install_shape(_floor_monkeypatch, "git")


@pytest.fixture
def spies(monkeypatch):
    """Everything an apply or a restart could call; any call fails the test."""
    from kiro_crew import platform_compat
    from kiro_crew.platform import update_provider

    boom = MagicMock(side_effect=AssertionError("an apply or a restart ran while serving"))
    aboom = AsyncMock(side_effect=AssertionError("an apply or a restart ran while serving"))
    monkeypatch.setattr(update_provider, "apply_policy_update", aboom)
    monkeypatch.setattr(update_provider.CommandProvider, "apply", aboom)
    monkeypatch.setattr(platform_compat, "reexec_launcher", boom)
    monkeypatch.setattr(platform_compat, "reexec_python_module", boom)
    monkeypatch.setattr("kiro_crew.dashboard.handlers.updates_apply._restart_gateway", aboom)
    monkeypatch.setattr("asyncio.create_subprocess_exec", aboom)
    monkeypatch.setattr("subprocess.run", boom)
    monkeypatch.setattr("os.execv", boom)
    return boom


async def _post(tmp_path, monkeypatch, body: object | None):
    from kiro_crew.dashboard.handlers import api_update_apply

    req, state = _request(tmp_path, monkeypatch, body)
    resp = await api_update_apply(req)
    return resp, state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [{"when": "next-start"}, None, {}, {"force": True}, {"force": True, "when": "next-start"}],
    ids=["next-start", "no-body", "empty", "legacy-force", "force-with-when"],
)
async def test_a_click_arms_and_changes_nothing_else(tmp_path, monkeypatch, spies, body) -> None:
    resp, state = await _post(tmp_path, monkeypatch, body)

    assert resp.status == 202
    assert json.loads(resp.body) == {"ok": True, "armed": True, "version": "9.9.9"}
    armed = update_next_start.read()
    assert armed.armed and armed.armed_version == "9.9.9"
    spies.assert_not_called()


@pytest.mark.asyncio
async def test_a_click_under_a_live_turn_still_only_arms(tmp_path, monkeypatch, spies) -> None:
    req, state = _request(tmp_path, monkeypatch, {"when": "next-start"})
    state._quiescence_probe = MagicMock(side_effect=AssertionError("Update does not ask"))
    from kiro_crew.dashboard.handlers import api_update_apply

    resp = await api_update_apply(req)

    assert resp.status == 202
    state._quiescence_probe.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_disarms(tmp_path, monkeypatch, spies) -> None:
    await _post(tmp_path, monkeypatch, {"when": "next-start"})

    resp, _state = await _post(tmp_path, monkeypatch, {"when": "cancel"})

    assert resp.status == 200
    assert json.loads(resp.body) == {"ok": True, "armed": False, "disarmed": True}
    assert update_next_start.read().armed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{"when": "now"}, {"when": 3}, ["next-start"], "next-start"])
async def test_any_other_body_is_refused_and_arms_nothing(
    tmp_path, monkeypatch, spies, body
) -> None:
    resp, _state = await _post(tmp_path, monkeypatch, body)

    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "invalid_body"
    assert update_next_start.read().armed is False


@pytest.mark.asyncio
async def test_there_is_nothing_to_arm_when_the_check_found_no_update(
    tmp_path, monkeypatch, spies
) -> None:
    from kiro_crew.dashboard.handlers import _update_info
    from kiro_crew.platform.update_capability import CHECK_SUCCEEDED

    _update_info.update({"update_available": False, "check_status": CHECK_SUCCEEDED})

    resp, _state = await _post(tmp_path, monkeypatch, {"when": "next-start"})

    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "no_update"
    assert update_next_start.read().armed is False


@pytest.mark.asyncio
async def test_before_the_first_check_the_arm_runs_it_and_arms_its_version(
    tmp_path, monkeypatch, spies
) -> None:
    from kiro_crew.dashboard.handlers import _update_info

    _update_info.clear()

    async def _check() -> None:
        _update_info.update(
            {"latest_version": "9.9.9", "update_available": True, "check_status": "succeeded"}
        )

    monkeypatch.setattr(updates, "_do_update_check", _check)

    resp, _state = await _post(tmp_path, monkeypatch, {"when": "next-start"})

    assert resp.status == 202
    assert update_next_start.read().armed_version == "9.9.9"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code"),
    [("unchecked", "update_check_pending"), ("failed", "arm_check_failed")],
)
async def test_an_arm_without_a_check_result_is_refused_and_writes_nothing(
    tmp_path, monkeypatch, spies, status, code
) -> None:
    from kiro_crew.dashboard.handlers import _update_info

    _update_info.clear()
    _update_info.update({"check_status": status})
    monkeypatch.setattr(updates, "_do_update_check", AsyncMock(return_value=None))

    resp, _state = await _post(tmp_path, monkeypatch, {"when": "next-start"})

    assert resp.status == 409
    assert json.loads(resp.body)["code"] == code
    assert update_next_start.read().armed is False
    assert not update_next_start._state_path().exists()


@pytest.mark.asyncio
async def test_a_check_that_names_no_version_arms_nothing(tmp_path, monkeypatch, spies) -> None:
    from kiro_crew.dashboard.handlers import _update_info

    _update_info.update({"latest_version": ""})

    resp, _state = await _post(tmp_path, monkeypatch, {"when": "next-start"})

    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "update_check_pending"
    assert not update_next_start._state_path().exists()


@pytest.mark.asyncio
async def test_a_desktop_hosted_gateway_records_the_version_the_desktop_names(
    tmp_path, monkeypatch, spies
) -> None:
    from kiro_crew.dashboard.handlers import _update_info

    _update_info.clear()
    monkeypatch.setenv("KIROCREW_DESKTOP_GATEWAY", "1")
    check = AsyncMock()
    monkeypatch.setattr(updates, "_do_update_check", check)

    resp, _state = await _post(tmp_path, monkeypatch, {"when": "next-start", "version": "2.0.0"})

    assert resp.status == 202
    assert json.loads(resp.body)["version"] == "2.0.0"
    assert update_next_start.read().armed_version == "2.0.0"
    check.assert_not_called()


@pytest.mark.asyncio
async def test_a_version_from_a_gateway_the_desktop_does_not_host_is_not_trusted(
    tmp_path, monkeypatch, spies
) -> None:
    monkeypatch.delenv("KIROCREW_DESKTOP_GATEWAY", raising=False)

    resp, _state = await _post(tmp_path, monkeypatch, {"when": "next-start", "version": "2.0.0"})

    assert resp.status == 202
    assert update_next_start.read().armed_version == "9.9.9"


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [3, "x" * 65])
async def test_a_malformed_version_is_refused(tmp_path, monkeypatch, spies, version) -> None:
    resp, _state = await _post(tmp_path, monkeypatch, {"version": version})

    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "invalid_body"
    assert update_next_start.read().armed is False


@pytest.mark.asyncio
async def test_a_cancel_naming_the_stamp_disarms_and_a_stale_stamp_does_not(
    tmp_path, monkeypatch, spies
) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    armed = update_next_start.arm("9.9.9")

    resp, _state = await _post(
        tmp_path, monkeypatch, {"when": "cancel", "armed_at": armed.armed_at - 1.0}
    )
    assert resp.status == 409
    assert json.loads(resp.body)["code"] == "arm_changed"
    assert update_next_start.read().armed is True

    resp, _state = await _post(
        tmp_path, monkeypatch, {"when": "cancel", "armed_at": armed.armed_at}
    )
    assert resp.status == 200
    assert update_next_start.read().armed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stamp", ["soon", True, [1]])
async def test_a_malformed_cancel_stamp_is_refused(tmp_path, monkeypatch, spies, stamp) -> None:
    monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    update_next_start.arm("9.9.9")

    resp, _state = await _post(tmp_path, monkeypatch, {"when": "cancel", "armed_at": stamp})

    assert resp.status == 400
    assert json.loads(resp.body)["code"] == "invalid_body"
    assert update_next_start.read().armed is True


@pytest.mark.asyncio
async def test_cancel_needs_no_check_result(tmp_path, monkeypatch, spies) -> None:
    from kiro_crew.dashboard.handlers import _update_info

    _update_info.clear()
    check = AsyncMock()
    monkeypatch.setattr(updates, "_do_update_check", check)

    resp, _state = await _post(tmp_path, monkeypatch, {"when": "cancel"})

    assert resp.status == 200
    check.assert_not_called()


@pytest.mark.asyncio
async def test_a_stop_in_progress_arms_nothing(tmp_path, monkeypatch, spies) -> None:
    with patch("kiro_crew.dashboard.handlers.updates.shutdown_event") as ev:
        ev.is_set.return_value = True
        resp, _state = await _post(tmp_path, monkeypatch, {"when": "next-start"})

    assert resp.status == 503
    assert update_next_start.read().armed is False


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["kirocrew", "container", "none"])
async def test_a_step_up_layout_never_arms_from_this_route(
    tmp_path, monkeypatch, spies, shape
) -> None:
    _install_shape(monkeypatch, shape)

    resp, _state = await _post(tmp_path, monkeypatch, {"when": "next-start"})

    assert resp.status == 409
    assert "Not a git checkout" in json.loads(resp.body)["error"]
    assert not update_next_start.read().armed


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["git", "electron"])
async def test_a_git_checkout_and_the_desktop_app_arm(tmp_path, monkeypatch, spies, shape) -> None:
    _install_shape(monkeypatch, shape)

    resp, _state = await _post(tmp_path, monkeypatch, {"when": "next-start"})

    assert resp.status == 202 and update_next_start.read().armed


@pytest.mark.asyncio
async def test_a_policy_provider_arms_whatever_the_layout(tmp_path, monkeypatch, spies) -> None:
    _install_shape(monkeypatch, "kirocrew", provider=object())

    resp, _state = await _post(tmp_path, monkeypatch, {"when": "next-start"})

    assert resp.status == 202 and update_next_start.read().armed


@pytest.mark.asyncio
async def test_cancel_works_on_any_layout(tmp_path, monkeypatch, spies) -> None:
    await _post(tmp_path, monkeypatch, {"when": "next-start"})
    _install_shape(monkeypatch, "kirocrew")

    resp, _state = await _post(tmp_path, monkeypatch, {"when": "cancel"})

    assert resp.status == 200 and not update_next_start.read().armed
