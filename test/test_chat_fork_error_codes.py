"""Every refusal from ``POST /api/chat/slots/{slot}/fork`` carries a ``code``.

``error-code-baseline.json`` — the worklist the contract gate in
``test/test_error_code_contract.py`` ratchets down — listed
``dashboard/chat_fork.py`` with ``missing_code: 17``. The module was already
half-converted: its three ``503`` snapshot refusals carried
``fork_snapshot_unstable``, so the wire contract was settled and only the
remaining seventeen had to follow it.

The prose is kept and keeps its meaning — demoted to advisory, not removed — so
a client that only reads ``error`` is unaffected. This is backend-only because
all three frontend callers of ``forkChatSlot`` (``useSessionActions.ts``,
``SessionGridView.tsx``, ``chatSlice.ts``) branch on ``ok``/``key`` and none
declares an ``onError`` or reads ``res.error`` at all.

**The one refusal that must NOT become distinguishable.** ``api_chat_slot_fork``
answers ``404 not found`` in three places: no such slot, an app-scoped caller on
an unscoped slot, and an app-scoped caller on another app's slot. The last two
are ``404`` rather than ``403`` on purpose — so a slot behind the App Kit
isolation boundary is indistinguishable from one that does not exist, which is
what stops an app enumerating slots across it (CWE-204). A conversion that gave
those three distinct codes would rebuild that oracle in the field it just added.
They share ``slot_not_found``, and ``test_the_three_404s_stay_indistinguishable``
is the assertion that keeps it that way.
"""

from __future__ import annotations

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

_TARGET = "dashboard/chat_fork.py"


def _findings():
    """Run the contract gate's OWN scanner, so this file cannot drift from it."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import test_error_code_contract as gate

    return [f for f in gate.scan() if f.path == _TARGET]


# ── the per-file ratchet ──
#
# The repo-wide gate only fails on a NET regression, so a new prose-only refusal
# could land here behind an unrelated deletion elsewhere in the tree.


def test_no_refusal_in_this_module_is_prose_only() -> None:
    missing = sorted(f.lineno for f in _findings() if f.bucket == "missing_code")
    assert missing == [], (
        f"these src/kiro_crew/{_TARGET} lines refuse with prose and no "
        f"machine-readable code: {missing}"
    )


def test_the_ratchet_can_actually_fail() -> None:
    """Self-check: a scan matching nothing would pass the assertion above vacuously."""
    coded = [f for f in _findings() if f.bucket == "compliant"]
    assert len(coded) == 24, f"scanner reached {len(coded)} coded sites, expected 24"
    assert all(f.code_value for f in coded)


# ── behaviour ──


def _seeded_state(tmp_path):
    state = _make_state(tmp_path)
    slot = state.get_or_create_slot("forkable")
    slot.append("user", "hello", "msg msg-u")
    slot.append("assistant", "hi", "msg msg-a")
    slot._resumed_count = len(slot.messages)
    slot._disk_window_len = len(slot.messages)
    slot._dirty = False
    return state


async def _fork(state, slot: str, payload) -> tuple[int, dict]:
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{slot}/fork", json=payload)
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_an_unknown_slot(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    status, body = await _fork(_seeded_state(tmp_path), "nosuchslot", {})
    assert status == 404
    assert body["code"] == "slot_not_found"
    assert body["error"]


@pytest.mark.asyncio
async def test_a_body_that_is_not_json(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seeded_state(tmp_path)
    async with TestClient(TestServer(_make_app(state))) as client:
        resp = await client.post(
            "/api/chat/slots/forkable/fork",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        body = await resp.json()
    assert resp.status == 400
    assert body["code"] == "invalid_json"


@pytest.mark.asyncio
async def test_a_body_that_is_json_but_not_an_object(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    status, body = await _fork(_seeded_state(tmp_path), "forkable", ["at_message_index"])
    assert status == 400
    assert body["code"] == "body_not_object"


@pytest.mark.asyncio
async def test_an_unknown_mode(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    status, body = await _fork(_seeded_state(tmp_path), "forkable", {"mode": "sideways"})
    assert status == 400
    assert body["code"] == "invalid_mode"


@pytest.mark.asyncio
async def test_an_unknown_direction(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    status, body = await _fork(_seeded_state(tmp_path), "forkable", {"direction": "sideways"})
    assert status == 400
    assert body["code"] == "invalid_direction"


@pytest.mark.asyncio
async def test_a_non_string_prompt(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    status, body = await _fork(_seeded_state(tmp_path), "forkable", {"prompt": 7})
    assert status == 400
    assert body["code"] == "invalid_field_type"


@pytest.mark.asyncio
async def test_an_over_long_prompt(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    status, body = await _fork(_seeded_state(tmp_path), "forkable", {"prompt": "x" * 32_769})
    assert status == 400
    assert body["code"] == "prompt_too_long"


@pytest.mark.asyncio
async def test_a_non_integer_index_and_an_out_of_range_one_differ(tmp_path, monkeypatch) -> None:
    """The distinction a caller could not previously make without matching English.

    "not an index" and "an index past the end" are different client bugs and want
    different handling; before the code they were both ``400`` with prose.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    _, bad_type = await _fork(_seeded_state(tmp_path), "forkable", {"at_message_index": "2"})
    _, out_of_range = await _fork(_seeded_state(tmp_path), "forkable", {"at_message_index": 999})
    assert bad_type["code"] == "invalid_field_type"
    assert out_of_range["code"] == "value_out_of_range"


@pytest.mark.asyncio
async def test_a_negative_index_is_a_type_refusal_not_a_range_one(tmp_path, monkeypatch) -> None:
    """``at_index < 0`` shares the branch with the type check, so it keeps that code."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    status, body = await _fork(_seeded_state(tmp_path), "forkable", {"at_message_index": -1})
    assert status == 400
    assert body["code"] == "invalid_field_type"


@pytest.mark.asyncio
async def test_a_boolean_index_is_refused_as_a_type(tmp_path, monkeypatch) -> None:
    """``isinstance(True, int)`` is True in Python; the handler rejects bools first."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    status, body = await _fork(_seeded_state(tmp_path), "forkable", {"at_message_index": True})
    assert status == 400
    assert body["code"] == "invalid_field_type"


@pytest.mark.asyncio
async def test_a_slot_with_no_forkable_messages(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    state.get_or_create_slot("empty")
    status, body = await _fork(state, "empty", {})
    assert status == 400
    assert body["code"] == "no_messages_to_fork"


@pytest.mark.asyncio
async def test_a_non_persistent_slot(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seeded_state(tmp_path)
    state._slots["forkable"].memory_mode = "ephemeral"
    status, body = await _fork(state, "forkable", {})
    assert status == 400
    assert body["code"] == "slot_not_persistent"


# ── the property the conversion must not break ──


@pytest.mark.asyncio
async def test_the_three_404s_stay_indistinguishable(tmp_path, monkeypatch) -> None:
    """An app-scoped caller must not be able to tell the three 404s apart.

    ``404`` (not ``403``) is deliberate: it makes a slot owned by another app, an
    unscoped slot, and a slot that does not exist look identical to a caller
    behind the App Kit isolation boundary, so the boundary cannot be used to
    enumerate slots (CWE-204). The added ``code`` is a NEW field on that same
    response, so it is a new place for the three to diverge — this pins that
    they do not. The true reason stays recorded server-side via SEL.
    """
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _seeded_state(tmp_path)
    # An unscoped slot (no ``_app``) and one owned by a DIFFERENT app.
    other = state.get_or_create_slot("othersapp")
    other.append("user", "hello", "msg msg-u")
    other._app = "app-b"

    @web.middleware
    async def _as_app_a(request: web.Request, handler):
        request["app"] = "app-a"
        request["user"] = "app-a"
        return await handler(request)

    app = _make_app(state)
    app.middlewares.insert(0, _as_app_a)

    seen = []
    async with TestClient(TestServer(app)) as client:
        for slot in ("nosuchslot", "forkable", "othersapp"):
            resp = await client.post(f"/api/chat/slots/{slot}/fork", json={})
            seen.append((resp.status, await resp.json()))

    statuses = {s for s, _ in seen}
    codes = {b["code"] for _, b in seen}
    errors = {b["error"] for _, b in seen}
    assert statuses == {404}, seen
    assert codes == {"slot_not_found"}, (
        "the three 404s now carry different codes, so an app-scoped caller can "
        f"tell a slot it may not see from one that does not exist: {seen}"
    )
    assert len(errors) == 1, seen


@pytest.mark.asyncio
async def test_every_refusal_carries_both_a_code_and_its_prose(tmp_path, monkeypatch) -> None:
    """The behavioural ratchet: no path regresses to prose-only, and none drops
    the advisory text an existing client still reads."""
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    for payload in (
        ["at_message_index"],
        {"mode": "sideways"},
        {"direction": "sideways"},
        {"prompt": 7},
        {"prompt": "x" * 32_769},
        {"at_message_index": "2"},
        {"at_message_index": 999},
        {"at_message_index": -1},
    ):
        status, body = await _fork(_seeded_state(tmp_path), "forkable", payload)
        assert status == 400, payload
        assert isinstance(body.get("code"), str) and body["code"], payload
        assert isinstance(body.get("error"), str) and body["error"], payload
