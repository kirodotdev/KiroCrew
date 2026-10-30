"""Session control: reviving an archived session -- the mirror of close.

Organized like ``test_session_control.py``: every refusal is asserted against
the REAL slot objects and the REAL history metadata, because ``revive_session``
reads ``workspace`` / ``app`` / ``created_by`` off the persisted metadata line
and a permissive double would let a dead guard look alive. The happy path goes
end to end through the real close (``close_target``) and the real resume core
(``resume_slot_from_history``), so the round trip a person expects -- close a
tab, revive it, find the transcript -- is what is exercised.

``test_session_control_revive_containment_hook.py`` holds the tests that drive
``resume_slot_from_history`` through its ``containment`` hook and ``final_check``, the
History click's own refusals, and the revive cases filed beside them: the global cap at
the hook, the ineligible caller's single answer, live targets authorized before they are
named, and channel links recorded during the deferred clear or the transcript read. Both
files import the autouse fixtures and the ``_slot``, ``_archive``, ``_revive`` and
``_folder`` helpers from ``session_control_revive_helpers.py``.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state
from session_control_revive_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _archive,
    _enabled,
    _folder,
    _fresh_windows,
    _key,
    _revive,
    _settle_metadata_rewrite_before_read,
    _slot,
)

from kiro_crew.dashboard import session_control as sc
from kiro_crew.testing.wait import async_wait_until

# ── Happy path ───────────────────────────────────────────────────────────────


def test_revive_brings_a_closed_peer_back_with_its_transcript(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    peer = _slot(state, "chat-2")
    key = _archive(state, caller, peer, messages=3, title="Prod Test Account Lookup")

    result = _revive(state, caller, key)

    assert result["ok"] is True
    assert result["target"] == key
    assert result["title"] == "Prod Test Account Lookup"
    assert result["messages"] == 3
    assert "already_live" not in result
    live = state._slots[key]
    assert [m["content"] for m in live.messages] == ["m0", "m1", "m2"]
    assert live.running is False
    # The reopen is durable: the closed flag is cleared so a restart restores it.
    assert not state.conversation_log.get_metadata(f"dashboard:{key}").get("closed")


def test_a_revive_in_the_close_clock_reading_keeps_the_marker(tmp_path, monkeypatch):
    """A revive that starts in the same clock reading as the close is refused,
    and the session stays archived.

    The resume's compare-and-clear keeps any ``closed`` stamped at or after the
    moment the resume started. Inside one clock reading it cannot tell the close
    it read from a new close that landed just after, so keeping the marker is
    the safe answer. The clock is frozen here so the tie happens on every OS; on
    Windows CPython 3.12 ``time.time()`` moves every ~15.6 ms, so an unfrozen
    close and revive tie there by themselves, which is why ``_archive`` backdates
    the close for every other test in this file."""
    frozen = [time.time()]
    monkeypatch.setattr(time, "time", lambda: frozen[0])
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    peer = _slot(state, "chat-2")
    peer.messages.append({"role": "user", "content": "m0"})
    peer._dirty = True
    key = peer.key
    hk = f"dashboard:{key}"
    asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=key))
    assert state.conversation_log.get_metadata(hk).get("closed_at") == frozen[0]

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)

    assert exc.value.code == "resume_conflict"
    assert key not in state._slots and key not in state._slots_under_construction
    assert state.conversation_log.get_metadata(hk).get("closed") is True

    # One clock step later the same revive lands and clears the marker.
    frozen[0] += 1.0
    assert _revive(state, caller, key)["target"] == key
    assert "closed" not in state.conversation_log.get_metadata(hk)


@pytest.mark.parametrize(
    "spelling",
    [
        lambda k: k,  # slot key
        lambda k: f"dashboard:{k}",  # session key
        lambda k: f"dashboard_{k}",  # transcript stem, as list_sessions reports it
    ],
)
def test_revive_accepts_every_key_spelling_a_caller_holds(tmp_path, spelling):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))

    assert _revive(state, caller, spelling(key))["target"] == key


def test_revive_accepts_a_unique_title_case_insensitively(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"), title="Prod Test Account Lookup")

    assert _revive(state, caller, "prod test account lookup")["target"] == key


def test_revive_does_not_transfer_ownership_to_the_reviver(tmp_path, monkeypatch):
    """Reviving is not creating: the session keeps the creator it had, when the
    gateway-authored lineage corroborates the metadata line's claim."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    peer = _slot(state, "chat-2")
    peer._created_by = "chat-9"
    key = _archive(state, caller, peer)
    _lineage(monkeypatch, {key: "chat-9"})

    _revive(state, caller, key)

    assert state._slots[key]._created_by == "chat-9"


@pytest.mark.parametrize("creator_is_reviver", [True, False], ids=["creator", "other-reviver"])
def test_a_revived_worker_takes_its_creators_posture_only_from_its_creator(
    tmp_path, monkeypatch, creator_is_reviver
):
    """A conductor that brings back its own worker hands it its posture, as create
    does at birth; a session that merely revives someone else's worker hands none."""
    state = _make_state(tmp_path)
    conductor = _slot(state, "chat-1")
    conductor._trust = True
    reviver = conductor if creator_is_reviver else _slot(state, "chat-3")
    reviver._trust = True
    peer = _slot(state, "chat-2")
    peer._created_by = conductor.key
    key = _archive(state, conductor, peer)
    _lineage(monkeypatch, {key: conductor.key})

    _revive(state, reviver, key)

    revived = state._slots[key]
    assert revived._trust is creator_is_reviver
    assert revived._posture_inherited is creator_is_reviver


def test_a_revived_crewmate_worker_is_left_to_its_profile(tmp_path, monkeypatch):
    """A crewmate's own worker takes its Profile Permissions at its first turn, never
    the conductor's Trust: a grant already on the slot would survive that seed."""
    from kiro_crew.mate_worker import MATE_WORKER_AGENT

    state = _make_state(tmp_path)
    conductor = _slot(state, "chat-1")
    conductor._trust = True
    peer = _slot(state, "chat-2")
    peer.agent = MATE_WORKER_AGENT
    peer._created_by = conductor.key
    key = _archive(state, conductor, peer)
    _lineage(monkeypatch, {key: conductor.key})

    _revive(state, conductor, key)

    revived = state._slots[key]
    assert revived.agent == MATE_WORKER_AGENT
    assert revived._trust is False
    assert revived._posture_inherited is False


@pytest.mark.parametrize("known", [True, False], ids=["lineage-names-another", "lineage-unknown"])
def test_an_uncorroborated_created_by_claim_is_not_stamped_on_the_revived_slot(
    tmp_path, monkeypatch, known
):
    """``created_by`` is read from an agent-editable file. A fenced agent that wrote
    its own key into an archived transcript must not be handed the slot when an
    UNFENCED tab revives it (the reviver's own ownership is not gated), so the
    stamp is taken only from creation records that name the same creator. Otherwise
    the slot gets a creator no caller matches and stays fenced: blank would read as
    a person's own tab."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    peer = _slot(state, "chat-2")
    peer._created_by = "member-agent"  # what a tampered metadata line would say
    key = _archive(state, caller, peer)
    _lineage(monkeypatch, {key: "chat-9"}, known=known)

    _revive(state, caller, key)

    from kiro_crew.dashboard.session_control_parts.caller_access import (
        _caller_is_ownership_fenced,
    )
    from kiro_crew.dashboard.session_control_parts.session_revive import UNVERIFIED_CREATOR

    revived = state._slots[key]
    assert revived._created_by == UNVERIFIED_CREATOR
    assert sc._created_by_other(revived, "member-agent")
    assert _caller_is_ownership_fenced(state, key)


# ── Filing ───────────────────────────────────────────────────────────────────


def test_revive_files_into_the_requested_folder(tmp_path):
    state = _make_state(tmp_path)
    _folder(state, "f1", "Gamma")
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))

    result = _revive(state, caller, key, folder_id="f1")

    assert result["folder_id"] == "f1"
    assert result["filed"] is True
    assert state._slots[key].folder_id == "f1"
    assert state.conversation_log.get_metadata(f"dashboard:{key}").get("folder_id") == "f1"


def test_revive_refuses_an_unknown_folder_before_touching_history(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key, folder_id="nope")

    assert exc.value.code == "folder_not_found"
    assert key not in state._slots
    assert state.conversation_log.get_metadata(f"dashboard:{key}").get("closed")


def test_revive_without_a_folder_keeps_the_previous_placement(tmp_path):
    state = _make_state(tmp_path)
    _folder(state, "f1", "Gamma")
    caller = _slot(state, "chat-1")
    peer = _slot(state, "chat-2")
    peer.folder_id = "f1"
    key = _archive(state, caller, peer)

    result = _revive(state, caller, key)

    assert result["folder_id"] == "f1"
    assert result["filed"] is False
    assert state._slots[key].folder_id == "f1"


# ── Resolution refusals ──────────────────────────────────────────────────────


def test_revive_refuses_a_live_target_and_names_its_key(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    live = _slot(state, "chat-2")
    live.title = "Open one"
    live._titled = True

    for target in (live.key, "Open one"):
        with pytest.raises(sc.SessionControlError) as exc:
            _revive(state, caller, target)
        assert exc.value.code == "target_already_live"
        assert exc.value.status == 409
        assert live.key in exc.value.message


def test_revive_refuses_an_unknown_target(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, "chat-404")

    assert exc.value.code == "target_not_found"
    assert exc.value.status == 404


def test_revive_refuses_an_ambiguous_title(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    _archive(state, caller, _slot(state, "chat-2"), title="Same name")
    _archive(state, caller, _slot(state, "chat-3"), title="Same name")

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, "Same name")

    assert exc.value.code == "ambiguous_target"
    assert exc.value.status == 409
    assert "chat-2" not in state._slots and "chat-3" not in state._slots


def test_revive_refuses_a_member_thread(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    log = state.conversation_log
    log.append("dashboard:member-alpha", "user", "hi")
    log.update_metadata("dashboard:member-alpha", {"closed": True, "mode": "member"})

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, "member-alpha")

    assert exc.value.code == "member_thread_target"


# ── Containment read from metadata ───────────────────────────────────────────


def _archived_with(state, caller, name: str, fields: dict) -> str:
    key = _archive(state, caller, _slot(state, name))
    state.conversation_log.update_metadata(f"dashboard:{key}", fields)
    return key


@pytest.mark.parametrize(
    "fields, code",
    [
        ({"workspace": "other"}, "workspace_mismatch"),
        ({"app": "some-app"}, "app_scoped_target"),
        ({"linked_session_key": "slack:123.456"}, "linked_session_target"),
        ({"channel_origin": "slack"}, "linked_session_target"),
        ({"memory_mode": "incognito"}, "ephemeral_target"),
    ],
)
def test_revive_applies_the_target_containment_from_metadata(tmp_path, fields, code):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archived_with(state, caller, "chat-2", fields)

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)

    assert exc.value.code == code
    assert key not in state._slots


def test_revive_refuses_an_unattended_target(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    log = state.conversation_log
    log.append("dashboard:cron-job1", "user", "hi")
    log.update_metadata("dashboard:cron-job1", {"closed": True})

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, "cron-job1")

    assert exc.value.code == "unattended_target"


@pytest.mark.parametrize(
    "spelling, code",
    [
        ("dashboard:dashboard:cron-job1", "unattended_target"),
        ("dashboard_dashboard_cron-job1", "unattended_target"),
        ("dashboard:dashboard:member-abc", "member_thread_target"),
    ],
)
def test_a_doubled_transport_prefix_cannot_slip_past_the_prefix_guards(tmp_path, spelling, code):
    """``_normalize_slot_key`` strips one prefix per call, so a doubled prefix
    folds to ``dashboard_cron-job1``: the guards would not match it, while its
    history key is the real ``dashboard:cron-job1`` transcript. The resolver
    folds to a fixed point, so the guards see the key that would be revived."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    log = state.conversation_log
    real = "dashboard:cron-job1" if "cron-" in spelling else "dashboard:member-abc"
    log.append(real, "user", "hi")
    log.update_metadata(real, {"closed": True})

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, spelling)

    assert exc.value.code == code
    assert not any(k.startswith("dashboard_") for k in state._slots)


def test_fenced_caller_may_revive_only_what_it_created(tmp_path, monkeypatch):
    """The ownership fence reads ``created_by`` off the metadata line, the same
    field ``authorize_target`` reads off a live slot's ``_created_by`` -- and, for
    a fenced caller, corroborates it against the crew-log lineage."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    own = _slot(state, "chat-2")
    own._created_by = caller.key
    foreign = _slot(state, "chat-3")
    foreign._created_by = "chat-7"
    own_key = _archive(state, caller, own)
    foreign_key = _archive(state, caller, foreign)
    _lineage(monkeypatch, {own_key: caller.key, foreign_key: "chat-7"})

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, foreign_key, caller_fenced=True)
    assert exc.value.code == "not_creator"
    assert foreign_key not in state._slots

    assert _revive(state, caller, own_key, caller_fenced=True)["target"] == own_key


def test_a_send_revive_keeps_the_fence_the_request_was_admitted_under(tmp_path, monkeypatch):
    """``session_send``'s own-worker revive carries the request's admission fence.
    Recomputed from config instead, a conductor whose binding moved mid-request
    reads as an unfenced owner chat and reopens an archive it did not create."""
    from kiro_crew import mate_worker
    from kiro_crew.dashboard.session_control_parts import session_revive

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    foreign = _slot(state, "chat-3")
    foreign._created_by = "chat-7"
    foreign_key = _archive(state, caller, foreign)
    _lineage(monkeypatch, {foreign_key: "chat-7"})
    conductor = MagicMock(selection_kind="member", template_id=mate_worker.MATE_CONDUCTOR_AGENT)
    monkeypatch.setattr("kiro_crew.execution_context.read_session_execution", lambda key: conductor)

    got = asyncio.run(
        session_revive.revive_own_archived_worker(
            state, _key(caller), foreign_key, caller_fenced=True
        )
    )

    assert got == foreign_key
    assert foreign_key not in state._slots


def _lineage(monkeypatch, parents: dict, *, known: bool = True):
    """Stand in for the crew-log session-tree projection: *parents* maps an
    archived slot key to the creator the gateway recorded for it."""
    monkeypatch.setattr(
        sc, "_slot_tree_parent", lambda slot_key: (known, parents.get(slot_key, ""), {})
    )
    monkeypatch.setattr(
        sc, "_creation_parent", lambda slot_key: parents.get(slot_key, "") if known else ""
    )


def test_fenced_caller_is_refused_when_metadata_claims_ownership_the_lineage_denies(
    tmp_path, monkeypatch
):
    """``created_by`` lives in an agent-editable transcript file. A fenced caller
    that rewrote it to name itself must still be refused: the gateway-authored
    lineage names a different parent, and that record wins."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    forged = _slot(state, "chat-2")
    forged._created_by = caller.key  # what a tampered metadata line would say
    key = _archive(state, caller, forged)
    _lineage(monkeypatch, {key: "chat-9"})

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key, caller_fenced=True)

    assert exc.value.code == "ownership_unverified"
    assert key not in state._slots


def test_fenced_caller_fails_closed_when_the_lineage_is_unreadable(tmp_path, monkeypatch):
    """Crew log off, projection unseeded or incomplete: no trusted record, so a
    fenced caller's ownership claim is refused rather than taken from metadata."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    own = _slot(state, "chat-2")
    own._created_by = caller.key
    key = _archive(state, caller, own)
    _lineage(monkeypatch, {key: caller.key}, known=False)

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key, caller_fenced=True)

    assert exc.value.code == "ownership_unverified"
    assert key not in state._slots


def test_unfenced_caller_does_not_consult_the_lineage(tmp_path, monkeypatch):
    """The person's own tab is not ownership-gated, so the human recovery this
    tool exists for works with the crew log off."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    _lineage(monkeypatch, {}, known=False)

    assert _revive(state, caller, key, caller_fenced=False)["target"] == key


def _drift_in_window(monkeypatch, mutate):
    """Run *mutate(built_slot)* inside the resume's pre-publish window.

    The resume core hands the built, still-unpublished slot to the caller's
    ``containment`` hook; wrapping that hook is how a test models a change that
    lands after the transcript read and before the publish -- a metadata line
    rewritten, a link or mirror recorded, a caller losing eligibility, a cap
    crossed. Also asserts what the core promises for that window: the built slot
    is not resolvable and never in the live table while the hook awaits."""
    from kiro_crew.dashboard import chat_handlers

    original = chat_handlers.resume_slot_from_history
    seen: dict = {}

    async def _wrapped_resume(state_, **kw):
        hook = kw["containment"]

        async def _hook(built):
            seen["resolvable"] = state_.get_slot(built.key) is not None
            seen["in_table"] = built.key in state_._slots
            mutate(built)
            return await hook(built)

        kw["containment"] = _hook
        return await original(state_, **kw)

    monkeypatch.setattr(chat_handlers, "resume_slot_from_history", _wrapped_resume)
    return seen


def _refused_in_window(state, caller, key, monkeypatch, mutate, code):
    seen = _drift_in_window(monkeypatch, mutate)
    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)
    assert exc.value.code == code
    assert key not in state._slots, "a refused revive left a slot published"
    assert state.get_slot(key) is None
    assert state.conversation_log.get_metadata(f"dashboard:{key}").get("closed")
    assert seen == {"resolvable": False, "in_table": False}, seen


def test_a_caller_that_loses_eligibility_in_the_window_is_refused_before_publish(
    tmp_path, monkeypatch
):
    """The caller-side refusals are read before the resume awaits; a caller that
    becomes app-scoped (or linked, or mirrored) in that window must not end up
    holding the revived slot. Re-asserted on the live caller slot in the
    pre-publish hook; the built slot is discarded, never published."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))

    def _scope(_built):
        caller._app = "some-app"

    _refused_in_window(state, caller, key, monkeypatch, _scope, "app_scoped_caller")


def test_a_channel_link_that_lands_in_the_window_is_refused_before_publish(tmp_path, monkeypatch):
    """Same window, other link: a Slack thread binding or inbound link recorded
    in the store while the resume awaited. Later gates read only the slot's own
    fields, so it is caught here or not at all."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))

    def _link(_built):
        state.sessions.set_slack_link(f"dashboard:{key}", "1712793600.1", "C1")

    _refused_in_window(state, caller, key, monkeypatch, _link, "linked_session_target")


def test_a_slot_cap_crossed_in_the_window_is_refused_before_publish(tmp_path, monkeypatch):
    """The ceilings are tested before the resume awaits and the allocation is
    inside it; a second slot landing in that window would leave the table over
    the cap. Re-tested in the pre-publish hook on the live table, inclusive
    because the built slot is retracted while it runs."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    monkeypatch.setattr(sc, "MAX_LIVE_SLOTS", 2)  # caller + the revived slot fit

    def _fill(_built):
        _slot(state, "chat-8")  # a concurrent allocation landed in the window

    _refused_in_window(state, caller, key, monkeypatch, _fill, "slot_cap_reached")


def test_revived_sessions_the_caller_did_not_create_still_charge_its_cap(tmp_path, monkeypatch):
    """Ownership is preserved on revive, so counting ``_created_by`` alone would
    never charge the reviver for a human-created or foreign-created tab; the
    per-caller cap must count what the caller REVIVED as well, or an unfenced
    caller could hold any number of such tabs past its ceiling."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    keys = [_archive(state, caller, _slot(state, f"chat-{n}")) for n in (2, 3)]
    monkeypatch.setattr(sc, "MAX_SLOTS_PER_CREATOR", 1)

    first = _revive(state, caller, keys[0])
    assert first["target"] == keys[0]
    assert getattr(state._slots[keys[0]], "_created_by", "") == ""  # ownership untouched
    assert state._slots[keys[0]]._revived_by == caller.key

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, keys[1])
    assert exc.value.code == "creator_slot_cap_reached"
    assert keys[1] not in state._slots


def test_a_slot_both_created_and_revived_by_the_caller_is_charged_once(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    own = _slot(state, "chat-2")
    own._created_by = caller.key
    own._revived_by = caller.key
    other = _slot(state, "chat-3")
    other._revived_by = caller.key
    assert state.creator_slot_count(caller.key) == 2
    assert state.creator_slot_count("") == 0


def test_create_and_revive_share_one_per_caller_accounting():
    """A caller at the cap through revives must not be able to fill the same
    ceiling again through creates: both verbs gate on the registry's
    ``creator_slot_count``, which charges created AND revived slots."""
    import inspect

    for fn in (sc.create_session, sc.revive_session):
        assert "state.creator_slot_count(caller_key)" in inspect.getsource(fn)


def test_a_creator_cap_crossed_in_the_window_is_refused_before_publish(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    own = _slot(state, "chat-2")
    own._created_by = caller.key
    key = _archive(state, caller, own)
    monkeypatch.setattr(sc, "MAX_SLOTS_PER_CREATOR", 1)

    def _fill(_built):
        _slot(state, "chat-8")._created_by = caller.key

    _refused_in_window(state, caller, key, monkeypatch, _fill, "creator_slot_cap_reached")


# ── Caller-side refusals are the shared ones ─────────────────────────────────


def test_revive_shares_the_caller_side_refusals(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))

    monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)
    assert exc.value.code == "session_control_disabled"
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.revive_session(state, caller_session_key="", target=key))
    assert exc.value.code == "caller_unidentified"

    caller._app = "some-app"
    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)
    assert exc.value.code == "app_scoped_caller"
    assert key not in state._slots


def test_revive_refuses_at_the_slot_cap(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    monkeypatch.setattr(sc, "MAX_LIVE_SLOTS", 1)

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)

    assert exc.value.code == "slot_cap_reached"
    assert exc.value.status == 429


# ── The shared helpers are what authorize_target uses ────────────────────────


def test_authorize_target_and_revive_share_the_caller_checks():
    """The parity the doc promises rests on both verbs calling the same helpers;
    pin that so a refusal added to one cannot silently miss the other."""
    import inspect

    for fn in (sc.authorize_target, sc.revive_session):
        src = inspect.getsource(fn)
        assert "refuse_caller_identity(" in src
        # Called directly or through ``asyncio.to_thread`` -- either spelling is
        # the same helper; the pin is on the helper being the one consulted.
        assert "refuse_caller_surface(" in src or "refuse_caller_surface," in src
        assert "_not_creator_reason(" in src


def test_revive_raises_every_target_side_code_authorize_target_raises():
    """The target-side checks are hand-written against the metadata line (there
    is no live slot to hand ``authorize_target``), so pin them by refusal code: a
    containment rule added to ``authorize_target`` later must show up here or
    this fails, instead of revive silently publishing what the live verbs would
    refuse."""
    import inspect
    import re

    def codes(fn) -> set[str]:
        # The code is always the last positional argument to ``deny(...)``, so
        # match it by shape (``, "code")`` or ``, "code", status=N)``) rather
        # than by walking the reason argument, which may itself hold parens.
        src = inspect.getsource(fn)
        out: set[str] = set()
        for m in re.finditer(r"\bdeny\(", src):
            # Walk to the matching close paren, then read the code argument off the
            # tail of the call text.
            depth, i = 0, m.end() - 1
            while i < len(src):
                depth += {"(": 1, ")": -1}.get(src[i], 0)
                if depth == 0:
                    break
                i += 1
            tail = re.search(r',\s*"([a-z_]+)"(?:,\s*status=[\w.]+)?\s*$', src[m.end() : i])
            if tail:
                out.add(tail.group(1))
        return out

    target_side = codes(sc.authorize_target) - {
        # Caller-side and shape refusals that have no archived counterpart: the
        # caller checks are shared helpers (pinned above), and the live-slot
        # lookup refusals are replaced by ``_resolve_archived_target``'s own.
        "self_target",
        "target_not_found",
        "target_unresolved",
        "ambiguous_target",
        "target_slot_unavailable",
    }

    def returned_codes(fn) -> set[str]:
        # The shared archived-target predicate answers ``(reason, code)``; the code
        # is the last element of each returned tuple.
        import ast
        import textwrap

        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        return {
            node.value.elts[-1].value
            for node in ast.walk(tree)
            if isinstance(node, ast.Return)
            and isinstance(node.value, ast.Tuple)
            and isinstance(node.value.elts[-1], ast.Constant)
        }

    assert target_side, "the regex found no refusal codes in authorize_target"
    revive_codes = codes(sc.revive_session) | returned_codes(sc._archived_target_refusal)
    assert target_side <= revive_codes, target_side - revive_codes


# ── HTTP wrapper ─────────────────────────────────────────────────────────────


def test_route_forwards_folder_id_and_fence_to_the_core(monkeypatch):
    from kiro_crew.dashboard.handlers import session_control as handlers_sc

    seen: dict = {}

    async def _ok(state, **kw):
        seen.update(kw)
        return {"ok": True, "target": "chat-2"}

    monkeypatch.setattr(sc, "revive_session", _ok)
    monkeypatch.setattr(handlers_sc, "_require_internal", _none_async())
    monkeypatch.setattr(handlers_sc, "_read_session_key", lambda request: "dashboard:chat-1")
    monkeypatch.setattr(handlers_sc, "_carried_fence", lambda request: True)

    async def _body(request):
        return {"target": "chat-2", "folder_id": "f1"}

    monkeypatch.setattr(handlers_sc, "_body", _body)
    request = MagicMock()
    request.app = {"state": object()}

    resp = asyncio.run(handlers_sc.api_session_control_revive(request))

    assert resp.status == 200
    assert seen == {
        "caller_session_key": "dashboard:chat-1",
        "target": "chat-2",
        "folder_id": "f1",
        "caller_fenced": True,
    }


def _none_async():
    async def _f(request):
        return None

    return _f


def test_route_is_registered_and_strict():
    """Every /api/session-control route must be in the strict-auth list too, or
    the MCP caller's internal secret is ignored and the tool is unreachable."""
    from kiro_crew.dashboard import server

    assert "/api/session-control/revive" in server._STRICT_INTERNAL_API_PATHS


# ── MCP tool ─────────────────────────────────────────────────────────────────


def _revive_tool(args: dict, routes: dict, caller=None):
    """One ``session_revive`` frame through the dashboard table, in memory."""
    from kiro_crew.mcp_dashboard import TABLE
    from kiro_crew.mcp_tools.dashboard_client import InMemoryDashboardClient
    from kiro_crew.mcp_tools.table import Caller, ToolContext

    dash = InMemoryDashboardClient(routes)
    ctx = ToolContext(dash, caller or Caller.strict("dashboard:chat-1"))
    return TABLE.call("session_revive", args, ctx), dash.requests


def test_mcp_tool_posts_to_the_revive_route_with_the_callers_key():
    reply = {"ok": True, "target": "chat-2", "title": "Lookup", "messages": 4, "filed": False}
    out, (post,) = _revive_tool({"target": "chat-2"}, {"POST /api/session-control/revive": reply})

    assert (post.path, post.body, post.session_key) == (
        "/api/session-control/revive",
        {"target": "chat-2"},
        "dashboard:chat-1",
    )
    assert "Revived `chat-2`" in out and "4 messages" in out


def test_mcp_tool_refuses_a_caller_without_a_strict_key():
    from kiro_crew.mcp_tools.table import Caller

    out, requests = _revive_tool(
        {"target": "chat-2"},
        {"POST /api/session-control/revive": {"ok": True}},
        Caller.unverified("dashboard:chat-1", diagnosis=" [no channel]"),
    )
    assert out.startswith("Error: this session cannot be identified well enough")
    assert out.endswith(" [no channel]")
    assert requests == []


def test_mcp_tool_names_the_live_key_when_the_target_is_open():
    refused = {"error": "'x' is already open as `chat-5`; address it directly"}
    out, _ = _revive_tool({"target": "x"}, {"POST /api/session-control/revive": refused})
    assert out.startswith("Error: could not revive") and "chat-5" in out


# ── Outbound mirror, read from the session store ─────────────────────────────


def _mirror_store(state, links: dict):
    """Give the mocked session store a ``get_mirror_link`` answering from *links*."""
    state.sessions.get_mirror_link = lambda key: links.get(key)


def test_revive_refuses_an_archived_session_with_an_outbound_mirror(tmp_path):
    """The link lives in the session store, not the transcript, so the
    ``linked_session_key`` metadata check alone would miss it."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    link = MagicMock(channel_type="slack", channel_id="C1", thread_id="1.2")
    _mirror_store(state, {f"dashboard:{key}": link})

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)

    assert exc.value.code == "mirrored_target"
    assert key not in state._slots


def test_a_channel_link_recorded_by_the_gateway_refuses_even_when_metadata_is_clean(tmp_path):
    """The metadata line can be edited to drop ``linked_session_key``; the
    session store is gateway-owned and is read as well."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    assert not state.conversation_log.get_metadata(f"dashboard:{key}").get("linked_session_key")
    state.sessions.set_origin_link(f"dashboard:{key}", MagicMock(channel_type="telegram"))

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)
    assert exc.value.code == "linked_session_target"
    assert key not in state._slots


def test_a_slack_thread_binding_in_the_store_refuses_the_revive(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    state.sessions.set_slack_link(f"dashboard:{key}", "1712793600.1", "C1")

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)
    assert exc.value.code == "linked_session_target"


def test_the_channel_link_probe_fails_closed_when_the_store_cannot_answer(tmp_path):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))

    def _boom(k):
        raise RuntimeError("store down")

    state.sessions.get_origin_link = _boom
    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)
    assert exc.value.code == "linked_session_target"


def test_revive_fails_closed_when_the_mirror_store_cannot_answer(tmp_path):
    """A store that cannot answer counts as mirrored on BOTH sides. The caller's
    own probe runs first (before the target is even resolved), so a store that is
    down for everyone refuses on the caller; a store that fails only for the
    target's key refuses on the target."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    real = state.sessions.get_mirror_link

    def _boom(_key):
        raise RuntimeError("store down")

    state.sessions.get_mirror_link = _boom
    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)
    assert exc.value.code == "mirrored_caller"

    def _boom_for_target(probe_key):
        if probe_key == f"dashboard:{key}":
            raise RuntimeError("store down for this key")
        return real(probe_key)

    state.sessions.get_mirror_link = _boom_for_target
    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)
    assert exc.value.code == "mirrored_target"


def test_a_mirror_that_lands_in_the_window_is_refused_before_publish(tmp_path, monkeypatch):
    """The resume core awaits before it publishes; a mirror attached in that
    window is caught on the built slot in the pre-publish hook, so the session is
    never open at all rather than opened and then undone."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    links: dict = {}
    _mirror_store(state, links)

    def _attach(_built):
        links[f"dashboard:{key}"] = MagicMock(channel_type="slack", channel_id="C1", thread_id="1")

    _refused_in_window(state, caller, key, monkeypatch, _attach, "mirrored_target")


def test_the_history_scan_runs_off_the_event_loop(tmp_path, monkeypatch):
    """`list_sessions` and `get_metadata_status` are disk reads; the revive
    path must reach them only through ``asyncio.to_thread``."""
    import threading

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"), title="Scan me")
    log = state.conversation_log
    loop_thread = threading.get_ident()
    seen: list[bool] = []
    real_list = log.list_sessions

    def _list():
        seen.append(threading.get_ident() == loop_thread)
        return real_list()

    monkeypatch.setattr(log, "list_sessions", _list)

    assert _revive(state, caller, "scan me")["target"] == key
    assert seen and not any(seen), "list_sessions ran on the event loop thread"


# ── Budgets ──────────────────────────────────────────────────────────────────


def test_revive_spends_the_create_budget_and_per_creator_cap(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))

    monkeypatch.setattr(sc, "allow_create", lambda kind, who: False)
    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)
    assert exc.value.code == "create_rate_limited"
    monkeypatch.setattr(sc, "allow_create", lambda kind, who: True)

    monkeypatch.setattr(sc, "MAX_SLOTS_PER_CREATOR", 0)
    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)
    assert exc.value.code == "creator_slot_cap_reached"
    assert key not in state._slots


def test_a_filing_failure_after_the_revive_does_not_fail_the_revive(tmp_path, monkeypatch):
    """The slot is live once the resume core published it; a folder-store error
    afterwards is logged, the placement rolls back, and the revive still
    reports success -- otherwise the caller retries into `target_already_live`."""
    state = _make_state(tmp_path)
    _folder(state, "f1", "Gamma")
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))

    async def _boom(state_, fid):
        raise OSError("folder store unreadable")

    monkeypatch.setattr(sc, "_unhide_folder", _boom)
    result = _revive(state, caller, key, folder_id="f1")

    assert result["ok"] is True and result["filed"] is False
    assert state._slots[key].folder_id == ""


def test_a_filing_save_that_raises_reports_filed_false_not_a_pending_flush(tmp_path, monkeypatch):
    """The durable save behind the filing is strict: a raised write must not be
    swallowed into a "dirty, flush later" True that the reply reports as
    ``filed: true`` -- a restart before that flush would restore the old folder.
    The placement rolls back and the revive still succeeds."""
    state = _make_state(tmp_path)
    _folder(state, "f1", "Gamma")
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    seen: dict = {}

    async def _strict_then_boom(state_, slot, **kw):
        seen["best_effort"] = kw.get("best_effort")
        raise OSError("disk full")

    monkeypatch.setattr(sc, "save_slot_off_loop", _strict_then_boom)
    result = _revive(state, caller, key, folder_id="f1")

    assert seen["best_effort"] is False
    assert result["ok"] is True and result["filed"] is False
    assert state._slots[key].folder_id == ""


def test_a_failed_filing_does_not_erase_a_placement_committed_meanwhile(tmp_path, monkeypatch):
    """The filing's un-hide and save are awaits on an already-published slot. A
    writer that does not take the txn lock (the folder-delete unfile loop) can
    commit a different placement inside them; the rollback compares the live value
    to this call's own write and leaves any other writer's value alone, the guard
    ``api_chat_slot_folder`` applies."""
    state = _make_state(tmp_path)
    _folder(state, "f1", "Gamma")
    _folder(state, "f2", "Delta")
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))

    async def _commit_other_then_boom(state_, slot, **kw):
        slot.folder_id = "f2"  # another writer's placement lands inside the save
        raise OSError("disk full")

    monkeypatch.setattr(sc, "save_slot_off_loop", _commit_other_then_boom)
    result = _revive(state, caller, key, folder_id="f1")

    assert result["ok"] is True and result["filed"] is False
    assert state._slots[key].folder_id == "f2"


def test_the_filing_runs_under_the_slot_metadata_txn_lock(tmp_path, monkeypatch):
    """Same lock the folder endpoint serializes its mutate/save/rollback under,
    so a PATCH cannot commit between this call's write and its rollback."""
    from kiro_crew.dashboard import chat_folders

    state = _make_state(tmp_path)
    _folder(state, "f1", "Gamma")
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    held: list[bool] = []

    async def _observe(state_, slot, **kw):
        held.append(chat_folders._slot_meta_txn_lock(state_).locked())
        return True

    monkeypatch.setattr(sc, "save_slot_off_loop", _observe)
    result = _revive(state, caller, key, folder_id="f1")

    assert result["filed"] is True
    assert held == [True]


def test_a_cancellation_during_the_filing_rolls_back_and_still_audits_the_revive(
    tmp_path, monkeypatch
):
    """The revive is committed before the filing. A ``CancelledError`` inside the
    filing's awaits (gateway shutdown, the MCP client's timeout) must not skip
    the rollback, and the allowed SEL record for the committed revive must be
    written before the cancellation propagates."""
    state = _make_state(tmp_path)
    _folder(state, "f1", "Gamma")
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    audits: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))

    async def _cancelled_save(state_, slot, **kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr(sc, "save_slot_off_loop", _cancelled_save)

    with pytest.raises(asyncio.CancelledError):
        _revive(state, caller, key, folder_id="f1")

    revived = state._slots[key]
    assert revived.folder_id == ""  # this call's provisional placement undone
    assert revived._dirty is True
    allowed = [a for a in audits if a["operation"] == "revive" and a["outcome"] == "allowed"]
    assert len(allowed) == 1 and allowed[0]["slot_key"] == key
    assert allowed[0]["detail"]["filed"] == "false"


def test_a_cancellation_while_waiting_for_the_filing_lock_still_audits_once(tmp_path, monkeypatch):
    """The lock acquisition is itself an await. A cancellation delivered while
    the filing waits for a contended ``_slot_meta_txn_lock`` has written nothing
    to roll back but must still leave exactly one allowed SEL record for the
    revive, which the resume core has already committed."""
    from kiro_crew.dashboard import chat_folders

    state = _make_state(tmp_path)
    _folder(state, "f1", "Gamma")
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    audits: list[dict] = []
    monkeypatch.setattr(sc, "_audit", lambda **kw: audits.append(kw))
    saves: list[str] = []

    async def _record_save(state_, slot, **kw):
        saves.append(slot.key)
        return True

    monkeypatch.setattr(sc, "save_slot_off_loop", _record_save)

    async def _run():
        lock = chat_folders._slot_meta_txn_lock(state)
        async with lock:  # another writer holds the state-wide lock
            task = asyncio.ensure_future(
                sc.revive_session(
                    state, caller_session_key=_key(caller), target=key, folder_id="f1"
                )
            )
            # Let the revive commit and reach the lock wait, then cancel it there.
            # The resume core's probes run off the loop, so this is a real wait.
            await async_wait_until(
                lambda: key in state._slots and lock._bound()._waiters,
                describe=lambda: f"slot={key in state._slots} waiters={lock._bound()._waiters}",
            )
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    asyncio.run(_run())

    revived = state._slots[key]
    assert revived.folder_id == ""  # never written: the lock was never acquired
    assert saves == []
    allowed = [a for a in audits if a["operation"] == "revive" and a["outcome"] == "allowed"]
    assert len(allowed) == 1 and allowed[0]["slot_key"] == key
    assert allowed[0]["detail"]["filed"] == "false"


def test_a_target_that_goes_live_during_the_scan_is_reported_live(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    real_scan = sc._scan_archived_candidates

    def _scan_then_reopen(log, key_candidate, wanted):
        out = real_scan(log, key_candidate, wanted)
        state.get_or_create_slot(key)  # a human click lands while the scan runs
        return out

    monkeypatch.setattr(sc, "_scan_archived_candidates", _scan_then_reopen)
    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)
    assert exc.value.code == "target_already_live" and key in exc.value.message


def test_mcp_reply_does_not_warn_when_the_session_was_already_in_the_folder():
    """``revive_session`` files only when the folder differs and answers
    ``filed: False`` for a session already where it was asked to go; the reply
    must not read that as a filing failure."""
    reply = {
        "ok": True,
        "target": "chat-2",
        "title": "T",
        "messages": 3,
        "folder_id": "f1",
        "filed": False,
    }
    out, requests = _revive_tool(
        {"target": "chat-2", "folder": "Gamma"},
        {
            "GET /api/chat/slots": [{"key": "chat-1", "title": "Caller"}],
            "GET /api/chat/folders": [{"id": "f1", "name": "Gamma", "parent_id": ""}],
            "POST /api/session-control/revive": reply,
        },
    )
    assert requests[-1].body == {"target": "chat-2", "folder_id": "f1"}
    assert "could not be applied" not in out
    assert "Revived `chat-2`" in out


def test_probe_without_a_store_answers_before_reading_the_slot():
    """The slot-form probe must keep main's order: with no session store to
    ask it answers "not mirrored" without touching the slot at all. The
    spec-builder queue path stamps containment on a duck-typed slot that has no
    `key`, and the history-key split of this probe once evaluated the key first
    and raised there (two spec_builder route tests went red)."""

    class _BareState:
        pass

    class _KeylessSlot:
        pass

    assert sc._probe_channel_mirror(_BareState(), _KeylessSlot()) == ""
    assert sc._has_channel_mirror(_BareState(), _KeylessSlot()) is False


# ── Catalog-resolved keys, hydrated-slot re-check, fail-closed retract ──────


def test_a_case_variant_of_a_live_key_is_reported_live_not_revived_twice(tmp_path, monkeypatch):
    """On a case-insensitive filesystem ``Chat-2`` opens ``dashboard_chat-2.jsonl``
    as well as ``chat-2`` does, so a metadata probe on the caller's spelling
    would succeed while the exact live-slot lookup misses the open ``chat-2`` and
    a second slot lands on the same file. The key is taken from the catalog's
    spelling, so the live check sees the key that is actually open."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    real_status = log.get_metadata_status

    def _case_blind(history_key: str):
        # Model the filesystem: any case spelling of the stem reads the file.
        for row in log.list_sessions():
            stem = str(row.get("key") or "")
            if stem.casefold() == history_key.replace("dashboard:", "dashboard_").casefold():
                return real_status("dashboard:" + stem[len("dashboard_") :])
        return real_status(history_key)

    monkeypatch.setattr(log, "get_metadata_status", _case_blind)

    # Archived: the variant resolves to the catalog's key, not the caller's spelling.
    assert _revive(state, caller, key.upper())["target"] == key
    assert key in state._slots and key.upper() not in state._slots

    # Live now: the same variant is reported live with the real key.
    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key.upper())
    assert exc.value.code == "target_already_live"
    assert key in exc.value.message
    assert len([k for k in state._slots if k.casefold() == key.casefold()]) == 1


@pytest.mark.parametrize(
    "field, value, code",
    [
        ("_app", "some-app", "app_scoped_target"),
        ("memory_mode", "incognito", "ephemeral_target"),
        ("linked_session_key", "slack:C1:1.2", "linked_session_target"),
        ("workspace", "elsewhere", "workspace_mismatch"),
    ],
)
def test_metadata_that_drifts_during_the_resume_is_caught_on_the_built_slot(
    tmp_path, monkeypatch, field, value, code
):
    """The resume core re-reads the metadata line after its threaded transcript
    read and hydrates from the fresh copy; the containment answered before the
    resume came from the old line. Every target-side boundary is re-read off the
    slot the resume built, before it is published."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))

    def _hydrated_drifted(built):
        setattr(built, field, value)  # what a rewritten line would hydrate

    _refused_in_window(state, caller, key, monkeypatch, _hydrated_drifted, code)


def test_hydrated_slot_refusal_mirrors_authorize_target_codes():
    """The re-check reads the same fields as the live-target gate, casefolds the
    unattended prefix (a case-variant key must not walk past it), and answers
    ``None`` when every boundary holds."""
    caller = MagicMock(workspace="default")
    assert (
        sc._revived_slot_refusal(
            MagicMock(
                key="chat-1",
                memory_mode="persistent",
                _app="",
                linked_session_key="",
                workspace="default",
            ),
            caller,
        )
        is None
    )
    assert sc._revived_slot_refusal(
        MagicMock(
            key="Cron-1",
            memory_mode="persistent",
            _app="",
            linked_session_key="",
            workspace="default",
        ),
        caller,
    ) == ("unattended sessions (scheduled runs) cannot be controlled", "unattended_target")


def test_the_session_store_probes_run_off_the_event_loop(tmp_path, monkeypatch):
    """The store's guarded getters share a lock its off-loop writer holds across
    a disk write; the revive path must reach them only through
    ``asyncio.to_thread``, on both sides of the resume.

    The sidebar broadcast the publish schedules (``push_slots_update`` ->
    ``serialize_slots`` -> ``_slot_links``) reads the same getters on the loop
    for EVERY slot; that is main's own projection, shared with the History click,
    and whether its coalesced frame fires before ``asyncio.run`` returns is a
    platform timing detail (it did on the Windows shard). It is silenced here so
    the assertion is about the revive path alone."""
    import threading

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    monkeypatch.setattr(state, "push_slots_update", lambda: None)
    loop_thread = threading.get_ident()
    seen: list[bool] = []
    links: dict = {}
    _mirror_store(state, links)
    sessions = state.sessions
    real_get_link = sessions.get_origin_link
    real_mirror = sessions.get_mirror_link

    def _get_link(k):
        if k == f"dashboard:{key}":
            seen.append(threading.get_ident() == loop_thread)
        return real_get_link(k)

    def _get_mirror(k):
        if k == f"dashboard:{key}":
            seen.append(threading.get_ident() == loop_thread)
        return real_mirror(k)

    monkeypatch.setattr(sessions, "get_origin_link", _get_link)
    monkeypatch.setattr(sessions, "get_mirror_link", _get_mirror)

    assert _revive(state, caller, key)["target"] == key
    assert len(seen) >= 4, "expected probes on both sides of the resume"
    assert not any(seen), "a session-store probe on the target ran on the event loop thread"


def test_the_live_target_authorization_runs_off_the_event_loop(tmp_path, monkeypatch):
    """A target that is already LIVE is authorized as a live target before it is
    named, and that authorization ends in the same guarded mirror getter as the
    archived probes, so it must reach the store through ``asyncio.to_thread`` too."""
    import threading

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    live = _slot(state, "chat-2")
    loop_thread = threading.get_ident()
    seen: list[bool] = []
    _mirror_store(state, {})
    sessions = state.sessions
    real_mirror = sessions.get_mirror_link

    def _get_mirror(k):
        if k == f"dashboard:{live.key}":
            seen.append(threading.get_ident() == loop_thread)
        return real_mirror(k)

    monkeypatch.setattr(sessions, "get_mirror_link", _get_mirror)

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, live.key)
    assert exc.value.code == "target_already_live"
    assert seen, "expected the live target's mirror probe to run"
    assert not any(seen), "the live target's mirror probe ran on the event loop thread"


def test_a_protection_landing_during_the_live_store_probe_is_caught_before_naming(
    tmp_path, monkeypatch
):
    """The store probes on a live target are awaits. A slot that becomes
    protected inside that hop (an app scope committed, a channel link rebound)
    must be answered from its LIVE fields, on the loop, before its key is named:
    the store-free checks run again after the last await."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    live = _slot(state, "chat-2")
    real_probe = sc._has_channel_mirror

    def _probe_then_protect(state_, slot, **kw):
        result = real_probe(state_, slot, **kw)
        if slot.key == live.key:
            slot._app = "some-app"
        return result

    monkeypatch.setattr(sc, "_has_channel_mirror", _probe_then_protect)

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, live.key)
    assert exc.value.code == "app_scoped_target"
    assert live.key not in exc.value.message
