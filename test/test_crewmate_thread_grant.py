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
* the manifest flag round-trips, and the one grant read
  (``app_session_control_grant``) holds the scope only beside ``sessionApproval``,
  taken from the very manifest the grant was judged against -- never from a
  second read, which an overlapping manifest update could make newer than the
  consent;
* consent: a manifest that newly declares the scope on a held grant widens
  user-session control, so an update disables the app pending re-consent, the
  scope is not reachable until the owner re-enables, and the SEL row names the
  scope rather than the grant it already held.
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
from kiro_crew.apps.permissions import (
    NO_SESSION_CONTROL,
    SessionControlGrant,
    app_session_control_grant,
)
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
_MANIFEST = "kiro_crew.apps.permissions.get_app_manifest"
_NOT_FOUND = {"error": "not found", "code": "slot_not_found"}
THREAD = member_slot_key(CREW)


def _manifest(**flags: bool) -> SimpleNamespace:
    """A live manifest declaring *flags*, the one read the grant and its scope share."""
    return SimpleNamespace(permissions=Permissions.from_dict(flags))


def _scoped(scope: bool) -> SimpleNamespace:
    """The grant-holding manifest, with or without the crewmate-thread scope."""
    return _manifest(sessionApproval=True, crewmateThreads=scope)


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
    with patch(_GRANT, return_value=True), patch(_MANIFEST, return_value=_scoped(scope)):
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
    with patch(_GRANT, return_value=True), patch(_MANIFEST, return_value=_scoped(scope)):
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
        patch(_MANIFEST, return_value=_scoped(scope)),
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


_RECORD = "kiro_crew.apps.permissions._read_installed"
_ENABLED = "kiro_crew.apps.permissions.is_app_enabled"
_CONSENTED = SimpleNamespace(sessionApprovalConsentPending=False, enabled=True)


@pytest.mark.parametrize(
    ("permissions", "expected"),
    [
        pytest.param(
            {"sessionApproval": True, "crewmateThreads": True},
            SessionControlGrant(granted=True, crewmate_threads=True),
            id="both",
        ),
        pytest.param(
            {"sessionApproval": True},
            SessionControlGrant(granted=True, crewmate_threads=False),
            id="grant-only",
        ),
        pytest.param({"crewmateThreads": True}, NO_SESSION_CONTROL, id="scope-only"),
    ],
)
def test_the_one_read_holds_the_scope_only_beside_the_grant(
    permissions: dict, expected: SessionControlGrant
) -> None:
    """A consented, enabled record: the verdict is the manifest's own two flags."""
    with (
        patch(_MANIFEST, return_value=_manifest(**permissions)),
        patch(_RECORD, return_value=_CONSENTED),
        patch(_ENABLED, return_value=True),
    ):
        assert app_session_control_grant(APP) == expected


@pytest.mark.parametrize(
    "record",
    [
        pytest.param(None, id="no-record"),
        pytest.param(
            SimpleNamespace(sessionApprovalConsentPending=True, enabled=False), id="pending"
        ),
        pytest.param(
            SimpleNamespace(sessionApprovalConsentPending=False, enabled=False), id="disabled"
        ),
    ],
)
def test_the_one_read_refuses_the_scope_with_the_grant(record) -> None:
    """Whatever the manifest declares, a grant not held carries no scope."""
    with (
        patch(_MANIFEST, return_value=_scoped(True)),
        patch(_RECORD, return_value=record),
        patch(_ENABLED, return_value=True),
    ):
        assert app_session_control_grant(APP) == NO_SESSION_CONTROL


def test_the_one_read_refuses_on_a_missing_or_unreadable_manifest() -> None:
    assert app_session_control_grant("") == NO_SESSION_CONTROL
    with patch(_MANIFEST, return_value=None):
        assert app_session_control_grant(APP) == NO_SESSION_CONTROL
    with patch(_MANIFEST, side_effect=OSError("gone")):
        assert app_session_control_grant(APP) == NO_SESSION_CONTROL


def test_the_scope_comes_from_the_manifest_the_consent_was_judged_against() -> None:
    """One manifest read per verdict, handed to the record read that decides consent
    and enablement, so grant and scope can never describe two different manifests."""
    manifest = _scoped(True)
    read = MagicMock(return_value=manifest)
    record = MagicMock(return_value=_CONSENTED)
    with patch(_MANIFEST, read), patch(_RECORD, record), patch(_ENABLED, return_value=True):
        assert app_session_control_grant(APP) == SessionControlGrant(
            granted=True, crewmate_threads=True
        )
    read.assert_called_once_with(APP)
    record.assert_called_once_with(APP, manifest=manifest)


def test_an_update_landing_mid_read_cannot_lend_its_scope_to_the_old_consent() -> None:
    """The hazard of two reads: a request overlapping an update that adds the scope
    sees the consented v1 record, then a v2 manifest. With one read the verdict is
    v1's -- the grant without the scope -- and v2 is never consulted."""
    v1, v2 = _scoped(False), _scoped(True)
    read = MagicMock(side_effect=[v1, v2])
    with (
        patch(_MANIFEST, read),
        patch(_RECORD, return_value=_CONSENTED),
        patch(_ENABLED, return_value=True),
    ):
        assert app_session_control_grant(APP) == SessionControlGrant(
            granted=True, crewmate_threads=False
        )
    assert read.call_count == 1


# ── consent ──


def _perms(**flags: bool) -> Permissions:
    return Permissions.from_dict(flags)


_GRANT_KEYS = "permissions.sessionApproval"
_SCOPE_KEYS = "permissions.crewmateThreads"
_BOTH_KEYS = "permissions.sessionApproval and permissions.crewmateThreads"


@pytest.mark.parametrize(
    ("prior", "requested", "added"),
    [
        pytest.param(None, _perms(sessionApproval=True), _GRANT_KEYS, id="first-grant"),
        pytest.param(
            None, _perms(sessionApproval=True, crewmateThreads=True), _BOTH_KEYS, id="first-both"
        ),
        pytest.param(None, _perms(crewmateThreads=True), None, id="first-scope-only"),
        pytest.param(_perms(sessionApproval=True), _perms(sessionApproval=True), None, id="same"),
        pytest.param(
            _perms(sessionApproval=True),
            _perms(sessionApproval=True, crewmateThreads=True),
            _SCOPE_KEYS,
            id="scope-added",
        ),
        pytest.param(
            _perms(sessionApproval=True, crewmateThreads=True),
            _perms(sessionApproval=True, crewmateThreads=True),
            None,
            id="both-kept",
        ),
        pytest.param(
            _perms(sessionApproval=True, crewmateThreads=True),
            _perms(sessionApproval=True),
            None,
            id="scope-dropped",
        ),
        pytest.param(
            _perms(crewmateThreads=True),
            _perms(sessionApproval=True, crewmateThreads=True),
            _BOTH_KEYS,
            id="grant-added-to-scope",
        ),
        pytest.param(_perms(), _perms(), None, id="neither"),
    ],
)
def test_widening_is_the_grant_or_its_scope_newly_declared(
    prior: Permissions | None, requested: Permissions, added: str | None
) -> None:
    """The decision and the words agree: a widening names exactly the keys the
    owner has not consented to, never the grant it already held."""
    from kiro_crew.apps.manager import _session_control_widened, _session_control_widening

    widening = _session_control_widening(prior, requested)
    assert (widening.keys if widening else None) == added
    assert _session_control_widened(prior, requested) is (added is not None)


def test_an_update_that_adds_the_scope_disables_until_reconsent(tmp_path, app_home) -> None:
    """The owner consented to sessions; crewmate threads are more, so the update
    lands disabled and pending, exactly as one that adds the grant itself does.
    Until they re-enable, the one grant read reaches NOTHING -- the scope the new
    manifest declares is not lent to the consent the old one earned -- and the
    SEL row says what was added, not that the grant they already held was."""
    from kiro_crew.apps.manager import enable_app, get_app, install_app, update_app

    assert install_app(_make_app_source(tmp_path, permissions={"sessionApproval": True})).ok
    assert enable_app("test-app", session_approval_consent=True).ok
    assert get_app("test-app")["enabled"] is True
    assert app_session_control_grant("test-app") == SessionControlGrant(
        granted=True, crewmate_threads=False
    )

    audit = MagicMock()
    with patch("kiro_crew.apps.manager_install.sel", return_value=audit):
        result = update_app(
            _make_app_source(
                tmp_path / "v2",
                version="2.0.0",
                permissions={"sessionApproval": True, "crewmateThreads": True},
            )
        )
    assert result.ok, result.error
    assert result.notice == "session_approval_reconsent"
    assert "crewmate threads" in result.message
    assert get_app("test-app")["enabled"] is False
    assert get_app("test-app")["sessionApprovalConsentPending"] is True
    assert app_session_control_grant("test-app") == NO_SESSION_CONTROL
    rows = [kw for kw in (c.kwargs for c in audit.log_api_access.call_args_list)]
    widened = [r for r in rows if r.get("operation") == "session_approval_widened"]
    assert [r["error"] for r in widened] == [
        "update added permissions.crewmateThreads; re-enable to consent"
    ]

    assert enable_app("test-app", session_approval_consent=True).ok
    assert app_session_control_grant("test-app") == SessionControlGrant(
        granted=True, crewmate_threads=True
    )


def test_a_registration_that_adds_the_scope_names_it_in_the_audit_row(tmp_path, app_home) -> None:
    """``register_external_app`` is the other writer of ``session_approval_widened``:
    the same row for a self-managed app whose re-registration adds only the scope."""
    from kiro_crew.apps.manager import enable_app, get_app, register_external_app

    def _register(**permissions: bool):
        return register_external_app(
            "self-managed",
            "1.0.0",
            "Self Managed",
            manifest_data={
                "name": "self-managed",
                "version": "1.0.0",
                "permissions": permissions,
            },
        )

    assert _register(sessionApproval=True).ok
    assert enable_app("self-managed", session_approval_consent=True).ok
    assert get_app("self-managed")["enabled"] is True

    audit = MagicMock()
    with patch("kiro_crew.apps.manager_external.sel", return_value=audit):
        result = _register(sessionApproval=True, crewmateThreads=True)
    assert result.ok, result.error
    assert result.notice == "session_approval_reconsent"
    assert "crewmate threads" in result.message
    assert get_app("self-managed")["enabled"] is False
    rows = [c.kwargs for c in audit.log_api_access.call_args_list]
    widened = [r for r in rows if r.get("operation") == "session_approval_widened"]
    assert [r["error"] for r in widened] == [
        "registration added permissions.crewmateThreads; re-enable to consent"
    ]
