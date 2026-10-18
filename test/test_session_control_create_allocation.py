"""Session creation: binding resolution, allocation re-checks, caps and lineage.

Covers ``create_session`` resolving the agent binding off the event loop with the
child's project directory before any slot exists, refusing an unresolved agent
name, and re-reading the caller, its workspace, its mirror link and the slot cap
after the await, so a change inside that window refuses. Source checks pin the
offloaded config load and no await from the caller gate to a configured slot.

Also pins ``_created_by`` attribution and ``MAX_SLOTS_PER_CREATOR``, the creator
session id frozen at mint and dropped past ``MAX_ACP_SESSION_ID_LEN``, the
``_crew_log_lineage`` and ``_crew_log_class`` seams, persist-failure retraction
that spares a started turn, and a ``MAX_LIVE_SLOTS`` shared with fork and import.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from unittest.mock import MagicMock

import pytest
from chat_test_helpers import _make_state
from session_control_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _agent_resolves,
    _enabled_fixture,
    _fresh_create_budget,
    _fresh_stop_windows,
    _key,
    _slot,
)

from kiro_crew.config import loader
from kiro_crew.dashboard import session_control as sc


def test_an_agent_name_that_does_not_resolve_is_refused(tmp_path, monkeypatch):
    """A session must not advertise an agent that is not the one answering.

    An unknown name falls back to the default agent's bindings, which pass the
    workspace check because they ARE the caller's default -- so no memory boundary
    is crossed, but `slot.agent` would store a name that nothing dispatches.
    `ResolvedBindings.requested_resolved` states exactly that contract for callers
    which store the requested name.

    Mutation guard: without the check the session is created and reports the
    unresolved name as its agent.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = set(state._slots)

    real = sc.resolve_agent_bindings

    def _unresolved(cfg, agent_name=None, project_dir=None, **kwargs):
        bindings = real(cfg, None, project_dir)
        return dataclasses.replace(bindings, requested_resolved=False)

    monkeypatch.setattr(sc, "resolve_agent_bindings", _unresolved)

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(
            sc.create_session(state, caller_session_key=_key(caller), agent="no-such-agent")
        )

    assert err.value.code == "agent_unresolved"
    assert set(state._slots) == before, "a refused creation leaves no slot behind"


def test_the_binding_is_resolved_with_the_childs_project_dir(tmp_path, monkeypatch):
    """A materialized kiro agent is declared per project directory, not in config.

    Resolving without the project directory reports an app's own agent as
    unresolvable, so the refusal above would reject a name that does resolve for
    the session being created.

    Mutation guard: dropping the argument passes None and the assertion fails.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    seen: dict[str, object] = {}

    real = sc.resolve_agent_bindings

    def _record(cfg, agent_name=None, project_dir=None, **kwargs):
        seen["project_dir"] = project_dir
        return dataclasses.replace(real(cfg, None, project_dir), requested_resolved=True)

    monkeypatch.setattr(sc, "resolve_agent_bindings", _record)

    asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    # Asserted as identity-against-None plus equality rather than truthiness: a
    # workspace with no configured project directory resolves to "", which is a
    # correct value to pass on. Dropping the argument yields None, which fails both
    # halves in every environment.
    assert seen["project_dir"] is not None, "the project dir must be passed, not omitted"
    assert seen["project_dir"] == loader.default_project_dir(caller.workspace)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["usable", "unavailable", "caller_closed"])
async def test_agent_binding_resolution_is_off_loop_and_precedes_allocation(
    tmp_path, monkeypatch, outcome
):
    from kiro_crew.memory_stores import UnknownMemoryStore

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = set(state._slots)
    allocate = MagicMock(wraps=state.get_or_create_slot)
    monkeypatch.setattr(state, "get_or_create_slot", allocate)
    real_resolve = sc.resolve_agent_bindings
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    lookup_threads = []

    def resolve(*args, **kwargs):
        lookup_threads.append(threading.get_ident())
        if outcome == "unavailable":
            raise UnknownMemoryStore("member memory cannot be read")
        bindings = real_resolve(*args, **kwargs)
        if outcome == "caller_closed":
            loop.call_soon_threadsafe(state._slots.pop, caller.key, None)
        return bindings

    monkeypatch.setattr(sc, "resolve_agent_bindings", resolve)

    if outcome == "usable":
        created = await sc.create_session(state, caller_session_key=_key(caller))
        allocate.assert_called_once()
        child = state.get_slot(created["target"])
        assert child is not None and child.workspace == caller.workspace
    else:
        with pytest.raises(sc.SessionControlError) as error:
            await sc.create_session(state, caller_session_key=_key(caller))
        assert error.value.code == (
            "agent_unverifiable" if outcome == "unavailable" else "caller_not_open"
        )
        allocate.assert_not_called()
        assert not set(state._slots) - before
    assert len(lookup_threads) == 1
    assert lookup_threads[0] != loop_thread


def test_a_caller_that_closes_during_the_await_cannot_still_create(tmp_path, monkeypatch):
    """A removed slot stays usable as an object, so presence must be re-read.

    Closing the caller's tab removes its slot from the table, but the reference
    resolved before the await keeps working -- every attribute still answers. Gating
    on that object would authorize against a caller whose authority already ended
    and publish a persistent session behind it.

    Mutation guard: gating the stale object instead of re-resolving publishes the
    session.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = set(state._slots)

    def _resolve_then_close(_workspace):
        # The caller's tab closes while the project directory is being resolved.
        state._slots.pop(caller.key, None)
        return str(tmp_path)

    monkeypatch.setattr(sc, "default_project_dir", _resolve_then_close)

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    assert err.value.code == "caller_not_open"
    assert set(state._slots) - before == set(), "no session may outlive its creator's authority"


def test_a_caller_that_moves_workspace_during_the_await_is_refused(tmp_path, monkeypatch):
    """The workspace fed the agent-binding decision, so a move invalidates it.

    Mutation guard: carrying the pre-await workspace forward puts the child on a
    boundary its creator does not sit behind.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")

    def _resolve_then_move(_workspace):
        caller.workspace = "somewhere-else"
        return str(tmp_path)

    monkeypatch.setattr(sc, "default_project_dir", _resolve_then_move)

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    assert err.value.code == "caller_workspace_changed"


def test_a_mirror_link_landing_during_the_await_still_refuses(tmp_path, monkeypatch):
    """Eligibility decided before a suspension point says nothing at allocation time.

    The project directory is resolved in a worker thread, so the coroutine suspends
    between the caller gate and the allocation. The caller gate reads the live
    session store, and a dashboard-born session can be given an outbound mirror link
    at any moment -- so a link registered inside that window would otherwise let a
    now-channel-backed caller publish a persistent session outside its containment.

    Mutation guard: without the re-assert next to the allocation, the slot is
    created and the refusal never fires.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = set(state._slots)

    def _resolve_then_mirror(_workspace):
        # Stand in for the interleaving: the mirror link lands while the project
        # directory is still being resolved off-loop -- in the store the gate's
        # re-assert reads, so it is the row that read sees.
        state.sessions.set_mirror_link(_key(caller), "C0FFEE", "1758.0003")
        return str(tmp_path)

    monkeypatch.setattr(sc, "default_project_dir", _resolve_then_mirror)

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    assert err.value.code == "mirrored_caller"
    assert set(state._slots) == before, "no slot may be published for a refused caller"


def test_the_slot_cap_is_re_checked_after_the_await(tmp_path, monkeypatch):
    """The ceiling is read from live state, so it can fill inside the window too.

    The ceiling is lowered for this test because what is under test is WHEN it is
    read, not what it is: minting the production 500 slots is superlinear and cost
    ~17s of pure setup for no extra property. The caller's own slot stays under the
    lowered cap -- asserted below -- so the refusal can only come from the fill that
    lands inside the await.

    Mutation guard: checking the cap only before the await lets two concurrent
    creations land over the ceiling.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    monkeypatch.setattr(sc, "MAX_LIVE_SLOTS", 3)
    # Without this the test could pass on a cap checked only BEFORE the await.
    assert state.live_slot_count() < sc.MAX_LIVE_SLOTS

    def _resolve_then_fill(_workspace):
        for i in range(sc.MAX_LIVE_SLOTS):
            _slot(state, f"filler-{i}")
        return str(tmp_path)

    monkeypatch.setattr(sc, "default_project_dir", _resolve_then_fill)

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    assert err.value.code == "slot_cap_reached"


def test_a_created_slot_records_the_caller_that_asked_for_it(tmp_path, monkeypatch):
    """Attribution is what makes the per-creator ceiling countable at all.

    Mutation guard: drop the ``_created_by`` write and every caller's count stays
    0, so ``MAX_SLOTS_PER_CREATOR`` can never be reached and the cap is decorative.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller.agent = "researcher"
    _agent_resolves(monkeypatch, "default")

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    child = state.get_slot(created["target"])
    assert child is not None
    # The resolved slot key, which is the identity `create_session` also audits --
    # not the history key the caller presents. Write and count must agree, or the
    # ceiling silently never binds.
    assert getattr(child, "_created_by", "") == caller.key
    assert state.creator_slot_count(caller.key) == 1


def test_the_creator_session_id_is_frozen_at_mint_not_read_live(tmp_path, monkeypatch):
    """The child's parent lineage must cite the creator that was live AT MINT.

    The creator SID is stamped on the child at ``session_create`` time, from the
    live caller handle. If instead it were read live at the child's first turn,
    a creator slot closed and replaced in between (a distinct handle with its own
    session id) would make the child cite the REPLACEMENT's crew log -- and that id
    lands in the append-only, immutable ``session/opened`` entry with no recovery.

    Mutation guard: re-read the creator SID live at emit (from the current slot
    handle) and this test reddens, because the replacement below carries a
    different session id than the one frozen at mint.
    """
    from unittest.mock import MagicMock

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller.agent = "researcher"
    creator_client = MagicMock()
    creator_client.session_id = "acp-sess-creator-at-mint"
    caller._acp_client = creator_client
    _agent_resolves(monkeypatch, "default")

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])
    assert child is not None

    # Frozen at mint from the live caller handle, and witnessed by this process.
    assert getattr(child, "_created_by_sid", "") == "acp-sess-creator-at-mint"
    assert getattr(child, "_lineage_minted", False) is True

    # The sid is NOT written into the birth metadata: the transcript is a file an
    # agent's file tools can edit, so nothing read back from it may become the
    # gateway-authored crew-log lineage. Only the attribution rides the metadata,
    # for the ownership boundary.
    written = state.conversation_log.get_metadata(sc.slot_history_key(child))
    assert written.get("created_by") == caller.key
    assert "created_by_sid" not in written

    # Now the creator's handle is replaced with a distinct session id -- the exact
    # window the finding names. The frozen value on the child must NOT follow it.
    replacement = MagicMock()
    replacement.session_id = "acp-sess-replacement"
    caller._acp_client = replacement
    assert getattr(child, "_created_by_sid", "") == "acp-sess-creator-at-mint"


def test_a_slot_nobody_minted_in_this_process_carries_no_lineage_witness(tmp_path):
    """A slot that was not created through ``session_create`` in THIS process --
    a person's own tab, a fork, a restore -- has no lineage witness, whatever its
    ``_created_by`` says. The crew-log ``session/opened.parent`` write is gated on
    the witness, so restored or hand-edited attribution never becomes lineage.

    Mutation guard: default the flag to True, or set it on the plain
    ``get_or_create_slot`` path, and this test reddens.
    """
    state = _make_state(tmp_path)
    plain = _slot(state, "chat-9")
    plain._created_by = "chat-1"  # what a restore from transcript metadata sets
    assert getattr(plain, "_lineage_minted", False) is False
    assert getattr(plain, "_created_by_sid", "") == ""


def test_the_opened_entry_cites_lineage_only_from_a_witnessed_mint():
    """``_crew_log_lineage`` is the one seam between the slot and the crew-log
    ``session/opened.parent`` write. It yields the creator only when this process
    minted the slot; attribution that arrived any other way -- restored from a
    transcript an agent's file tools can edit, or set by hand -- yields nothing,
    so the emitter writes no ``parent`` and no metadata edit can forge lineage.

    Mutation guard: drop the witness check and the second case reddens; read the
    sid live instead of the frozen field and the first case reddens.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard.chat_runner import _crew_log_lineage

    minted = SimpleNamespace(
        _created_by="chat-1", _created_by_sid="acp-sess-creator-at-mint", _lineage_minted=True
    )
    assert _crew_log_lineage(minted) == ("chat-1", "acp-sess-creator-at-mint")

    restored = SimpleNamespace(
        _created_by="chat-1", _created_by_sid="acp-forged-by-editing-the-transcript"
    )
    assert _crew_log_lineage(restored) == ("", "")
    restored_explicit = SimpleNamespace(
        _created_by="chat-1", _created_by_sid="acp-sess-x", _lineage_minted=False
    )
    assert _crew_log_lineage(restored_explicit) == ("", "")

    minted_without_handle = SimpleNamespace(
        _created_by="chat-1", _created_by_sid="", _lineage_minted=True
    )
    assert _crew_log_lineage(minted_without_handle) == ("chat-1", "")


def test_the_opened_entry_records_the_session_class_from_the_live_slot(monkeypatch):
    """``_crew_log_class`` is the seam between the slot and the ``class`` write.

    The facts a reader needs to decide whether one session may read this one's crew
    log, taken at the moment the log is opened because the reader that asks is
    usually asking about a session that has since closed. A cron tab's link is not a
    channel: it names the job's own run and republishes to nobody, which is the
    exemption ``CRON_LINK_PREFIX`` carries wherever that boundary is enforced.

    Mutation guard: drop the cron exemption and the third case reddens; drop the
    mirror probe and the fourth reddens; let the probe's exception escape and the
    fifth reddens instead of recording a channel.
    """
    from types import SimpleNamespace

    from kiro_crew.dashboard import session_control as sc
    from kiro_crew.dashboard.chat_runner import _crew_log_class
    from kiro_crew.dashboard.session_control import CRON_LINK_PREFIX

    mirrored: dict[str, bool] = {"value": False}
    monkeypatch.setattr(sc, "_has_channel_mirror", lambda state, slot: mirrored["value"])
    state = SimpleNamespace()

    plain = SimpleNamespace(memory_mode="persistent", _app="", linked_session_key="", key="chat-9")
    assert _crew_log_class(state, plain) == ("persistent", "", False)

    owned = SimpleNamespace(
        memory_mode="incognito", _app="travel-desk", linked_session_key="", key="chat-9"
    )
    assert _crew_log_class(state, owned) == ("incognito", "travel-desk", False)

    cron = SimpleNamespace(
        memory_mode="persistent", _app="", linked_session_key=f"{CRON_LINK_PREFIX}nightly", key="c"
    )
    assert _crew_log_class(state, cron) == ("persistent", "", False)

    linked = SimpleNamespace(
        memory_mode="persistent", _app="", linked_session_key="slack:C123", key="chat-9"
    )
    assert _crew_log_class(state, linked) == ("persistent", "", True)

    # An OUTBOUND mirror lives in the session store rather than on the slot, so a
    # slot with an empty link can still be republishing every turn.
    mirrored["value"] = True
    assert _crew_log_class(state, plain) == ("persistent", "", True)

    # A probe that cannot answer at all records a channel rather than recording
    # "unpublished": opening a log must not raise, and the safe direction costs a
    # dispatcher one refusal it can ask about.
    def _raises(state, slot):
        raise RuntimeError("no store")

    monkeypatch.setattr(sc, "_has_channel_mirror", _raises)
    assert _crew_log_class(state, plain) == ("persistent", "", True)


def test_an_oversize_creator_session_id_is_dropped_at_mint_not_retained(tmp_path, monkeypatch):
    """The creator sid is backend-authored, so it is bounded where it is RETAINED.

    An id past ``MAX_ACP_SESSION_ID_LEN`` is not stored on the child -- dropped,
    never truncated, so it cannot push the child's ``session/opened`` entry over
    the crew log's size cap and lose the whole entry. The sid is optional: absent
    is a legal record, a clipped id would be a wrong one. (The sid never reaches
    the birth metadata in any case; the bound is about the in-memory slot and the
    entry it feeds.)
    """
    from unittest.mock import MagicMock

    from kiro_crew.validation import MAX_ACP_SESSION_ID_LEN

    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    caller.agent = "researcher"
    creator_client = MagicMock()
    creator_client.session_id = "s" * (MAX_ACP_SESSION_ID_LEN + 1)
    caller._acp_client = creator_client
    _agent_resolves(monkeypatch, "default")

    created = asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))
    child = state.get_slot(created["target"])
    assert child is not None
    assert getattr(child, "_created_by_sid", "") == ""
    # Still a witnessed mint: the slot half of the lineage is recorded, sid absent.
    assert getattr(child, "_lineage_minted", False) is True
    written = state.conversation_log.get_metadata(sc.slot_history_key(child))
    assert "created_by_sid" not in written
    # The attribution itself is unaffected: the slot key is ours, not the backend's.
    assert getattr(child, "_created_by", "") == caller.key


def test_one_caller_cannot_consume_everybody_elses_slots(tmp_path, monkeypatch):
    """The per-creator ceiling bounds the DISTRIBUTION, not just the total.

    The global cap alone leaves an automated creator on a nudge loop able to hold
    all ``MAX_LIVE_SLOTS`` itself, after which every later create -- the person
    opening a new chat tab included -- gets the 429. A resource one caller can
    exhaust is not bounded from anybody else's point of view, so this is the half
    of the bound that keeps the verb safe to auto-approve.

    Mutation guard: test the caller's count against ``MAX_LIVE_SLOTS`` instead of
    ``MAX_SLOTS_PER_CREATOR`` and the first assertion stops refusing.
    """
    state = _make_state(tmp_path)
    hog = _slot(state, "chat-hog")
    hog.agent = "researcher"
    other = _slot(state, "chat-other")
    other.agent = "researcher"
    _agent_resolves(monkeypatch, "default")

    # Exactly what create_session writes, without paying for 50 real creates.
    for i in range(sc.MAX_SLOTS_PER_CREATOR):
        _slot(state, f"held-{i}")._created_by = hog.key

    with pytest.raises(sc.SessionControlError) as err:
        asyncio.run(sc.create_session(state, caller_session_key=_key(hog)))
    assert err.value.code == "creator_slot_cap_reached"
    assert err.value.status == 429, "a cap breach is a 429, not a 500"

    # The global ceiling is nowhere near full, which is the point: the refusal came
    # from this caller's own share, and everyone else still has room.
    assert state.live_slot_count() < sc.MAX_LIVE_SLOTS
    created = asyncio.run(sc.create_session(state, caller_session_key=_key(other)))
    assert created["ok"] is True, "one caller's full share must not starve another"


def test_slots_nobody_asked_for_are_charged_to_nobody(tmp_path):
    """A person's own tab and a fork reach ``get_or_create_slot`` unattributed.

    Two failure modes this pins at once: charging those to a caller would let
    ordinary human use burn an automated caller's budget, and counting an empty
    creator key would make every unattributed slot match at once -- so the first
    ``create_session`` call would refuse on a tree the conductor never touched.
    """
    state = _make_state(tmp_path)
    person = _slot(state, "chat-person")

    assert state.creator_slot_count("") == 0, "an empty key must match nothing"
    assert state.creator_slot_count(person.key) == 0


def test_creation_never_loads_the_config_on_the_event_loop():
    """A cache miss reads and validates the config file, stalling every task.

    `KiroCrewConfig.load()` is synchronous filesystem work. Called directly from a
    coroutine it blocks the whole gateway, not just this request -- the loop cannot
    run anything else while it reads. Offloading it is also what the repository's
    no-blocking-call-on-event-loop rule requires.

    Comments are stripped before the check, so the prose explaining WHY the call is
    offloaded cannot satisfy or break the assertion about the call itself.

    Mutation guard: calling it directly fails the first assertion.
    """
    import inspect

    src = inspect.getsource(sc.create_session)
    code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
    assert "KiroCrewConfig.load()" not in code, "the config load must not run inline on the loop"
    assert (
        "await asyncio.to_thread(KiroCrewConfig.load)" in code
    ), "the config load must be offloaded to a worker thread"


def test_nothing_suspends_while_the_created_slot_is_half_configured():
    """`get_or_create_slot` publishes, so the agent must not be set after an await.

    The slot is in the table the moment it is constructed. `await` is a suspension
    point, so a slot published with no agent can be addressed in that window, and
    `/api/chat` would resolve bindings from a blank agent -- running the turn
    against the DEFAULT workspace's memory store instead of this one. The agent
    therefore rides in the constructor, and the project directory is resolved
    BEFORE the slot exists.

    Asserted on ORDER rather than on the finished slot: every field is correct by
    the time `create_session` returns either way, so a state assertion passes under
    the very interleaving this pins.
    """
    import inspect

    src = inspect.getsource(sc.create_session)
    publish = src.index("get_or_create_slot(")
    assert "await asyncio.to_thread(default_project_dir" in src
    resolve = src.index("await asyncio.to_thread(default_project_dir")
    assert resolve < publish, "the project directory must be resolved before the slot is published"
    # The agent is a constructor argument, not a later assignment.
    assert "agent=agent_name" in src, "the agent must be set at construction"
    assert (
        "slot.agent = " not in src
    ), "assigning the agent after construction reopens the half-configured window"
    # Nothing may suspend between publishing the slot and the last field it needs.
    configured = src.index("slot._titled = True")
    assert "await" not in src[publish:configured], (
        "an await between publishing the slot and configuring it makes a "
        "half-built session addressable"
    )
    # The eligibility gate must be the last thing before the allocation, with no
    # suspension between them, or its answer is stale by the time it is acted on.
    gate = src.rindex("_refuse_ineligible_creator(")
    assert (
        resolve < gate < publish
    ), "the caller gate must be re-asserted after the await and before the slot"
    assert "await" not in src[gate:publish], (
        "an await between the eligibility gate and the allocation lets a caller "
        "that has since become ineligible publish a session"
    )
    # The re-check must read the slot TABLE again, not the object resolved before
    # the await: a closed tab's slot is removed while the reference keeps working.
    reresolve = src.index("live_caller = state.get_slot(caller_key)")
    assert resolve < reresolve < gate, "the caller must be re-resolved from its key after the await"
    assert (
        "_refuse_ineligible_creator(state, live_caller)" in src
    ), "the gate must run on the re-resolved slot, not the pre-await object"
    # Nothing may suspend from the re-resolve onward: this span re-reads every input
    # the allocation rests on, so an await inside it puts the decisions back out of
    # date. Asserted from the re-resolve rather than the gate because more than one
    # await precedes it, and the property is about the LAST one.
    assert "await" not in src[reresolve:publish], (
        "an await between re-resolving the caller and allocating the slot makes "
        "every re-read decision stale again"
    )
    # The folder confirmation is a suspension (it takes the folder-store lock),
    # so it must sit BEFORE the re-resolve: after it, nothing suspends until the
    # slot is configured, which is what makes "confirmed under the lock" still
    # true at the assignment (folder mutations run on this loop).
    exists_check = src.index("await state.read_folders(")
    assert exists_check < reresolve, (
        "the folder existence check suspends, so it must precede the caller "
        "re-resolve -- after the re-gate nothing may suspend"
    )
    # And the filing itself happens inside the synchronous configuration window,
    # so no caller ever observes the published slot unfiled -- the atomicity
    # this test requires.
    filed = src.index("slot.folder_id = folder_id")
    assert publish < filed < configured, (
        "the folder must be assigned between publishing the slot and the end of "
        "its synchronous configuration, or a caller can observe it unfiled"
    )
    # The Model-B un-hide is a DURABLE folder-store mutation, so it may run only
    # after the filing actually landed: before the persist, any of the re-gate's
    # refusals (or the write itself failing) would leave a folder the user hid
    # permanently visible for a call that failed.
    unhide = src.index("await _unhide_folder(")
    persist = src.index("log.update_metadata")
    assert persist < unhide, (
        "un-hiding must follow the persist -- a refused create must leave no "
        "durable folder-tree mutation behind"
    )
    # The allocation-to-persist span is broadcast-atomic: `get_or_create_slot`
    # broadcasts on a leading edge, so without the suspend an idle gateway sends
    # the new slot to every client BEFORE folder_id is assigned -- the session
    # renders at the top level for a frame, the observable unfiled state this
    # feature removes. Same pattern the move path pins for its own filing.
    suspend = src.index("with state.suspend_slots_push():")
    assert gate < suspend < publish, (
        "the slot allocation must sit inside suspend_slots_push, so the slot's "
        "first broadcast frame already shows it filed"
    )


def test_create_refuses_at_the_same_slot_cap_as_fork_and_import(tmp_path, monkeypatch):
    """A third creation path must not make the shared ceiling advisory.

    `chat_fork` and `session_transfer` both refuse at 500 live slots. Nothing else
    bounds how many sessions one caller may open, so a creator that skipped the cap
    would let a looping agent exhaust slots and transcript files.

    Mutation guard: dropping the check lets this create succeed.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    monkeypatch.setattr(type(state), "live_slot_count", lambda self: 500)

    with pytest.raises(sc.SessionControlError) as exc:
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    assert exc.value.code == "slot_cap_reached"
    assert exc.value.status == 429


def test_a_failed_persist_retracts_the_slot(tmp_path, monkeypatch):
    """A session whose ownership did not reach disk must not be left behind.

    Reporting the failure while leaving the slot in the table is the worst outcome:
    the caller sees an error and the session exists anyway -- usable in memory,
    addressable by its creator, and gone on the next restart.

    Mutation guard: without the retraction the slot survives the raise.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = set(state._slots)

    def _boom(*_a, **_kw):
        raise OSError("disk full")

    monkeypatch.setattr(state.conversation_log, "update_metadata", _boom)

    with pytest.raises(OSError):
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    assert set(state._slots) == before, "the unpersisted slot must not survive"


def test_retraction_spares_a_slot_a_turn_already_started_on(tmp_path, monkeypatch):
    """Retraction must not detach work that began inside the persist window.

    `get_or_create_slot` publishes, and the birth write is awaited, so a turn can
    start on the slot while that write is in a worker thread. Popping the slot then
    leaves the turn running with nothing pointing at it -- unreachable by the read
    verb and unstoppable by the stop verb, because both resolve through the slot
    table. A phantom session that vanishes on the next restart is the lesser harm.

    Mutation guard: an unconditional pop removes the slot and loses the turn.
    """
    state = _make_state(tmp_path)
    caller = _slot(state, "chat-1")
    before = set(state._slots)

    def _boom(*_a, **_kw):
        # Stand in for the interleaving: the turn lands on the freshly published
        # slot while the birth write is still in flight, and only then does the
        # write fail.
        born = [k for k in state._slots if k not in before]
        assert born, "the slot is published before the birth write, so it exists here"
        state._slots[born[0]].append("user", "work that must not be orphaned", "msg msg-u")
        raise OSError("disk full")

    monkeypatch.setattr(state.conversation_log, "update_metadata", _boom)

    with pytest.raises(OSError):
        asyncio.run(sc.create_session(state, caller_session_key=_key(caller)))

    survivors = set(state._slots) - before
    assert len(survivors) == 1, "a slot carrying a started turn must not be retracted"
    assert state._slots[survivors.pop()].messages, "the turn's work is still reachable"


def test_slot_cap_has_one_owning_constant() -> None:
    """Every slot-creating path reads the SAME owning ceiling constant.

    The live-slot ceiling has one home -- ``state.MAX_LIVE_SLOTS`` in the module
    that owns ``live_slot_count()`` -- and each slot-creating door (session
    create, chat fork, session import) imports that one name, so the effective
    limit cannot diverge by which door the caller came through. This
    pins that no door has re-introduced its own literal: all three modules must
    expose the identical owning object.
    """
    from kiro_crew.dashboard import chat_fork
    from kiro_crew.dashboard import session_control as sc_mod
    from kiro_crew.dashboard import session_transfer
    from kiro_crew.dashboard import state as state_mod

    owning = state_mod.MAX_LIVE_SLOTS
    # Each door imported the owning constant into its own namespace; assert they
    # are the very same object, so a re-introduced per-door literal is caught.
    assert sc_mod.MAX_LIVE_SLOTS is owning
    assert chat_fork.MAX_LIVE_SLOTS is owning
    assert session_transfer.MAX_LIVE_SLOTS is owning
