"""Session control reaching an app's sessions when the app opts in.

An app declares ``permissions.reachableByUserSessions`` (``apps/user_session_reach.py``)
to let the owner's own chats read its sessions and send them messages. These cases
install a real app into a private home so the grant comes from the same manifest,
approved-grants set and record the gate reads. They pin the default refusal, the
opt-in once approved, an update that adds a verb waiting for the owner, every caller
kind the opt-in does not reach, the verbs it does not cover, and revive.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _busy,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew.apps import protected_app_state
from kiro_crew.apps import user_session_reach as usr
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    disable_app,
    enable_app,
    get_app,
    install_app,
    update_app,
)
from kiro_crew.apps.manifest_sections import Permissions
from kiro_crew.apps.official_catalog import _disclosed_permissions_manifest
from kiro_crew.apps.point_grants import grant_source, point_grant_families
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.session_control_parts import target_authorization as ta
from kiro_crew.testing.clock import ManualClock

APP = "work-items"
TARGET = "chat-app"


@pytest.fixture(autouse=True)
def _isolate(tmp_path, _floor_monkeypatch, sel_private_root):
    """A private app home that admits a local third-party app."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(home))
    (home / "config.json").write_text(
        json.dumps({"agent": {"apps_allow_third_party": True}}), encoding="utf-8"
    )
    # The primed verdicts are process-wide; a verdict left by one case must not admit
    # a gate in the next.
    usr._verdicts.clear()
    yield
    usr._verdicts.clear()


def _source(tmp_path: Path, sub: str, verbs: list[str] | None, version: str = "1.0.0") -> Path:
    src = tmp_path / sub / APP
    src.mkdir(parents=True)
    permissions: dict = {"storage": True}
    if verbs is not None:
        permissions["reachableByUserSessions"] = verbs
    manifest = {
        "name": APP,
        "version": version,
        "displayName": "Work Items",
        "description": "one chat session per work item",
        "author": "tester",
        "permissions": permissions,
    }
    (src / APP_MANIFEST_FILENAME).write_text(json.dumps(manifest), encoding="utf-8")
    return src


def _install(tmp_path: Path, verbs: list[str] | None) -> None:
    result = install_app(_source(tmp_path, "v1", verbs))
    assert result.ok, result.error
    assert enable_app(APP).ok


def _update(tmp_path: Path, verbs: list[str] | None) -> None:
    result = update_app(_source(tmp_path, "v2", verbs, version="2.0.0"))
    assert result.ok, result.error


def _pair(tmp_path):
    """The owner's chat and a live session the app owns."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    target = _slot(state, TARGET, app=APP)
    return state, caller, target


def _authorize(state, caller, operation: str = "read"):
    """Prime, then gate: the order every read, summary and send path follows."""
    asyncio.run(sc.prewarm_app_reach(state))
    return sc.authorize_target(
        state, caller_session_key=_key(caller), target=TARGET, operation=operation
    )


def _refusal(state, caller, operation: str = "read") -> str:
    with pytest.raises(sc.SessionControlError) as exc:
        _authorize(state, caller, operation)
    return exc.value.code


# ── the declaration ─────────────────────────────────────────────────────────


def test_only_known_verbs_in_a_list_declare_anything():
    assert Permissions.from_dict({"reachableByUserSessions": ["send", "stop", 3, "read"]}) == (
        Permissions(reachableByUserSessions=["read", "send"])
    )
    for malformed in ("read", True, {"read": True}, None):
        parsed = Permissions.from_dict({"reachableByUserSessions": malformed})
        assert parsed.reachableByUserSessions == []
    assert "reachableByUserSessions" not in Permissions().to_dict()


def test_the_declaration_is_a_staged_grant_family():
    assert usr.GRANT_FAMILY in point_grant_families()
    manifest = {"permissions": {"reachableByUserSessions": ["read"]}}
    assert grant_source(manifest)[usr.GRANT_FAMILY] == ["read"]


def test_the_catalog_discloses_the_verbs_before_install():
    entry = {"permissions": {"reachableByUserSessions": ["send", "close"]}}
    assert _disclosed_permissions_manifest(entry) == {
        "permissions": {"reachableByUserSessions": ["send"]}
    }


# ── the default refusal and the opt-in ──────────────────────────────────────


def test_an_app_that_does_not_declare_it_keeps_its_sessions_unaddressable(tmp_path):
    _install(tmp_path, None)
    state, caller, _target = _pair(tmp_path)
    assert _refusal(state, caller) == "app_scoped_target"
    assert "userSessionReachGrant" not in get_app(APP)


def test_an_approved_read_lets_the_owners_chat_read_and_summarize(tmp_path):
    _install(tmp_path, ["read"])
    state, caller, target = _pair(tmp_path)
    assert get_app(APP)["userSessionReachGrant"] == {"approved": ["read"], "staged": []}

    assert _authorize(state, caller, "read") is target
    assert _authorize(state, caller, "summary") is target
    # Read does not imply send.
    assert _refusal(state, caller, "send") == "app_scoped_target"


def test_an_approved_send_queues_the_message_with_the_sender_tag(tmp_path):
    _install(tmp_path, ["read", "send"])
    state, caller, target = _pair(tmp_path)
    _busy(target)

    out = asyncio.run(
        sc.send_to_target(
            state, caller_session_key=_key(caller), target=TARGET, message="follow up"
        )
    )

    assert out["started"] is False
    entry = next(q for q in target._queue if "follow up" in q.get("content", ""))
    relay = (entry.get("meta") or {}).get("relay")
    assert relay, "the queued send must name the session that sent it"
    assert relay["via"] == "session_send"
    assert relay["senderKey"] == caller.key


@pytest.mark.parametrize(
    "operation",
    [
        "stop",
        "close",
        "end_wait",
        "reload",
        "rename",
        "set_color",
        "set_model",
        "fork",
        "adopt",
        "release",
        "handoff",
    ],
)
def test_the_opt_in_covers_no_other_verb(tmp_path, operation):
    _install(tmp_path, ["read", "send"])
    state, caller, _target = _pair(tmp_path)
    assert _refusal(state, caller, operation) == "app_scoped_target"


def test_a_disabled_app_withdraws_the_reach(tmp_path):
    _install(tmp_path, ["read"])
    state, caller, target = _pair(tmp_path)
    assert _authorize(state, caller) is target

    assert disable_app(APP).ok
    assert _refusal(state, caller) == "app_scoped_target"


def test_the_gate_reads_the_primed_verdict_and_no_file(tmp_path, monkeypatch):
    """``authorize_target`` runs on the event loop, so the app's files are read before it."""
    _install(tmp_path, ["read"])
    state, caller, target = _pair(tmp_path)
    asyncio.run(sc.prewarm_app_reach(state))

    def _no_disk(app):
        raise AssertionError(f"the gate read {app}'s files")

    monkeypatch.setattr(usr, "_allowed_verbs", _no_disk)
    gated = sc.authorize_target(
        state, caller_session_key=_key(caller), target=TARGET, operation="read"
    )
    assert gated is target


def test_a_gate_with_no_prime_fails_closed(tmp_path):
    _install(tmp_path, ["read"])
    state, caller, _target = _pair(tmp_path)
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(state, caller_session_key=_key(caller), target=TARGET, operation="read")
    assert exc.value.code == "app_scoped_target"


def test_a_stale_verdict_allows_nothing(tmp_path, monkeypatch):
    clock = ManualClock()
    clock.install(monkeypatch, usr)
    _install(tmp_path, ["read"])
    state, caller, target = _pair(tmp_path)
    asyncio.run(sc.prewarm_app_reach(state))

    def _gate():
        return sc.authorize_target(
            state, caller_session_key=_key(caller), target=TARGET, operation="read"
        )

    clock.advance(usr._VERDICT_TTL_SECS)
    assert _gate() is target
    clock.advance(1.0)
    with pytest.raises(sc.SessionControlError) as exc:
        _gate()
    assert exc.value.code == "app_scoped_target"


def test_a_slower_prime_that_read_first_does_not_replace_a_newer_verdict(monkeypatch):
    """A prime that read before a disable must not overwrite the refusal read after it."""
    clock = ManualClock()
    clock.install(monkeypatch, usr)
    calls: list[list[str]] = []

    def _verbs_of(apps):
        # The first read is overtaken: a newer prime runs to the end before it returns.
        calls.append(list(apps))
        if len(calls) == 1:
            clock.advance(1.0)
            asyncio.run(usr.prime_user_session_reach(apps))
            return {APP: frozenset({"read"})}
        return {APP: frozenset()}

    monkeypatch.setattr(usr, "_allowed_verbs_of", _verbs_of)

    asyncio.run(usr.prime_user_session_reach([APP]))

    assert len(calls) == 2
    assert not usr.app_allows_user_session_reach(APP, "read")


def test_another_apps_grant_does_not_reach_this_apps_sessions(tmp_path):
    _install(tmp_path, ["read"])
    state, caller, _target = _pair(tmp_path)
    _slot(state, "chat-other", app="other-app")
    asyncio.run(sc.prewarm_app_reach(state))
    with pytest.raises(sc.SessionControlError) as exc:
        sc.authorize_target(
            state, caller_session_key=_key(caller), target="chat-other", operation="read"
        )
    assert exc.value.code == "app_scoped_target"


def test_the_targets_own_containment_still_refuses(tmp_path):
    """The opt-in lifts the app clause alone: a linked app session stays linked."""
    _install(tmp_path, ["read"])
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _slot(state, TARGET, app=APP, linked_session_key="slack:1777.0001")
    assert _refusal(state, caller) == "linked_session_target"


# ── the owner's approval on install and update ──────────────────────────────


def test_an_update_that_first_declares_it_waits_for_the_owner(tmp_path):
    _install(tmp_path, None)
    _update(tmp_path, ["read"])
    state, caller, target = _pair(tmp_path)

    assert get_app(APP)["userSessionReachGrant"] == {"approved": [], "staged": ["read"]}
    assert _refusal(state, caller) == "app_scoped_target"

    approved = enable_app(APP, grants_consent={usr.GRANT_FAMILY: ["read"]})
    assert approved.ok, approved.error
    assert get_app(APP)["userSessionReachGrant"] == {"approved": ["read"], "staged": []}
    assert _authorize(state, caller) is target


def test_an_update_that_widens_it_holds_back_only_the_new_verb(tmp_path):
    _install(tmp_path, ["read"])
    _update(tmp_path, ["read", "send"])
    state, caller, target = _pair(tmp_path)

    assert get_app(APP)["userSessionReachGrant"] == {"approved": ["read"], "staged": ["send"]}
    assert _authorize(state, caller, "read") is target
    assert _refusal(state, caller, "send") == "app_scoped_target"

    assert enable_app(APP, grants_consent={usr.GRANT_FAMILY: ["send"]}).ok
    assert _authorize(state, caller, "send") is target


def test_a_set_stored_before_the_field_existed_approves_nothing(tmp_path):
    """A pre-existing install whose manifest gains the field in place is not approved.

    Every other family a stored set predates approves what is declared; this one
    could not have been declared when that set was written.
    """
    _install(tmp_path, ["read"])
    _store_a_set_from_before_the_field()
    state, caller, target = _pair(tmp_path)

    assert get_app(APP)["userSessionReachGrant"] == {"approved": [], "staged": ["read"]}
    assert _refusal(state, caller) == "app_scoped_target"

    assert enable_app(APP, grants_consent={usr.GRANT_FAMILY: ["read"]}).ok
    assert _authorize(state, caller) is target


def _store_a_set_from_before_the_field() -> None:
    """Rewrite the stored set as a host from before the field wrote it: no such family."""
    stored = json.loads(protected_app_state.read_grants_text(APP) or "{}")
    stored.pop(usr.GRANT_FAMILY)
    protected_app_state.write_grants_text(APP, json.dumps(stored))


def test_approving_one_verb_on_a_set_from_before_the_field_approves_only_that_verb(tmp_path):
    _install(tmp_path, ["read", "send"])
    _store_a_set_from_before_the_field()
    state, caller, target = _pair(tmp_path)

    assert enable_app(APP, grants_consent={usr.GRANT_FAMILY: ["read"]}).ok

    assert get_app(APP)["userSessionReachGrant"] == {"approved": ["read"], "staged": ["send"]}
    assert _authorize(state, caller, "read") is target
    assert _refusal(state, caller, "send") == "app_scoped_target"


def test_an_update_of_a_set_from_before_the_field_approves_no_verb(tmp_path):
    """Both manifests declaring a verb is not the owner's approval of it."""
    _install(tmp_path, ["read", "send"])
    _store_a_set_from_before_the_field()
    _update(tmp_path, ["read", "send"])
    state, caller, _target = _pair(tmp_path)

    assert get_app(APP)["userSessionReachGrant"] == {"approved": [], "staged": ["read", "send"]}
    assert _refusal(state, caller, "read") == "app_scoped_target"
    assert _refusal(state, caller, "send") == "app_scoped_target"


# ── callers the opt-in does not reach ───────────────────────────────────────


def _cron_caller(state):
    state.crons.list_jobs.return_value = [SimpleNamespace(id="abc123", created_by="U0123ABCD")]
    return _slot(state, "cron-abc123")


def _agent_created(state):
    slot = _slot(state, "chat-7")
    slot._created_by = "chat-1"
    return slot


def _remote(state):
    slot = _slot(state, "chat-7")
    slot.executor = "remote"
    return slot


def _mirrored(state):
    slot = _slot(state, "chat-7")
    state.sessions.set_mirror_link(slot_history_key(slot), "C123", "1777.0")
    return slot


@pytest.mark.parametrize(
    "make_caller, code",
    [
        (lambda s: _slot(s, "chat-7", memory_mode="incognito"), "ephemeral_caller"),
        (lambda s: _slot(s, "chat-7", memory_mode="temporary"), "ephemeral_caller"),
        (lambda s: _slot(s, "chat-7", app="other-app"), "app_scoped_caller"),
        (lambda s: _slot(s, "chat-7", app=APP), "app_scoped_caller"),
        (_cron_caller, "app_scoped_target"),
        (lambda s: _slot(s, "workflow-run7"), "unattended_caller"),
        (_agent_created, "app_scoped_target"),
        (_remote, "app_scoped_target"),
        (lambda s: _slot(s, "chat-7", linked_session_key="slack:1777.0002"), None),
        (_mirrored, "mirrored_caller"),
    ],
    ids=[
        "incognito",
        "temporary",
        "other-app",
        "same-app",
        "cron",
        "workflow",
        "agent-created",
        "remote",
        "channel-linked",
        "mirrored",
    ],
)
def test_a_caller_that_is_not_the_owners_own_chat_stays_refused(tmp_path, make_caller, code):
    _install(tmp_path, ["read", "send"])
    state, owner, target = _pair(tmp_path)
    assert _authorize(state, owner) is target, "precondition: the owner's chat is admitted"

    caller = make_caller(state)
    with pytest.raises(sc.SessionControlError) as exc:
        _authorize(state, caller)
    if code is None:
        # A channel link is refused by the caller-side gate, under one of its codes.
        assert exc.value.code in {"linked_session_caller", "mirrored_caller"}
    else:
        assert exc.value.code == code


def test_a_bots_chat_tab_stays_refused(tmp_path, monkeypatch):
    """A chat tab bound to a Bot's own memory store is the Bot's session, not the owner's.

    The member check reads the Bot's config record; the case under test is what the
    gate does with its answer, so the answer is patched where the gate reads it.
    """
    _install(tmp_path, ["read"])
    state, _owner, _target = _pair(tmp_path)
    caller = _slot(state, "chat-7")
    monkeypatch.setattr(sc, "_member_caller", lambda _state, key: key == caller.key)

    with pytest.raises(sc.SessionControlError) as exc:
        _authorize(state, caller)
    assert exc.value.code == "app_scoped_target"
    assert "Bot's session" in exc.value.message


def test_an_owner_dm_the_caller_gate_admits_is_still_refused(tmp_path, monkeypatch):
    """The owner-DM exemption admits a mirrored caller elsewhere, but not here."""
    _install(tmp_path, ["read"])
    state, _owner, _target = _pair(tmp_path)
    caller = _mirrored(state)
    real = ta.judge_owner_dm

    def _admitted(state_, slot):
        verdict = real(state_, slot)
        return dataclasses.replace(verdict, refusal="") if slot is caller else verdict

    monkeypatch.setattr(ta, "judge_owner_dm", _admitted)

    with pytest.raises(sc.SessionControlError) as exc:
        _authorize(state, caller)
    assert exc.value.code == "app_scoped_target"
    assert "mirrored to a channel" in exc.value.message


# ── revive ──────────────────────────────────────────────────────────────────


def test_revive_still_refuses_an_archived_session_of_an_app_that_opted_in(tmp_path):
    _install(tmp_path, ["read", "send"])
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    peer = _slot(state, "chat-2")
    peer.messages.append({"role": "user", "content": "m0"})
    peer._dirty = True
    asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=peer.key))
    state.conversation_log.update_metadata(f"dashboard:{peer.key}", {"app": APP})

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.revive_session(state, caller_session_key=_key(caller), target=peer.key))

    assert exc.value.code == "app_scoped_target"
    assert peer.key not in state._slots
