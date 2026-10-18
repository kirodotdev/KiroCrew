"""Session control revive: the resume core's pre-publish containment hook.

Covers what ``resume_slot_from_history`` publishes, refuses and rolls back when
its ``containment`` hook or ``final_check`` answers, when the deferred reopen
write, its verification read or the marker restore fails, and when the task is
cancelled or the session is deleted, recreated, linked or published live before
the publish. Also here: the History click's own refusals, the global cap at the
hook, the one answer an ineligible caller gets, a live target authorized before
it is named, and the pin on the ``SessionManager`` getters the link probes read.
Shared fixtures and helpers live in ``session_control_revive_helpers.py``.
"""

from __future__ import annotations

import asyncio
import errno
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state
from session_control_revive_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _archive,
    _enabled,
    _folder,
    _fresh_windows,
    _revive,
    _settle_metadata_rewrite_before_read,
    _slot,
)

from kiro_crew.dashboard import session_control as sc

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


def test_a_history_click_that_loses_the_race_leaves_the_marker_untouched(tmp_path, monkeypatch):
    """The History path clears ``closed`` only after construction. A
    click that passes the early construction guard and then finds the key under
    construction (a revive retracted its build inside the click's read window)
    is refused ``resume_in_progress`` with the marker exactly as it found it, so
    if that revive is then refused too, the archived session stays archived."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    real_agent = chat_handlers._restored_agent_name

    def _agent_then_contend(*a, **kw):
        # Runs inside the click's read window, before the post-read guard:
        # another resume of the same key takes the construction mark meanwhile.
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


def test_a_history_click_refused_by_an_in_flight_delete_writes_nothing(tmp_path, monkeypatch):
    """A delete of the session is in flight when the click reaches its last
    check. The click refuses with a retryable ``resume_conflict`` before any
    durable write: the ``closed`` marker is still set, and neither the reopen
    clear nor a marker restore was attempted, so there is nothing a failed
    rollback could leave half-done."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    writes = []
    real_clear = log.clear_closed
    real_update = log.update_metadata_if

    def _record_clear(*a, **kw):
        writes.append("clear_closed")
        return real_clear(*a, **kw)

    def _record_update(*a, **kw):
        writes.append("update_metadata_if")
        return real_update(*a, **kw)

    monkeypatch.setattr(log, "clear_closed", _record_clear)
    monkeypatch.setattr(log, "update_metadata_if", _record_update)

    with log.delete_in_flight_window(f"dashboard:{key}"):
        outcome = asyncio.run(
            chat_handlers.resume_slot_from_history(state, name=key, history_key=f"dashboard:{key}")
        )

    assert outcome.refusal is not None
    assert outcome.refusal.code == "resume_conflict" and outcome.refusal.status == 409
    assert writes == [], f"a refused resume wrote durable state: {writes}"
    assert log.get_metadata(f"dashboard:{key}").get("closed") is True
    assert key not in state._slots
    assert key not in state._slots_under_construction


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


def test_a_clear_that_lands_then_reads_unreadable_once_retries_and_publishes(tmp_path):
    """A just-rewritten metadata line is transiently unopenable on Windows, so a
    successful ``clear_closed`` can be followed by an unreadable verification.
    The handler re-reads it off the loop, so a transient that clears on the next
    try lets the read succeed and the resume publishes a correctly-reopened
    session -- rather than refusing on the one-shot transient. The fail-safe
    stands: a read that is unreadable across every retry still refuses (covered
    by the persistent-lock cases)."""
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
        # rewrite cannot open the file once; the handler's next re-read can.
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
    # The one-shot transient was ridden out; the resume publishes.
    assert outcome.refusal is None
    assert outcome.slot is not None and outcome.slot.key == key
    assert key in state._slots and key not in state._slots_under_construction
    # The clear stands, because the resume succeeded: the session is live.
    assert "closed" not in log.get_metadata(f"dashboard:{key}")


def test_a_persistently_unreadable_verification_refuses_and_restores_the_marker(tmp_path):
    """When the verification read is unreadable across EVERY retry, the resume
    stays fail-closed: it refuses and publishes nothing, and the ``closed``
    marker is written back so the session does not reopen at the next start.
    With the confirmation re-read also unreadable the refusal escalates to
    ``reopen_rollback_failed`` (the marker write landed but could not be
    confirmed) -- either way it never publishes. The handler's extra re-reads
    widen the window a transient has to clear; they never accept a line that
    stays unreadable."""
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    real_clear = log.clear_closed
    armed = {"on": False, "cleared": None}

    def _clear(k, **kw):
        real_clear(k, **kw)  # actually drops the marker
        armed["cleared"] = "closed" not in log.get_metadata(k)
        armed["on"] = True

    real_status = log.get_metadata_status

    def _unreadable(k):
        # Every read after the clear is unreadable: the lock never clears.
        if armed["on"]:
            return {}, False
        return real_status(k)

    async def _ok(_built):
        return None

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(log, "clear_closed", _clear)
        mp.setattr(log, "get_metadata_status", _unreadable)
        outcome = asyncio.run(
            chat_handlers.resume_slot_from_history(
                state, name=key, history_key=f"dashboard:{key}", containment=_ok
            )
        )
    assert armed["cleared"] is True, "fixture did not actually clear the marker"
    # Fail-closed: refused, nothing published.
    assert outcome.refusal is not None
    assert outcome.refusal.code in ("resume_conflict", "reopen_rollback_failed")
    assert outcome.slot is None
    assert key not in state._slots and key not in state._slots_under_construction
    # The marker write landed (``update_metadata_if`` is not the stubbed read),
    # so a now-unstubbed read sees the session closed again, not reopened.
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
        # flake-ok: pre-existing 10 ms pause that gives the replacement a fresh created_at stamp; moved verbatim and tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 10 ms pause that gives the replacement a fresh created_at stamp and moves verbatim
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
        # flake-ok: pre-existing 10 ms pause that gives the replacement a fresh created_at stamp; moved verbatim and tracked for a de-flake pass
        # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing 10 ms pause that gives the replacement a fresh created_at stamp and moves verbatim
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
    """A mutation landing after the last await refuses rather than publishing.

    The identity re-read runs off the loop so it survives a transient Windows
    file lock, then the containment pass is the last await. A delete or
    delete-and-recreate landing in that pass bumps the key's invalidation
    generation; the synchronous generation re-check after the pass is the
    delete-and-recreate's lock-free signature, and refuses ``resume_conflict``
    with the marker rolled back instead of publishing over it.
    """
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    hk = f"dashboard:{key}"
    passes: list[int] = []

    async def _count(_built):
        passes.append(1)
        if len(passes) == 2:
            # A delete-and-recreate landing during the last await bumps the
            # generation under the file lock, which the re-check below catches.
            log._invalidate_cache(hk)
        return None

    outcome = asyncio.run(
        chat_handlers.resume_slot_from_history(state, name=key, history_key=hk, containment=_count)
    )
    assert len(passes) == 2
    assert outcome.refusal is not None and outcome.refusal.code == "resume_conflict"
    assert key not in state._slots and key not in state._slots_under_construction
    assert log.get_metadata(hk).get("closed") is True


def test_a_delete_in_flight_during_the_last_await_refuses_before_it_unlinks(tmp_path, monkeypatch):
    """An in-flight delete opened during the containment await, before it bumps
    the generation, is caught by the lock-free in-flight marker.

    ``delete_session`` bumps the invalidation generation only after the unlink,
    so a delete that opens its in-flight window during the last await has not
    bumped it yet. ``_identity_refusal``'s own marker check runs before that
    await, so the synchronous re-check after it carries the marker term too and
    refuses ``resume_conflict`` with the marker rolled back.
    """
    from kiro_crew.dashboard import chat_handlers

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    key = _archive(state, caller, _slot(state, "chat-2"))
    log = state.conversation_log
    hk = f"dashboard:{key}"
    passes: list[int] = []
    window = {}

    async def _count(_built):
        passes.append(1)
        if len(passes) == 2:
            # A delete opens its in-flight window during the last await but has
            # not reached its generation-bumping unlink yet.
            window["cm"] = log.delete_in_flight_window(hk)
            window["cm"].__enter__()
        return None

    try:
        outcome = asyncio.run(
            chat_handlers.resume_slot_from_history(
                state, name=key, history_key=hk, containment=_count
            )
        )
    finally:
        if "cm" in window:
            window["cm"].__exit__(None, None, None)
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
