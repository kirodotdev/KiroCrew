"""``_deny_cross_app_slot_access`` refuses channel-backed slots for app callers.

The chokepoint guards every route that resolves a slot's transcript through
``slot_history_key`` (detail's transcript read first among them), so an app
owning a channel-backed slot must get the same anti-enumeration 404 there as a
slot it does not own -- otherwise the read route hands over the same foreign
conversation the send/export/fork/rewind boundaries refuse.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from kiro_crew.dashboard.chat_handlers import _deny_cross_app_slot_access


def _slot(app="", linked="", channel_origin=False):
    return SimpleNamespace(_app=app, linked_session_key=linked, channel_origin=channel_origin)


def _request(app=""):
    return SimpleNamespace(get=lambda k, default="": app if k == "app" else default)


def _body(resp):
    return json.loads(resp.body)


def test_an_app_is_refused_its_own_channel_linked_slot() -> None:
    resp = _deny_cross_app_slot_access(
        _request("my-app"), _slot(app="my-app", linked="slack:1700000000.000100"), "s", "op"
    )
    assert resp is not None and resp.status == 404
    assert _body(resp)["code"] == "slot_not_found"


def test_an_app_is_refused_its_own_channel_origin_slot() -> None:
    resp = _deny_cross_app_slot_access(
        _request("my-app"), _slot(app="my-app", channel_origin=True), "s", "op"
    )
    assert resp is not None and resp.status == 404
    assert _body(resp)["code"] == "slot_not_found"


def test_the_refusal_is_indistinguishable_from_the_not_owned_answer() -> None:
    backed = _deny_cross_app_slot_access(
        _request("my-app"), _slot(app="my-app", channel_origin=True), "s", "op"
    )
    foreign = _deny_cross_app_slot_access(_request("my-app"), _slot(app="other-app"), "s", "op")
    assert backed is not None and foreign is not None
    assert backed.status == foreign.status
    assert backed.body == foreign.body


def test_an_app_still_passes_on_its_own_plain_slot() -> None:
    assert _deny_cross_app_slot_access(_request("my-app"), _slot(app="my-app"), "s", "op") is None


def test_the_dashboard_owner_passes_on_a_channel_linked_slot() -> None:
    slot = _slot(app="my-app", linked="slack:1700000000.000100")
    assert _deny_cross_app_slot_access(_request(""), slot, "s", "op") is None
