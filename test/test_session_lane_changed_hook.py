"""Tests for the SessionLaneChanged hook event.

Covers the Phase 1 exit criteria: the event fires from BOTH tag writers with a
correct added/removed delta, fires only on a STATUS-tag change, and cannot fail
or block the tag write when a hook errors.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, _make_tags_app

from kiro_crew import hooks
from kiro_crew.dashboard.chat_tags import create_tag_definition
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.hooks import HOOK_EVENT_SESSION_LANE_CHANGED, HOOK_EVENTS


class _StoreStub:
    """Minimal ScriptHookStore stand-in answering ``list_all``.

    Both the writers' subscriber precondition and the dispatcher read the registry,
    so a stub must answer it. Carries one enabled lane hook by default, so a test
    driving the real writer path is not skipped before it fires.
    """

    def __init__(self, hook_ids=("stub-hook",)):
        import kiro_crew.hooks as _H

        self._live = [
            type(
                "_H",
                (),
                {"id": h, "enabled": True, "event": _H.HOOK_EVENT_SESSION_LANE_CHANGED},
            )()
            for h in hook_ids
        ]

    def list_all(self):
        return list(self._live)


class _Accepted:
    """Test-local count of deltas ACCEPTED onto a dispatch queue.

    Replaces a production module global that existed only for this guard. Counts
    ``Queue.put_nowait`` calls that RETURN: a ``QueueFull`` raise is a rejection,
    so counting the call itself would score an overflow as an acceptance.

    Filtered to the dispatch tuple rather than counting every queue in the
    process, because the patch is on ``asyncio.Queue`` itself and an unrelated
    queue would otherwise inflate the count.
    """

    count = 0


_accepted = _Accepted()


async def _settle() -> None:
    """Let the fire-and-forget dispatch task run to completion.

    The dispatch is deliberately off the request path, so the response returns
    before the hook has fired and a test that asserts immediately sees nothing.

    DRAINS THE ACTUAL TASK rather than spending a fixed number of loop turns. A
    turn budget is a race dressed as a wait: it passes only while the stubbed
    coroutine happens to finish inside the budget, so on a loaded or slow runner
    the same code observes nothing and the assertion fails -- a flake that reads as
    a defect in the writer and reproduces nowhere locally. Awaiting the task makes
    the wait a fact about the task instead of about the machine.

    Falls back to yielding when nothing was scheduled, so a caller that asserts
    NOTHING fired still gets a turn for anything else pending.
    """
    await _drain(require=False)
    for _ in range(4):
        await asyncio.sleep(0)


async def _settle_dispatch() -> None:
    """Await the REAL dispatch task to completion, not just a few loop turns.

    ``_settle`` is sufficient only when ``_fire_session_lane_changed`` itself is
    stubbed. The real ``ScriptHookStore.fire`` awaits ``asyncio.to_thread`` twice
    -- once to evaluate a matcher, once to persist status bookkeeping -- and a
    thread round-trip cannot complete on bare ``sleep(0)`` turns however many you
    take. A test that drives the genuine fire path through ``_settle`` therefore
    observes NOTHING and passes vacuously no matter what the code does, which is
    exactly how an earlier version of the fan-out test below passed while
    asserting nothing at all.
    """
    await _drain(require=True)


async def _drain(*, require: bool = True) -> None:
    """Await every queued delta to completion, not a fixed number of loop turns.

    A turn budget is a race dressed as a wait: it passes only while the stubbed
    coroutine happens to finish inside the budget, so on a loaded runner the same
    code observes nothing and the assertion fails -- a flake that reads as a
    defect in the writer and reproduces nowhere locally. ``Queue.join`` makes the
    wait a fact about the work instead of about the machine.

    ``require`` guards the vacuous pass: with the real fire path a test that
    never enqueued anything would sail through ``join`` and assert against an
    empty list no matter what the code did.
    """
    from kiro_crew import hooks as H

    if require:
        assert _accepted.count > 0, "no delta was accepted -- the test would prove nothing"
    if H._LANE_QUEUE is not None:
        await asyncio.wait_for(H._LANE_QUEUE.join(), timeout=15)


def _lane(state, name: str) -> str:
    """Create a status tag (a board lane) and return its id.

    Built explicitly rather than read from the seeded vocabulary: seeding is a
    behaviour of a different code path, so depending on it here would make this
    file fail for a reason that has nothing to do with the event.
    """
    return create_tag_definition(state, name, status=True)["id"]


@pytest.fixture(autouse=True)
def _isolate_dispatch_state():
    """Drop the module-global queues and workers around every test in this file.

    The queues and their workers are shared mutable state, and the workers are
    bound to ONE event loop while the suite runs a fresh loop per test. Left
    dirty, a queue from an earlier test satisfies ``_drain``'s "something was
    accepted" assertion without this test having enqueued anything -- an
    order-dependent pass that only shows up under randomised ordering.

    Also installs the acceptance spy, for the same isolation reason: the count
    is per-test and must not carry over.
    """
    from kiro_crew.hooks import _reset_lane_dispatch_state

    _reset_lane_dispatch_state()
    _accepted.count = 0
    real_put = asyncio.Queue.put_nowait

    def _counted(queue, item):
        real_put(queue, item)  # raises QueueFull BEFORE the count on a full queue
        if isinstance(item, tuple) and len(item) == 2:
            if isinstance(item[1], hooks.SessionLaneDelta):
                _accepted.count += 1

    with patch.object(asyncio.Queue, "put_nowait", _counted):
        yield
    _reset_lane_dispatch_state()


class TestLaneDeletionFires:
    """Deleting a status tag is a lane transition and must fire.

    Every session holding that tag just left the lane. Without this a hook bound
    to ``*removed:done*`` for cleanup automation silently misses lane deletion --
    the exact polling gap the event exists to close -- so "fires when status tags
    change" would be untrue of one of the writers that changes them.
    """

    @pytest.mark.asyncio
    async def test_deleting_a_status_tag_fires_for_each_holder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        seen: list[dict] = []

        async def _record(store, **kw):
            seen.append(kw)

        async with TestClient(TestServer(app)) as client:
            lane = _lane(state, "Done")
            state._slots["s1"] = _ChatSlot("s1")
            state._slots["s1"].tags = [lane]
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                resp = await client.delete(f"/api/chat/tags/{lane}")
                await _settle()

        assert resp.status == 200
        assert len(seen) == 1, f"a status-tag deletion must fire once per holder; got {seen!r}"
        assert seen[0]["removed"] == [lane]
        assert seen[0]["added"] == []
        # The id token must survive even though the vocabulary entry is gone, or a
        # matcher could never match a deletion.
        assert f"removed:{lane}" in hooks._session_lane_matcher_context(
            seen[0]["added"], seen[0]["removed"]
        )
        assert seen[0]["slot_key"] == "s1", "the key the dashboard: prefix derives from"

    @pytest.mark.asyncio
    async def test_deleting_a_NON_status_tag_stays_quiet(self, tmp_path, monkeypatch):
        """Scope control: a plain label carries no lane meaning."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        seen: list[dict] = []

        async def _record(store, **kw):
            seen.append(kw)

        async with TestClient(TestServer(app)) as client:
            plain = await (await client.post("/api/chat/tags", json={"name": "repo"})).json()
            assert not plain.get("status"), "fixture must be a non-status tag"
            state._slots["s1"] = _ChatSlot("s1")
            state._slots["s1"].tags = [plain["id"]]
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                resp = await client.delete(f"/api/chat/tags/{plain['id']}")
                await _settle()

        assert resp.status == 200
        assert state._slots["s1"].tags == []  # the strip still happened
        assert seen == [], "deleting a non-status tag is not a lane transition"


class TestOnlyIdTokensAreEmitted:
    """The matcher grammar carries tag IDS only -- names are payload, not contract.

    Name tokens were dropped as a deliberate subtraction. They existed only to
    make a matcher readable, and they cost: a user-controlled string reaching a
    structural grammar needed an injective escape encoder purely to stop one lane
    forging another's token, and the resulting spelling (``*added:In_20Review*``
    for a lane shown as ``In Review``) had to become frozen contract at the first
    subscriber. Ids already select a lane, are rename-proof, and carry no
    separator to escape.

    The asymmetry is what settles it: adding name tokens later is ADDITIVE, while
    removing them later is BREAKING, and the event ships with zero subscribers --
    so the cheapest moment to not freeze that grammar is now. A subscriber that
    wants the human-readable name reads ``added``/``removed``/``tags`` from the
    payload, which carries the ids it can resolve.
    """

    def test_a_lane_name_contributes_no_token(self):
        ctx = hooks._session_lane_matcher_context(["t1"], [])
        assert ctx == "added:t1;", f"expected the id token alone, got {ctx!r}"

    def test_both_directions_are_id_only(self):
        ctx = hooks._session_lane_matcher_context(["t-new"], ["t-old"])
        assert ctx == "added:t-new; removed:t-old;", ctx

    def test_no_user_controlled_text_can_reach_the_grammar(self):
        """The forgery surface is removed rather than escaped.

        With names gone the only token source is an id, so there is no
        user-controlled string to sanitize -- which is why the injective escape
        encoder and its collision pins went with them. What is retained is an
        ALLOWLIST validating persisted ids, not the encoder.
        """
        assert not hasattr(hooks, "_encode_token"), "the escape encoder should be gone"
        assert hooks._is_token_safe("a3f9c21b0e44") is True
        assert hooks._is_token_safe("has space") is False

    def test_a_malformed_persisted_id_cannot_forge_a_token(self):
        """Ids are generated hex, but ``tags.json`` is persisted state.

        A corrupt or hand-edited id carrying a separator would still split into two
        tokens or forge a direction, so the id is validated structurally and skipped
        when it cannot be a token. Skipping degrades matching for that one tag; it
        does not fire the wrong hook.
        """
        ctx = hooks._session_lane_matcher_context(["ok", "has space", "has:colon", ""], [])
        assert ctx == "added:ok;", f"a malformed id reached the grammar: {ctx!r}"

    def test_a_glob_metacharacter_id_cannot_reach_the_grammar(self):
        """The grammar is consumed by ``fnmatch``, so a wildcard id is refused.

        Refusing only the three separators still admitted glob metacharacters. An
        id of ``*`` makes the selector written for it -- ``*added:*;*`` -- match
        EVERY lane change, so that tag's hook would run on sessions it was never
        registered for.
        """
        import fnmatch

        for meta in ("*", "?", "[a-z]", "a*b", "tag?", "[", "]"):
            assert hooks._is_token_safe(meta) is False, f"{meta!r} must not be a token"

        assert fnmatch.fnmatch(
            "added:*;", "*added:*;*"
        ), "control: a wildcard token really does forge this match"

        ctx = hooks._session_lane_matcher_context(["*"], [])
        assert ctx == "", f"a wildcard id reached the grammar: {ctx!r}"
        assert not fnmatch.fnmatch(ctx, "*added:*;*"), "the wildcard id forged a match"

        assert hooks._is_token_safe("0123456789ab") is True, "a generated id"
        assert hooks._is_token_safe("implementation") is True, "a seeded lane key"
        assert hooks._is_token_safe("t-new_1") is True, "hyphen and underscore"

    def test_two_ids_differing_only_in_case_cannot_alias(self):
        """The allowlist must agree with the matcher, which folds case.

        ``_context_matches`` lowercases both sides, so admitting an uppercase id
        would let two DISTINCT lanes share one token and fire each other's hooks.
        The allowlist refuses them instead, which skips matching for that one tag
        rather than firing the wrong lane's hook.
        """
        import fnmatch

        # The mechanism, asserted rather than assumed: matching really does fold case.
        assert fnmatch.fnmatch("added:abc;".lower(), "*added:ABC;*".lower()) is True

        assert hooks._is_token_safe("A3F9C21B0E44") is False, "uppercase must be refused"
        assert hooks._is_token_safe("MixedCase12") is False, "mixed case must be refused"
        # Positive controls: every shape a real id actually takes still passes.
        assert hooks._is_token_safe("a3f9c21b0e44") is True, "a generated hex id"
        assert hooks._is_token_safe("implementation") is True, "a seeded lane key"


class TestThePayloadKeysAreAlwaysPresent:
    """All three keys are stamped even when the caller supplies only some.

    The four event-specific ``fire`` kwargs were collapsed into one ``event_payload``
    mapping (review-flagged: the signature would otherwise widen once per future
    event, and ``SessionTagsChanged`` is already reserved). That moved key naming to
    the caller, which reintroduces a hazard the kwargs could not have: a caller
    omitting a key would hand a hook a payload missing it, and a hook that always
    reads ``added`` would KeyError on a removal-only change.

    So defaults are stamped BEFORE the caller's mapping is applied. A negative
    control caught this as unpinned -- deleting the defaults left every existing test
    passing, because the production caller happens to supply all four.
    """

    @pytest.mark.asyncio
    async def test_a_partial_payload_is_completed_with_defaults(self, tmp_path):
        import kiro_crew.hooks as H

        seen: list[dict] = []

        async def _capture(h, context="", hook_event=None):
            seen.append(dict(hook_event or {}))
            return H.ScriptHookResult(hook_id=h.id, hook_name=h.name, event=h.event)

        store = H.ScriptHookStore(config_dir=tmp_path)
        store._hooks = {
            "h": H.ScriptHook(
                id="h",
                name="h",
                event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
                command="true",
            )
        }

        with patch.object(H, "run_script_hook", _capture):
            # Deliberately partial: only the added half, as a future caller might.
            await store.fire(
                H.HOOK_EVENT_SESSION_LANE_CHANGED,
                context="added:t1",
                event_payload={"added": ["t1"]},
            )

        assert len(seen) == 1, seen
        ev = seen[0]
        for key, empty in (("slot", ""), ("removed", [])):
            assert key in ev, f"{key!r} missing -- a hook reading it would KeyError: {ev!r}"
            assert ev[key] == empty, f"{key!r} should default to {empty!r}, got {ev[key]!r}"
        assert ev["added"] == ["t1"], "the caller's own value must survive the defaults"


class TestDeltaIsStatusOnly:
    """A bundled edit must not leak a non-status tag into the delta.

    The fire GATE is status-filtered, but that only decides whether to fire. If the
    delta itself came from the raw set difference, one request changing a lane AND a
    plain label would put `added:<label>` in the matcher context, so a hook could
    match a non-status tag -- contradicting the status-only contract.
    """

    @pytest.mark.asyncio
    async def test_a_bundled_edit_yields_only_the_status_tag(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        seen: list[dict] = []

        async def _record(store, **kw):
            seen.append(kw)

        async with TestClient(TestServer(app)) as client:
            lane = _lane(state, "Done")
            label = await (await client.post("/api/chat/tags", json={"name": "repo"})).json()
            label_id = label["id"]
            assert not label.get("status"), "fixture must be a NON-status tag"

            state._slots["s1"] = _ChatSlot("s1")
            state._slots["s1"].tags = []
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                # ONE request adding both: a lane AND a plain label.
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [lane, label_id]})
                await _settle()

        assert resp.status == 200
        assert set(state._slots["s1"].tags) == {lane, label_id}, "both tags must still apply"
        assert len(seen) == 1, f"a status transition happened, so it must fire once: {seen!r}"
        ev = seen[0]
        assert ev["added"] == [lane], f"the delta must name ONLY the status tag: {ev['added']!r}"
        assert label_id not in ev["added"] and label_id not in ev["removed"]
        ctx = hooks._session_lane_matcher_context(ev["added"], ev["removed"])
        assert label_id not in ctx, (
            "a non-status id in the matcher context is the observable leak: " f"{ctx!r}"
        )
        assert "repo" not in ctx, "nor its display name"

    @pytest.mark.asyncio
    async def test_a_label_only_edit_still_does_not_fire(self, tmp_path, monkeypatch):
        """Control: filtering the delta must not start firing on label-only edits."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        seen: list[dict] = []

        async def _record(store, **kw):
            seen.append(kw)

        async with TestClient(TestServer(app)) as client:
            label_id = (await (await client.post("/api/chat/tags", json={"name": "repo"})).json())[
                "id"
            ]
            state._slots["s1"] = _ChatSlot("s1")
            state._slots["s1"].tags = []
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [label_id]})
                await _settle()

        assert resp.status == 200
        assert seen == [], f"a label-only edit is not a lane transition: {seen!r}"

    @pytest.mark.asyncio
    async def test_the_filtered_delta_is_never_empty_when_it_fires(self, tmp_path, monkeypatch):
        """The regression the filtering could have introduced, pinned directly.

        An EMPTY delta would be worse than a wrong one: ``fire`` consults a matcher
        only when the context is non-empty, so an empty context skips filtering and
        runs EVERY hook for the event.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        seen: list[dict] = []

        async def _record(store, **kw):
            seen.append(kw)

        async with TestClient(TestServer(app)) as client:
            done = _lane(state, "Done")
            doing = _lane(state, "Doing")
            label_id = (await (await client.post("/api/chat/tags", json={"name": "repo"})).json())[
                "id"
            ]
            state._slots["s1"] = _ChatSlot("s1")
            state._slots["s1"].tags = [doing, label_id]
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                # Lane swap bundled with dropping the label.
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [done]})
                await _settle()

        assert resp.status == 200
        assert len(seen) == 1
        ev = seen[0]
        assert ev["added"] == [done] and ev["removed"] == [doing], ev
        assert label_id not in ev["removed"], "the dropped label must not appear in the delta"
        ctx = hooks._session_lane_matcher_context(ev["added"], ev["removed"])
        assert ctx.strip(), "an empty context would run EVERY hook"
        assert hooks._context_matches(f"*added:{done}*", "glob", ctx) is True
        assert hooks._context_matches(f"*removed:{doing}*", "glob", ctx) is True


class TestBulkDeletionShedsNoHolder:
    """A lane can have more holders than the worker pool, and each is a transition.

    The scheduler this replaced capped CONCURRENCY and dropped everything past the
    cap, so a cleanup hook missed most holders on a widely-used lane -- the very
    gap that firing from the delete path was added to close. The queue must ABSORB
    the fan-out: deferring the work is acceptable, losing it is not.
    """

    @pytest.mark.asyncio
    async def test_more_holders_than_the_worker_pool_all_fire(self):
        import kiro_crew.hooks as H

        fired: list[str] = []

        async def _record(store, **kw):
            fired.append(kw["slot_key"])

        n = 20
        items = [
            H.SessionLaneDelta(
                slot_key=f"s{i}",
                added=[],
                removed=["lane"],
            )
            for i in range(n)
        ]
        with patch.object(H, "_fire_session_lane_changed", _record):
            H.dispatch_session_lane_changed_bulk(_StoreStub(), items=items)
            assert (
                _accepted.count == n
            ), "the whole fan-out must be accepted, not shed at a concurrency cap"
            await _drain()

        # Compared as SETS: the guarantee asserted here is that no holder is
        # lost. Order is checked by the per-session test below.
        assert sorted(fired) == sorted(
            f"s{i}" for i in range(n)
        ), f"{n - len(fired)} holders were shed; got {len(fired)}"

    @pytest.mark.asyncio
    async def test_one_session_keeps_its_own_order(self):
        """Per-session ordering is the invariant the single FIFO protects.

        A pool would reorder freely. Across sessions that is harmless; within a
        single session it is not -- a card dragged out of a lane and back again
        would deliver "left" and "entered" in either order, and the close-out
        subscriber this event was built for acts irreversibly on whichever it
        sees first.

        The sleep below is UNEVEN deliberately. A bare ``sleep(0)`` is not a
        discriminator: a round-robin drain matches the expected order by
        coincidence, so the test would pass against the very hazard it is meant
        to catch -- measured, this control did not fire until the sleep was made
        uneven. Making the FIRST delta the slow one separates them: one FIFO
        still delivers it first, while any concurrent drain finishes later work
        while this one sleeps.
        """
        import kiro_crew.hooks as H

        seen: list[str] = []

        async def _record(store, **kw):
            # Uneven on purpose -- see the docstring.
            await asyncio.sleep(0.05 if kw["added"][0] == "l0" else 0)
            seen.append(kw["added"][0])

        items = [
            H.SessionLaneDelta(
                slot_key="one-session",
                added=[f"l{i}"],
                removed=[],
            )
            for i in range(12)
        ]
        with patch.object(H, "_fire_session_lane_changed", _record):
            H.dispatch_session_lane_changed_bulk(_StoreStub(), items=items)
            await _drain()

        assert seen == [
            f"l{i}" for i in range(12)
        ], f"one session's own deltas were reordered: {seen!r}"

    @pytest.mark.asyncio
    async def test_one_failing_fire_does_not_strand_the_rest(self):
        import kiro_crew.hooks as H

        fired: list[str] = []

        async def _record(store, **kw):
            if kw["slot_key"] == "s1":
                raise RuntimeError("hook unreachable")
            fired.append(kw["slot_key"])

        items = [
            H.SessionLaneDelta(
                slot_key=f"s{i}",
                added=[],
                removed=["l"],
            )
            for i in range(3)
        ]
        with patch.object(H, "_fire_session_lane_changed", _record):
            H.dispatch_session_lane_changed_bulk(_StoreStub(), items=items)
            await _drain()

        assert sorted(fired) == ["s0", "s2"], f"a failing fire stranded the rest: {fired!r}"

    def test_empty_or_storeless_bulk_is_refused_without_accepting(self):
        import kiro_crew.hooks as H

        H.dispatch_session_lane_changed_bulk(_StoreStub(), items=[])
        one = H.SessionLaneDelta(slot_key="s", added=[], removed=["l"])
        H.dispatch_session_lane_changed_bulk(None, items=[one])
        assert _accepted.count == 0


class TestDispatchIsBounded:
    """Absorbing a burst must not become absorbing without limit.

    Each queued delta is memory, and a queue nobody drains is exactly the
    unbounded scheduler the bound exists to prevent -- so the ceiling survives the
    move from concurrency to depth. It is reached only when hooks are not draining
    at all, which is why crossing it is audited rather than silent: a shed cleanup
    is otherwise indistinguishable from a hook that ran and did nothing.
    """

    @pytest.mark.asyncio
    async def test_overflow_past_the_bound_is_refused_and_audited(self, monkeypatch):
        import kiro_crew.hooks as H

        audits: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                audits.append(kw)

        async def _record(store, **kw):
            return None

        monkeypatch.setattr(H, "sel", lambda: _Sel())
        # Depth 2, so the bound is reached by arithmetic: the dispatcher never
        # awaits, so every put lands before the worker can run even once.
        monkeypatch.setattr(H, "_LANE_QUEUE_MAXSIZE", 2)
        H._reset_lane_dispatch_state()

        items = [
            H.SessionLaneDelta(
                slot_key=f"s{i}",
                added=[],
                removed=["l"],
            )
            for i in range(5)
        ]
        with patch.object(H, "_fire_session_lane_changed", _record):
            H.dispatch_session_lane_changed_bulk(_StoreStub(), items=items)

        assert _accepted.count == 2, (
            "the bound must be hard: " f"{_accepted.count} accepted against a depth of 2"
        )
        rejected = [a for a in audits if a.get("outcome") == "rejected"]
        assert rejected, f"the drop must be audited, not silent; got {audits!r}"
        # The audit ``operation`` is a QUERYABLE surface, and it is the one the
        # pre-merge rename first missed. The spec reserves ``SessionTagsChanged``
        # for a future all-tags event, so a stale ``hooks.session_tags_changed``
        # here would make that event's audit records indistinguishable from this
        # one's. Pinned so a later rename cannot silently leave it behind.
        assert rejected[0].get("operation") == "hooks.session_lane_changed", (
            "audit operation must track the event name: " f"{rejected[0].get('operation')!r}"
        )
        err = rejected[0].get("error") or ""
        assert "queue full" in err, f"the audit must name the cause: {err!r}"
        assert "3 hook(s) not run" in err, f"the audit must name the loss: {err!r}"
        # Every shed slot must be identifiable from the record, not just counted.
        assert "s4" in (rejected[0].get("resources") or "")

    @pytest.mark.asyncio
    async def test_under_the_bound_it_still_runs(self):
        """The bound must not be so eager that the normal path stops working."""
        import kiro_crew.hooks as H

        fired: list[str] = []

        async def _record(store, **kw):
            fired.append(kw["slot_key"])

        with patch.object(H, "_fire_session_lane_changed", _record):
            H.dispatch_session_lane_changed_bulk(
                _StoreStub(),
                items=[
                    H.SessionLaneDelta(
                        slot_key="s1",
                        added=["a"],
                        removed=[],
                    )
                ],
            )
            await _drain()

        assert fired == ["s1"]

    @pytest.mark.asyncio
    async def test_no_store_is_refused_without_accepting(self):
        """Refused for the RIGHT reason: no store, not merely no event loop.

        Deliberately async. Run synchronously this assertion passes vacuously --
        with the store check removed there is no running loop, so the queue lookup
        returns ``None`` and the refusal still happens, leaving the test unable to
        tell the two causes apart. Under a running loop only the store check can
        produce it.
        """
        import kiro_crew.hooks as H

        assert asyncio.get_running_loop() is not None  # the discriminator
        H.dispatch_session_lane_changed_bulk(
            None,
            items=[
                H.SessionLaneDelta(
                    slot_key="s1",
                    added=["a"],
                    removed=[],
                )
            ],
        )
        # The observable: nothing was accepted. Under a running loop this can only
        # be the store check, which is what makes the refusal attributable.
        assert _accepted.count == 0


class TestMatcherGrammarLivesWithTheEvent:
    """The token grammar is the EVENT's contract, so it lives beside the event."""

    def test_builder_is_exported_from_hooks(self):
        from kiro_crew.hooks import _session_lane_matcher_context

        assert callable(_session_lane_matcher_context)

    def test_grammar_is_direction_tagged_and_carries_ids(self):
        from kiro_crew.hooks import _session_lane_matcher_context

        ctx = _session_lane_matcher_context(["t-done"], ["t-review"])
        assert "added:t-done" in ctx and "removed:t-review" in ctx
        # Names are payload, never grammar -- see TestOnlyIdTokensAreEmitted.
        assert "Done" not in ctx and "Review" not in ctx, ctx

    def test_a_bare_lane_id_matches_nothing_under_default_glob(self):
        """The documented trap, pinned: whole-string fnmatch needs wildcards."""
        from kiro_crew.hooks import _context_matches, _session_lane_matcher_context

        ctx = _session_lane_matcher_context(["t-done"], [])
        assert _context_matches("t-done", "glob", ctx) is False, "bare id must not match"
        assert _context_matches("*added:t-done*", "glob", ctx) is True
        assert _context_matches("*removed:t-done*", "glob", ctx) is False
        # contains mode needs no wildcards -- also documented.
        assert _context_matches("added:t-done", "contains", ctx) is True

    def test_a_selector_for_a_short_id_does_not_fire_on_an_id_it_prefixes(self):
        """A documented selector must not match a DIFFERENT, longer lane id.

        Matching is whole-string ``fnmatch``, so without a boundary a selector
        written for a short id also matches every longer id starting with it --
        and the hook that runs then belongs to a different lane, which for a
        close-out hook means an irreversible action on the wrong session. Two real
        ids are ``uuid4().hex[:12]`` and cannot prefix each other, but ``tags.json``
        is hand-editable, the same path the token validator already guards for
        whitespace and ``:``.

        Pinned with the SHORT id as the selector and the LONG id as the context,
        which is the only direction that can collide: the reverse never matches.
        """
        from kiro_crew.hooks import _context_matches, _session_lane_matcher_context

        short_ctx = _session_lane_matcher_context(["abc"], [])
        long_ctx = _session_lane_matcher_context(["abcdef"], [])
        assert short_ctx == "added:abc;", short_ctx
        assert long_ctx == "added:abcdef;", long_ctx

        # The discriminator: the documented selector for the short lane.
        selector = "*added:abc;*"
        assert (
            _context_matches(selector, "glob", short_ctx) is True
        ), "the selector must still match its OWN lane"
        assert (
            _context_matches(selector, "glob", long_ctx) is False
        ), "a selector for 'abc' fired on lane 'abcdef' -- the wrong hook runs"

    def test_an_id_carrying_the_terminator_cannot_forge_a_boundary(self):
        """A hand-edited id containing ``;`` is refused, not emitted.

        The terminator is only a boundary while an id cannot contain one; an id
        such as ``a;added:b`` would otherwise forge a second token and a direction
        it never had.
        """
        from kiro_crew.hooks import _is_token_safe, _session_lane_matcher_context

        assert _is_token_safe("a;added:b") is False
        assert _session_lane_matcher_context(["a;added:b"], []) == ""


class TestEventRegistration:
    def test_event_is_registered(self):
        assert HOOK_EVENT_SESSION_LANE_CHANGED in HOOK_EVENTS

    def test_event_is_not_a_kiro_cli_event(self):
        """It must NOT reach the generated kiro-cli agent config.

        NOT a bare ``not in _VALID_HOOK_EVENTS`` assertion: that set is camelCase
        (``preToolUse``, ``stop`` -- agent.py:1528), so NO PascalCase name is ever
        a member and the assertion passes for every string ever written. It would
        keep passing if the exclusion broke, which is the definition of vacuous.

        So drive the actual filter, ``_kiro_hooks_only``, and pin that it strips
        the event under BOTH spellings -- the PascalCase name this event really
        uses, and the camelCase form someone would add if they tried to make it a
        kiro-cli event -- with a positive control proving the filter keeps what it
        should.
        """
        from kiro_crew.agent import _VALID_HOOK_EVENTS, _kiro_hooks_only

        camel = "sessionTagsChanged"
        submitted = {
            HOOK_EVENT_SESSION_LANE_CHANGED: [{"command": "x"}],
            camel: [{"command": "y"}],
            "stop": [{"command": "keep"}],
        }
        kept = _kiro_hooks_only(submitted)

        assert "stop" in kept, f"positive control: the filter must keep a real event; {kept!r}"
        assert HOOK_EVENT_SESSION_LANE_CHANGED not in kept
        assert camel not in kept, (
            "the camelCase spelling must be stripped too, or adding it to "
            "defaults.json would silently make it a kiro-cli event"
        )
        # Guards the premise above: if this set ever became PascalCase, the
        # camelCase half of this test would stop meaning anything.
        assert all(
            not e[:1].isupper() for e in _VALID_HOOK_EVENTS
        ), f"_VALID_HOOK_EVENTS is no longer camelCase; revisit this test: {sorted(_VALID_HOOK_EVENTS)!r}"

    def test_event_is_registrable_through_the_api(self):
        """A supported surface must be able to register a hook for it.

        Without this the hook create/update API rejects the event and
        hand-editing hooks.json is the only way to register one, which makes the
        feature unusable through any supported path.
        """
        from kiro_crew.validation import ALLOWED_HOOK_EVENTS

        assert HOOK_EVENT_SESSION_LANE_CHANGED in ALLOWED_HOOK_EVENTS

    def test_three_event_allowlists_diverge_intentionally(self):
        """Pin all three memberships AND the reason, so neither side drifts.

        Three allowlists govern this event and they do NOT agree. Each membership
        is load-bearing in a different direction, so a follow-up must not "fix"
        the divergence by syncing them:

        * ``HOOK_EVENTS``          -- IN. The dispatcher must be able to fire it.
        * ``ALLOWED_HOOK_EVENTS``  -- IN. The registration API must accept it, or
          no supported surface can create such a hook.
        * ``_VALID_HOOK_EVENTS``   -- OUT. kiro-cli rejects a generated agent
          config naming an event it does not know, so including it there breaks
          the config for every agent.

        Asserted as one test rather than three so the rationale lives beside the
        contradiction it explains.
        """
        from kiro_crew.agent import _VALID_HOOK_EVENTS
        from kiro_crew.validation import ALLOWED_HOOK_EVENTS

        assert HOOK_EVENT_SESSION_LANE_CHANGED in HOOK_EVENTS
        assert HOOK_EVENT_SESSION_LANE_CHANGED in ALLOWED_HOOK_EVENTS
        assert HOOK_EVENT_SESSION_LANE_CHANGED not in _VALID_HOOK_EVENTS


class TestFiresFromTagWriters:
    @pytest.mark.asyncio
    async def test_put_tags_fires_with_delta(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        seen: list[dict] = []

        async def _record(store, **kw):
            seen.append(kw)

        async with TestClient(TestServer(app)) as client:
            lane = _lane(state, "Done")
            state._slots["s1"] = _ChatSlot("s1")
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [lane]})
                await _settle()

        assert resp.status == 200
        assert len(seen) == 1
        assert seen[0]["added"] == [lane]
        assert seen[0]["removed"] == []
        assert seen[0]["slot_key"] == "s1"

    @pytest.mark.asyncio
    async def test_drop_fires_and_reports_the_replaced_lane(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        seen: list[dict] = []

        async def _record(store, **kw):
            seen.append(kw)

        async with TestClient(TestServer(app)) as client:
            first = _lane(state, "Implementation")
            second = _lane(state, "Done")
            slot = _ChatSlot("s1")
            slot.tags = [first]
            state._slots["s1"] = slot
            col = await (
                await client.post(
                    "/api/chat/tag-columns", json={"name": "Done", "tag_ids": [second]}
                )
            ).json()
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": col["id"]})
                await _settle()

        assert resp.status == 200
        assert len(seen) == 1
        assert seen[0]["added"] == [second]
        assert seen[0]["removed"] == [first]


class TestScope:
    @pytest.mark.asyncio
    async def test_non_status_tag_does_not_fire(self, tmp_path, monkeypatch):
        """maybe_auto_tag writes non-status tags routinely; those must stay quiet."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        seen: list[dict] = []

        async def _record(store, **kw):
            seen.append(kw)

        async with TestClient(TestServer(app)) as client:
            plain = await (await client.post("/api/chat/tags", json={"name": "repo"})).json()
            assert not plain.get("status"), "fixture must be a non-status tag"
            state._slots["s1"] = _ChatSlot("s1")
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [plain["id"]]})
                await _settle()

        assert resp.status == 200
        assert state._slots["s1"].tags == [plain["id"]]  # the write still happened
        assert seen == []


class TestCannotBlockTheWrite:
    @pytest.mark.asyncio
    async def test_hook_error_does_not_fail_the_response(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)

        async def _explode(store, **kw):
            raise RuntimeError("hook blew up")

        async with TestClient(TestServer(app)) as client:
            lane = _lane(state, "Done")
            state._slots["s1"] = _ChatSlot("s1")
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _explode),
            ):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [lane]})
                await _settle()

        assert resp.status == 200
        assert state._slots["s1"].tags == [lane]

    @pytest.mark.asyncio
    async def test_blocking_exit_code_is_ignored(self):
        """A hook exiting 2 blocks a PreToolUse call; here it must not.

        Asserted at the wrapper rather than through the handler: exit 2 is
        surfaced by the store's own result, and the wrapper's contract is that
        it returns normally regardless.
        """
        from kiro_crew.hooks import _fire_session_lane_changed

        class _BlockingStore:
            def __init__(self):
                self.calls = 0

            async def fire(self, event, **kw):
                self.calls += 1
                raise RuntimeError("simulated blocked hook")

        store = _BlockingStore()
        await _fire_session_lane_changed(store, slot_key="s1", added=["a"], removed=[])
        assert store.calls == 1  # it really did dispatch

    @pytest.mark.asyncio
    async def test_no_store_is_a_noop(self):
        from kiro_crew.hooks import _fire_session_lane_changed

        await _fire_session_lane_changed(None, slot_key="s1", added=["a"], removed=[])


class TestRefusedWriteDoesNotFire:
    """A write that was REFUSED and rolled back must not announce a change.

    Upstream pins each forced save with ``expected_history_key``; when the save
    refuses (session deleted or rebound mid-persist) the endpoint rolls the live
    tags back and answers with its rejection shape. The hook dispatch sits after
    ``push_slots_update()``, below every refusal's early return, so it must not
    fire -- otherwise a hook would act on a lane transition that never persisted
    and was undone in memory too.
    """

    @pytest.mark.asyncio
    async def test_refused_drop_does_not_fire(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        seen: list[dict] = []

        async def _record(store, **kw):
            seen.append(kw)

        async def _refuse(*a, **kw):
            return False  # the pinned save refused without writing

        async with TestClient(TestServer(app)) as client:
            lane = _lane(state, "Done")
            col = await (
                await client.post("/api/chat/tag-columns", json={"name": "Done", "tag_ids": [lane]})
            ).json()
            state._slots["s1"] = _ChatSlot("s1")
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop", _refuse),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": col["id"]})
                await _settle()

        assert resp.status == 200
        assert seen == [], "a refused, rolled-back drop must not fire SessionLaneChanged"
        # And the rollback really happened, so there was nothing to announce.
        assert state._slots["s1"].tags == []


class TestRemovalOnlyDoesNotFanOut:
    """An empty matcher context makes ``fire`` skip filtering entirely.

    ``ScriptHookStore.fire`` only consults a hook's matcher when the context is
    non-empty (``if hook.matcher: ... elif context:``), so a removal-only
    transition that produced no tokens would run EVERY hook registered for the
    event -- including ones deliberately narrowed to a different lane.
    """

    @pytest.mark.asyncio
    async def test_an_all_invalid_transition_refuses_rather_than_fanning_out(self):
        """The fan-out hazard, closed at its only reachable entrance.

        ``_session_lane_matcher_context`` SKIPS an id that fails token validation
        (``tags.json`` is persisted state a hand edit or a legacy writer can leave
        malformed), so a transition whose every id is unsafe yields an EMPTY
        context -- and ``fire`` consults a matcher only when the context is
        non-empty. Dispatching that runs every hook registered for the event, on a
        lane none of them named; for a destructive close-out hook that is the worst
        outcome available. The fire must refuse instead. Asserted at the store,
        because the observable is that ``fire`` is never reached.
        """
        from kiro_crew.hooks import _fire_session_lane_changed

        calls: list[dict] = []

        class _Store(_StoreStub):
            async def fire(self, event, **kw):
                calls.append(kw)
                return []

        # A space and a ``:`` are the two structural separators, so neither id can
        # be tokenized and the context comes out empty.
        await _fire_session_lane_changed(
            _Store(),
            slot_key="s1",
            added=["has space"],
            removed=["has:colon"],
        )
        assert calls == [], (
            "an empty matcher context reached fire, which SKIPS matcher filtering "
            f"and therefore runs every registered hook: {calls!r}"
        )

    @pytest.mark.asyncio
    async def test_a_valid_transition_still_reaches_fire_with_its_derived_context(self):
        """Counter-control: the guard must refuse ONLY the empty case.

        Without this, the test above also passes when the fire never dispatches at
        all -- which would silence the event rather than narrow it.
        """
        from kiro_crew.hooks import _fire_session_lane_changed

        calls: list[dict] = []

        class _Store(_StoreStub):
            async def fire(self, event, **kw):
                calls.append(kw)
                return []

        await _fire_session_lane_changed(
            _Store(), slot_key="s1", added=["t-done"], removed=["t-doing"]
        )
        assert len(calls) == 1, "a valid transition must still fire"
        ctx = calls[0]["context"]
        assert "added:t-done" in ctx and "removed:t-doing" in ctx, ctx

    @pytest.mark.asyncio
    async def test_a_partly_invalid_transition_still_fires_on_the_surviving_ids(self):
        """Degradation stays PER TAG: one malformed id must not silence the rest."""
        from kiro_crew.hooks import _fire_session_lane_changed

        calls: list[dict] = []

        class _Store(_StoreStub):
            async def fire(self, event, **kw):
                calls.append(kw)
                return []

        await _fire_session_lane_changed(
            _Store(),
            slot_key="s1",
            added=["t-ok", "has space"],
            removed=[],
        )
        assert len(calls) == 1, "a survivable id must still produce a fire"
        assert calls[0]["context"] == "added:t-ok;"

    @pytest.mark.asyncio
    async def test_removal_only_still_produces_a_matcher_context(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        seen: list[dict] = []

        async def _record(store, **kw):
            seen.append(kw)

        async with TestClient(TestServer(app)) as client:
            lane = _lane(state, "Done")
            state._slots["s1"] = _ChatSlot("s1")
            state._slots["s1"].tags = [lane]
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": []})
                await _settle()

        assert resp.status == 200
        assert len(seen) == 1
        assert seen[0]["removed"] == [lane]
        assert seen[0]["added"] == []
        ctx = hooks._session_lane_matcher_context(seen[0]["added"], seen[0]["removed"])
        assert ctx, "a removal-only transition must not yield an empty matcher context"
        # Direction-tagged, so entering and leaving a lane are distinguishable.
        assert f"removed:{lane}" in ctx
        assert "added:" not in ctx, "a removal-only transition must emit no added: token"

    @pytest.mark.asyncio
    async def test_removal_only_fires_leave_hook_but_not_enter_hook(self, tmp_path, monkeypatch):
        """End to end, and DISCRIMINATING in both degenerate directions.

        Two hooks differing only in the direction they select. A removal must run
        the ``*removed:done*`` one and not the ``*added:done*`` one.

        This is deliberately built so it cannot pass for the wrong reason. Two
        earlier versions did. The first used a bare ``review`` matcher: the
        default glob mode fnmatches the WHOLE context, so ANY bare word fails and
        the assertion held without the matcher being consulted. The second still
        used ``_settle``, whose bare loop turns cannot complete the two
        ``asyncio.to_thread`` awaits inside the real ``fire`` -- so the dispatch
        never reached a hook and an empty ``ran`` was guaranteed regardless of the
        code. Hence ``_settle_dispatch`` (which asserts a task existed and awaits
        it) plus the leave-hook as a POSITIVE control: if the context were
        untagged its selector would not match either and ``ran`` would be empty;
        if the context were empty, ``fire`` would skip filtering and BOTH would run.
        """
        import kiro_crew.hooks as H

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        ran: list[str] = []

        # Created before the store so the selectors can name the lane's real ID.
        # The grammar carries ids only, so a selector spelling the display name
        # ("*removed:done*") matches nothing -- which would make this test pass
        # for the wrong reason in the enter direction and fail in the leave one.
        lane = _lane(state, "Done")

        store = H.ScriptHookStore(config_dir=tmp_path)
        store._hooks = {
            "enter": H.ScriptHook(
                id="enter",
                name="on-enter-done",
                event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
                matcher=f"*added:{lane}*",
                command="true",
            ),
            "leave": H.ScriptHook(
                id="leave",
                name="on-leave-done",
                event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
                matcher=f"*removed:{lane}*",
                command="true",
            ),
        }

        async def _fake_run(h, context="", hook_event=None):
            ran.append(h.id)
            return H.ScriptHookResult(hook_id=h.id, hook_name=h.name, event=h.event)

        async with TestClient(TestServer(app)) as client:
            state._slots["s1"] = _ChatSlot("s1")
            state._slots["s1"].tags = [lane]
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: store),
                patch.object(H, "run_script_hook", _fake_run),
            ):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": []})
                await _settle_dispatch()

        assert resp.status == 200
        assert ran == ["leave"], (
            "a removal must run only the removed: selector -- "
            f"got {ran!r} (empty means the context is not direction-tagged; "
            "both means matcher filtering was skipped)"
        )


class TestGovernanceProfileIsConsulted:
    """The capability gate must resolve the ORIGINATING surface's profile.

    ``_script_hooks_capability_denied`` infers the surface from the session key
    it is handed. With no key it falls back to policy-only resolution, so a
    profile denying script hooks for the dashboard is never consulted and a
    profile-denied script runs.
    """

    @pytest.mark.asyncio
    async def test_dispatch_threads_the_effective_session_key(self, tmp_path, monkeypatch):
        """End-to-end, through the REAL fire, because the key is now derived there.

        This once patched ``_fire_session_lane_changed`` and asserted the key the
        WRITER passed it. The writer no longer passes one -- the fire derives it --
        so that assertion would now check a value the test itself supplied. Patching
        the hook STORE instead leaves the derivation under test and makes the
        assertion end-to-end: HTTP write in, ``parent_session_key`` out.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        seen: list[dict] = []

        class _Store(_StoreStub):
            async def fire(self, event, **kw):
                seen.append(kw)
                return []

        async with TestClient(TestServer(app)) as client:
            lane = _lane(state, "Done")
            state._slots["s1"] = _ChatSlot("s1")
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _Store()),
            ):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [lane]})
                await _settle()

        assert resp.status == 200
        assert len(seen) == 1
        # The PREFIXED form, not the bare slot id: sel._infer_source classifies
        # the dashboard surface off "dashboard:", and an unprefixed key hits the
        # bare-key "slack" fallback -- binding the wrong surface, not none.
        assert seen[0]["parent_session_key"] == "dashboard:s1"

    @pytest.mark.asyncio
    async def test_wrapper_forwards_the_key_as_parent_session_key(self):
        from kiro_crew.hooks import _fire_session_lane_changed

        captured: dict = {}

        class _Store(_StoreStub):
            async def fire(self, event, **kw):
                captured.update(kw)
                return []

        await _fire_session_lane_changed(
            _Store(),
            slot_key="s1",
            added=["a"],
            removed=[],
        )
        # DERIVED, not passed: the prefixed form is now built inside the fire, so
        # this asserts the derivation itself rather than a value the test supplied.
        assert captured["parent_session_key"] == "dashboard:s1"

    @pytest.mark.asyncio
    async def test_gate_receives_the_session_key_from_the_hook_event(self, tmp_path):
        """run_script_hook resolves the gate against the threaded key."""
        import kiro_crew.hooks as H

        keys: list[str] = []

        def _gate(session_key: str = "") -> str | None:
            keys.append(session_key)
            return "denied by test profile"  # returns early, so nothing spawns

        hook = H.ScriptHook(
            id="h1", name="h1", event=H.HOOK_EVENT_SESSION_LANE_CHANGED, command="true"
        )
        with patch.object(H, "_script_hooks_capability_denied", _gate):
            result = await H.run_script_hook(
                hook, context="Done", hook_event={"parent_session_key": "dashboard:s1"}
            )

        assert keys == ["dashboard:s1"]
        assert "governance" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_the_gate_never_walks_profiles_on_the_event_loop(self):
        """Every async caller resolves the gate off-loop, for EVERY event.

        Asserted behaviourally rather than by patching ``asyncio.to_thread``: a slow gate
        must not starve a concurrently scheduled coroutine. Resolved inline the ticker
        gets almost no turns. Driven on a PRE-EXISTING event on purpose -- the seam is
        unconditional, so scoping it back to one event must fail this.
        """
        import time

        import kiro_crew.hooks as H

        ticks: list[int] = []

        def _slow_gate(session_key: str = "") -> str | None:
            time.sleep(0.30)  # stands in for the profiles/ directory walk
            return "denied by test profile"  # returns early, so nothing spawns

        async def _ticker() -> None:
            for _ in range(60):
                await asyncio.sleep(0.005)
                ticks.append(1)

        hook = H.ScriptHook(
            id="h1", name="h1", event=H.HOOK_EVENT_USER_PROMPT_SUBMIT, command="true"
        )
        with patch.object(H, "_script_hooks_capability_denied", _slow_gate):
            ticker = asyncio.create_task(_ticker())
            await H.run_script_hook(hook, context="hello", hook_event={})
            ticker.cancel()

        assert len(ticks) > 5, (
            f"event loop starved during the governance lookup (only {len(ticks)} ticks) "
            "-- the capability gate is resolving inline on the loop"
        )


class TestAppTokensCannotTriggerLaneHooks:
    """An app caller must not have a lane hook run for it.

    The dispatch resolves the DASHBOARD profile's ``capabilities.script_hooks``,
    so if an app token that reaches these routes could dispatch, an app profile
    DENYING script hooks would still get a hook command executed on its behalf --
    the app's own governance never consulted. The event carries no app identity to
    resolve instead, so the only correct answer for an app caller is to skip.

    The tag write itself is still applied and still returns 200: the write is
    authorized, only the hook dispatch is not.
    """

    @pytest.mark.asyncio
    async def test_an_app_token_lane_change_does_not_fire(self, tmp_path, monkeypatch):
        """The PUT path: an app-token lane write applies but dispatches nothing."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        # The discriminator: this request carries an APP claim, not the dashboard "".
        app = _make_tags_app(state, app_identity="evil-app")
        seen: list[dict] = []

        async def _record(store, **kw):
            seen.append(kw)

        async with TestClient(TestServer(app)) as client:
            lane = _lane(state, "Done")
            state._slots["s1"] = _ChatSlot("s1")
            state._slots["s1"].tags = []
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [lane]})
                await _drain(require=False)

        assert resp.status == 200, "the WRITE is authorized -- only the dispatch is not"
        assert state._slots["s1"].tags == [lane], "the tag write must still apply"
        assert seen == [], (
            "an app token had a lane hook dispatched for it under the dashboard "
            f"profile -- its own script-hook denial was never consulted: {seen!r}"
        )

    @pytest.mark.asyncio
    async def test_an_app_token_drop_does_not_fire(self, tmp_path, monkeypatch):
        """The drop path: same gate, second of the three dispatch sites."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state, app_identity="evil-app")
        seen: list[dict] = []

        async def _record(store, **kw):
            seen.append(kw)

        async with TestClient(TestServer(app)) as client:
            lane = _lane(state, "Done")
            col = await (
                await client.post("/api/chat/tag-columns", json={"name": "Done", "tag_ids": [lane]})
            ).json()
            state._slots["s1"] = _ChatSlot("s1")
            state._slots["s1"].tags = []
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                resp = await client.post("/api/chat/slots/s1/drop", json={"column_id": col["id"]})
                await _drain(require=False)

        assert resp.status == 200, resp.status
        assert seen == [], f"an app-token DROP dispatched a lane hook: {seen!r}"

    @pytest.mark.asyncio
    async def test_the_dashboard_user_still_fires(self, tmp_path, monkeypatch):
        """Positive control: the gate must not silence the dashboard caller.

        Without this, a gate that refused EVERYTHING would pass the two tests
        above while removing the feature.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)  # no app_identity -> the dashboard user
        seen: list[dict] = []

        async def _record(store, **kw):
            seen.append(kw)

        async with TestClient(TestServer(app)) as client:
            lane = _lane(state, "Done")
            state._slots["s1"] = _ChatSlot("s1")
            state._slots["s1"].tags = []
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [lane]})
                await _settle()

        assert resp.status == 200
        assert len(seen) == 1, f"the dashboard user must still fire exactly once: {seen!r}"

    @pytest.mark.asyncio
    async def test_an_app_token_denial_is_audited(self, tmp_path, monkeypatch):
        """A denial must leave a SEL permission-decision record.

        Without one the refusal is invisible: an app whose profile denies script
        hooks is indistinguishable from an app that never changed a lane, so the
        gate cannot be shown to have run at all. Asserts on the emitted RECORD
        rather than on the absence of a dispatch, because absence is what the
        sibling tests already cover and it cannot prove the audit happened.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state, app_identity="evil-app")
        audits: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                audits.append(kw)

        async with TestClient(TestServer(app)) as client:
            lane = _lane(state, "Done")
            state._slots["s1"] = _ChatSlot("s1")
            state._slots["s1"].tags = []
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.sel", lambda: _Sel()),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
            ):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [lane]})

        assert resp.status == 200, "the WRITE is authorized -- only the dispatch is not"
        denied = [a for a in audits if a.get("outcome") == "denied"]
        assert denied, (
            "the dispatch was refused for an app token with NO permission-decision "
            f"record -- the denial is unauditable; got {audits!r}"
        )
        # The operation is the QUERYABLE surface, and must match the overflow
        # audit in ``hooks`` so both refusals for this event share one operation.
        ops = {a.get("operation") for a in denied}
        assert (
            "hooks.session_lane_changed" in ops
        ), f"the denial must be queryable under this event's operation; got {ops!r}"
        rec = next(a for a in denied if a.get("operation") == "hooks.session_lane_changed")
        assert (
            rec.get("caller") == "evil-app"
        ), f"the record must name WHO was refused, not the surface; got {rec.get('caller')!r}"

    @pytest.mark.asyncio
    async def test_a_label_only_app_edit_audits_no_lane_denial(self, tmp_path, monkeypatch):
        """No lane transition means no lane-dispatch decision to audit.

        The permission gate EMITS a denial record, so consulting it before the
        status comparison made every app-token tag write -- a plain-label edit,
        or a no-op -- log a refusal for an event that was never going to fire.
        That inflates the denied count for
        ``operation="hooks.session_lane_changed"`` and disagrees with the delete
        path, which reaches its dispatch only once a status tag really went.
        """
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state, app_identity="evil-app")
        audits: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                audits.append(kw)

        async with TestClient(TestServer(app)) as client:
            label_id = create_tag_definition(state, "repo", status=False)["id"]
            state._slots["s1"] = _ChatSlot("s1")
            state._slots["s1"].tags = []
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.sel", lambda: _Sel()),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
            ):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": [label_id]})

        assert resp.status == 200, "the write itself is authorized"
        lane_denials = [
            a
            for a in audits
            if a.get("outcome") == "denied" and a.get("operation") == "hooks.session_lane_changed"
        ]
        assert not lane_denials, (
            "a label-only edit changes no lane, so there is no dispatch decision "
            f"to refuse -- yet a denial was audited: {lane_denials!r}"
        )

    def test_an_absent_claim_denial_is_audited_and_distinguished(self):
        """A missing claim is also a denial, and says so differently.

        An absent claim means the caller is not a confirmed dashboard caller --
        the middleware may not have run, or it ran and left the claim absent for
        a person -- so the two reasons must not collapse into one
        indistinguishable record.
        """
        from kiro_crew.dashboard import chat_tags as CT

        audits: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                audits.append(kw)

        with patch("kiro_crew.dashboard.chat_tags.sel", lambda: _Sel()):
            assert CT._lane_dispatch_is_permitted({}) is False
            assert CT._lane_dispatch_is_permitted({"app": "some-app"}) is False

        assert len(audits) == 2, f"both denials must be audited; got {audits!r}"
        errors = [a.get("error", "") for a in audits]
        assert (
            errors[0] != errors[1]
        ), f"the two denial reasons must be distinguishable in the record; got {errors!r}"
        assert audits[0].get("caller") == "unknown", audits[0].get("caller")
        assert audits[1].get("caller") == "some-app", audits[1].get("caller")

    def test_an_absent_app_claim_does_not_dispatch(self):
        """FAIL-CLOSED: a missing claim means the middleware did not run.

        Read directly rather than through a client, because the point is the
        predicate's behaviour on a request the auth layer never stamped -- which a
        client wired to that layer cannot produce. A truthiness test would read
        this as the dashboard user and dispatch (CWE-269).
        """
        from kiro_crew.dashboard import chat_tags as CT

        class _Sel:
            def log_api_access(self, **kw):
                pass

        with patch("kiro_crew.dashboard.chat_tags.sel", lambda: _Sel()):
            assert CT._lane_dispatch_is_permitted({}) is False, "absent claim"
            assert CT._lane_dispatch_is_permitted({"app": None}) is False, "None fails closed"
            assert CT._lane_dispatch_is_permitted({"app": "some-app"}) is False
            assert CT._lane_dispatch_is_permitted({"app": ""}) is True, "the dashboard user"

    def test_the_permitted_branch_is_sel_audited(self):
        """The allow arm must NOT be silent: an authorized dispatch leaves a record.

        A gate that returns True with no row is an un-audited decision, so the arm
        records unconditionally. What keeps that off a hot path is the SUBSCRIBER
        precondition at the writers, pinned separately below -- not a weaker arm.
        """
        from kiro_crew.dashboard import chat_tags as CT

        audits: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                audits.append(kw)

        with patch("kiro_crew.dashboard.chat_tags.sel", lambda: _Sel()):
            assert CT._lane_dispatch_is_permitted({"app": ""}, resources="t1") is True

        allowed = [a for a in audits if a.get("outcome") == "allowed"]
        assert len(allowed) == 1, f"the permitted decision must be audited; got {audits!r}"
        assert allowed[0].get("operation") == "hooks.session_lane_changed", allowed[0]
        assert allowed[0].get("caller") == "dashboard", allowed[0]
        assert allowed[0].get("resources") == "t1", allowed[0]

    def test_the_subscriber_precondition_reads_the_registry(self):
        """The writers skip the gate unless an enabled lane hook exists.

        This is what stops the allow row above accruing once per lane drag in the
        default shipping state. Every arm is driven with a REAL store stub: asserting
        only the no-hook case would pass on an uninitialised store, which is False for
        a reason that has nothing to do with the registry.
        """
        import kiro_crew.hooks as H
        from kiro_crew.dashboard import chat_tags as CT

        def _stub(*events):
            return type(
                "_S",
                (),
                {
                    "list_all": lambda self, e=events: [
                        H.ScriptHook(id=f"h{i}", name=f"h{i}", event=ev, command="true")
                        for i, ev in enumerate(e)
                    ]
                },
            )()

        cases = [
            (None, False, "an uninitialised store cannot report a subscriber"),
            (_stub(), False, "no hook registered at all"),
            (_stub(H.HOOK_EVENT_USER_PROMPT_SUBMIT), False, "another event's hook"),
            (_stub(H.HOOK_EVENT_SESSION_LANE_CHANGED), True, "one enabled lane hook"),
            (
                _stub(H.HOOK_EVENT_USER_PROMPT_SUBMIT, H.HOOK_EVENT_SESSION_LANE_CHANGED),
                True,
                "a lane hook among others",
            ),
        ]
        for store, want, why in cases:
            with patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda s=store: s):
                assert CT._any_lane_hook_registered() is want, why

    def test_a_disabled_lane_hook_is_not_a_subscriber(self):
        """A toggled-off hook must not re-open the audit path it cannot reach."""
        import kiro_crew.hooks as H
        from kiro_crew.dashboard import chat_tags as CT

        off = H.ScriptHook(
            id="h1",
            name="h1",
            event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
            command="true",
            enabled=False,
        )
        store = type("_S", (), {"list_all": lambda self: [off]})()
        with patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: store):
            assert CT._any_lane_hook_registered() is False
