"""Session control: reviving an archived session -- the mirror of close.

Organized like ``test_session_control.py``: every refusal is asserted against
the REAL slot objects and the REAL history metadata, because ``revive_session``
reads ``workspace`` / ``app`` / ``created_by`` off the persisted metadata line
and a permissive double would let a dead guard look alive. The happy path goes
end to end through the real close (``close_target``) and the real resume core
(``resume_slot_from_history``), so the round trip a person expects -- close a
tab, revive it, find the transcript -- is what is exercised.
"""

from __future__ import annotations

import asyncio
import errno
from unittest.mock import MagicMock, patch

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard import stop_retry
from kiro_crew.dashboard.chat_utils import slot_history_key


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _fresh_windows():
    stop_retry.reset_for_tests()
    create_rate_limit.reset_for_tests()
    yield
    stop_retry.reset_for_tests()
    create_rate_limit.reset_for_tests()


def _slot(state, name: str, **kwargs):
    return state.get_or_create_slot(name, **kwargs)


def _key(slot) -> str:
    return slot_history_key(slot)


def _archive(state, caller, peer, *, messages=2, title="") -> str:
    """Close *peer* through the real close verb and return its slot key.

    The transcript is written first so the revive has something to bring back;
    the close is the production one, so the metadata line carries exactly what a
    ✕ leaves behind (``closed``, ``closed_at``, title, workspace, creator).
    """
    for i in range(messages):
        peer.messages.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"})
    if title:
        peer.title = title
        peer._titled = True
    peer._dirty = True
    key = peer.key
    asyncio.run(sc.close_target(state, caller_session_key=_key(caller), target=key))
    assert key not in state._slots
    assert state.conversation_log.get_metadata(f"dashboard:{key}").get("closed")
    return key


def _revive(state, caller, target: str, **kwargs):
    return asyncio.run(
        sc.revive_session(state, caller_session_key=_key(caller), target=target, **kwargs)
    )


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


@pytest.mark.parametrize("known", [True, False], ids=["lineage-names-another", "lineage-unknown"])
def test_an_uncorroborated_created_by_claim_is_not_stamped_on_the_revived_slot(
    tmp_path, monkeypatch, known
):
    """``created_by`` is read from an agent-editable file. A fenced agent that wrote
    its own key into an archived transcript must not be handed the slot when an
    UNFENCED tab revives it (the reviver's own ownership is not gated), so the
    stamp is taken only from a lineage that names the same parent; otherwise the
    field stays blank, which matches no caller."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    peer = _slot(state, "chat-2")
    peer._created_by = "member-agent"  # what a tampered metadata line would say
    key = _archive(state, caller, peer)
    _lineage(monkeypatch, {key: "chat-9"}, known=known)

    _revive(state, caller, key)

    revived = state._slots[key]
    assert getattr(revived, "_created_by", "") == ""
    assert sc._created_by_other(revived, "member-agent")


# ── Filing ───────────────────────────────────────────────────────────────────


def _folder(state, fid: str, name: str):
    state._folders.append({"id": fid, "name": name, "parent_id": None, "position": 0})


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


def _lineage(monkeypatch, parents: dict, *, known: bool = True):
    """Stand in for the crew-log session-tree projection: *parents* maps an
    archived slot key to the parent the gateway recorded for it."""
    monkeypatch.setattr(
        sc, "_slot_tree_parent", lambda slot_key: (known, parents.get(slot_key, ""), {})
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
        assert "_check_caller_identity(" in src
        # Called directly or through ``asyncio.to_thread`` -- either spelling is
        # the same helper; the pin is on the helper being the one consulted.
        assert "_check_caller_slot(" in src or "_check_caller_slot," in src
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
    assert target_side, "the regex found no refusal codes in authorize_target"
    assert target_side <= codes(sc.revive_session), target_side - codes(sc.revive_session)


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


def test_mcp_tool_posts_to_the_revive_route_with_the_callers_key(monkeypatch):
    from kiro_crew import mcp_dashboard

    posted: dict = {}

    def _post(path, body, session_key=""):
        posted.update(path=path, body=body, session_key=session_key)
        return {"ok": True, "target": "chat-2", "title": "Lookup", "messages": 4, "filed": False}

    monkeypatch.setattr(mcp_dashboard, "_post", _post)
    monkeypatch.setattr(
        mcp_dashboard, "require_strict_session_key", lambda *a, **k: ("dashboard:chat-1", "")
    )

    out = mcp_dashboard._call_tool_inner("session_revive", {"target": "chat-2"})

    assert posted == {
        "path": "/api/session-control/revive",
        "body": {"target": "chat-2"},
        "session_key": "dashboard:chat-1",
    }
    assert "Revived `chat-2`" in out and "4 messages" in out


def test_mcp_tool_refuses_a_caller_without_a_strict_key(monkeypatch):
    from kiro_crew import mcp_dashboard

    monkeypatch.setattr(
        mcp_dashboard, "require_strict_session_key", lambda *a, **k: ("", "Error: nope")
    )
    with patch.object(mcp_dashboard, "_post") as post:
        out = mcp_dashboard._call_tool_inner("session_revive", {"target": "chat-2"})
    assert out == "Error: nope"
    post.assert_not_called()


def test_mcp_tool_names_the_live_key_when_the_target_is_open(monkeypatch):
    from kiro_crew import mcp_dashboard

    monkeypatch.setattr(
        mcp_dashboard,
        "_post",
        lambda *a, **k: {"error": "'x' is already open as `chat-5`; address it directly"},
    )
    monkeypatch.setattr(
        mcp_dashboard, "require_strict_session_key", lambda *a, **k: ("dashboard:chat-1", "")
    )
    out = mcp_dashboard._call_tool_inner("session_revive", {"target": "x"})
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


def test_mcp_reply_does_not_warn_when_the_session_was_already_in_the_folder(monkeypatch):
    """``revive_session`` files only when the folder differs and answers
    ``filed: False`` for a session already where it was asked to go; the reply
    must not read that as a filing failure."""
    from kiro_crew import mcp_dashboard

    monkeypatch.setattr(
        mcp_dashboard, "require_strict_session_key", lambda *a, **k: ("dashboard:chat-1", "")
    )
    monkeypatch.setattr(
        mcp_dashboard,
        "_resolve_folder_for_new_session",
        lambda ref, verb: ("f1", "Gamma", "", None),
    )
    monkeypatch.setattr(
        mcp_dashboard,
        "_post",
        lambda *a, **k: {
            "ok": True,
            "target": "chat-2",
            "title": "T",
            "messages": 3,
            "folder_id": "f1",
            "filed": False,
        },
    )
    out = mcp_dashboard._call_tool_inner("session_revive", {"target": "chat-2", "folder": "Gamma"})
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


# ── The resume core's pre-publish containment hook ───────────────────────────


def test_a_refused_hook_leaves_the_durable_session_as_it_found_it(tmp_path):
    """The core clears ``closed`` before it builds the slot (so a resumed session
    restores on the next start). A refusal from the hook must not leave that
    clear behind: the marker goes back with its original ``closed_at``, no slot
    is published, and the construction mark is released."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    before = state.conversation_log.get_metadata(f"dashboard:{key}")
    seen: dict = {}

    async def _refuse(built):
        seen["resolvable"] = state.get_slot(built.key) is not None
        seen["under_construction"] = built.key in state._slots_under_construction
        return chat_handlers.ResumeRefusal("no", "hook_said_no", 403)

    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(
            state, name=key, history_key=f"dashboard:{key}", containment=_refuse
        )
    )

    assert outcome.refusal is not None and outcome.refusal.code == "hook_said_no"
    assert seen == {"resolvable": False, "under_construction": True}
    assert key not in state._slots
    assert key not in state._slots_under_construction
    after = state.conversation_log.get_metadata(f"dashboard:{key}")
    assert after.get("closed") is True
    assert after.get("closed_at") == before.get("closed_at")


def test_a_hook_that_raises_discards_the_built_slot(tmp_path):
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))

    async def _boom(_built):
        raise RuntimeError("probe died")

    with pytest.raises(RuntimeError):
        asyncio.run(
            chat_handlers.resume_slot_from_history(
                state, name=key, history_key=f"dashboard:{key}", containment=_boom
            )
        )

    assert key not in state._slots
    assert key not in state._slots_under_construction
    assert state.conversation_log.get_metadata(f"dashboard:{key}").get("closed") is True


def test_a_clean_hook_publishes_once_and_the_history_click_passes_none(tmp_path):
    """No hook is the History tab's path and is unchanged; a hook that answers
    ``None`` publishes the same slot the click would."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    calls: list[str] = []

    async def _ok(built):
        calls.append(built.key)
        return None

    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(
            state, name=key, history_key=f"dashboard:{key}", containment=_ok
        )
    )
    # Two passes, one publish: the hook runs before the deferred reopen write and
    # again after it as the last awaiting act, so a store-recorded binding that
    # lands during those awaits is still caught; the slot is published once.
    assert outcome.slot is not None and outcome.slot.key == key and calls == [key, key]
    assert state.get_slot(key) is outcome.slot
    assert not state.conversation_log.get_metadata(f"dashboard:{key}").get("closed")


def test_a_refused_hook_restores_the_marker_before_releasing_the_reservation(tmp_path):
    """The reopen write is deferred past the async hook, so a hook refusal
    changes nothing on disk. The only refusal that can follow the clear is the
    synchronous ``final_check``; its rollback restores the marker while the
    construction mark still reserves the key (a concurrent resume could
    otherwise publish in the gap and be marked closed), and releases the mark
    last."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    before = log.get_metadata(f"dashboard:{key}")
    real = log.update_metadata_if
    seen: dict = {}

    def _spy(k, fields, guard, **kw):
        seen["mark_held_during_restore"] = key in state._slots_under_construction
        return real(k, fields, guard, **kw)

    log.update_metadata_if = _spy  # type: ignore[method-assign]

    async def _ok(_built):
        # The hook runs twice (once before the deferred clear, once after it as
        # the last awaiting act); the first pass is the one that must still see
        # the marker.
        seen.setdefault("closed_during_hook", log.get_metadata(f"dashboard:{key}").get("closed"))
        return None

    def _final_no(_built):
        seen["closed_during_final"] = log.get_metadata(f"dashboard:{key}").get("closed")
        return chat_handlers.ResumeRefusal("no", "final_said_no", 403)

    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(
            state,
            name=key,
            history_key=f"dashboard:{key}",
            containment=_ok,
            final_check=_final_no,
        )
    )
    assert outcome.refusal is not None and outcome.refusal.code == "final_said_no"
    # Deferred: still closed while the async hook ran; cleared by the time the
    # synchronous last word ran; restored by its refusal with the mark held.
    assert seen["closed_during_hook"] is True
    assert seen["closed_during_final"] is None
    assert seen["mark_held_during_restore"] is True
    assert key not in state._slots_under_construction
    after = log.get_metadata(f"dashboard:{key}")
    assert after.get("closed") is True and after.get("closed_at") == before.get("closed_at")


def test_a_reopen_write_that_cannot_land_refuses_instead_of_publishing(tmp_path, monkeypatch):
    """A hooked resume whose deferred ``clear_closed`` raises must not publish a
    tab whose line still says closed (it would vanish at the next start); it
    refuses ``reopen_failed`` and leaves the marker as it was."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log

    def _boom(*a, **kw):
        raise TimeoutError("lock held elsewhere")

    monkeypatch.setattr(log, "clear_closed", _boom)

    async def _ok(_built):
        return None

    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(
            state, name=key, history_key=f"dashboard:{key}", containment=_ok
        )
    )
    assert outcome.refusal is not None
    assert outcome.refusal.code == "reopen_failed" and outcome.refusal.status == 503
    assert key not in state._slots and key not in state._slots_under_construction
    assert log.get_metadata(f"dashboard:{key}").get("closed") is True


def test_a_cancellation_during_the_deferred_clear_still_discards_the_build(tmp_path, monkeypatch):
    """``CancelledError`` is a ``BaseException``: a task torn down while the
    deferred reopen write runs must still reach ``_discard``, or the construction
    mark stays reserved for the process lifetime and the cleared ``closed`` marker
    stays cleared. The clear lands, then the task is cancelled before the
    verification read; the mark must be released and the marker restored."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    real_clear = log.clear_closed
    task_box: dict = {}

    def _clear_then_cancel(*a, **kw):
        # Runs in the to_thread worker; the cancel is scheduled onto the loop.
        result = real_clear(*a, **kw)
        task_box["loop"].call_soon_threadsafe(task_box["task"].cancel)
        return result

    monkeypatch.setattr(log, "clear_closed", _clear_then_cancel)

    async def _ok(_built):
        return None

    async def _run():
        task_box["task"] = asyncio.current_task()
        task_box["loop"] = asyncio.get_running_loop()
        return await chat_handlers.resume_slot_from_history(
            state, name=key, history_key=f"dashboard:{key}", containment=_ok
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    assert key not in state._slots
    assert key not in state._slots_under_construction
    assert log.get_metadata(f"dashboard:{key}").get("closed") is True


def test_a_history_click_that_loses_the_race_after_its_eager_clear_restores_the_marker(
    tmp_path, monkeypatch
):
    """The hook-less History path clears ``closed`` eagerly. A click that passes
    the early construction guard, clears, and then finds the key under
    construction (a revive retracted its build inside the click's read window)
    is refused ``resume_in_progress``; if that revive is then refused too, the
    click's clear would be the only durable change left, and the archived
    session would come back as a sidebar row at the next start. The click puts
    the marker back before answering."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    real_agent = chat_handlers._restored_agent_name

    def _agent_then_contend(*a, **kw):
        # Runs after the eager clear and before the post-clear guard: another
        # resume of the same key takes the construction mark meanwhile.
        state.begin_slot_construction(key)
        return real_agent(*a, **kw)

    monkeypatch.setattr(chat_handlers, "_restored_agent_name", _agent_then_contend)

    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(state, name=key, history_key=f"dashboard:{key}")
    )
    state.end_slot_construction(key)

    assert outcome.refusal is not None and outcome.refusal.code == "resume_in_progress"
    assert key not in state._slots
    assert log.get_metadata(f"dashboard:{key}").get("closed") is True


def test_a_history_click_that_loses_the_race_and_cannot_restore_answers_rollback_failed(
    tmp_path, monkeypatch
):
    """Same race as above, but the marker restore keeps raising and the re-read
    shows the marker absent: the click must not answer an ordinary
    ``resume_in_progress`` that a retry would clear, because the durable session
    is now reopened. Same ``reopen_rollback_failed`` 503 the hooked discard gives."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    real_agent = chat_handlers._restored_agent_name
    real_update = log.update_metadata_if

    def _agent_then_contend(*a, **kw):
        state.begin_slot_construction(key)
        return real_agent(*a, **kw)

    def _restore_fails(k, fields, guard, **kw):
        if "closed" in fields:
            raise OSError(errno.EIO, "injected")
        return real_update(k, fields, guard, **kw)

    monkeypatch.setattr(chat_handlers, "_restored_agent_name", _agent_then_contend)
    monkeypatch.setattr(log, "update_metadata_if", _restore_fails)

    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(state, name=key, history_key=f"dashboard:{key}")
    )
    state.end_slot_construction(key)

    assert outcome.refusal is not None
    assert outcome.refusal.code == "reopen_rollback_failed" and outcome.refusal.status == 503
    assert key not in state._slots


def test_a_concurrent_resume_during_the_hook_gets_a_coded_conflict_not_a_500(tmp_path):
    """While one resume holds the built slot retracted and under construction, a
    second resume of the same key must not reach the constructor's bare
    ``ValueError``; it is answered with ``resume_in_progress`` (409)."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    inner: dict = {}

    async def _hook_runs_a_second_resume(_built):
        inner["outcome"] = await chat_handlers.resume_slot_from_history(
            state, name=key, history_key=f"dashboard:{key}"
        )
        return None

    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(
            state, name=key, history_key=f"dashboard:{key}", containment=_hook_runs_a_second_resume
        )
    )
    assert outcome.slot is not None and state.get_slot(key) is outcome.slot
    second = inner["outcome"]
    assert second.refusal is not None
    assert second.refusal.code == "resume_in_progress" and second.refusal.status == 409


def test_with_a_hook_the_folder_un_hide_keeps_the_history_paths_place(tmp_path):
    """The folder un-hide stays where the History path has it, before
    construction, on both paths: it is the existence verdict hydration binds
    ``folder_id`` from, and moving it past the hook would put an await between
    the hook's last answer and the publish. So a refused hooked resume can leave
    a hidden folder visible, exactly as a click refused at the member barrier
    already can; nothing is rolled back there either."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    _folder(state, "f1", "Hidden one")
    caller = _slot(state, "chat-1")
    peer = _slot(state, "chat-2")
    peer.folder_id = "f1"
    key = _archive(state, caller, peer)
    state._folders[0]["hidden"] = True
    seen: dict = {}

    async def _refuse(_built):
        seen["hidden_during_hook"] = state._folders[0].get("hidden")
        return chat_handlers.ResumeRefusal("no", "hook_said_no", 403)

    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(
            state, name=key, history_key=f"dashboard:{key}", containment=_refuse
        )
    )
    assert outcome.refusal is not None
    assert seen == {"hidden_during_hook": False}
    assert state._folders[0].get("hidden") is False


def test_with_a_hook_a_gone_folder_still_drops_the_filing_like_the_click_does(tmp_path):
    """The existence verdict hydration binds ``folder_id`` from is still taken
    before construction under a hook (read-only); a session whose folder no
    longer exists comes back unfiled, as it does from the History tab."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    _folder(state, "f1", "Doomed")
    caller = _slot(state, "chat-1")
    peer = _slot(state, "chat-2")
    peer.folder_id = "f1"
    key = _archive(state, caller, peer)
    state._folders.clear()

    async def _ok(_built):
        return None

    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(
            state, name=key, history_key=f"dashboard:{key}", containment=_ok
        )
    )
    assert outcome.slot is not None and outcome.slot.folder_id == ""


def test_the_global_cap_in_the_hook_is_exclusive_like_every_other_allocation(tmp_path, monkeypatch):
    """``live_slot_count`` counts the built slot (under construction) even while
    it is retracted from the table, so at exactly the cap the revive must still
    land: caller + revived slot == MAX_LIVE_SLOTS is allowed."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    monkeypatch.setattr(sc, "MAX_LIVE_SLOTS", 2)

    assert _revive(state, caller, key)["target"] == key
    assert state.live_slot_count() == 2


def test_an_ineligible_caller_learns_nothing_about_archived_sessions(tmp_path):
    """Every caller-side refusal runs before the target is resolved, so an
    app-scoped (or otherwise ineligible) caller gets the same answer for an
    archived session that exists and one that does not -- no existence oracle."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"), title="Secret title")
    caller._app = "some-app"

    answers = []
    for target in (key, "Secret title", "chat-404", "No such title"):
        with pytest.raises(sc.SessionControlError) as exc:
            _revive(state, caller, target)
        answers.append((exc.value.code, exc.value.status))
    assert len(set(answers)) == 1, answers
    assert answers[0] == ("app_scoped_caller", 403)


def _publish_live_during_resume(monkeypatch, state, key, **fields):
    """A human History click (or another reviver) publishes *key* while the
    revive's own resume is between its resolution and its publish, with the
    given slot fields on the live slot."""
    from kiro_crew.dashboard import chat_handlers

    original = chat_handlers.resume_slot_from_history

    async def _race(state_, **kw):
        live = await original(state_, name=kw["name"], history_key=kw["history_key"])
        for name, value in fields.items():
            setattr(live.slot, name, value)
        return await original(state_, **kw)

    monkeypatch.setattr(chat_handlers, "resume_slot_from_history", _race)


def test_an_already_live_slot_is_reauthorized_before_it_is_named(tmp_path, monkeypatch):
    """The dedup arm returns a LIVE slot hydrated from a line this call never
    checked. A protected one (here app-scoped) answers with the live-target
    refusal and never surfaces its key or title; an unprotected one answers as
    the pre-resume probe does, ``target_already_live`` with its key."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"), title="Quiet title")

    _publish_live_during_resume(monkeypatch, state, key, _app="some-app")
    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, "Quiet title")
    assert exc.value.code == "app_scoped_target"
    assert key not in exc.value.message and "Quiet title" not in exc.value.message
    assert state._slots[key]._app == "some-app"  # untouched, not re-filed


def test_an_already_live_unprotected_slot_answers_target_already_live(tmp_path, monkeypatch):
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))

    _publish_live_during_resume(monkeypatch, state, key)
    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key, folder_id="")
    assert exc.value.code == "target_already_live" and exc.value.status == 409
    assert key in exc.value.message


def test_a_clear_that_lands_then_reads_unreadable_still_restores_the_marker(tmp_path):
    """A just-rewritten metadata line is transiently unopenable on Windows, so a
    successful ``clear_closed`` can be followed by an unreadable verification.
    That refuses (``resume_conflict``), and because the clear DID land the marker
    must be restored, or the refused session reopens at the next start."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    real_clear = log.clear_closed
    landed: dict = {}

    def _clear(k, **kw):
        real_clear(k, **kw)  # actually drops the marker
        landed["cleared"] = "closed" not in log.get_metadata(k)
        landed["unreadable_next"] = True

    real_status = log.get_metadata_status

    def _unreadable(k):
        # Transient, as on Windows: the verification read right after the
        # rewrite cannot open the file; the restore's own confirmation read
        # that follows the rollback write can.
        if landed.pop("unreadable_next", False):
            return {}, False
        return real_status(k)

    async def _ok(_built):
        return None

    # The two stubs live in their own context so leaving it restores only
    # them; no shared fixture instance is undone mid-test.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(log, "clear_closed", _clear)
        mp.setattr(log, "get_metadata_status", _unreadable)
        outcome = asyncio.run(
            chat_handlers.resume_slot_from_history(
                state, name=key, history_key=f"dashboard:{key}", containment=_ok
            )
        )
    assert landed.get("cleared") is True, "fixture did not actually clear the marker"
    assert outcome.refusal is not None and outcome.refusal.code == "resume_conflict"
    assert key not in state._slots and key not in state._slots_under_construction
    # Restored despite the unreadable verification, since the clear landed.
    assert log.get_metadata(f"dashboard:{key}").get("closed") is True


def test_a_refusal_whose_marker_restore_cannot_be_confirmed_answers_rollback_failed(
    tmp_path, monkeypatch
):
    """After the deferred clear has landed, a refusal must put ``closed`` back
    and CONFIRM it. When the restore keeps raising and the re-read shows the
    marker absent, the caller hears ``reopen_rollback_failed`` (503) instead of
    the refusal that triggered the discard, because the durable session is now
    in a state the caller must act on (it would reopen at the next start)."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    real_update = log.update_metadata_if
    attempts: list[dict] = []

    def _restore_fails(k, fields, guard, **kw):
        if "closed" in fields:
            attempts.append(fields)
            raise OSError(errno.EIO, "injected")
        return real_update(k, fields, guard, **kw)

    monkeypatch.setattr(log, "update_metadata_if", _restore_fails)

    async def _ok(_built):
        return None

    def _final_refuse(_built):
        return chat_handlers.ResumeRefusal("no", "final_said_no", 403)

    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(
            state,
            name=key,
            history_key=f"dashboard:{key}",
            containment=_ok,
            final_check=_final_refuse,
        )
    )
    assert outcome.refusal is not None
    assert outcome.refusal.code == "reopen_rollback_failed"
    assert outcome.refusal.status == 503
    assert len(attempts) == 2, "the restore is retried once before giving up"
    assert key not in state._slots and key not in state._slots_under_construction


def test_a_marker_restore_that_fails_once_recovers_on_the_retry(tmp_path, monkeypatch):
    """One raise from the restore write is retried; when the retry lands and the
    re-read confirms the marker, the original refusal is what the caller hears."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    before = log.get_metadata(f"dashboard:{key}")
    real_update = log.update_metadata_if
    failed_once: list[bool] = []

    def _flaky(k, fields, guard, **kw):
        if "closed" in fields and not failed_once:
            failed_once.append(True)
            raise OSError(errno.EIO, "injected")
        return real_update(k, fields, guard, **kw)

    monkeypatch.setattr(log, "update_metadata_if", _flaky)

    async def _ok(_built):
        return None

    def _final_refuse(_built):
        return chat_handlers.ResumeRefusal("no", "final_said_no", 403)

    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(
            state,
            name=key,
            history_key=f"dashboard:{key}",
            containment=_ok,
            final_check=_final_refuse,
        )
    )
    assert outcome.refusal is not None and outcome.refusal.code == "final_said_no"
    after = log.get_metadata(f"dashboard:{key}")
    assert after.get("closed") is True and after.get("closed_at") == before.get("closed_at")
    assert key not in state._slots


def test_a_session_recreated_during_the_hook_is_refused_not_published(tmp_path):
    """The existence and ``created_at`` identity barrier runs before the hook;
    a delete-and-recreate landing INSIDE the hook window would otherwise publish
    the old transcript under the new file's identity, and every later save would
    take the delete-won arm and drop its rows. The barrier is re-run after the
    hook's last await with the same ``resume_session_deleted`` answer."""
    import time

    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"), messages=2)
    log = state.conversation_log
    hk = f"dashboard:{key}"
    old_created = log.get_metadata(hk).get("created_at")
    assert old_created, "fixture transcript must carry a created_at stamp"

    async def _recreate(_built):
        assert log.delete_session(hk) is True
        time.sleep(0.01)  # a fresh created_at stamp for the replacement
        log.append(hk, "user", "a different conversation")
        return None

    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(
            state, name=key, history_key=hk, containment=_recreate
        )
    )
    assert outcome.refusal is not None
    assert outcome.refusal.code == "resume_session_deleted" and outcome.refusal.status == 409
    assert key not in state._slots and key not in state._slots_under_construction
    replacement = log.get_metadata(hk)
    assert replacement.get("created_at") != old_created
    # The replacement is untouched: no ``closed`` marker was restored onto it.
    assert "closed" not in replacement
    assert [m["content"] for m in log.read_messages_chained(hk)] == ["a different conversation"]


def test_a_session_deleted_during_the_hook_is_refused_not_resurrected(tmp_path):
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"), messages=2)
    log = state.conversation_log
    hk = f"dashboard:{key}"

    async def _delete(_built):
        assert log.delete_session(hk) is True
        return None

    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(state, name=key, history_key=hk, containment=_delete)
    )
    assert outcome.refusal is not None and outcome.refusal.code == "resume_session_deleted"
    assert key not in state._slots and key not in state._slots_under_construction
    assert log.get_metadata(hk) == {}, "the refused resume must not recreate the deleted session"


def test_a_channel_link_recorded_during_the_deferred_clear_is_still_refused(tmp_path, monkeypatch):
    """The deferred reopen write and its verification read are awaits AFTER the
    hook's store-backed probes. A channel binding written to the session store
    inside that window (a channel-side resume of the same key) must not publish:
    the hook runs once more after those awaits, as the last awaiting act."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    hk = f"dashboard:{key}"
    real_clear = log.clear_closed

    def _clear_then_bind(k, **kw):
        real_clear(k, **kw)
        state.sessions.set_origin_link(hk, MagicMock(channel_type="telegram"))

    monkeypatch.setattr(log, "clear_closed", _clear_then_bind)

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)

    assert exc.value.code == "linked_session_target"
    assert key not in state._slots and key not in state._slots_under_construction
    # The refusal rolled the reopen back: the marker is on disk again.
    assert log.get_metadata(hk).get("closed") is True


def test_the_rollback_never_archives_a_replacement_transcript(tmp_path, monkeypatch):
    """A delete and same-key recreate landing inside the ``clear_closed`` worker
    call leaves a replacement with its own ``created_at``. The identity barrier
    refuses, and the rollback must NOT put the old ``closed`` marker onto the
    replacement: both its write guard and its confirmation read compare the
    stamp against the one this resume read."""
    import time

    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"), messages=2)
    log = state.conversation_log
    hk = f"dashboard:{key}"
    old_created = log.get_metadata(hk).get("created_at")
    real_clear = log.clear_closed

    def _clear_then_replace(k, **kw):
        real_clear(k, **kw)
        assert log.delete_session(hk) is True
        time.sleep(0.01)
        log.append(hk, "user", "a different conversation")

    monkeypatch.setattr(log, "clear_closed", _clear_then_replace)

    async def _ok(_built):
        return None

    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(state, name=key, history_key=hk, containment=_ok)
    )
    assert outcome.refusal is not None
    assert outcome.refusal.code == "resume_session_deleted", outcome.refusal
    assert key not in state._slots and key not in state._slots_under_construction
    replacement = log.get_metadata(hk)
    assert replacement.get("created_at") not in (None, old_created)
    assert "closed" not in replacement, "the old marker must not land on the replacement"


@pytest.mark.parametrize("branch", ["before_scan", "during_scan"])
def test_a_protected_live_target_is_authorized_before_it_is_named(tmp_path, monkeypatch, branch):
    """Both pre-resume live branches (a live match before the history scan, and
    a match that goes live during it) answer through the same authorize-then-name
    builder the post-resume branch uses: a protected live slot (here app-scoped)
    answers with the live-target refusal and reveals neither its existence nor
    its key; an unprotected one is named."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"), title="Quiet title")

    if branch == "before_scan":
        state.get_or_create_slot(key)._app = "some-app"
    else:
        real_scan = sc._scan_archived_candidates

        def _scan_then_reopen(log, key_candidate, wanted):
            out = real_scan(log, key_candidate, wanted)
            state.get_or_create_slot(key)._app = "some-app"
            return out

        monkeypatch.setattr(sc, "_scan_archived_candidates", _scan_then_reopen)

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key if branch == "before_scan" else "Quiet title")
    assert exc.value.code == "app_scoped_target"
    assert key not in exc.value.message and "Quiet title" not in exc.value.message


def test_a_channel_link_written_to_the_line_during_the_read_is_seen_by_the_hook(
    tmp_path, monkeypatch
):
    """The resume core does not hydrate ``linked_session_key``; the hook's link
    check reads it off the built slot, so without restoring it from the FRESH
    metadata re-read the check would be a constant. A link written to the line
    between the pre-resume check and the transcript read must refuse."""
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    hk = f"dashboard:{key}"
    real_read = log.read_messages_chained

    def _read_then_link(k, *a, **kw):
        out = real_read(k, *a, **kw)
        log.update_metadata(hk, {"linked_session_key": "telegram:dm:12345"})
        return out

    monkeypatch.setattr(log, "read_messages_chained", _read_then_link)

    with pytest.raises(sc.SessionControlError) as exc:
        _revive(state, caller, key)
    assert exc.value.code == "linked_session_target"
    assert key not in state._slots and key not in state._slots_under_construction
    assert log.get_metadata(hk).get("closed") is True


def test_an_unreadable_final_identity_read_refuses_rather_than_publishing(tmp_path, monkeypatch):
    """The synchronous identity read after the last await is the one place
    nothing follows: an unreadable answer there is the delete-and-recreate's
    own signature (the file being rewritten) and refuses ``resume_conflict``
    instead of falling through, with the marker rolled back."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    hk = f"dashboard:{key}"
    real_status = log.get_metadata_status
    passes: list[int] = []
    flags: dict = {}

    async def _count(_built):
        passes.append(1)
        if len(passes) == 2:
            flags["unreadable_next"] = True  # the very next read is the final one
        return None

    def _status(k):
        if flags.pop("unreadable_next", False):
            return {}, False
        return real_status(k)

    monkeypatch.setattr(log, "get_metadata_status", _status)
    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(state, name=key, history_key=hk, containment=_count)
    )
    assert len(passes) == 2
    assert outcome.refusal is not None and outcome.refusal.code == "resume_conflict"
    assert key not in state._slots and key not in state._slots_under_construction
    assert log.get_metadata(hk).get("closed") is True


def test_the_archived_link_probe_reads_getters_the_session_manager_actually_has():
    """The helper's in-memory store model would happily answer a getter the real
    ``SessionManager`` never defines (``get_link`` was such a name: always ``None``
    at runtime, so the corroborating read was inert). Every ``getattr(sessions,
    "<name>", None)`` the probes use must be a real SessionManager method."""
    import inspect
    import re

    from kiro_crew.session import SessionManager

    names: set[str] = set()
    for fn in (sc._archived_session_is_channel_linked, sc._probe_channel_mirror_for_key):
        names |= set(re.findall(r'getattr\(sessions, "([a-z_]+)"', inspect.getsource(fn)))
    assert names >= {"get_origin_link", "get_slack_link", "get_mirror_link"}
    missing = {n for n in names if not callable(getattr(SessionManager, n, None))}
    assert not missing, f"probe reads getters SessionManager lacks: {sorted(missing)}"
