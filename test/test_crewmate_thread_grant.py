"""``permissions.crewmateThreads``: the ``sessionApproval`` grant reaches a crewmate's
pinned DM thread only with this scope, and the scope reaches nothing else.

Pinned here, against ``slot_ownership`` and the handlers that apply it without the
checkpoint middleware (``test_slot_ownership_checkpoint.py`` pins the checkpoint):

* the rule, ``app_may_control_session``: a live ``member-*`` slot with
  ``mode="member"`` is reached with grant and scope, never with the grant alone,
  never with the scope alone, and a member-mode slot under another key never;
* the scope widens nothing but that: every other app-control target keeps its
  verdict when the scope is held;
* the routes: ``POST /api/chat/slots/{slot}/approve``, ``POST /api/chat/mode`` and
  ``POST /api/chat`` admit the crewmate thread with grant and scope, and answer the
  uniform 404 without the scope;
* the reservation: a member key with no live thread stays 404 for an app, scope or
  not, and nothing is minted under it;
* the manifest flag round-trips, and the scope read holds only beside
  ``sessionApproval``;
* consent: a manifest that newly declares the scope on a held grant widens
  user-session control, so an update disables the app pending re-consent.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _make_app_source
from chat_mode_security_helpers import (  # noqa: F401 -- fixtures are requested by name
    _APP_CONTROL_TARGETS,
    _app_client,
    _hermetic_home,
    _isolate_safety_override,
    _make_app_control_target,
    state_fixture,
)
from members_dm_thread_helpers import CREW, _patched_config

from kiro_crew.apps.manifest_sections import Permissions
from kiro_crew.apps.permissions import app_session_grant_reaches_crewmate_threads
from kiro_crew.dashboard.slot_ownership import (
    MEMBER_KEY_RESERVED,
    app_may_control_session,
    app_new_key_refusal,
    is_crewmate_thread,
)
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.members import DM_SLOT_MODE, member_slot_key, write_dm_binding

APP = "crew-keyboard"
_GRANT = "kiro_crew.apps.permissions.app_can_manage_session_approvals"
_SCOPE = "kiro_crew.apps.permissions.app_session_grant_reaches_crewmate_threads"
_NOT_FOUND = {"error": "not found", "code": "slot_not_found"}
THREAD = member_slot_key(CREW)


def _crewmate_thread(state):
    """The slot ``POST /api/members/{slug}/thread`` mints: binding first, then the
    member-mode slot under the ``member-`` key, as the owner's own tab."""
    write_dm_binding(THREAD[len("member-") :], member=CREW, slot_key=THREAD)
    return state.get_or_create_slot(THREAD, agent=CREW, mode=DM_SLOT_MODE, origin=SlotOrigin.USER)


# ── the rule ──


def test_the_rule_reaches_a_crewmate_thread_only_with_grant_and_scope(state) -> None:
    slot = _crewmate_thread(state)
    assert app_may_control_session(APP, slot, True) is False
    assert app_may_control_session(APP, slot, True, crewmate_threads=True) is True
    assert app_may_control_session(APP, slot, False, crewmate_threads=True) is False


def test_a_member_mode_slot_under_another_key_is_never_reached(state) -> None:
    slot = _make_app_control_target(state, "member")
    assert app_may_control_session(APP, slot, True, crewmate_threads=True) is False


@pytest.mark.parametrize(
    ("slot", "expected"),
    [
        pytest.param(None, False, id="none"),
        pytest.param(SimpleNamespace(mode=DM_SLOT_MODE, key=THREAD), True, id="thread"),
        pytest.param(SimpleNamespace(mode=DM_SLOT_MODE, key="s1"), False, id="other-key"),
        pytest.param(SimpleNamespace(mode="", key=THREAD), False, id="other-mode"),
    ],
)
def test_a_crewmate_thread_is_judged_on_both_its_marks(slot, expected: bool) -> None:
    assert is_crewmate_thread(slot) is expected


@pytest.mark.parametrize(("target", "allowed"), _APP_CONTROL_TARGETS)
def test_the_scope_widens_nothing_else(state, target: str, allowed: bool) -> None:
    slot = _make_app_control_target(state, target)
    assert app_may_control_session(APP, slot, True, crewmate_threads=True) is allowed


# ── the reservation ──


def test_a_member_key_with_no_live_thread_stays_reserved(state) -> None:
    for grant_route in (False, True):
        assert app_new_key_refusal(
            state, THREAD, f"dashboard:{THREAD}", session_grant_route=grant_route
        ) == (True, MEMBER_KEY_RESERVED)


def test_a_live_thread_is_judged_not_reserved_on_a_grant_route_only(state) -> None:
    _crewmate_thread(state)
    assert app_new_key_refusal(state, THREAD, f"dashboard:{THREAD}") == (True, MEMBER_KEY_RESERVED)
    assert app_new_key_refusal(state, THREAD, f"dashboard:{THREAD}", session_grant_route=True) == (
        False,
        "",
    )


# ── the routes, behind the handlers' own decision ──


@pytest.mark.parametrize("scope", [False, True])
@pytest.mark.asyncio
async def test_approve_on_a_crewmate_thread_needs_the_scope(state, scope: bool) -> None:
    slot = _crewmate_thread(state)
    future = asyncio.get_running_loop().create_future()
    slot._approval_futures["req-1"] = future
    with patch(_GRANT, return_value=True), patch(_SCOPE, return_value=scope):
        async with _app_client(state) as client:
            resp = await client.post(
                f"/api/chat/slots/{THREAD}/approve",
                json={"action": "approved", "request_id": "req-1"},
            )
            body = await resp.json()
    if scope:
        assert resp.status == 200, body
        assert future.result() == "approved"
    else:
        assert (resp.status, body) == (404, _NOT_FOUND)
        assert future.done() is False


@pytest.mark.parametrize("scope", [False, True])
@pytest.mark.asyncio
async def test_mode_change_on_a_crewmate_thread_needs_the_scope(state, scope: bool) -> None:
    slot = _crewmate_thread(state)
    with patch(_GRANT, return_value=True), patch(_SCOPE, return_value=scope):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust", "slot": THREAD})
            body = await resp.json()
    assert (resp.status, slot._trust) == ((200, True) if scope else (404, False)), body


async def _app_send(
    state, slot_name: str, *, scope: bool
) -> tuple[int, dict, AsyncMock, MagicMock]:
    """``POST /api/chat?ws=1`` as the app, with the grant held and the scope as given."""
    run_chat = AsyncMock()
    audit = MagicMock()
    with (
        patch(_GRANT, return_value=True),
        patch(_SCOPE, return_value=scope),
        patch("kiro_crew.dashboard.chat_handlers._run_chat", new=run_chat),
        patch("kiro_crew.dashboard.chat_handlers._maybe_auto_title", new=AsyncMock()),
        patch("kiro_crew.dashboard.chat_handlers.sel", return_value=audit),
        _patched_config([CREW]),
    ):
        async with _app_client(state) as client:
            resp = await client.post("/api/chat?ws=1", json={"slot": slot_name, "message": "yes"})
            return resp.status, await resp.json(), run_chat, audit


@pytest.mark.asyncio
async def test_a_send_onto_a_crewmate_thread_with_the_scope_is_a_turn(state) -> None:
    slot = _crewmate_thread(state)
    status, body, run_chat, audit = await _app_send(state, THREAD, scope=True)
    assert status == 200, body
    run_chat.assert_called_once()
    assert slot.agent == CREW
    audit.log_api_access.assert_any_call(
        caller=APP,
        operation="chat_send",
        outcome="allowed",
        source="app_isolation",
        resources=f"permissions.sessionApproval+crewmateThreads|slot={THREAD}",
    )


@pytest.mark.asyncio
async def test_a_send_onto_a_crewmate_thread_without_the_scope_is_the_uniform_404(
    state,
) -> None:
    slot = _crewmate_thread(state)
    status, body, run_chat, audit = await _app_send(state, THREAD, scope=False)
    assert (status, body) == (404, _NOT_FOUND)
    run_chat.assert_not_called()
    assert [m for m in slot.messages if m.get("role") == "user"] == []
    outcomes = [c.kwargs.get("outcome") for c in audit.log_api_access.call_args_list]
    assert "allowed" not in outcomes


@pytest.mark.asyncio
async def test_a_send_never_mints_a_member_slot(state) -> None:
    """With no live thread the member key is reserved, scope or not: the app gets the
    uniform 404 (not the owner's 409) and no slot appears under the key."""
    status, body, run_chat, _ = await _app_send(state, THREAD, scope=True)
    assert (status, body) == (404, _NOT_FOUND)
    run_chat.assert_not_called()
    assert THREAD not in state._slots


# ── the manifest flag and its live read ──


def test_the_flag_round_trips_and_fails_closed() -> None:
    both = Permissions.from_dict({"sessionApproval": True, "crewmateThreads": True})
    assert (both.sessionApproval, both.crewmateThreads) == (True, True)
    assert both.to_dict() == {"sessionApproval": True, "crewmateThreads": True}
    assert Permissions.from_dict({"crewmateThreads": "yes"}).crewmateThreads is False
    assert "crewmateThreads" not in Permissions().to_dict()


@pytest.mark.parametrize(
    ("permissions", "expected"),
    [
        pytest.param({"sessionApproval": True, "crewmateThreads": True}, True, id="both"),
        pytest.param({"sessionApproval": True}, False, id="grant-only"),
        pytest.param({"crewmateThreads": True}, False, id="scope-only"),
    ],
)
def test_the_scope_read_holds_only_beside_the_grant(permissions: dict, expected: bool) -> None:
    manifest = SimpleNamespace(permissions=Permissions.from_dict(permissions))
    with patch("kiro_crew.apps.permissions.get_app_manifest", return_value=manifest):
        assert app_session_grant_reaches_crewmate_threads(APP) is expected


def test_the_scope_read_refuses_on_a_missing_or_unreadable_manifest() -> None:
    assert app_session_grant_reaches_crewmate_threads("") is False
    with patch("kiro_crew.apps.permissions.get_app_manifest", return_value=None):
        assert app_session_grant_reaches_crewmate_threads(APP) is False
    with patch("kiro_crew.apps.permissions.get_app_manifest", side_effect=OSError("gone")):
        assert app_session_grant_reaches_crewmate_threads(APP) is False


# ── consent ──


def _perms(**flags: bool) -> Permissions:
    return Permissions.from_dict(flags)


@pytest.mark.parametrize(
    ("prior", "requested", "widened"),
    [
        pytest.param(None, _perms(sessionApproval=True), True, id="first-grant"),
        pytest.param(
            None, _perms(sessionApproval=True, crewmateThreads=True), True, id="first-both"
        ),
        pytest.param(None, _perms(crewmateThreads=True), False, id="first-scope-only"),
        pytest.param(_perms(sessionApproval=True), _perms(sessionApproval=True), False, id="same"),
        pytest.param(
            _perms(sessionApproval=True),
            _perms(sessionApproval=True, crewmateThreads=True),
            True,
            id="scope-added",
        ),
        pytest.param(
            _perms(sessionApproval=True, crewmateThreads=True),
            _perms(sessionApproval=True, crewmateThreads=True),
            False,
            id="both-kept",
        ),
        pytest.param(
            _perms(sessionApproval=True, crewmateThreads=True),
            _perms(sessionApproval=True),
            False,
            id="scope-dropped",
        ),
        pytest.param(
            _perms(crewmateThreads=True),
            _perms(sessionApproval=True, crewmateThreads=True),
            True,
            id="grant-added-to-scope",
        ),
        pytest.param(_perms(), _perms(), False, id="neither"),
    ],
)
def test_widening_is_the_grant_or_its_scope_newly_declared(
    prior: Permissions | None, requested: Permissions, widened: bool
) -> None:
    from kiro_crew.apps.manager import _session_control_widened

    assert _session_control_widened(prior, requested) is widened


def test_an_update_that_adds_the_scope_disables_until_reconsent(tmp_path, app_home) -> None:
    """The owner consented to sessions; crewmate threads are more, so the update
    lands disabled and pending, exactly as one that adds the grant itself does."""
    from kiro_crew.apps.manager import enable_app, get_app, install_app, update_app

    assert install_app(_make_app_source(tmp_path, permissions={"sessionApproval": True})).ok
    assert enable_app("test-app", session_approval_consent=True).ok
    assert get_app("test-app")["enabled"] is True

    result = update_app(
        _make_app_source(
            tmp_path / "v2",
            version="2.0.0",
            permissions={"sessionApproval": True, "crewmateThreads": True},
        )
    )
    assert result.ok, result.error
    assert result.notice == "session_approval_reconsent"
    assert get_app("test-app")["enabled"] is False
    assert get_app("test-app")["sessionApprovalConsentPending"] is True
    assert app_session_grant_reaches_crewmate_threads("test-app") is True
