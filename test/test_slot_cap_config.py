"""The per-creator session cap is a setting, and nothing moves by default.

``session.max_per_creator`` sets how many live sessions one creator may hold. The
default stays 50, a value outside 10-450 is clamped to the nearest bound, and a value
that is not a whole number loads as 50, so no value stops the gateway or a session
from starting. ``creator_slot_cap_refusal`` is the one reader, so create, fork,
revive and handoff refuse at the same number. The global live-session ceiling is
tested first and holds whatever the setting says. Only the dashboard owner may save
the setting: another dashboard user, an app token and a caller with no app claim
are each refused by the route's own gate.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _agent_resolves,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew import change_card_catalog as catalog
from kiro_crew.config.loader import KiroCrewConfig, _invalidate_config_cache
from kiro_crew.config.loader_builders import _SECURITY_BOUNDED_FIELDS
from kiro_crew.config.sections import (
    DEFAULT_MAX_PER_CREATOR,
    MAX_PER_CREATOR_MAX,
    MAX_PER_CREATOR_MIN,
)
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.handlers.core import _EDITABLE_CONFIG
from kiro_crew.dashboard.state import (
    CREATOR_CAP_RESERVE,
    MAX_LIVE_SLOTS,
    MAX_PER_CREATOR_SETTING,
    MAX_SLOTS_PER_CREATOR,
    creator_slot_cap_refusal,
    max_slots_per_creator,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, _floor_monkeypatch):
    """Every test reads its own data home, never the live one."""
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
    _invalidate_config_cache()
    yield
    _invalidate_config_cache()


def _set_cap(tmp_path: Path, value: object) -> None:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.json").write_text(
        json.dumps({"session": {"max_per_creator": value}}), encoding="utf-8"
    )
    # Rewritten within one mtime tick of the last write, so drop the cached parse.
    _invalidate_config_cache()


def _hold(state, creator, count: int) -> None:
    """Charge *creator* with *count* live slots, as ``create_session`` would."""
    for i in range(count):
        _slot(state, f"held-{i}")._created_by = creator.key


def _caller(state, monkeypatch, name: str = "chat-lead"):
    caller = _slot(state, name)
    caller.agent = "researcher"
    _agent_resolves(monkeypatch, "default")
    return caller


def _create(state, caller) -> dict:
    return asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))


# ── the default and the bounds ────────────────────────────────────────────


def test_the_default_is_fifty_and_the_range_keeps_a_reserve():
    assert (DEFAULT_MAX_PER_CREATOR, MAX_PER_CREATOR_MIN, MAX_PER_CREATOR_MAX) == (50, 10, 450)
    assert MAX_SLOTS_PER_CREATOR == DEFAULT_MAX_PER_CREATOR
    assert KiroCrewConfig.load().session.max_per_creator == 50
    assert max_slots_per_creator() == 50
    # The highest setting still leaves a reserve under the global ceiling, so one
    # creator can never hold every live session.
    assert CREATOR_CAP_RESERVE == 50
    assert MAX_PER_CREATOR_MAX == MAX_LIVE_SLOTS - CREATOR_CAP_RESERVE


@pytest.mark.parametrize("value", [10, 51, 120, 450])
def test_a_value_in_range_is_the_cap_in_force(tmp_path: Path, value: int):
    _set_cap(tmp_path, value)
    assert KiroCrewConfig.load().session.max_per_creator == value
    assert max_slots_per_creator() == value


@pytest.mark.parametrize("value", [9, 0, -5, "3"])
def test_a_value_below_the_range_is_raised_to_the_floor(tmp_path: Path, value: object):
    _set_cap(tmp_path, value)
    assert max_slots_per_creator() == MAX_PER_CREATOR_MIN


@pytest.mark.parametrize("value", [451, 500, 10**9, "600"])
def test_a_value_above_the_range_is_lowered_to_the_ceiling(tmp_path: Path, value: object):
    """The ceiling holds for a hand-edited file, which never passes the write gate."""
    _set_cap(tmp_path, value)
    assert max_slots_per_creator() == MAX_PER_CREATOR_MAX


@pytest.mark.parametrize("bad", [True, "abc", 2.5, None, [60], {"n": 60}])
def test_a_value_that_is_not_a_whole_number_loads_as_the_default(tmp_path: Path, bad: object):
    _set_cap(tmp_path, bad)
    assert KiroCrewConfig.load().session.max_per_creator == DEFAULT_MAX_PER_CREATOR
    assert max_slots_per_creator() == DEFAULT_MAX_PER_CREATOR


def test_a_config_that_cannot_be_read_answers_the_default(monkeypatch):
    def _raise() -> KiroCrewConfig:
        raise OSError("config unreadable")

    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(_raise))
    assert max_slots_per_creator() == DEFAULT_MAX_PER_CREATOR


@pytest.mark.parametrize(("held", "expected"), [(10**6, MAX_PER_CREATOR_MAX), (1, 10)])
def test_the_reader_holds_the_range_for_a_config_the_loader_did_not_build(
    monkeypatch, held: int, expected: int
):
    """A config object built by hand never passed the loader's clamp."""
    config = KiroCrewConfig()
    config.session.max_per_creator = held
    monkeypatch.setattr(KiroCrewConfig, "load", staticmethod(lambda: config))
    assert max_slots_per_creator() == expected


def test_a_changed_setting_applies_without_a_restart(tmp_path: Path):
    _set_cap(tmp_path, 60)
    assert max_slots_per_creator() == 60
    _set_cap(tmp_path, 80)
    assert max_slots_per_creator() == 80


# ── the cap through the real verb ─────────────────────────────────────────


def test_the_default_still_refuses_the_fifty_first_session(tmp_path: Path, monkeypatch):
    state = _make_state(tmp_path)
    lead = _caller(state, monkeypatch)
    _hold(state, lead, DEFAULT_MAX_PER_CREATOR)

    with pytest.raises(sc.SessionControlError) as err:
        _create(state, lead)

    assert err.value.code == "creator_slot_cap_reached"
    assert err.value.status == 409


def test_a_raised_cap_admits_a_create_the_default_refuses(tmp_path: Path, monkeypatch):
    _set_cap(tmp_path, 60)
    state = _make_state(tmp_path)
    lead = _caller(state, monkeypatch)
    _hold(state, lead, DEFAULT_MAX_PER_CREATOR)

    assert _create(state, lead)["ok"] is True


def test_the_refusal_says_what_is_held_and_where_the_owner_changes_it(tmp_path: Path, monkeypatch):
    _set_cap(tmp_path, 12)
    state = _make_state(tmp_path)
    lead = _caller(state, monkeypatch)
    _hold(state, lead, 12)

    with pytest.raises(sc.SessionControlError) as err:
        _create(state, lead)

    message = err.value.message
    assert err.value.code == "creator_slot_cap_reached"
    assert "holds 12 live sessions" in message
    assert "the limit is 12" in message
    assert "Settings > Chat > Sessions > Sessions per creator" in message
    assert MAX_PER_CREATOR_SETTING in message
    assert f"{MAX_PER_CREATOR_MIN}-{MAX_PER_CREATOR_MAX}" in message
    assert message == creator_slot_cap_refusal(state, lead.key)


def test_a_creator_with_room_gets_no_refusal(tmp_path: Path):
    state = _make_state(tmp_path)
    lead = _slot(state, "chat-lead")
    _hold(state, lead, DEFAULT_MAX_PER_CREATOR - 1)
    assert creator_slot_cap_refusal(state, lead.key) is None


def test_the_global_ceiling_still_refuses_under_the_highest_setting(tmp_path: Path, monkeypatch):
    """The live-session ceiling is tested before the creator's own share."""
    _set_cap(tmp_path, MAX_PER_CREATOR_MAX)
    state = _make_state(tmp_path)
    lead = _caller(state, monkeypatch)
    _hold(state, lead, 3)
    monkeypatch.setattr(sc, "MAX_LIVE_SLOTS", state.live_slot_count())

    with pytest.raises(sc.SessionControlError) as err:
        _create(state, lead)

    assert err.value.code == "slot_cap_reached"
    assert MAX_PER_CREATOR_MAX < MAX_LIVE_SLOTS


# ── who may change it ─────────────────────────────────────────────────────


def test_the_write_gate_and_the_load_sweep_share_the_bounds():
    assert _EDITABLE_CONFIG[MAX_PER_CREATOR_SETTING] == {
        "type": "int",
        "min": MAX_PER_CREATOR_MIN,
        "max": MAX_PER_CREATOR_MAX,
    }
    assert ("session", "max_per_creator", MAX_PER_CREATOR_MIN, MAX_PER_CREATOR_MAX) in (
        _SECURITY_BOUNDED_FIELDS
    )


_OWNER = "U-OWNER"
#: Sent as ``app`` by a caller whose request carries no app claim at all, as an
#: internal-secret caller acting as the person does.
_NO_APP_CLAIM = object()


def _config_app(user: str, app_claim: object) -> web.Application:
    """The real PATCH route behind a middleware that stamps one caller identity."""
    from kiro_crew.dashboard.handlers import api_kirocrew_config_patch

    @web.middleware
    async def _identity(request, handler):
        request["user"] = user
        if app_claim is not _NO_APP_CLAIM:
            request["app"] = app_claim
        return await handler(request)

    app = web.Application(middlewares=[_identity])
    state = MagicMock()
    state.owner_id = _OWNER
    app["state"] = state
    app.router.add_patch("/api/config/kirocrew", api_kirocrew_config_patch)
    return app


async def _patch_cap(user: str, app_claim: object, value: int) -> web.Response:
    async with TestClient(TestServer(_config_app(user, app_claim))) as client:
        resp = await client.patch(
            "/api/config/kirocrew", json={"path": MAX_PER_CREATOR_SETTING, "value": value}
        )
        await resp.read()
        return resp


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("user", "app_claim"),
    [
        pytest.param("U-OTHER", "", id="another-dashboard-user"),
        pytest.param(_OWNER, "some-app", id="an-app-token"),
        pytest.param(_OWNER, _NO_APP_CLAIM, id="an-internal-caller-with-no-app-claim"),
        pytest.param("", "", id="no-identity"),
    ],
)
async def test_only_the_owner_may_save_the_cap(tmp_path: Path, user: str, app_claim: object):
    """The owner gate itself decides, on the real identities the route tells apart."""
    _set_cap(tmp_path, 60)
    before = (tmp_path / "home" / "config.json").read_bytes()

    resp = await _patch_cap(user, app_claim, 450)

    assert resp.status == 403, await resp.text()
    assert (await resp.json())["code"] == "owner_only"
    assert (tmp_path / "home" / "config.json").read_bytes() == before
    _invalidate_config_cache()
    assert max_slots_per_creator() == 60


@pytest.mark.asyncio
async def test_the_owner_saves_the_cap_and_the_next_read_sees_it(tmp_path: Path):
    """The control for the refusals above: the same request from the owner lands."""
    _set_cap(tmp_path, 60)

    resp = await _patch_cap(_OWNER, "", 120)

    assert resp.status == 200, await resp.text()
    _invalidate_config_cache()
    assert max_slots_per_creator() == 120


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [9, 451, 100000])
async def test_the_owner_cannot_save_a_value_outside_the_range(tmp_path: Path, value: int):
    _set_cap(tmp_path, 60)

    resp = await _patch_cap(_OWNER, "", value)

    assert resp.status == 400, await resp.text()
    _invalidate_config_cache()
    assert max_slots_per_creator() == 60


def test_a_card_that_raises_the_cap_is_marked_as_widening():
    """An agent can only propose the change; the card the owner reads says it widens."""
    risk = catalog._setting_risk
    assert risk(MAX_PER_CREATOR_SETTING, 50, 120) == catalog.RISK_WIDEN
    assert risk(MAX_PER_CREATOR_SETTING, 120, 50) == catalog.RISK_TIGHTEN
    assert risk(MAX_PER_CREATOR_SETTING, 50, 50) == catalog.RISK_NORMAL


@pytest.mark.parametrize(("before", "after"), [(50, 449.5), (50, 50.5), (10, 10.9)])
def test_a_fractional_raise_is_ranked_by_the_number_the_route_saves(before: int, after: float):
    """The card check admits a float for an int setting and the route saves ``int()``
    of it, so 449.5 writes 449: the card is ranked by 449, never left unranked."""
    from kiro_crew.dashboard.change_cards_settings import check_setting_value

    check_setting_value(_EDITABLE_CONFIG[MAX_PER_CREATOR_SETTING], after)  # admitted
    expected = catalog.RISK_WIDEN if int(after) > before else catalog.RISK_NORMAL
    assert catalog._setting_risk(MAX_PER_CREATOR_SETTING, before, after) == expected
    assert catalog._setting_risk(MAX_PER_CREATOR_SETTING, 120, 60.5) == catalog.RISK_TIGHTEN
