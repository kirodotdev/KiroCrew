"""Tests for the SessionLaneChanged hook event.

Covers the Phase 1 exit criteria: the event fires from BOTH tag writers with a
correct added/removed delta, fires only on a STATUS-tag change, and cannot fail
or block the tag write when a hook errors.
"""

from __future__ import annotations

import asyncio
import contextlib
import pathlib
import sys
import threading
from unittest.mock import patch

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state, _make_tags_app

from kiro_crew import hooks
from kiro_crew.dashboard.chat_tags import create_tag_definition
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.hooks import HOOK_EVENT_SESSION_LANE_CHANGED, HOOK_EVENTS


@pytest.fixture(autouse=True)
def _audit_singleton_warm_by_default(monkeypatch):
    """Default the audit singleton WARM for every test in this module.

    A fresh CI worker starts COLD, and a cold audit log makes run_script_hook refuse
    the run before it reaches the terminal branches most of these tests exist to
    observe -- so they passed locally, where something had already warmed it, and
    failed on the shard. Warmth is now stated rather than inherited from the
    environment. The cold path is not left uncovered: its own tests re-stub this to
    False in their bodies, which takes precedence over this fixture.
    """
    monkeypatch.setattr(hooks, "sel_is_warm", lambda: True)


def _appends_audit_to(sink: list):
    """An awaitable terminal-audit double that records the call ordering."""

    async def _audit(*_args, **_kwargs):
        sink.append("audit")

    return _audit


def _appends_inline_to(sink: list):
    """An awaitable terminal-audit double that records that it ran at all."""

    async def _audit(*_args, **_kwargs):
        sink.append("inline")

    return _audit


def _appends_outcome_to(sink: list):
    """An awaitable terminal-audit double that records the outcome it was handed."""

    async def _audit(_sk, _label, outcome, **_kw):
        sink.append(outcome)

    return _audit


class _FakeProc:
    """Stand-in for a hook's spawned child, so a run needs no real process.

    The tests using it exercise ``run_script_hook``'s own terminal, timeout and
    cancellation paths. Driving those with a real ``true`` or ``sleep 30`` made them
    depend on the host's sandbox and shell -- which is why they passed here, where the
    userns probe fails permanently and the wrap is a no-op, and failed on both CI
    platforms at once.
    """

    def __init__(self, exit_code: int = 0, pid: int = 424242):
        self.pid = pid
        self.returncode: int | None = None
        self._exit_code = exit_code
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.killed = False

    async def wait(self) -> int:
        self.returncode = self._exit_code
        return self._exit_code

    def kill(self) -> None:
        self.killed = True


@pytest.fixture
def fake_spawn(monkeypatch):
    """Replace the child spawn and its drain; return the control knob.

    ``exit_code`` selects the terminal branch, since that branch keys on the exit code
    rather than on the command text. ``hang`` makes the drain outlast the hook timeout,
    so the timeout arm is entered deterministically with no sleeping process -- a
    cancellable await, so a test injecting a cancellation there cannot wedge.
    """
    from kiro_crew import sandbox

    knob: dict = {"exit_code": 0, "hang": False, "proc": None}

    async def _wrap(argv, env=None, _prepare=None):
        # A wrapped argv that DIFFERS from argv keeps both platforms on the single
        # create_subprocess_limited path, so this fake is not Windows-specific.
        return (["fake-wrap", *argv], env, None)

    async def _spawn(*_argv, **_kwargs):
        knob["proc"] = _FakeProc(exit_code=knob["exit_code"])
        return knob["proc"]

    async def _drain(proc, _stdin_data, _cap):
        if knob["hang"]:
            await asyncio.sleep(3600)
        proc.returncode = knob["exit_code"]
        return b"", False, b"", False

    monkeypatch.setattr(sandbox, "sandboxed_spawn_argv_async", _wrap)
    monkeypatch.setattr(sandbox, "create_subprocess_limited", _spawn)
    monkeypatch.setattr(hooks, "_communicate_capped", _drain)
    return knob


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
        store._publish_snapshot()

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
                is_current=lambda: True,
            )
            for i in range(n)
        ]
        with patch.object(H, "_fire_session_lane_changed", _record):
            await H.dispatch_session_lane_changed_bulk(_StoreStub(), items=items)
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
                is_current=lambda: True,
            )
            for i in range(12)
        ]
        with patch.object(H, "_fire_session_lane_changed", _record):
            await H.dispatch_session_lane_changed_bulk(_StoreStub(), items=items)
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
                is_current=lambda: True,
            )
            for i in range(3)
        ]
        with patch.object(H, "_fire_session_lane_changed", _record):
            await H.dispatch_session_lane_changed_bulk(_StoreStub(), items=items)
            await _drain()

        assert sorted(fired) == ["s0", "s2"], f"a failing fire stranded the rest: {fired!r}"

    @pytest.mark.asyncio
    async def test_empty_or_storeless_bulk_is_refused_without_accepting(self):
        import kiro_crew.hooks as H

        await H.dispatch_session_lane_changed_bulk(_StoreStub(), items=[])
        one = H.SessionLaneDelta(slot_key="s", added=[], removed=["l"], is_current=lambda: True)
        await H.dispatch_session_lane_changed_bulk(None, items=[one])
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
                is_current=lambda: True,
            )
            for i in range(5)
        ]
        with patch.object(H, "_fire_session_lane_changed", _record):
            await H.dispatch_session_lane_changed_bulk(_StoreStub(), items=items)

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
            await H.dispatch_session_lane_changed_bulk(
                _StoreStub(),
                items=[
                    H.SessionLaneDelta(
                        slot_key="s1",
                        added=["a"],
                        removed=[],
                        is_current=lambda: True,
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
        await H.dispatch_session_lane_changed_bulk(
            None,
            items=[
                H.SessionLaneDelta(
                    slot_key="s1",
                    added=["a"],
                    removed=[],
                    is_current=lambda: True,
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

    def test_the_any_movement_selector_does_not_fire_on_an_id_it_suffixes(self):
        """The direction-FREE selector must not match a lane whose id ENDS with it.

        The sibling test above covers the PREFIX direction, which the trailing ``;``
        already bounded. This is the other end: a context for lane ``xabc`` is
        ``added:xabc;``, which CONTAINS ``abc;``, so an un-anchored ``*abc;*`` fires
        for a hook bound to lane ``abc`` and a close-out hook acts irreversibly on
        the wrong session. The documented form carries the ``:`` that already
        precedes every id, and ``:`` is outside the id allowlist so it cannot be
        forged -- making ``:<id>;`` matchable only at a token boundary.
        """
        from kiro_crew.hooks import _context_matches, _session_lane_matcher_context

        own = _session_lane_matcher_context(["abc"], [])
        other = _session_lane_matcher_context(["xabc"], [])
        assert own == "added:abc;", own
        assert other == "added:xabc;", other

        # The DOCUMENTED any-movement selector, both modes.
        for mode, selector in (("glob", "*:abc;*"), ("contains", ":abc;")):
            assert (
                _context_matches(selector, mode, own) is True
            ), f"{mode}: the selector must still match its OWN lane"
            assert (
                _context_matches(selector, mode, other) is False
            ), f"{mode}: a selector for 'abc' fired on lane 'xabc' -- the wrong hook runs"

        # Any movement still means EITHER direction; the bound costs nothing there.
        assert (
            _context_matches("*:abc;*", "glob", _session_lane_matcher_context([], ["abc"])) is True
        )

        # Why the form changed: the un-anchored spelling really does collide.
        assert (
            _context_matches("*abc;*", "glob", other) is True
        ), "un-anchored selector no longer collides -- this test no longer discriminates"

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
        store._publish_snapshot()

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
        """End-to-end, through the REAL fire, because the key is derived there.

        Patching ``_fire_session_lane_changed`` and asserting the key the WRITER passed
        would check a value the test itself supplied, since the writer passes none.
        Patching the hook STORE instead leaves the derivation under test and makes the
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

        def _gate(session_key: str = "", app: str = "") -> str | None:
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

        def _slow_gate(session_key: str = "", app: str = "") -> str | None:
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

    @pytest.mark.asyncio
    async def test_an_absent_claim_denial_is_audited_and_distinguished(self):
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
            assert await CT._lane_dispatch_is_permitted({}) is False
            assert await CT._lane_dispatch_is_permitted({"app": "some-app"}) is False

        assert len(audits) == 2, f"both denials must be audited; got {audits!r}"
        errors = [a.get("error", "") for a in audits]
        assert (
            errors[0] != errors[1]
        ), f"the two denial reasons must be distinguishable in the record; got {errors!r}"
        assert audits[0].get("caller") == "unknown", audits[0].get("caller")
        assert audits[1].get("caller") == "some-app", audits[1].get("caller")

    @pytest.mark.asyncio
    async def test_an_absent_app_claim_does_not_dispatch(self):
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
            assert await CT._lane_dispatch_is_permitted({}) is False, "absent claim"
            assert await CT._lane_dispatch_is_permitted({"app": None}) is False, "None fails closed"
            assert await CT._lane_dispatch_is_permitted({"app": "some-app"}) is False
            # `app == ""` marks a confirmed dashboard token, not the owner: Slack hands one to
            # an allow-listed user too, so a bare claim is refused.
            assert (
                await CT._lane_dispatch_is_permitted({"app": ""}) is False
            ), "a bare dashboard claim was accepted as the owner"

    @pytest.mark.asyncio
    async def test_the_gate_records_denials_only(self):
        """The gate audits refusals; a permitted dispatch is recorded per hook RUN.

        The permitted decision is not left un-audited -- it is audited where the
        privileged thing happens, once per hook that actually runs, which the
        companion test below pins. Recording it here as well would file a row on
        every lane drag, including the default state where no hook can run.
        """
        from kiro_crew.dashboard import chat_tags as CT

        audits: list[dict] = []

        class _Sel:
            def log_api_access(self, **kw):
                audits.append(kw)

        # Owner-shaped, because the gate admits the OWNER's dashboard identity only: a bare
        # {"app": ""} marks a confirmed dashboard token, which is not proof of ownership.
        class _OwnerRequest(dict):
            app = {"state": type("S", (), {"owner_id": "owner-1"})()}

        owner_request = _OwnerRequest({"app": "", "user": "owner-1"})

        with patch("kiro_crew.dashboard.chat_tags.sel", lambda: _Sel()):
            assert await CT._lane_dispatch_is_permitted(owner_request, resources="t1") is True

        assert audits == [], f"the gate must record denials only; got {audits!r}"

    @pytest.mark.asyncio
    async def test_a_permitted_hook_run_files_its_allowed_governance_decision(self):
        """A permitted run files BOTH its governance decision and its outcome row.

        The two are not a redundant pair. The governance log is read on its own, and a log
        holding only refusals cannot answer "was this permitted?" -- it is indistinguishable
        from a log in which the run never reached the gate. Deriving the permit from an
        invocation row in a different log is a claim about how much the gap costs, not a record
        of the decision, and the skills-only path in ``fire()`` already files both. The deny arm
        keeps its own decision row: it returns before any outcome row can be written, which is
        asserted separately.
        """
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        decisions: list[dict] = []
        invocations: list[dict] = []

        class _Sel:
            def log_governance_decision(self, **kw):
                decisions.append(kw)

            def log_tool_invocation(self, **kw):
                invocations.append(kw)

        hook = H.ScriptHook(
            id="h1",
            name="h1",
            event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
            command=f'"{sys.executable}" -c "pass"',
        )
        with patch.object(H, "sel", lambda: _Sel()):
            with patch.object(H, "_script_hooks_capability_denied", lambda sk="", app="": None):
                with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
                    await H.run_script_hook(hook, context="c", hook_event={"session_key": "s1"})

        runs = [
            r
            for r in invocations
            if r.get("tool_kind") == "script_hook" and r.get("outcome") != "started"
        ]
        assert len(runs) == 1, f"a permitted run must leave one outcome row; got {invocations!r}"
        assert runs[0]["tool_name"] == "run_script_hook:h1", runs[0]
        assert runs[0]["session_key"] == "s1", runs[0]
        allowed = [d for d in decisions if d.get("outcome") == "allowed"]
        assert len(allowed) == 1, (
            "a permitted run must file exactly one `allowed` governance decision: the governance "
            "log is read on its own, so a log carrying only refusals cannot show that this run "
            f"was permitted rather than never gated; got {decisions!r}"
        )
        assert allowed[0]["session_key"] == "s1", allowed[0]
        assert allowed[0]["tool_name"] == "run_script_hook:h1", allowed[0]
        assert allowed[0]["scope"] == "capabilities.script_hooks", allowed[0]

    def test_the_subscriber_precondition_reads_the_registry(self):
        """The writers skip the gate unless an enabled lane hook exists.

        Nothing can consume a dispatch with no subscriber, so neither the gate nor the
        queue is entered -- which also keeps a refusal from being audited for a lane
        change that could never have fired. Every arm is driven with a REAL store stub:
        asserting only the no-hook case would pass on an uninitialised store, which is
        False for a reason that has nothing to do with the registry.
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


class TestGateOffloadIsACallerSideWorkaround:
    """The ``to_thread`` seam exists ONLY because resolution is still synchronous.

    First Principles is right that this is symptom-level: the cause-level fix is
    non-blocking resolution (or a cached fingerprint) in the owning module, and the
    wrapper is a permanent seam a later fix must remember to unwind. This class is
    that reminder, expressed as a test rather than a description footnote -- when the
    cause IS fixed, this fails and names the seam to delete.
    """

    def test_the_gate_is_still_synchronous_so_the_seam_is_still_needed(self):
        """FAILS once resolution stops blocking -- then delete the wrapper.

        If ``_script_hooks_capability_denied`` becomes a coroutine, or the walk it
        performs is cached away, the offload has no remaining justification and
        ``_script_hooks_capability_denied_async`` plus both ``await`` sites should go.
        """
        import inspect

        import kiro_crew.hooks as H

        assert not inspect.iscoroutinefunction(H._script_hooks_capability_denied), (
            "the capability gate is no longer synchronous -- the cause-level fix has "
            "landed, so remove _script_hooks_capability_denied_async and await sites"
        )
        assert inspect.iscoroutinefunction(H._script_hooks_capability_denied_async), (
            "the offload seam is gone while the gate is still synchronous -- an async "
            "caller resolving it inline stalls the event loop"
        )


class TestWriterConventionHasOneSpelling:
    """A future writer must have ONE call to remember, not two in either order.

    The precondition is a PAIR -- is there a subscriber, and is this caller permitted --
    and a writer that remembers only one half dispatches either to nobody or for a
    caller the gate would have refused. Spelling it twice and differently
    (two early returns in one writer, two ``and`` terms in another) leaves the spec's
    writer table as the only thing keeping them in step. This asserts the structure
    instead, so drift fails the build rather than a review.
    """

    def _source(self) -> str:
        from pathlib import Path

        import kiro_crew.dashboard.chat_tags as CT

        return Path(CT.__file__).read_text(encoding="utf-8")

    def test_the_underlying_helpers_are_called_only_by_the_shared_predicate(self):
        """Each half is reachable from ONE place: ``_lane_dispatch_allowed``."""
        src = self._source()
        for helper in ("_any_lane_hook_registered", "_lane_dispatch_is_permitted"):
            # One definition, one use inside the predicate. A writer calling it
            # directly is a third occurrence and fails here.
            assert src.count(f"def {helper}") == 1, helper
            uses = src.count(f"{helper}(") - src.count(f"def {helper}(")
            assert uses == 1, (
                f"{helper} is called from {uses} place(s); every writer must go through "
                "_lane_dispatch_allowed so the pair cannot drift apart"
            )

    @pytest.mark.asyncio
    async def test_every_writer_goes_through_the_shared_predicate(self):
        """The predicate is the only precondition a writer spells."""
        src = self._source()
        assert src.count("def _lane_dispatch_allowed") == 1
        callers = src.count("_lane_dispatch_allowed(request") - 1  # minus the def line
        assert callers >= 2, (
            f"only {callers} writer(s) consult the shared precondition; the delete, PUT "
            "and drop paths must all reach a lane dispatch through it"
        )


class TestHookInvocationOutcomeIsAudited:
    """A permitted hook's RESULT must reach the audit trail, not only its permission.

    The governance helper records whether a hook was ALLOWED to run; this records what
    happened when it did. Both are needed: a permitted hook that crashed, hung or exited
    non-zero otherwise left the decision audited and the result visible only in process
    memory (``hook.last_status``), which no audit query can reach. Keyed on the same
    session key as the decision row so the two join per invocation.

    The hook must really execute for these to mean anything, so each test opts into
    unsandboxed exec the way the rest of the suite does -- CI provides no sandbox
    backend and refuses otherwise, which makes every outcome ``error``. Commands are
    ``sys.executable`` programs rather than shell builtins: cmd.exe has no ``true``,
    ``exit`` or ``sleep``, so a POSIX one-liner fails to launch on the Windows shard
    instead of producing the outcome under test.
    """

    def _capture(self):
        rows: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                rows.append(kw)

            def log_governance_decision(self, **kw):
                pass

        return rows, _Sel

    @staticmethod
    def _py(program: str) -> str:
        return f'"{sys.executable}" -c "{program}"'

    async def _run(self, hook, session_key: str):
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        rows, sel_cls = self._capture()
        with patch.object(H, "sel", lambda: sel_cls()):
            with patch.object(H, "_script_hooks_capability_denied", lambda sk="", app="": None):
                with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
                    await H.run_script_hook(
                        hook, context="c", hook_event={"session_key": session_key}
                    )
        return [
            r for r in rows if r.get("tool_kind") == "script_hook" and r.get("outcome") != "started"
        ], hook

    @pytest.mark.asyncio
    async def test_a_successful_run_is_audited(self):
        import kiro_crew.hooks as H

        hook = H.ScriptHook(
            id="h1", name="h1", event=H.HOOK_EVENT_SESSION_LANE_CHANGED, command=self._py("pass")
        )
        runs, hook = await self._run(hook, "s1")

        assert len(runs) == 1, f"a completed run must be audited exactly once; got {runs!r}"
        assert runs[0]["outcome"] == hook.last_status, (
            "the audited outcome must be the outcome the run actually produced, "
            f"not a different one; row={runs[0]!r} last_status={hook.last_status!r}"
        )
        assert runs[0]["outcome"] == "ok", runs[0]
        assert runs[0]["tool_name"] == "run_script_hook:h1", runs[0]
        assert runs[0]["session_key"] == "s1", (
            "the invocation row must carry the SAME session key as the permission row, "
            "or the two cannot be joined"
        )

    @pytest.mark.asyncio
    async def test_a_failing_run_is_audited_with_its_exit_code(self):
        import kiro_crew.hooks as H

        hook = H.ScriptHook(
            id="h2",
            name="h2",
            event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
            command=self._py("raise SystemExit(3)"),
        )
        runs, hook = await self._run(hook, "s2")

        assert len(runs) == 1, f"a failed run must be audited; got {runs!r}"
        assert runs[0]["outcome"] == "error", runs[0]
        assert runs[0]["metadata"].get("exit_code") == 3, (
            "the exit status is the point of the record -- without it a failure is "
            f"indistinguishable from a success; got {runs[0]!r}"
        )

    @pytest.mark.asyncio
    async def test_a_timed_out_run_is_audited(self):
        """The path with no exit code at all still has to leave a record."""
        import kiro_crew.hooks as H

        hook = H.ScriptHook(
            id="h3",
            name="h3",
            event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
            command=self._py("import time; time.sleep(30)"),
            timeout=1,
        )
        async with _reaped_children() as children:
            runs, hook = await self._run(hook, "s3")
        assert children, "no child was captured, so this cleanup proved nothing"
        assert all(
            p.returncode is not None for p in children
        ), "a sleeper outlived the test: %r" % [p.returncode for p in children]

        assert len(runs) == 1, f"a timed-out run must be audited; got {runs!r}"
        assert runs[0]["outcome"] == "timeout", runs[0]


class TestSpawnedChildrenCannotWriteIntoTheCheckout:
    """A child spawned by a hook run must not inherit the repository working directory.

    The reaper patches both spawn seams, so a forwarded **kw with no `cwd` put every
    child in the checkout. A command touching a relative path would then write into the
    tree under the test run, which no test may do -- so this drives a child that really
    does write one and asserts where it landed, rather than asserting the kwarg.

    Windows offers no sandbox backend, so `wrap_argv` fail-closes there and a spawn is
    refused unless `agent.sandbox_allow_unsandboxed_exec` is set. This grants that
    opt-in, the same precondition a real Windows script hook needs: without it no child
    starts at all and the assertion below reads as a cwd failure instead.
    """

    @pytest.mark.asyncio
    async def test_a_relative_write_lands_in_the_temporary_directory(self, tmp_path):
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        marker = "kc-cwd-probe.txt"
        repo_marker = pathlib.Path(marker)
        assert not repo_marker.exists(), "precondition: the checkout has no probe file"

        hook = H.ScriptHook(
            id="cwd",
            name="cwd",
            event="Stop",
            matcher="*",
            command=f"\"{sys.executable}\" -c \"open({marker!r},'w').write('x')\"",
            timeout=30,
        )
        async with _reaped_children(cwd=tmp_path):
            with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
                result = await H.run_script_hook(hook, context="c", hook_event={})

        # Separate "no child ran" from "the child ran in the wrong directory". Both leave the
        # marker missing below, and only the second is about cwd.
        assert not result.error, (
            "the hook did not execute, so this proves nothing about cwd: %r (a host with no "
            "sandbox backend refuses the spawn unless the unsandboxed-exec opt-in is set)"
            % (result.error,)
        )
        assert result.exit_code == 0, (
            "the child exited %r with stderr %r, so it never reached the write and the "
            "assertion below cannot speak about cwd" % (result.exit_code, result.stderr)
        )

        assert (tmp_path / marker).exists(), (
            "the child did not write into the isolated directory, so the spawn seams are "
            "not forcing cwd and a relative write would land wherever the runner started"
        )
        assert not repo_marker.exists(), (
            f"a spawned child wrote {marker} into the repository checkout; children must "
            "run in a temporary directory so a test cannot mutate the tree"
        )

    @pytest.mark.asyncio
    async def test_a_relative_write_through_the_store_stays_out_of_the_checkout(self, tmp_path):
        """The same guarantee via ``store.fire``, which is how every other test spawns.

        ``run_script_hook`` is one entry point; the fire path is the one the rest of this file
        drives, and it reaches the same seams. A fire whose cwd is not isolated puts a
        relative write in the repository.
        """
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        marker = "kc-fire-probe.txt"
        repo_marker = pathlib.Path(marker)
        assert not repo_marker.exists(), "precondition: the checkout has no probe file"

        store = H.ScriptHookStore(config_dir=tmp_path)
        created = store.create(
            {
                "name": "fire-cwd-probe",
                "event": H.HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": f"\"{sys.executable}\" -c \"open({marker!r},'w').write('x')\"",
                "enabled": True,
            }
        )
        assert created is not None

        async with _reaped_children(cwd=tmp_path):
            with patch.object(H, "_script_hooks_capability_denied", lambda sk="", app="": None):
                with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
                    await store.fire(H.HOOK_EVENT_USER_PROMPT_SUBMIT, context="anything")

        assert (tmp_path / marker).exists(), (
            "the child did not write into the isolated directory, so the fire path is not "
            "forcing cwd and a relative write would land wherever the runner started"
        )
        assert (
            not repo_marker.exists()
        ), f"a hook fired through the store wrote {marker} into the repository checkout"


@contextlib.asynccontextmanager
async def _reaped_children(cwd=None):
    """Kill and await every subprocess spawned inside the block, in an isolated cwd.

    Independent of the code under test: the hook's own timeout and cancellation paths
    also reap, but a test must not rely on the thing it is testing to clean up after it.

    Every child is forced into a temporary directory rather than inheriting the
    repository checkout, so a command that touched a relative path could not write into
    the tree. The directory is created here when the caller passes none, so no call site
    can forget it; pass ``tmp_path`` to inspect what a child wrote.
    """
    import asyncio as _asyncio
    import tempfile as _tempfile

    import kiro_crew.platform_compat as _platform_compat
    import kiro_crew.sandbox as _sandbox

    with _tempfile.TemporaryDirectory() as _own:
        _cwd = cwd if cwd is not None else _own
        async with _spawn_recorder(_asyncio, _sandbox, _platform_compat, str(_cwd)) as spawned:
            yield spawned


@contextlib.asynccontextmanager
async def _spawn_recorder(_asyncio, _sandbox, _platform_compat, isolated):
    """Record and reap every child spawned while both spawn seams are patched."""
    spawned: list = []
    real_shell = _asyncio.create_subprocess_shell
    real_limited = _sandbox.create_subprocess_limited

    # Forwarding **kw untouched let every child inherit the repository checkout, so a
    # relative write would land in the tree. The docstring above carries the rest.

    async def recording_shell(*a, **kw):
        kw["cwd"] = isolated
        proc = await real_shell(*a, **kw)
        spawned.append(proc)
        return proc

    async def recording_limited(*a, **kw):
        kw["cwd"] = isolated
        proc = await real_limited(*a, **kw)
        spawned.append(proc)
        return proc

    _asyncio.create_subprocess_shell = recording_shell
    _sandbox.create_subprocess_limited = recording_limited
    try:
        yield spawned
    finally:
        _asyncio.create_subprocess_shell = real_shell
        _sandbox.create_subprocess_limited = real_limited
        for proc in spawned:
            # A bare kill drops only the shell, orphaning the python grandchild on
            # Windows; kill_and_reap signals the tree and drains the pipes.
            with contextlib.suppress(Exception):
                await _platform_compat.kill_and_reap(proc, timeout=10)


class TestADeadWorkerIsReplaced:
    """A finished worker task must not be reused, or dispatch stops permanently.

    The guard checked only ``_LANE_WORKER is None``. A worker that was cancelled, or
    that let a ``BaseException`` escape the per-item ``except Exception``, remains a
    non-None task, so every later call reused a task that would never drain again:
    deltas accumulate to the 512 bound and then drop for the life of the process, with
    no recovery path.
    """

    @pytest.mark.asyncio
    async def test_a_finished_worker_is_replaced_on_the_next_dispatch(self):
        import kiro_crew.hooks as H

        H._LANE_QUEUE = None
        H._LANE_WORKER = None
        H._LANE_QUEUE_LOOP = None
        try:
            assert H._lane_dispatch_queue() is not None
            first = H._LANE_WORKER
            assert first is not None

            # Kill it the way a cancellation or escaping BaseException would.
            first.cancel()
            try:
                await first
            except asyncio.CancelledError:
                pass
            assert first.done(), "precondition: the worker must actually be finished"

            assert H._lane_dispatch_queue() is not None
            assert H._LANE_WORKER is not first, (
                "a finished worker was reused -- the queue would fill to its bound and "
                "drop every later delta with no recovery"
            )
            assert not H._LANE_WORKER.done(), "the replacement must be live"
        finally:
            if H._LANE_WORKER is not None:
                H._LANE_WORKER.cancel()
            H._LANE_QUEUE = None
            H._LANE_WORKER = None
            H._LANE_QUEUE_LOOP = None


class TestAnUncommittedHookIsNeverVisible:
    """A hook whose persist FAILS must never be observable as registered.

    Every writer edits ``self._hooks`` before ``_save``, and ``_atomic_mutation`` rolls
    that back when the save raises. The rollback closes the window only AFTER the
    failure, so an unsynchronized reader inside it -- which is what the lane readiness
    check on the tag-write path is -- could observe a hook that never reached disk and
    dispatch to it while the API reported the create as failed.
    """

    def test_a_reader_cannot_observe_a_hook_whose_persist_fails(self, tmp_path):
        import threading

        import kiro_crew.hooks as H

        store = H.ScriptHookStore(config_dir=tmp_path)
        mid_flight = threading.Event()
        reader_tried = threading.Event()
        observed: list[int] = []

        def failing_save():
            # The hook is in _hooks now and not yet on disk: this is the window.
            mid_flight.set()
            reader_tried.wait(timeout=5)
            raise OSError("disk full")

        def reader():
            mid_flight.wait(timeout=5)
            reader_tried.set()
            observed.append(
                sum(
                    1
                    for h in store.list_all()
                    if h.enabled and h.event == H.HOOK_EVENT_SESSION_LANE_CHANGED
                )
            )

        t = threading.Thread(target=reader, daemon=True)
        t.start()
        with patch.object(store, "_save", failing_save):
            with pytest.raises(OSError):
                store.create(
                    {
                        "name": "u1",
                        "event": H.HOOK_EVENT_SESSION_LANE_CHANGED,
                        "command": "true",
                        "enabled": True,
                    }
                )
        t.join(timeout=5)

        assert observed == [0], (
            "the reader observed an uncommitted hook -- the failed create rolled back, "
            f"but the lane had already seen it as registered; observed={observed!r}"
        )
        assert store.list_all() == [], "the rollback itself must leave nothing behind"


class TestOnLoopReadsTakeNoLock:
    """The readiness read must never wait on a writer's mutex.

    Mutations run in ``asyncio.to_thread`` and hold the RLock across ``_save`` ->
    file lock + fsync, while the readiness check runs on the event loop itself. Any
    lock on the read path therefore parks that loop behind a writer's disk I/O,
    freezing requests and heartbeats.
    """

    def test_a_reader_completes_while_a_writer_holds_the_mutex(self, tmp_path):
        import threading

        import kiro_crew.hooks as H

        store = H.ScriptHookStore(config_dir=tmp_path)
        store.create(
            {
                "name": "held",
                "event": H.HOOK_EVENT_SESSION_LANE_CHANGED,
                "command": "true",
                "enabled": True,
            }
        )

        held = threading.Event()
        release = threading.Event()

        def writer():
            with store._mutex:
                held.set()
                release.wait(timeout=10)

        t = threading.Thread(target=writer, daemon=True)
        t.start()
        try:
            assert held.wait(timeout=5), "precondition: the writer must hold the mutex"
            done = threading.Event()
            seen: list[int] = []

            def reader():
                seen.append(len(store.list_all()))
                done.set()

            threading.Thread(target=reader, daemon=True).start()
            assert done.wait(timeout=3), (
                "the on-loop readiness read BLOCKED on a writer's mutex -- on the event "
                "loop this parks every request and the heartbeat behind a disk fsync"
            )
            assert seen == [1], seen
        finally:
            release.set()
            t.join(timeout=5)


class TestSnapshotTracksCommittedState:
    """The lock-free snapshot must equal committed state -- never more, never less.

    Two failure directions, both real: publishing too eagerly re-opens the
    uncommitted-hook finding, and failing to publish makes a committed hook invisible
    so the lane silently stops dispatching.
    """

    def _store(self, tmp_path):
        import kiro_crew.hooks as H

        return H.ScriptHookStore(config_dir=tmp_path), H

    def test_every_committed_mutation_is_visible(self, tmp_path):
        store, H = self._store(tmp_path)
        hook = store.create(
            {
                "name": "a",
                "event": H.HOOK_EVENT_SESSION_LANE_CHANGED,
                "command": "true",
                "enabled": True,
            }
        )
        assert [h.id for h in store.list_all()] == [hook.id], "create must publish"

        store.update(hook.id, {"enabled": False})
        assert [h.enabled for h in store.list_all()] == [False], "update must publish"

        store.delete(hook.id)
        assert store.list_all() == [], "delete must publish"

    def test_an_in_place_update_is_invisible_until_it_persists(self, tmp_path):
        """The reason the snapshot is deep-copied rather than shared."""
        store, H = self._store(tmp_path)
        hook = store.create(
            {
                "name": "b",
                "event": H.HOOK_EVENT_SESSION_LANE_CHANGED,
                "command": "true",
                "enabled": True,
            }
        )

        with patch.object(store, "_save", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                store.update(hook.id, {"enabled": False})

        assert [h.enabled for h in store.list_all()] == [True], (
            "a failed save left the in-place field edit visible to readers -- a shared "
            "(non-copied) snapshot is what causes this"
        )


class TestTheUiPickerMatchesTheBackendAllowlist:
    """A backend-only event is UNREACHABLE: the dashboard cannot create a hook for it.

    The dashboard's hooks page pins its own static event list, so adding an event to
    ``ALLOWED_HOOK_EVENTS`` alone leaves the picker one short and the described user
    with no way to reach the feature. Nothing coupled the two lists, so the drift was
    silent -- this test is the coupling.
    """

    def _events_from_tsx(self) -> set[str]:
        import re
        from pathlib import Path

        page = Path(__file__).resolve().parents[1] / "website/src/pages/hookEventWireValues.ts"
        text = page.read_text(encoding="utf-8")
        m = re.search(r"export const EVENTS = \[(.*?)\]", text, re.S)
        assert m, f"could not find the EVENTS list in {page}"
        found = set(re.findall(r"'([A-Za-z]+)'", m.group(1)))
        assert found, "parsed an EMPTY event list -- the test would prove nothing"
        return found

    def test_the_picker_offers_exactly_the_allowed_events(self):
        from kiro_crew.validation import ALLOWED_HOOK_EVENTS

        ui = self._events_from_tsx()
        assert ui == set(ALLOWED_HOOK_EVENTS), (
            "the dashboard picker and the backend allowlist disagree; "
            f"ui-only={sorted(ui - set(ALLOWED_HOOK_EVENTS))} "
            f"backend-only={sorted(set(ALLOWED_HOOK_EVENTS) - ui)}"
        )

    def test_the_new_event_is_offered_and_styled(self):
        """A picker entry with no style/badge row renders unlabelled."""
        import re
        from pathlib import Path

        import kiro_crew.hooks as H

        page = Path(__file__).resolve().parents[1] / "website/src/pages/HooksPage.tsx"
        text = page.read_text(encoding="utf-8")
        event = H.HOOK_EVENT_SESSION_LANE_CHANGED
        assert event in self._events_from_tsx(), f"{event} is not offered by the picker"
        for const in ("EVENT_STYLE", "EVENT_BADGE"):
            m = re.search(r"const %s[^{]*\{(.*?)\n\}" % const, text, re.S)
            assert m, f"could not find {const} in {page.name}"
            assert event in m.group(1), f"{event} has no {const} entry, so it renders unstyled"


class TestRunStatusIsVisibleThroughListAll:
    """After a hook fires, its run bookkeeping must reach readers.

    Serving readers from a committed snapshot made "live view" vs "committed view" a
    contract, and the post-fire path mutates ``last_run`` / ``last_status`` /
    ``last_error`` / ``run_count`` on the LIVE objects. Without a republish the hooks
    API keeps serving status frozen at the last create/update/toggle/delete, so the
    page's run count and last status stop moving once hooks start firing.
    """

    def test_status_bookkeeping_after_a_fire_reaches_readers(self, tmp_path):
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(config_dir=tmp_path)
        hook = store.create(
            {
                "name": "s1",
                "event": H.HOOK_EVENT_SESSION_LANE_CHANGED,
                "command": "true",
                "enabled": True,
            }
        )
        assert store.list_all()[0].run_count == 0, "precondition: no runs recorded yet"

        # What the post-fire path does: mutate the live object, then persist.
        live = store._hooks[hook.id]
        live.last_status = "ok"
        live.last_error = ""
        live.run_count += 1
        live.last_run = 1234567890.0
        store._persist_current()

        served = store.list_all()[0]
        assert served.run_count == 1, (
            "the hooks API serves run_count frozen at the last CRUD write -- a fired "
            f"hook's bookkeeping never reaches readers; got {served.run_count}"
        )
        assert served.last_status == "ok", served.last_status
        assert served.last_run == 1234567890.0, served.last_run

    def test_status_still_reaches_readers_when_the_save_fails(self, tmp_path):
        """The run HAPPENED, so hiding it would be the same staleness defect."""
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(config_dir=tmp_path)
        hook = store.create(
            {
                "name": "s2",
                "event": H.HOOK_EVENT_SESSION_LANE_CHANGED,
                "command": "true",
                "enabled": True,
            }
        )
        live = store._hooks[hook.id]
        live.last_status = "error"
        live.run_count += 1

        with patch.object(store, "_save", side_effect=OSError("disk full")):
            store._persist_current()

        served = store.list_all()[0]
        assert served.run_count == 1 and served.last_status == "error", (
            "a run whose status write failed is still a run that happened; readers "
            f"must see it; got run_count={served.run_count} status={served.last_status!r}"
        )


class TestARebindMidPersistDoesNotDispatch:
    """A slot rebound while the delete's save awaited must not get a lane delta.

    The delta keys on the bare slot key, so an unpinned enqueue fires "left lane X"
    against whatever session now routes there. The save already treats rebind-mid-persist
    as ordinary -- it refuses with ``applied=False`` -- and the in-memory strip stands
    either way, so the dispatch is the only place a wrong target can reach an irreversible
    close-out hook. Pinned with the same ownership test the drop endpoint applies.
    """

    @pytest.mark.asyncio
    async def test_a_rebound_slot_is_not_dispatched(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        app = _make_tags_app(state)
        seen: list[dict] = []

        async def _record(store, **kw):
            seen.append(kw)

        async with TestClient(TestServer(app)) as client:
            lane = _lane(state, "Done")
            original = _ChatSlot("s1")
            original.tags = [lane]
            state._slots["s1"] = original

            async def rebinding_save(*a, **kw):
                # Exactly what the save's expected_history_key pin exists to detect:
                # s1 now routes to a DIFFERENT slot object, so it refuses the write.
                replacement = _ChatSlot("s1")
                replacement.tags = [lane]
                state._slots["s1"] = replacement
                return False

            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop", rebinding_save),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                resp = await client.delete(f"/api/chat/tags/{lane}")
                await _settle()

        assert resp.status == 200, resp.status
        assert state._slots["s1"] is not original, "precondition: the rebind must have happened"
        assert seen == [], (
            "a rebound slot was dispatched -- the delta targets whatever session now "
            f"routes to s1, not the one that left the lane; got {seen!r}"
        )


class TestACancelledInvocationIsAudited:
    """A cancelled hook run must still leave an outcome row, and must re-raise.

    ``CancelledError`` is a ``BaseException``, so it bypassed the success branch, the
    ``TimeoutError`` branch and the ``Exception`` branch -- and the tree-kill that only
    the timeout branch performs. Gateway shutdown mid-run is routine, so that left both
    an unaudited invocation and a possibly-orphaned process tree.
    """

    @pytest.mark.asyncio
    async def test_a_cancelled_run_is_audited_and_reraises(self):
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        rows: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                rows.append(kw)

            def log_governance_decision(self, **kw):
                pass

        hook = H.ScriptHook(
            id="hc",
            name="hc",
            event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
            command=f'"{sys.executable}" -c "import time; time.sleep(30)"',
            timeout=30,
        )

        async with _reaped_children() as children:
            with patch.object(H, "sel", lambda: _Sel()):
                with patch.object(H, "_script_hooks_capability_denied", lambda sk="", app="": None):
                    with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
                        task = asyncio.ensure_future(
                            H.run_script_hook(hook, context="c", hook_event={"session_key": "s9"})
                        )
                        await asyncio.sleep(1.5)  # let the subprocess actually start
                        assert not task.done(), "precondition: the run must be in flight"
                        task.cancel()
                        with pytest.raises(asyncio.CancelledError):
                            await task
        assert children, "no child was captured, so this cleanup proved nothing"
        assert all(
            p.returncode is not None for p in children
        ), "a sleeper outlived the test: %r" % [p.returncode for p in children]

        runs = [
            r for r in rows if r.get("tool_kind") == "script_hook" and r.get("outcome") != "started"
        ]
        assert len(runs) == 1, (
            "a cancelled invocation left no audit row -- CancelledError is a "
            f"BaseException and passed every audit branch; got {rows!r}"
        )
        assert runs[0]["outcome"] == "cancelled", runs[0]
        assert runs[0]["session_key"] == "s9", runs[0]


class TestPublishSnapshotHoldsItsOwnLock:
    """Publishing must not iterate the hook map unlocked.

    ``_publish_snapshot`` and the rollback capture in ``_atomic_mutation`` both walk
    ``self._hooks``; a concurrent insert or delete during that walk raises
    ``RuntimeError: dictionary changed size during iteration``. Two of the three publish
    call sites (``__init__`` and ``_atomic_mutation``) hold no mutex, so the safety was
    non-local -- it rested on callers. Each now takes the reentrant mutex itself.
    """

    def test_a_concurrent_mutator_cannot_break_publish(self, tmp_path):
        import threading

        import kiro_crew.hooks as H

        store = H.ScriptHookStore(config_dir=tmp_path)
        for i in range(20):
            store._hooks["h%d" % i] = H.ScriptHook(
                id="h%d" % i, name="h%d" % i, event=H.HOOK_EVENT_SESSION_LANE_CHANGED
            )

        # Deterministic interleaving, not a timing race: this deepcopy releases a
        # mutator mid-walk and waits, so unlocked the insert resizes the dict.
        released = threading.Event()
        mutated = threading.Event()

        class _Tripwire(H.ScriptHook):
            def __deepcopy__(self, memo):
                released.set()
                mutated.wait(timeout=3)
                return self

        store._hooks["tripwire"] = _Tripwire(
            id="tripwire", name="tripwire", event=H.HOOK_EVENT_SESSION_LANE_CHANGED
        )

        def mutator():
            released.wait(timeout=5)
            try:
                with store._mutex:
                    for j in range(50):
                        store._hooks["late%d" % j] = H.ScriptHook(
                            id="late%d" % j,
                            name="late%d" % j,
                            event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
                        )
            finally:
                mutated.set()

        m = threading.Thread(target=mutator, daemon=True)
        m.start()
        error: list[BaseException] = []
        try:
            store._publish_snapshot()
        except BaseException as exc:  # noqa: BLE001 - catching it IS the assertion
            error.append(exc)
        mutated.set()
        m.join(timeout=5)

        assert not error, (
            "publishing raced a concurrent mutation: %r -- the publish must take the "
            "mutex itself rather than trusting its callers" % error[:1]
        )

    def test_the_mutex_is_reentrant_so_the_locked_caller_still_works(self, tmp_path):
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(config_dir=tmp_path)
        # _persist_current already holds the mutex and then publishes; a non-reentrant
        # lock would deadlock here rather than fail an assertion.
        with store._mutex:
            store._publish_snapshot()
        assert isinstance(store._snapshot, tuple)


class TestCancelledRunPersistsItsBookkeeping:
    """A cancelled run must not silently lose its monotonic run counter.

    ``fire`` persists BELOW its loop, so a re-raised ``CancelledError`` skipped it.
    ``last_status`` would be overwritten by a later persist, but ``run_count`` is
    monotonic -- a lost increment never self-corrects -- so the cancelled path now
    persists on its way out.
    """

    @pytest.mark.asyncio
    async def test_a_cancelled_fire_still_persists(self, tmp_path):
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(config_dir=tmp_path)
        hook = H.ScriptHook(
            id="hc",
            name="hc",
            event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
            command="true",
            enabled=True,
        )
        store._hooks["hc"] = hook
        # Firing reads COMMITTED state, so an injected hook must be published to exist.
        store._publish_snapshot()

        persisted: list[int] = []
        real = store._persist_current

        def counting_persist():
            persisted.append(hook.run_count)
            return real()

        async def hang(*a, **kw):
            hook.run_count += 1
            await asyncio.sleep(30)

        with patch.object(store, "_persist_current", counting_persist):
            with patch.object(H, "run_script_hook", hang):
                task = asyncio.ensure_future(store.fire(H.HOOK_EVENT_SESSION_LANE_CHANGED))
                await asyncio.sleep(0.3)
                assert not task.done(), "precondition: the run must be in flight"
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

        assert persisted, (
            "a cancelled fire persisted nothing -- the monotonic run_count increment "
            "is lost permanently, since the persist below the loop never runs"
        )
        assert persisted[0] >= 1, persisted


class TestEveryTerminalRunPathIsAudited:
    """Every terminal path of a run must file exactly one invocation audit row.

    The permitted path's audit lives at four separate terminal sites, so the contract
    was asserted by reading rather than enforced. This pins it structurally: each
    terminal branch of the run function carries an audit call.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "exit_code,expected",
        [(0, "ok"), (1, "error"), (2, "blocked")],
    )
    async def test_each_terminal_branch_files_exactly_one_row(
        self, monkeypatch, fake_spawn, exit_code, expected
    ):
        import kiro_crew.hooks as H

        fake_spawn["exit_code"] = exit_code
        hook = H.ScriptHook(id="tb", name="tb", event="Stop", matcher="*", command="whatever")
        rows: list[str] = []

        # A plain lambda returns None, which every caller's `await` rejects -- and that
        # TypeError lands in the generic except arm while the stub still recorded a row.
        async def _record(_sk, _label, outcome, **_kw):
            rows.append(outcome)
            return True

        monkeypatch.setattr(H, "_audit_hook_invocation_now", _record)

        await H.run_script_hook(hook, context="c", hook_event={})

        assert rows == [expected], (
            f"the {expected!r} branch filed {rows!r} rather than exactly one {expected!r} "
            "row, so a run outcome goes unrecorded or gets counted twice"
        )

    @pytest.mark.asyncio
    async def test_a_failed_audit_on_the_exception_path_is_marked(self, monkeypatch, fake_spawn):
        """An unrecorded run must stay distinguishable from a recorded one on EVERY arm.

        This arm used a fire-and-forget seam that swallowed the failure, so a run whose audit
        never landed reported a clean error and looked identical to one that was recorded.
        """
        import kiro_crew.hooks as H

        async def _boom(*_a, **_kw):
            raise RuntimeError("command blew up")

        async def _audit_fails(*_a, **_kw):
            return False

        monkeypatch.setattr(H, "_communicate_capped", _boom)
        monkeypatch.setattr(H, "_audit_hook_invocation_now", _audit_fails)

        hook = H.ScriptHook(id="xp", name="xp", event="Stop", matcher="*", command="whatever")
        result = await H.run_script_hook(hook, context="c", hook_event={})

        assert hook.last_status == "error", hook.last_status
        assert "(audit row not recorded)" in (hook.last_error or ""), (
            "the exception path swallowed its audit failure, so a run that reached no audit "
            f"is indistinguishable from one that did: {hook.last_error!r}"
        )
        assert result.error, result

    def test_no_terminal_branch_lacks_an_audit(self):
        """A floor over BOTH seams, so a new branch cannot ship unaudited.

        The parametrised cases above cover the branches an exit code selects; this
        catches a terminal path added later that files nothing at all.
        """
        import inspect

        import kiro_crew.hooks as H

        body = inspect.getsource(H.run_script_hook)
        # Two spellings file a row: the awaited helper, and `audit_off_loop` composed over the
        # shared writer. Counting only one leaves the other free to be dropped unnoticed.
        audits = body.count("_audit_hook_invocation_now(") + body.count("_hook_invocation_writer(")
        outcomes = {
            o for o in ("ok", "error", "timeout", "cancelled", "blocked") if '"%s"' % o in body
        }
        assert audits >= 4, (
            "only %d audit call(s) in the run function; every terminal path must file "
            "one or a run outcome goes unrecorded" % audits
        )
        for want in ("timeout", "cancelled", "error"):
            assert want in outcomes, "no %r outcome is audited: %r" % (want, sorted(outcomes))


class TestARunsStatusReachesSnapshotReaders:
    """A hook run must republish, or its status is invisible to readers.

    Readers are served the snapshot, so a run mutating ``last_status`` / ``run_count``
    on the live object showed nothing through ``list_all`` until an unrelated write
    republished -- a regression against base, where ``list_all`` returned live objects.
    The Test endpoint hit exactly that gap, so the republish is wrapped in the store.
    """

    @pytest.mark.asyncio
    async def test_a_run_is_visible_through_list_all(self, tmp_path):
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(config_dir=tmp_path)
        hook = H.ScriptHook(
            id="hv", name="hv", event=H.HOOK_EVENT_SESSION_LANE_CHANGED, enabled=True
        )
        store._hooks["hv"] = hook
        store._publish_snapshot()
        assert store.list_all()[0].last_status in (None, ""), "precondition: no run yet"

        async def fake_run(h, ctx=None, ev=None):
            # The real runner always stamps last_run beside the status, and the fold keys the
            # status on it, so a fake that omits it is not a faithful stand-in.
            h.last_status = "ok"
            h.last_run = 1.0
            h.run_count += 1
            return H.ScriptHookResult(hook_id=h.id, hook_name=h.name, event=h.event)

        with patch.object(H, "run_script_hook", fake_run):
            await store.run_one_and_fold(hook, "ctx")

        seen = store.list_all()[0]
        assert seen.last_status == "ok", (
            "a run's status never reached the snapshot: readers see %r, so the hooks "
            "table shows stale status until an unrelated write" % seen.last_status
        )
        assert seen.run_count == 1, seen.run_count

    @pytest.mark.asyncio
    async def test_a_failing_run_still_republishes(self, tmp_path):
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(config_dir=tmp_path)
        hook = H.ScriptHook(
            id="hv", name="hv", event=H.HOOK_EVENT_SESSION_LANE_CHANGED, enabled=True
        )
        store._hooks["hv"] = hook
        store._publish_snapshot()

        async def boom(h, ctx=None, ev=None):
            h.last_status = "error"
            h.last_run = 1.0
            raise RuntimeError("nope")

        with patch.object(H, "run_script_hook", boom):
            with pytest.raises(RuntimeError):
                await store.run_one_and_fold(hook, "ctx")

        assert (
            store.list_all()[0].last_status == "error"
        ), "a failed run did not republish -- the wrapper must publish in `finally`"


class TestThePublishNeverBlocksTheEventLoop:
    """A run's republish must not take the store mutex on the event loop.

    A CRUD writer holds that same mutex across its file lock and fsync, so acquiring it
    inline would park the event loop for that whole write. The publish
    is therefore offloaded; this pins that the event loop keeps running meanwhile.
    """

    @pytest.mark.asyncio
    async def test_the_loop_still_runs_while_a_writer_holds_the_mutex(self, tmp_path):
        import threading
        import time

        import kiro_crew.hooks as H

        store = H.ScriptHookStore(config_dir=tmp_path)
        hook = H.ScriptHook(
            id="hb", name="hb", event=H.HOOK_EVENT_SESSION_LANE_CHANGED, enabled=True
        )
        store._hooks["hb"] = hook
        # Before the holder thread takes the mutex: publishing needs it, and every real
        # store mutation republishes, so a run reads this hook from the snapshot.
        store._publish_snapshot()

        held = threading.Event()
        release = threading.Event()

        def hold_the_mutex():
            with store._mutex:
                held.set()
                release.wait(timeout=5)

        ticks = 0

        async def heartbeat():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        async def fake_run(h, ctx=None, ev=None):
            h.last_status = "ok"
            h.last_run = 1.0
            return H.ScriptHookResult(hook_id=h.id, hook_name=h.name, event=h.event)

        t = threading.Thread(target=hold_the_mutex, daemon=True)
        t.start()
        assert held.wait(timeout=5), "precondition: the writer must hold the mutex"

        beat = asyncio.ensure_future(heartbeat())
        try:
            with patch.object(H, "run_script_hook", fake_run):
                runner = asyncio.ensure_future(store.run_one_and_fold(hook, "ctx"))
                # WALL-CLOCK overshoot: a parked loop also delays the measuring await,
                # so a tick count catches up before it can be read.
                started = time.monotonic()
                await asyncio.sleep(0.2)
                elapsed = time.monotonic() - started
                # 2.0s leaves a 2.5x margin either way: the hold is 5s, so a parked
                # loop overshoots far past this, while a busy machine does not.
                assert elapsed < 2.0, (
                    "a 0.2s sleep took %.2fs, so the event loop was parked for the "
                    "mutex hold -- the publish must not acquire it inline" % elapsed
                )
                assert ticks > 0, "precondition: the heartbeat must have run at all"
                release.set()
                await asyncio.wait_for(runner, timeout=5)
        finally:
            beat.cancel()
            release.set()
            t.join(timeout=5)

        assert store.list_all()[0].last_status == "ok", "the publish must still land"


class TestProcIsNeverUnbound:
    """Cancellation before the subprocess binds must not dereference an unbound name.

    The reap sites sit inside `except ...: try: ... except Exception: pass`, so an
    UnboundLocalError was absorbed rather than surfacing. Binding the name up front and
    testing it makes the None case explicit instead of resting on that swallow.
    """

    def test_the_name_is_bound_before_the_try_and_tested_at_both_reaps(self):
        import inspect

        import kiro_crew.hooks as H

        body = inspect.getsource(H.run_script_hook)
        assert "proc: asyncio.subprocess.Process | None = None" in body, (
            "proc is not pre-bound, so cancellation before the subprocess is created "
            "reaches the reap with the name unbound"
        )
        guarded = body.count("if proc is not None and proc.returncode is None:")
        assert guarded == 2, "a reap site still dereferences proc unguarded: %d" % guarded
        assert body.count("if proc.returncode is None:") == 0, "an unguarded reap remains"
        # Keyed on the first DEREFERENCE rather than the first `try:`: the function opens more
        # than one try statement, and only the one that reaps matters here.
        assert body.index("proc: asyncio.subprocess.Process | None = None") < body.index(
            "if proc is not None and proc.returncode is None:"
        ), "the binding must come before the first reap that dereferences it"


class TestAQueuedDeltaCannotTargetAReplacementSession:
    """Validate slot-instance identity at DRAIN, not only at enqueue.

    The key does not identify a session: a delete/recreate under the same
    channel-derived key rebinds it while the delta waits behind a slow hook, bounded
    only by the 1-300s hook timeout. The writers already guard the enqueue; without the
    same test at the fire, a close-out hook acts irreversibly on the replacement.
    """

    @pytest.mark.asyncio
    async def test_a_rebound_slot_is_dropped_at_drain(self):
        import kiro_crew.hooks as H

        fired: list[dict] = []

        async def _record(store, **kw):
            fired.append(kw)

        live = {"k": "original"}
        # False only AFTER the rebind, so the enqueue is legitimate and the drop is
        # decided at drain -- which is the whole point of the finding.
        delta = H.SessionLaneDelta(
            slot_key="chat-1",
            added=["done"],
            removed=[],
            is_current=lambda: live["k"] == "original",
        )

        H._LANE_QUEUE = None
        H._LANE_WORKER = None
        with patch.object(H, "_fire_session_lane_changed", _record):
            with patch.object(H, "get_global_hook_store", lambda: _StoreStub()):
                live["k"] = "replacement"  # delete/recreate under the same key
                await H.dispatch_session_lane_changed_bulk(_StoreStub(), items=[delta])
                await _settle()

        assert fired == [], (
            "a rebound slot was fired at drain -- the hook would act on the "
            f"replacement session, not the one that changed lane; got {fired!r}"
        )

    @pytest.mark.asyncio
    async def test_an_unrebound_slot_still_fires(self):
        """The arm that must PASS, so the drop above is not vacuous."""
        import kiro_crew.hooks as H

        fired: list[dict] = []

        async def _record(store, **kw):
            fired.append(kw)

        delta = H.SessionLaneDelta(
            slot_key="chat-1", added=["done"], removed=[], is_current=lambda: True
        )

        H._LANE_QUEUE = None
        H._LANE_WORKER = None
        with patch.object(H, "_fire_session_lane_changed", _record):
            with patch.object(H, "get_global_hook_store", lambda: _StoreStub()):
                await H.dispatch_session_lane_changed_bulk(_StoreStub(), items=[delta])
                await _settle()

        assert len(fired) == 1, f"a live slot must still fire; got {fired!r}"
        assert fired[0]["slot_key"] == "chat-1", fired[0]

    def test_both_writers_carry_the_identity_check(self):
        """A writer that omits it silently loses the guard, so pin both sites."""
        import inspect

        from kiro_crew.dashboard import chat_tags as CT

        body = inspect.getsource(CT)
        constructions = body.count("SessionLaneDelta(")
        carried = body.count("is_current=_slot_identity_check(")
        assert constructions == carried == 2, (
            "every SessionLaneDelta construction must carry the identity check: "
            f"{constructions} construction(s), {carried} carrying it"
        )


class TestTheChildReaperIsLoadBearing:
    """The test-side teardown must reap on its own, not lean on the code under test.

    Both sleeper tests run through ``run_script_hook``, which reaps via its timeout and
    cancellation paths -- so those tests cannot show whose cleanup ran. Here nothing but
    the reaper's ``finally`` can reap, so the guarantee is actually observable.
    """

    @pytest.mark.asyncio
    async def test_a_sleeper_is_reaped_with_no_other_cleanup_in_play(self, tmp_path):
        import kiro_crew.sandbox as _sandbox

        # Spawned through the exec seam, not a shell line: a shell wrapper makes the sleeper a
        # GRANDCHILD, and a child sharing this process group is reaped pid-scoped by design.
        work = tmp_path / "sleeper-cwd"
        work.mkdir()
        proc = None
        async with _reaped_children(cwd=work) as children:
            proc = await _sandbox.create_subprocess_limited(
                sys.executable, "-c", "import time; time.sleep(30)"
            )
            assert proc.returncode is None, "precondition: the sleeper must be running"
            assert children == [proc], (
                "the spawn was not captured, so the teardown has nothing to reap: %r" % children
            )

        assert proc.returncode is not None, (
            "the sleeper outlived the block -- the reaper's finally must kill and await "
            "each captured child, since no hook path is present here to do it"
        )
        # Mechanism rather than liveness: an OS that refuses to unlink a directory held as a
        # live process's cwd makes this removal the proof that the handle is gone.
        work.rmdir()
        assert not work.exists(), "the reaped child still holds its working directory"


class TestAFailedAuditWriteIsAlwaysReported:
    """No completion path may swallow a failed audit write.

    Every terminal arm appends ``(audit row not recorded)`` when the row does not land, so an
    operator reading ``last_error`` can tell "this run failed" from "this run failed and the
    trail does not have it". An arm that discards the writer's bool reports a clean failure
    over a silent audit gap -- the one case the marker exists for.
    """

    @pytest.mark.asyncio
    async def test_the_mid_run_cancellation_arm_reports_a_lost_row(
        self, tmp_path, monkeypatch, fake_spawn
    ):
        import kiro_crew.hooks as H

        fake_spawn["hang"] = True

        async def _write_fails(*a, **kw):
            return False

        monkeypatch.setattr(H, "_audit_hook_invocation_now", _write_fails)
        monkeypatch.setattr(H, "_audit_gate_row", lambda sk, label, gating=True: True)

        hook = H.ScriptHook(
            id="a1", name="a1", event="Stop", command="true", enabled=True, timeout=30
        )
        task = asyncio.ensure_future(H.run_script_hook(hook, "ctx", {"session_key": "s"}))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert hook.last_status == "cancelled", hook.last_status
        assert "(audit row not recorded)" in (hook.last_error or ""), (
            "a lost audit row went unreported, so last_error claims a clean cancellation: %r"
            % hook.last_error
        )

    @pytest.mark.asyncio
    async def test_the_before_start_cancellation_arm_reports_a_lost_row(
        self, tmp_path, monkeypatch
    ):
        import kiro_crew.hooks as H

        in_write = threading.Event()
        release = threading.Event()

        def _slow_start(session_key, hook_label, gating=True):
            in_write.set()
            release.wait(timeout=5)
            return True

        async def _write_fails(*a, **kw):
            return False

        monkeypatch.setattr(H, "_audit_gate_row", _slow_start)
        monkeypatch.setattr(H, "_audit_hook_invocation_now", _write_fails)
        monkeypatch.setattr(H, "_script_hooks_capability_denied_async", lambda sk, app="": _none())

        hook = H.ScriptHook(
            id="a2",
            name="a2",
            event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
            command="true",
            enabled=True,
            timeout=5,
        )
        task = asyncio.ensure_future(H.run_script_hook(hook, "ctx", {"session_key": "s"}))
        await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, in_write.wait), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert "(audit row not recorded)" in (hook.last_error or ""), (
            "the before-start arm swallowed a lost row: %r" % hook.last_error
        )


class TestAStartRowIsNeverLeftUnpaired:
    """A cancellation must not leave a ``started`` row with no outcome beside it.

    The start row is written in a worker thread. Cancelling the ``await`` does NOT stop that
    thread, so the row still lands; and the write sits ABOVE the try statement whose
    ``CancelledError`` arm files the ``cancelled`` outcome, so nothing pairs it. A reader of
    the trail then sees a run that started and never ended -- a hook that looks hung when it
    was interrupted before it ever spawned.
    """

    @pytest.mark.asyncio
    async def test_cancelling_during_the_start_write_still_pairs_the_row(
        self, tmp_path, monkeypatch
    ):
        import kiro_crew.hooks as H

        rows: list[str] = []
        in_write = threading.Event()
        release = threading.Event()

        def _slow_start(session_key, hook_label, gating=True):
            rows.append("started")
            # Hold the thread open so the cancellation lands while the await is pending.
            in_write.set()
            release.wait(timeout=5)
            return True

        async def _outcome(session_key, hook_label, status, error=None, **kw):
            rows.append(status)
            return True

        monkeypatch.setattr(H, "_audit_gate_row", _slow_start)
        monkeypatch.setattr(H, "_audit_hook_invocation_now", _outcome)
        monkeypatch.setattr(H, "_script_hooks_capability_denied_async", lambda sk, app="": _none())

        hook = H.ScriptHook(
            id="c1",
            name="c1",
            event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
            command="true",
            enabled=True,
            timeout=5,
        )
        task = asyncio.ensure_future(H.run_script_hook(hook, "ctx", {"session_key": "s"}))
        # Cancel strictly inside the window the finding names: after the start write has been
        # scheduled and entered, before the run can reach its own try statement.
        await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(None, in_write.wait), 5)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert "started" in rows, "precondition: the start row must actually have been written"
        assert rows.count("cancelled") == 1, (
            "a started row was left with no outcome beside it, so the trail shows a run that "
            "began and never ended: %r" % rows
        )


async def _none():
    return ""


class TestTheTestEndpointRunsOffACopyToo:
    """A single-hook run must not mutate the STORED object on the event-loop thread.

    The fire loop runs deep copies and folds their bookkeeping back under ``_mutex`` on a
    worker thread. A caller that hands the LIVE hook to a run instead mutates ``run_count``
    and ``last_*`` on the event loop with no lock, so a concurrent fire's
    ``live.run_count += diff`` interleaves with it -- a lost increment, or a ``last_run`` paired
    with another run's status. Both run sites must therefore isolate the same way.
    """

    def _store(self, tmp_path):
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(config_dir=tmp_path)
        hook = store.create(
            {"name": "t", "event": "Stop", "matcher": "*", "command": "true", "enabled": True}
        )
        return store, hook, H

    @pytest.mark.asyncio
    async def test_a_single_run_never_receives_the_stored_object(self, tmp_path, monkeypatch):
        store, hook, H = self._store(tmp_path)
        seen: list = []

        async def _capture(h, *_a, **_kw):
            seen.append(h)
            h.run_count += 1
            h.last_run = 1.0
            h.last_status = "ok"
            return H.ScriptHookResult(hook_id=h.id, hook_name=h.name, event=h.event)

        monkeypatch.setattr(H, "run_script_hook", _capture)

        await store.run_one_and_fold(store.get(hook.id), context="c", hook_event={})

        assert seen, "the run never happened"
        assert seen[0] is not store.get(hook.id), (
            "the run mutated the STORED hook on the event-loop thread, so a concurrent fire's "
            "fold interleaves with it"
        )

    @pytest.mark.asyncio
    async def test_the_run_still_reaches_the_stored_hook_through_the_fold(
        self, tmp_path, monkeypatch
    ):
        """Positive control: isolation is worthless if the run's outcome then goes missing."""
        store, hook, H = self._store(tmp_path)

        async def _run(h, *_a, **_kw):
            h.run_count += 1
            h.last_run = 1.0
            h.last_status = "ok"
            return H.ScriptHookResult(hook_id=h.id, hook_name=h.name, event=h.event)

        monkeypatch.setattr(H, "run_script_hook", _run)

        await store.run_one_and_fold(store.get(hook.id), context="c", hook_event={})

        live = store.get(hook.id)
        assert live.run_count == 1, live.run_count
        assert live.last_status == "ok", live.last_status
        # and readers see it, which is what the publish is for
        assert store.list_all()[0].run_count == 1, store.list_all()[0].run_count

    @pytest.mark.asyncio
    async def test_a_concurrent_fire_and_single_run_both_count(self, tmp_path, monkeypatch):
        """Two overlapping runs of one hook must total two, because run_count is monotonic."""
        store, hook, H = self._store(tmp_path)

        async def _run(h, *_a, **_kw):
            h.run_count += 1
            h.last_run = 1.0
            h.last_status = "ok"
            return H.ScriptHookResult(hook_id=h.id, hook_name=h.name, event=h.event)

        monkeypatch.setattr(H, "run_script_hook", _run)

        await asyncio.gather(
            store.fire("Stop", context="c"),
            store.run_one_and_fold(store.get(hook.id), context="c", hook_event={}),
        )

        assert store.get(hook.id).run_count == 2, (
            "one of two overlapping runs was lost: %r" % store.get(hook.id).run_count
        )


class TestThePreToolUseGateReadsAnEquivalentSet:
    """What the PreToolUse gate reads must equal what the store holds, field for field.

    The gate is the security-relevant reader, and the snapshot rework changed what it reads
    for all five pre-existing events. Equivalence is therefore a property to MEASURE, not a
    claim to make: these compare the frozen fire targets against the stored hooks directly,
    and pin the one intended difference -- identity, so bookkeeping lands on the copy.
    """

    def _store(self, tmp_path):
        import kiro_crew.hooks as H

        return H.ScriptHookStore(config_dir=tmp_path), H

    def test_the_gate_sees_the_same_hooks_the_store_holds(self, tmp_path):
        store, H = self._store(tmp_path)
        for name, matcher in (("a", "fs_write"), ("b", "*")):
            store.create(
                {
                    "name": name,
                    "event": H.HOOK_EVENT_PRE_TOOL_USE,
                    "matcher": matcher,
                    "command": "true",
                    "enabled": True,
                }
            )
        stored = [h for h in store.list_all() if h.event == H.HOOK_EVENT_PRE_TOOL_USE]
        targets = store._committed_fire_targets(H.HOOK_EVENT_PRE_TOOL_USE)

        fields = ("id", "name", "event", "matcher", "matcher_mode", "command", "timeout")
        assert [tuple(getattr(h, f) for f in fields) for h in targets] == [
            tuple(getattr(h, f) for f in fields) for h in stored
        ], "the gate reads a different set than the store holds, so the rework is not equivalent"

    def test_a_disabled_or_other_event_hook_is_excluded_exactly_as_before(self, tmp_path):
        store, H = self._store(tmp_path)
        wanted = store.create(
            {
                "name": "on",
                "event": H.HOOK_EVENT_PRE_TOOL_USE,
                "matcher": "*",
                "command": "true",
                "enabled": True,
            }
        )
        store.create(
            {
                "name": "off",
                "event": H.HOOK_EVENT_PRE_TOOL_USE,
                "matcher": "*",
                "command": "true",
                "enabled": False,
            }
        )
        store.create({"name": "other", "event": "Stop", "command": "true", "enabled": True})

        targets = store._committed_fire_targets(H.HOOK_EVENT_PRE_TOOL_USE)
        assert [h.id for h in targets] == [
            wanted.id
        ], "the gate's filter diverged from enabled-and-this-event: %r" % [
            (h.name, h.event, h.enabled) for h in targets
        ]

    def test_the_only_difference_is_identity_so_a_run_cannot_edit_the_store(self, tmp_path):
        store, H = self._store(tmp_path)
        hook = store.create(
            {
                "name": "iso",
                "event": H.HOOK_EVENT_PRE_TOOL_USE,
                "matcher": "*",
                "command": "true",
                "enabled": True,
            }
        )
        target = store._committed_fire_targets(H.HOOK_EVENT_PRE_TOOL_USE)[0]
        assert target is not store.get(hook.id), "the gate handed out the stored object itself"

        target.command = "rm -rf /"
        target.run_count += 5
        assert store.get(hook.id).command == "true", (
            "a fire target's edit reached the stored hook, so a run can rewrite what the gate "
            "reads next time"
        )
        assert store.get(hook.id).run_count == 0, store.get(hook.id).run_count

    def test_two_runs_of_one_hook_do_not_share_a_target(self, tmp_path):
        """The snapshot alone cannot carry this, which is why the per-call copy earns its place.

        ``_publish_snapshot`` already deep-copies, so isolation from the STORE is settled before
        this method runs. What the per-call copy adds is isolation between concurrent fires:
        ``_merge_run_bookkeeping`` folds back the DIFFERENCE against each entry's own pre-run
        counters, so two runs sharing one target would read each other's counters as their own.
        """
        store, H = self._store(tmp_path)
        store.create(
            {
                "name": "shared",
                "event": H.HOOK_EVENT_PRE_TOOL_USE,
                "matcher": "*",
                "command": "true",
                "enabled": True,
            }
        )
        first = store._committed_fire_targets(H.HOOK_EVENT_PRE_TOOL_USE)[0]
        second = store._committed_fire_targets(H.HOOK_EVENT_PRE_TOOL_USE)[0]

        assert first is not second, (
            "two fires share one target object, so one run's bookkeeping is the other run's "
            "pre-run baseline"
        )
        first.run_count += 3
        assert second.run_count == 0, (
            "a run's counter moved on another run's target: %r" % second.run_count
        )

    @pytest.mark.asyncio
    async def test_a_pretooluse_run_folds_back_into_what_the_gate_reads_next(self, tmp_path):
        """FP's verify-before-merge item, as an arm rather than a claim.

        The gate reads the snapshot, and a run's bookkeeping only reaches that snapshot through
        the fold. A fold that silently dropped its update would leave the gate reading a hook
        whose status never advances -- degradation with no error anywhere, which is exactly the
        failure mode a claim in the description cannot catch.
        """
        store, H = self._store(tmp_path)
        hook = store.create(
            {
                "name": "gate",
                "event": H.HOOK_EVENT_PRE_TOOL_USE,
                "matcher": "*",
                "command": "true",
                "enabled": True,
            }
        )
        before = store._committed_fire_targets(H.HOOK_EVENT_PRE_TOOL_USE)[0]
        assert before.run_count == 0, before.run_count

        ran = store._committed_fire_targets(H.HOOK_EVENT_PRE_TOOL_USE)[0]
        ran.run_count += 1
        ran.last_run = 1.0
        ran.last_status = "ok"
        await asyncio.to_thread(store._merge_run_bookkeeping, [(ran, 0, 0.0)])

        after = store._committed_fire_targets(H.HOOK_EVENT_PRE_TOOL_USE)[0]
        assert after.run_count == 1, (
            "the fold did not reach the set the PreToolUse gate reads, so the gate sees a hook "
            "whose runs never advance: %r" % after.run_count
        )
        assert after.last_status == "ok", after.last_status
        assert store.get(hook.id).run_count == 1, store.get(hook.id).run_count


class TestARolledBackHookNeverFires:
    """A hook whose write never reached disk must not reach the shell."""

    @pytest.mark.asyncio
    async def test_a_hook_mid_rollback_does_not_run_its_command(self, tmp_path, monkeypatch):
        import threading

        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        sentinel = tmp_path / "FIRED"
        rolled_back = (
            '"' + sys.executable + '" -c "open(r\'' + str(sentinel) + "', chr(119)).close()\""
        )
        committed = '"' + sys.executable + '" -c "pass"'

        store = H.ScriptHookStore(config_dir=tmp_path)
        created = store.create(
            {
                "name": "rollback-probe",
                "event": H.HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": committed,
                "enabled": True,
            }
        )
        assert created is not None

        inside = threading.Event()
        release = threading.Event()

        def blocking_save(*a, **kw):
            inside.set()
            release.wait(timeout=10)
            raise OSError("disk full")

        monkeypatch.setattr(store, "_save", blocking_save)
        mutation = asyncio.create_task(
            asyncio.to_thread(store.update, created.id, {"command": rolled_back})
        )
        assert await asyncio.to_thread(inside.wait, 10), "the mutation never opened its window"

        # Precondition: the live object MUST carry the uncommitted command right now,
        # or the interleaving under test is not actually in effect.
        assert store.get(created.id).command == rolled_back, "window not open"

        async with _reaped_children(cwd=tmp_path):
            with patch.object(H, "_script_hooks_capability_denied", lambda sk="", app="": None):
                with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
                    await store.fire(H.HOOK_EVENT_USER_PROMPT_SUBMIT, context="anything")

        release.set()
        with pytest.raises(OSError):
            await mutation

        assert not sentinel.exists(), "a hook whose write never reached disk executed its command"

    @pytest.mark.asyncio
    async def test_a_committed_hook_still_fires(self, tmp_path, monkeypatch):
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        sentinel = tmp_path / "COMMITTED"
        command = '"' + sys.executable + '" -c "open(r\'' + str(sentinel) + "', chr(119)).close()\""
        store = H.ScriptHookStore(config_dir=tmp_path)
        created = store.create(
            {
                "name": "committed-probe",
                "event": H.HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": command,
                "enabled": True,
            }
        )
        assert created is not None

        async with _reaped_children(cwd=tmp_path):
            with patch.object(H, "_script_hooks_capability_denied", lambda sk="", app="": None):
                with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
                    await store.fire(H.HOOK_EVENT_USER_PROMPT_SUBMIT, context="anything")

        assert sentinel.exists(), "a committed hook stopped firing"


class TestTheStartRowGatesTheRun:
    """The start row is AUDIT-OR-DENY: it gates, and it is written critically.

    ``critical=True`` is what makes it gate at all -- a non-critical write only enqueues, so a
    persist failure is swallowed and the gate would pass on a row that never reaches disk. The
    refusal costs availability under an SEL fault, deliberately: a run nobody can attest to does
    not happen. Outcome rows stay soft, because by then the command has run.
    """

    @pytest.mark.asyncio
    async def test_a_warm_sel_with_an_unwritable_log_refuses_the_run(self, monkeypatch):
        """Warm means the singleton exists, not that its log is writable -- and neither may gate."""
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        spawned: list[str] = []
        seen: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                seen.append(kw)
                if kw.get("outcome") == "started":
                    raise OSError("audit volume is full")

        async def _spawn(*_args, **_kwargs):
            spawned.append("spawn")
            return b"", False, b"", False

        monkeypatch.setattr(H, "sel", lambda: _Sel())
        monkeypatch.setattr(H, "sel_is_warm", lambda: True)
        monkeypatch.setattr(H, "_communicate_capped", _spawn)
        monkeypatch.setattr(H, "_script_hooks_capability_denied", lambda sk="", app="": None)

        hook = H.ScriptHook(
            id="w",
            name="w",
            event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
            matcher="*",
            command="true",
        )
        with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
            result = await H.run_script_hook(hook, context="c", hook_event={})

        assert not spawned, (
            "an unwritable log let a warm run execute unaudited, so the pre-run row is not "
            f"gating (last_error={hook.last_error!r})"
        )
        assert result.exit_code != 2, result
        assert [
            r for r in seen if r.get("outcome") == "started"
        ], "no start row was even attempted, so the record is not being written at all: %r" % (
            seen,
        )

    @pytest.mark.asyncio
    async def test_a_healthy_run_files_one_outcome_row_beside_the_gate_row(self, monkeypatch):
        """Positive control: without it the test above passes on a hook that never runs at all."""
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        spawned: list[str] = []
        seen: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                seen.append(kw)

        async def _spawn(*_args, **_kwargs):
            spawned.append("spawn")
            return b"", False, b"", False

        monkeypatch.setattr(H, "sel", lambda: _Sel())
        monkeypatch.setattr(H, "sel_is_warm", lambda: True)
        monkeypatch.setattr(H, "_communicate_capped", _spawn)
        monkeypatch.setattr(H, "_script_hooks_capability_denied", lambda sk="", app="": None)

        hook = H.ScriptHook(
            id="w2", name="w2", event=H.HOOK_EVENT_SESSION_LANE_CHANGED, matcher="*", command="true"
        )
        with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
            result = await H.run_script_hook(hook, context="c", hook_event={})

        assert spawned, "a writable log still refused the run: %r" % (hook.last_error,)
        assert result.exit_code != 2, result
        assert [r["outcome"] for r in seen] == ["started", "ok"], (
            "a healthy run must file the writability proof and then its outcome, in that "
            "order: %r" % (seen,)
        )

    @pytest.mark.asyncio
    async def test_a_governance_denied_run_files_no_started_row(self, monkeypatch):
        """A denial must leave the decision row alone, with no invocation claimed beside it.

        The writability probe writes a real ``started`` invocation row. Probing before the denial
        branch left every denied attempt claiming a run that never happened, and the capability
        gate defaults OFF, so that was the ordinary path rather than an edge case.
        """
        import kiro_crew.hooks as H

        seen: list[dict] = []
        spawned: list[str] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                seen.append(kw)

        async def _spawn(*_args, **_kwargs):
            spawned.append("spawn")
            return b"", False, b"", False

        async def _denied(sk="", app=""):
            return "capabilities.script_hooks is off"

        monkeypatch.setattr(H, "sel", lambda: _Sel())
        monkeypatch.setattr(H, "sel_is_warm", lambda: True)
        monkeypatch.setattr(H, "_communicate_capped", _spawn)
        monkeypatch.setattr(H, "_script_hooks_capability_denied_async", _denied)

        hook = H.ScriptHook(
            id="g2", name="g2", event=H.HOOK_EVENT_SESSION_LANE_CHANGED, matcher="*", command="true"
        )
        result = await H.run_script_hook(hook, context="c", hook_event={"session_key": "s"})

        assert not spawned, "a governance-denied hook still spawned"
        assert result.exit_code == 2, result
        started = [r for r in seen if r.get("outcome") == "started"]
        assert not started, (
            "the denied run filed a started invocation row, so the audit trail claims a run that "
            f"never happened: {started!r}"
        )

    @pytest.mark.asyncio
    async def test_a_permitted_run_still_files_its_started_row(self, monkeypatch):
        """Positive control: without it the assertion above passes on a probe that never runs."""
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        seen: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                seen.append(kw)

        async def _spawn(*_args, **_kwargs):
            return b"", False, b"", False

        monkeypatch.setattr(H, "sel", lambda: _Sel())
        monkeypatch.setattr(H, "sel_is_warm", lambda: True)
        monkeypatch.setattr(H, "_communicate_capped", _spawn)
        monkeypatch.setattr(H, "_script_hooks_capability_denied", lambda sk="", app="": None)

        hook = H.ScriptHook(
            id="g3", name="g3", event=H.HOOK_EVENT_SESSION_LANE_CHANGED, matcher="*", command="true"
        )
        with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
            await H.run_script_hook(hook, context="c", hook_event={"session_key": "s"})

        assert [
            r for r in seen if r.get("outcome") == "started"
        ], f"a permitted run filed no started row, so the probe is not running at all: {seen!r}"

    @pytest.mark.asyncio
    async def test_the_start_row_is_written_critically(self, monkeypatch):
        """`critical=True` is the audit-or-DENY spelling, and this row needs it.

        Without the flag the write only ENQUEUES: a persist failure is swallowed, the gate always
        sees success, and the refusal branch is dead code. Pinning the flag stops the gate being
        silently disarmed by a one-word change.
        """
        import kiro_crew.hooks as H

        seen: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                seen.append(kw)
                raise OSError("audit volume is full")

        monkeypatch.setattr(H, "sel", lambda: _Sel())
        monkeypatch.setattr(H, "sel_is_warm", lambda: False)

        async def _warm_that_does_not_take():
            return None

        monkeypatch.setattr(H, "warm_sel_singleton", _warm_that_does_not_take)

        hook = H.ScriptHook(
            id="g", name="g", event=H.HOOK_EVENT_SESSION_LANE_CHANGED, matcher="*", command="true"
        )
        result = await H.run_script_hook(hook, context="c", hook_event={})

        assert result.exit_code != 2, result
        rows = [r for r in seen if r.get("outcome") == "started"]
        assert rows, "no start row was attempted at all"
        assert rows[0].get("critical") is True, (
            "the start row is written non-critically, so a persist failure is swallowed and the "
            "refusal branch below can never fire: %r" % (rows[0],)
        )

    @pytest.mark.asyncio
    async def test_an_unwritable_terminal_row_is_recorded_on_the_hook(self, monkeypatch):
        """Post-warm must not swallow: a run whose row never landed says so."""
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        class _Sel:
            def log_tool_invocation(self, **kw):
                # The pre-run probe must LAND, or the run is refused and never reaches the
                # terminal write this test is about.
                if kw.get("tool_kind") == "script_hook" and kw.get("outcome") != "started":
                    raise OSError("audit volume is full")

        monkeypatch.setattr(H, "sel", lambda: _Sel())
        monkeypatch.setattr(H, "sel_is_warm", lambda: True)

        hook = H.ScriptHook(id="t", name="t", event="Stop", matcher="*", command="true")
        with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
            await H.run_script_hook(hook, context="c", hook_event={})

        assert "audit row not recorded" in hook.last_error, (
            "a completed run whose audit row failed to write reports nothing, so it is "
            "indistinguishable from an audited run: %r" % (hook.last_error,)
        )


class TestTheFireTargetIsFrozenAtCommit:
    """A writer mutating mid-fire must not change what the command executes."""

    @pytest.mark.asyncio
    async def test_a_mutation_after_selection_does_not_change_the_command(
        self, tmp_path, monkeypatch
    ):
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        committed_marker = tmp_path / "COMMITTED"
        injected_marker = tmp_path / "INJECTED"

        def writer(path):
            return '"' + sys.executable + '" -c "open(r\'' + str(path) + "', chr(119)).close()\""

        store = H.ScriptHookStore(config_dir=tmp_path)
        created = store.create(
            {
                "name": "frozen-probe",
                "event": H.HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": writer(committed_marker),
                "matcher": "anything",
                "enabled": True,
            }
        )
        assert created is not None

        # Runs AFTER fire has chosen its target and BEFORE the command executes.
        def mutate_then_match(*a, **kw):
            store._hooks[created.id].command = writer(injected_marker)
            return True

        async with _reaped_children(cwd=tmp_path):
            with patch.object(H, "_context_matches", mutate_then_match):
                with patch.object(H, "_script_hooks_capability_denied", lambda sk="", app="": None):
                    with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
                        await store.fire(H.HOOK_EVENT_USER_PROMPT_SUBMIT, context="anything")

        # Precondition: the interleaving must actually have happened.
        assert store._hooks[created.id].command == writer(injected_marker), "never mutated"

        assert not injected_marker.exists(), "a command injected after target selection executed"
        assert committed_marker.exists(), "the committed command did not execute"

    @pytest.mark.asyncio
    async def test_a_runs_bookkeeping_reaches_the_stored_hook(self, tmp_path, monkeypatch):
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        store = H.ScriptHookStore(config_dir=tmp_path)
        created = store.create(
            {
                "name": "bookkeeping-probe",
                "event": H.HOOK_EVENT_USER_PROMPT_SUBMIT,
                "command": '"' + sys.executable + '" -c "pass"',
                "enabled": True,
            }
        )
        assert created is not None
        assert store._hooks[created.id].run_count == 0

        with patch.object(H, "_script_hooks_capability_denied", lambda sk="", app="": None):
            with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
                await store.fire(H.HOOK_EVENT_USER_PROMPT_SUBMIT, context="anything")

        stored = store._hooks[created.id]
        assert stored.run_count == 1, "the run never reached the stored hook"
        assert stored.last_status == "ok", stored.last_status
        # Readers are served the snapshot, so it must carry it too.
        published = [h for h in store.list_all() if h.id == created.id]
        assert published and published[0].run_count == 1, "readers never saw the run"


class TestColdSelNeverBlocksTheLoop:
    """The audit seam must hand a COLD ``sel()`` to a thread, never run it inline.

    A cold ``sel()`` does trust-dir creation, an HMAC key load and a tail read on the
    caller's thread, so an audit site the request path awaits would stall the gateway.
    Warm is the steady state, so both arms are pinned: warm stays inline (no thread hop
    to pay per row) and cold offloads.
    """

    @pytest.mark.asyncio
    async def test_a_cold_audit_write_is_offloaded_to_a_thread(self, monkeypatch):
        import kiro_crew.hooks as H
        import kiro_crew.sel as _sel_mod

        ran_on = []

        def _write():
            ran_on.append(threading.current_thread().name)

        monkeypatch.setattr(H, "sel_is_warm", lambda: False)
        # The off-loop seam lives in sel, so it reads sel's own name, not hooks'.
        monkeypatch.setattr(_sel_mod, "sel_is_warm", lambda: False)
        await H.audit_off_loop(_write, "probe")

        assert ran_on, "the row was never written at all"
        assert ran_on[0] != threading.current_thread().name, (
            "a cold audit write ran on the caller's thread, which is the event loop: "
            f"{ran_on[0]}"
        )

    @pytest.mark.asyncio
    async def test_a_warm_audit_write_still_goes_off_the_loop(self, monkeypatch):
        """Warmth answers whether `sel()` is cheap to OBTAIN, never whether the row is cheap
        to WRITE. The append and flush block either way, so a warm write is threaded too --
        the same reason `_audit_hook_invocation_now` threads its own."""
        import kiro_crew.hooks as H
        import kiro_crew.sel as _sel_mod

        ran_on = []

        def _write():
            ran_on.append(threading.current_thread().name)

        monkeypatch.setattr(H, "sel_is_warm", lambda: True)
        # The off-loop seam lives in sel, so it reads sel's own name, not hooks'.
        monkeypatch.setattr(_sel_mod, "sel_is_warm", lambda: True)
        await H.audit_off_loop(_write, "probe")

        assert ran_on and ran_on != [threading.current_thread().name], (
            "a warm write ran on the event loop, so the flush stalls every other task while "
            f"the helper's name says otherwise: {ran_on}"
        )

    @pytest.mark.asyncio
    async def test_a_failed_audit_never_escapes_into_the_response(self, monkeypatch):
        import kiro_crew.hooks as H
        import kiro_crew.sel as _sel_mod

        def _boom():
            raise OSError("trust dir unwritable")

        monkeypatch.setattr(H, "sel_is_warm", lambda: False)
        # The off-loop seam lives in sel, so it reads sel's own name, not hooks'.
        monkeypatch.setattr(_sel_mod, "sel_is_warm", lambda: False)
        await H.audit_off_loop(_boom, "probe")

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "helper,args",
        [
            ("_audit_governance_hook_decision", ("s1", "run_script_hook:h", "allowed", "ok")),
            ("_audit_hook_invocation_now", ("s1", "h", "ok")),
        ],
    )
    async def test_no_audit_helper_writes_synchronously_on_the_loop(
        self, helper, args, monkeypatch
    ):
        """Drive each helper rather than counting seam mentions.

        A census of ``audit_off_loop(`` occurrences passes whether or not the site
        under test is one of them, so it shares the blind spot of the thing it guards.
        Calling the helper with a cold singleton is what actually distinguishes an
        offloaded write from one the event loop pays for.
        """
        import kiro_crew.hooks as H
        import kiro_crew.sel as _sel_mod

        wrote_on = []
        loop_thread = threading.current_thread().name

        class _Sel:
            def __getattr__(self, _name):
                def _record(**_kw):
                    wrote_on.append(threading.current_thread().name)

                return _record

        monkeypatch.setattr(H, "sel", _Sel)
        monkeypatch.setattr(H, "sel_is_warm", lambda: False)
        # The off-loop seam lives in sel, so it reads sel's own name, not hooks'.
        monkeypatch.setattr(_sel_mod, "sel_is_warm", lambda: False)

        await getattr(H, helper)(*args)

        assert wrote_on, f"{helper} wrote no audit row at all"
        assert wrote_on[0] != loop_thread, (
            f"{helper} performed a COLD sel() write on the event loop thread "
            f"({wrote_on[0]}); a cold sel() does synchronous filesystem I/O, so this "
            "stalls the gateway for every permitted hook"
        )


class TestTheGetSeamCannotLeakAnUncommittedMutation:
    """`get()` hands back the LIVE object while `list_all()` serves committed copies.

    Both real callers -- the DELETE handler and `api_hook_test` -- only READ through it,
    and the run counters `api_hook_test` moves reach readers via the bookkeeping fold. So the
    live object stays, and this pins the invariant a future caller could break instead:
    a mutation written straight onto the object `get()` returns must not become visible
    to readers until it is routed through `update` or the fold.
    """

    def _store(self, tmp_path):
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(config_dir=tmp_path)
        store._hooks = {
            "h1": H.ScriptHook(
                id="h1", name="watcher", event="Stop", matcher="*", command="echo hi"
            )
        }
        store._publish_snapshot()
        return store

    def test_a_mutation_written_through_get_is_invisible_to_readers(self, tmp_path):
        store = self._store(tmp_path)

        live = store.get("h1")
        assert live is not None
        live.command = "echo SMUGGLED"

        served = [h for h in store.list_all() if h.id == "h1"]
        assert served, "the hook vanished from the reader surface"
        assert served[0].command == "echo hi", (
            "a mutation written straight onto get()'s live object reached readers "
            f"without passing through update/the fold: {served[0].command!r}. "
            "list_all must serve committed copies, not the live objects."
        )

    def test_the_same_mutation_through_update_IS_visible(self, tmp_path):
        store = self._store(tmp_path)

        assert store.update("h1", {"command": "echo COMMITTED"}) is not None

        served = [h for h in store.list_all() if h.id == "h1"]
        assert served and served[0].command == "echo COMMITTED", (
            "the sanctioned route did not reach readers, so the test above proves "
            "nothing about routing -- it would pass on a store that serves nobody"
        )


class TestCancellationDuringTheTerminalAudit:
    """A hook that already finished must not be re-counted as cancelled.

    An outcome audit off the event loop puts an await AFTER the
    subprocess reaches a terminal status and after ``run_count`` is incremented. A
    cancellation delivered at that await reaches the ``CancelledError`` arm,
    which increments the counter a second time and overwrites the real outcome with
    ``cancelled`` -- so a completed run is recorded as never having completed, and
    ``run_count`` is monotonic, so it never self-corrects.
    """

    @pytest.mark.asyncio
    async def test_a_completed_run_keeps_its_outcome_and_counts_once(self, monkeypatch, fake_spawn):
        import kiro_crew.hooks as H

        hook = H.ScriptHook(id="h1", name="done", event="Stop", matcher="*", command="true")
        box: list[asyncio.Task] = []
        seen: list[tuple[str, int]] = []

        async def _cancel_at_the_audit(*_args, **_kwargs):
            # Reached only once the subprocess has exited and the terminal status is
            # recorded, so cancelling here lands at the first point after that.
            seen.append((hook.last_status, hook.run_count))
            box[0].cancel()

        monkeypatch.setattr(H, "_audit_hook_invocation_now", _cancel_at_the_audit)

        box.append(asyncio.create_task(H.run_script_hook(hook, context="c", hook_event={})))
        with contextlib.suppress(asyncio.CancelledError):
            await box[0]

        assert seen, "the terminal audit never ran, so this pins nothing"
        terminal, counted_at_audit = seen[0]
        assert (
            counted_at_audit == 1
        ), f"the run was not counted once before its audit (run_count={counted_at_audit})"

        assert hook.run_count == 1, (
            f"a completed run was counted more than once (run_count={hook.run_count}) "
            "on a counter that is monotonic and so never self-corrects"
        )
        assert hook.last_status == terminal, (
            f"a completed run's outcome was overwritten with {hook.last_status!r}, "
            f"losing the real terminal status {terminal!r}"
        )

    def test_the_cancellation_arm_guards_its_bookkeeping(self):
        """The arm's bookkeeping must sit behind the already-recorded check.

        This is asserted structurally because it is unreachable at runtime:
        the terminal audit is synchronous, so no suspension point remains between
        ``run_count += 1`` and the return, and a cancellation requested there is never
        delivered inside the function. The guard is the defence that keeps it that way
        if an await is ever reintroduced, so it is pinned by shape rather than left
        resting on a test that would pass with it deleted.
        """
        import inspect
        import re

        import kiro_crew.hooks as H

        src = inspect.getsource(H.run_script_hook)
        arm = src.split("except asyncio.CancelledError:")[-1]
        guard = arm.find("if not outcome_recorded:")
        bump = arm.find("hook.run_count += 1")

        assert guard != -1, (
            "the cancellation arm no longer checks whether the outcome was already "
            "recorded, so a completed run can be counted twice on a monotonic counter"
        )
        assert bump != -1 and guard < bump, (
            "the cancellation arm increments run_count outside the already-recorded "
            f"guard (guard at {guard}, increment at {bump})"
        )
        assert re.search(r"if not outcome_recorded:\s*\n\s+hook\.last_run", arm), (
            "the guard no longer immediately precedes the bookkeeping it protects, so "
            "a statement could be added between them and escape it"
        )

    @pytest.mark.asyncio
    async def test_a_cancellation_before_the_subprocess_finishes_still_records_it(
        self, monkeypatch, fake_spawn
    ):
        import kiro_crew.hooks as H

        hook = H.ScriptHook(id="h2", name="slow", event="Stop", matcher="*", command="true")
        running = asyncio.Event()

        # Synchronised on the mid-run await, not a wall-clock sleep: a loaded runner can
        # finish the subprocess first, which would test the wrong window entirely.
        async def _never_finishes(*_args, **_kwargs):
            running.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(H, "_communicate_capped", _never_finishes)

        task = asyncio.create_task(H.run_script_hook(hook, context="c", hook_event={}))
        # The spawn is faked, so reaching this point is deterministic rather than
        # dependent on a real child the runner may never start.
        await asyncio.wait_for(running.wait(), timeout=10)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        assert hook.last_status == "cancelled", (
            "a genuine mid-run cancellation must still be recorded; skipping the "
            f"bookkeeping unconditionally would lose it (got {hook.last_status!r})"
        )
        assert hook.run_count == 1

    @pytest.mark.asyncio
    async def test_the_terminal_audit_row_still_lands_when_cancelled(self, monkeypatch, fake_spawn):
        """The row describes a run that ALREADY happened, so cancelling must not void it.

        Distinct from the bookkeeping guard above: that keeps the counters honest, this
        keeps the audit trail from losing a completed invocation entirely. The
        cancellation is requested from inside the write, so it is delivered at the
        earliest suspension point after the row is recorded -- the exact window a
        shielded coroutine lost the row in, because deferring a cancellation does not
        ensure the deferred write is ever driven to completion.
        """
        import kiro_crew.hooks as H

        hook = H.ScriptHook(id="h3", name="audited", event="Stop", matcher="*", command="true")
        rows: list[str] = []
        box: list[asyncio.Task] = []

        async def _record(_sk, _label, outcome, **_kw):
            rows.append(outcome)
            box[0].cancel()

        monkeypatch.setattr(H, "_audit_hook_invocation_now", _record)

        box.append(asyncio.create_task(H.run_script_hook(hook, context="c", hook_event={})))
        with contextlib.suppress(asyncio.CancelledError):
            await box[0]

        assert rows, (
            "the terminal audit row was lost to the cancellation: a completed hook run "
            "left no invocation record, so the audit query cannot see it happened"
        )
        assert hook.run_count == 1, (
            f"the cancellation arm re-ran bookkeeping (run_count={hook.run_count}) for a "
            "run the success path had already counted"
        )

    @pytest.mark.asyncio
    async def test_the_singleton_is_warmed_before_the_command_runs(self, monkeypatch, fake_spawn):
        """The inline terminal write must never be the blocking first-touch init.

        Writing the row inline is what makes it survive a cancellation, so the warm
        has to happen earlier -- off the event loop, before any run exists to lose.
        """
        import kiro_crew.hooks as H

        hook = H.ScriptHook(id="h4", name="warmed", event="Stop", matcher="*", command="true")
        order: list[str] = []
        warmed: list[str] = []

        async def _warm():
            order.append("warm")
            warmed.append("yes")

        # Cold until the warm runs, warm after: the SUCCESSFUL path, so the run proceeds
        # to its terminal audit. A warm that never takes is refused, pinned separately.
        monkeypatch.setattr(H, "sel_is_warm", lambda: bool(warmed))
        monkeypatch.setattr(H, "warm_sel_singleton", _warm)
        monkeypatch.setattr(H, "_audit_hook_invocation_now", _appends_audit_to(order))

        await H.run_script_hook(hook, context="c", hook_event={})

        assert order[:1] == ["warm"], (
            f"the singleton was not warmed before the run (order={order}); the inline "
            "terminal audit would then run the blocking init on the event loop"
        )
        assert "audit" in order, "the terminal audit never ran, so this pins nothing"

    @pytest.mark.asyncio
    async def test_the_critical_row_is_written_off_the_event_loop(self, monkeypatch):
        """A synchronous append and flush on the event loop stalls every other task meanwhile.

        The SEL write is not cancellable and can be slow, so it belongs on a worker thread. This
        pins the thread the write actually runs on, which a mock of the seam could not show.
        """
        import kiro_crew.hooks as H

        loop_thread = threading.get_ident()
        ran_on: list[int] = []

        def _write(*_args, **_kwargs):
            ran_on.append(threading.get_ident())

        monkeypatch.setattr(H, "_hook_invocation_writer", lambda *_a, **_k: _write)

        assert await H._audit_hook_invocation_now("sk", "label", "success")

        assert ran_on, "the writer never ran, so this pins nothing"
        assert ran_on[0] != loop_thread, (
            "the critical audit row was written on the event-loop thread, so a slow SEL "
            "filesystem stalls gateway chat and the heartbeat for the length of the write"
        )


class TestTheMatcherContractIsSharedWithTheFrontend:
    """One fixture decides both spellings of the matcher grammar.

    The picker warns when a matcher cannot fire, which means the frontend has to
    re-decide this grammar in TypeScript. Both suites read the same fixture, so a
    change to either implementation reddens the other side's test rather than
    silently drifting into a warning that contradicts what the backend will do.
    """

    def test_every_contract_case_agrees_with_the_backend(self):
        import kiro_crew.hooks as H

        # Inlined rather than a shared fixture: the only reader is this test. When the
        # frontend re-decides this grammar it can lift these back out into one file.
        cases = [
            {
                "matcher": "*added:9f2c1ab77e40;*",
                "mode": "glob",
                "fires": True,
                "why": "the wrapped id is the shape the picker writes",
            },
            {
                "matcher": "*removed:9f2c1ab77e40;*",
                "mode": "glob",
                "fires": True,
                "why": "leaving a column is the same grammar, other direction",
            },
            {
                "matcher": "Done",
                "mode": "glob",
                "fires": False,
                "why": "a bare column NAME never appears in the context",
            },
            {
                "matcher": "*Done*",
                "mode": "glob",
                "fires": False,
                "why": "wrapping a name does not help; the context carries ids",
            },
            {
                "matcher": "added:",
                "mode": "contains",
                "fires": True,
                "why": "contains is a substring test, so no wildcards are needed",
            },
            {
                "matcher": "nope|added:",
                "mode": "contains",
                "fires": True,
                "why": "contains is a pipe-split OR",
            },
            {
                "matcher": "added:[a-f0-9]+",
                "mode": "regex",
                "fires": True,
                "why": "regex searches rather than matching the whole string",
            },
            {
                "matcher": "(?i)ADDED:",
                "mode": "regex",
                "fires": True,
                "why": "a LEADING global flag directive is honoured, not treated as literal",
            },
            {
                "matcher": "[",
                "mode": "regex",
                "fires": False,
                "why": "an unparseable pattern fails closed on both sides",
            },
            {
                "matcher": "*added:[0-9a-f]*",
                "mode": "glob",
                "fires": True,
                "why": "a character class is fnmatch grammar; the ids are hex so this matches server-side",
            },
        ]
        assert cases, "the contract is empty, so this pins nothing"

        lanes = ["9f2c1ab77e40"]
        contexts = [H._session_lane_matcher_context([lane], []) for lane in lanes]
        contexts += [H._session_lane_matcher_context([], [lane]) for lane in lanes]

        for case in cases:
            fired = any(H._context_matches(case["matcher"], case["mode"], ctx) for ctx in contexts)
            assert fired is case["fires"], (
                "contract case %r (%s) fired=%s on the backend but the shared fixture "
                "says %s -- %s" % (case["matcher"], case["mode"], fired, case["fires"], case["why"])
            )


class TestTheSharedRunPathCarriesEveryEvent:
    """The rewrite is on the path all five events take, so pin it per event.

    The committed snapshot were introduced for the new event
    but are shared, so a regression there would land on the four pre-existing events.
    Only the new one had coverage, which is the blast radius the review names.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event",
        ["PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit", "SessionLaneChanged"],
    )
    async def test_a_run_is_visible_through_the_snapshot_for_every_event(self, tmp_path, event):
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(config_dir=tmp_path)
        hook = store.create({"name": "e", "event": event, "command": "true", "enabled": True})

        await store.run_one_and_fold(hook, "ctx")

        published = [h for h in store.list_all() if h.id == hook.id]
        assert published, f"{event}: the hook vanished from the snapshot readers are served"
        assert published[0].run_count == 1, (
            f"{event}: the run is invisible through list_all (run_count="
            f"{published[0].run_count}); a reader sees no run at all"
        )
        assert published[0].last_status, f"{event}: the terminal status never reached the snapshot"


class TestAColdAuditLogStopsTheRun:
    """A cold or unwritable audit log stops the hook, and says so.

    The refusal deliberately does NOT reuse ``exit_code=2`` -- the PreToolUse BLOCKED marker --
    because an audit fault must not reach a caller wearing a policy decision's clothes. The
    separate loop-safety invariant still holds and is pinned below: nothing audits INLINE while
    cold.
    """

    @pytest.mark.asyncio
    async def test_a_failed_warm_still_runs_without_an_inline_audit(self, monkeypatch):
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        hook = H.ScriptHook(
            id="hc",
            name="cold",
            event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
            matcher="*",
            command="true",
        )
        monkeypatch.setattr(H, "_audit_gate_row", lambda *_a, **_k: False)
        spawned: list[str] = []
        inline: list[str] = []

        async def _warm_that_does_not_take():
            return None

        async def _spawn(*_args, **_kwargs):
            spawned.append("spawn")
            return b"", False, b"", False

        # Cold BEFORE and AFTER the warm: the best-effort warm is allowed to fail.
        monkeypatch.setattr(H, "sel_is_warm", lambda: False)
        monkeypatch.setattr(H, "warm_sel_singleton", _warm_that_does_not_take)
        monkeypatch.setattr(H, "_communicate_capped", _spawn)
        monkeypatch.setattr(H, "_script_hooks_capability_denied", lambda sk="", app="": None)
        monkeypatch.setattr(H, "_audit_hook_invocation_now", _appends_inline_to(inline))

        with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
            result = await H.run_script_hook(hook, context="c", hook_event={})

        # Loop safety is `TestColdSelNeverBlocksTheLoop`, which owns that invariant.
        assert not spawned, (
            "a cold audit log let the command run unaudited, which is what audit-or-DENY on the "
            f"pre-run row exists to stop (last_error={hook.last_error!r})"
        )
        # NOT the PreToolUse block code: an audit fault must not reach the caller wearing a
        # policy decision's clothes.
        assert result.exit_code != 2, result
        assert hook.last_status != "blocked", hook.last_status
        assert "audit record could not be written" in (hook.last_error or ""), hook.last_error

    @pytest.mark.asyncio
    async def test_an_informational_event_is_refused_when_the_audit_log_is_cold(self, monkeypatch):
        """The record still gets attempted; only the refusal is gone.

        Item 4's outcome row is what the audit trail needs, and it is written on every completion
        path. Refusing the run bought nothing extra and cost availability, so this pins that the
        run proceeds AND that the outcome row is still attempted.
        """
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        hook = H.ScriptHook(
            id="hi",
            name="info",
            event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
            matcher="*",
            command="true",
        )
        monkeypatch.setattr(H, "_audit_gate_row", lambda *_a, **_k: False)
        spawned: list[str] = []
        recorded: list[str] = []

        async def _warm_that_does_not_take():
            return None

        async def _spawn(*_args, **_kwargs):
            spawned.append("spawn")
            return b"", False, b"", False

        monkeypatch.setattr(H, "sel_is_warm", lambda: False)
        monkeypatch.setattr(H, "warm_sel_singleton", _warm_that_does_not_take)
        monkeypatch.setattr(H, "_communicate_capped", _spawn)
        monkeypatch.setattr(H, "_script_hooks_capability_denied", lambda sk="", app="": None)
        monkeypatch.setattr(H, "_audit_hook_invocation_now", _appends_inline_to(recorded))

        with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
            result = await H.run_script_hook(hook, context="c", hook_event={})

        assert not spawned, (
            "an unwritten start row let a Stop hook run unaudited, so the pre-run gate is not "
            f"actually gating (last_error={hook.last_error!r})"
        )
        assert result.exit_code != 2, result
        assert "audit record could not be written" in (hook.last_error or ""), hook.last_error

    @pytest.mark.asyncio
    async def test_the_same_event_runs_when_the_audit_log_is_writable(self, monkeypatch):
        """Positive control: without it the refusal above passes on a hook that never runs at all."""
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        hook = H.ScriptHook(id="hi2", name="info", event="Stop", matcher="*", command="true")
        monkeypatch.setattr(H, "_audit_gate_row", lambda *_a, **_k: True)
        spawned: list[str] = []

        async def _warm_that_does_not_take():
            return None

        async def _spawn(*_args, **_kwargs):
            spawned.append("spawn")
            return b"", False, b"", False

        monkeypatch.setattr(H, "sel_is_warm", lambda: False)
        monkeypatch.setattr(H, "warm_sel_singleton", _warm_that_does_not_take)
        monkeypatch.setattr(H, "_communicate_capped", _spawn)
        monkeypatch.setattr(H, "_script_hooks_capability_denied", lambda sk="", app="": None)
        monkeypatch.setattr(H, "_audit_hook_invocation_now", _appends_inline_to([]))

        with patch.object(sandbox_module, "_allow_unsandboxed_exec", lambda: True):
            result = await H.run_script_hook(hook, context="c", hook_event={})

        assert spawned, (
            "a writable audit log still refused an informational run, so the refusal above is not "
            f"discriminating the audit gate (last_error={hook.last_error!r})"
        )
        assert result.exit_code != 2, result


class TestBookkeepingSurvivesAnyRaiseNotJustCancellation:
    """A later hook's raise must not discard an earlier hook's recorded run.

    ``run_count`` is monotonic, so a dropped increment never self-corrects: the
    counter is permanently short and the last_status of a run that really happened
    is lost. Cancellation was already covered; any other exception was not.
    """

    @pytest.mark.asyncio
    async def test_a_later_raise_keeps_the_first_hook_s_persisted_run(self, tmp_path):
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(config_dir=tmp_path)
        first = store.create({"name": "a", "event": "Stop", "command": "true", "enabled": True})
        store.create({"name": "b", "event": "Stop", "command": "true", "enabled": True})

        seen: list[str] = []

        async def _second_one_raises(hook, *args, **kwargs):
            seen.append(hook.id)
            if len(seen) == 1:
                hook.run_count += 1
                hook.last_run = 1.0
                hook.last_status = "ok"
                return H.ScriptHookResult(hook_id=hook.id, hook_name=hook.name, event=hook.event)
            raise ValueError("a malformed later matcher")

        with patch.object(H, "run_script_hook", _second_one_raises):
            with pytest.raises(ValueError):
                await store.fire("Stop", context="c")

        assert len(seen) == 2, f"the second hook never ran, so this pins nothing (seen={seen})"

        # Read from DISK: the exception arm's job is the persist, and the in-memory
        # snapshot is already updated by the fold either way.
        reloaded = H.ScriptHookStore(config_dir=tmp_path)
        got = [h for h in reloaded.list_all() if h.id == first.id]
        assert got, "the first hook vanished from the persisted set"
        assert got[0].run_count == 1, (
            "the first hook's completed run was discarded by the later raise "
            f"(persisted run_count={got[0].run_count})"
        )


class TestARefusedRunIsNotAPolicyBlock:
    """An unwritable audit log must not read as a BLOCK at the tool gate.

    ``_fire`` emits its ``BLOCKED:`` marker only for exit code 2, so returning 2 because SEL could
    not be written would deny every tool call a broad PreToolUse matcher covers. Availability does
    not ride on the audit filesystem; the record is the outcome row every completion path writes.
    """

    @pytest.mark.asyncio
    async def test_an_unwritable_log_does_not_emit_the_blocking_exit_code(self, monkeypatch):
        import kiro_crew.hooks as H

        hook = H.ScriptHook(id="hb", name="cold", event="PreToolUse", matcher="*", command="true")
        monkeypatch.setattr(H, "_audit_gate_row", lambda *_a, **_k: False)

        async def _warm_that_does_not_take():
            return None

        monkeypatch.setattr(H, "sel_is_warm", lambda: False)
        monkeypatch.setattr(H, "warm_sel_singleton", _warm_that_does_not_take)

        result = await H.run_script_hook(hook, context="c", hook_event={})

        assert result.exit_code != 2, (
            "an unwritable audit log returned exit 2, which is the BLOCKED marker the PreToolUse "
            "gate keys on -- so every tool call a broad matcher covers would be denied"
        )
        assert not result.blocked, "the result reports itself as blocking"
        assert hook.last_status != "blocked", hook.last_status


class TestHandEditedBookkeepingCannotCrashDispatch:
    """A hand-edited hooks.json must not make a FINISHED run raise.

    last_run and run_count are only compared once a run completes, inside
    _merge_run_bookkeeping. A non-numeric persisted value therefore raises TypeError
    after the hook already executed -- and on PreToolUse the caller reads that as the
    tool being rejected, so junk in a config file becomes a blocked tool call.
    """

    def test_non_numeric_persisted_values_are_coerced_at_load(self):
        import kiro_crew.hooks as H

        hook = H.ScriptHook.from_dict(
            {
                "id": "hx",
                "name": "hx",
                "event": "Stop",
                "command": "true",
                "last_run": "yesterday",
                "run_count": None,
            }
        )
        assert isinstance(hook.last_run, float), f"last_run is {type(hook.last_run).__name__}"
        assert isinstance(hook.run_count, int), f"run_count is {type(hook.run_count).__name__}"
        assert hook.last_run == 0.0
        assert hook.run_count == 0

    @pytest.mark.asyncio
    async def test_a_completed_run_merges_without_raising(self, tmp_path):
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(tmp_path)
        created = store.create({"name": "hx", "event": "Stop", "matcher": "*", "command": "true"})

        # Reload through from_dict with the junk value, exactly as a hand edit would.
        raw = created.to_dict()
        raw["last_run"] = "yesterday"
        reloaded = H.ScriptHook.from_dict(raw)
        store._hooks[reloaded.id] = reloaded
        store._publish_snapshot()

        await store.fire("Stop", context="c")

        got = [h for h in store.list_all() if h.id == reloaded.id]
        assert got and got[0].run_count >= 1, "the fire did not record a run"


class TestEveryPreExistingEventFoldsThroughTheSnapshot:
    """fire() must publish run bookkeeping for the pre-existing events too.

    The store serves readers a committed snapshot and folds a completed run back via
    _merge_run_bookkeeping. That path is shared by all five events, so this change to it
    is only safe if each event is shown to traverse it, not just the new one.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event",
        ["PreToolUse", "PostToolUse", "Stop", "UserPromptSubmit", "AgentSpawn"],
    )
    async def test_a_fired_event_reaches_the_published_snapshot(self, event, tmp_path):
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(tmp_path)
        hook = store.create(
            {"name": f"h-{event}", "event": event, "matcher": "*", "command": "true"}
        )
        before = [h for h in store.list_all() if h.id == hook.id][0]
        assert before.run_count == 0, "precondition: the snapshot starts at zero"

        await store.fire(event, context="c", tool_name="Bash")

        after = [h for h in store.list_all() if h.id == hook.id]
        assert after, f"{event}: the hook vanished from the published snapshot"
        assert after[0].run_count == 1, (
            f"{event}: the snapshot reports run_count={after[0].run_count}, so the "
            "bookkeeping fold did not reach readers"
        )
        assert after[0].last_status, f"{event}: no last_status in the snapshot"


class TestPreExistingEventBookkeepingIsUnchanged:
    """The shared merge must not disturb a pre-existing event's counters.

    ``_merge_run_bookkeeping`` is now reached by all five events, including the
    security-critical ``PreToolUse`` gate, so an error in the fold would degrade every
    event silently rather than failing loudly. Two invariants carry that risk: the count
    is applied as a DIFFERENCE, so a concurrent increment is never rolled back, and a
    status is taken only from a run at least as new as the stored one, so a slow older
    run cannot overwrite a newer outcome.
    """

    def test_a_concurrent_increment_is_not_rolled_back(self, tmp_path):
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(tmp_path)
        hook = store.create(
            {
                "name": "pre-tool",
                "event": H.HOOK_EVENT_PRE_TOOL_USE,
                "matcher": "*",
                "command": "irrelevant",
            }
        )
        live = store.get(hook.id)
        live.run_count = 5

        ran = H.ScriptHook(
            id=hook.id,
            name="pre-tool",
            event=H.HOOK_EVENT_PRE_TOOL_USE,
            matcher="*",
            command="irrelevant",
        )
        ran.run_count = 1
        ran.last_run = 0.0
        store._merge_run_bookkeeping([(ran, 0, 0.0)])

        assert store.get(hook.id).run_count == 6, (
            "the merge assigned the run's own count instead of applying the difference, "
            f"rolling a concurrent run's increment back (run_count="
            f"{store.get(hook.id).run_count}, expected 5+1)"
        )

    def test_an_older_run_cannot_overwrite_a_newer_outcome(self, tmp_path):
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(tmp_path)
        hook = store.create(
            {
                "name": "pre-tool",
                "event": H.HOOK_EVENT_PRE_TOOL_USE,
                "matcher": "*",
                "command": "irrelevant",
            }
        )
        live = store.get(hook.id)
        live.last_run = 100.0
        live.last_status = "ok"

        ran = H.ScriptHook(
            id=hook.id,
            name="pre-tool",
            event=H.HOOK_EVENT_PRE_TOOL_USE,
            matcher="*",
            command="irrelevant",
        )
        ran.last_run = 60.0
        ran.last_status = "error"
        store._merge_run_bookkeeping([(ran, 0, 50.0)])

        merged = store.get(hook.id)
        assert merged.last_status == "ok" and merged.last_run == 100.0, (
            "an older run that finished second overwrote a newer outcome "
            f"(status={merged.last_status!r}, last_run={merged.last_run})"
        )


class TestARecoveredAuditLogStopsSuppressingTheHook:
    """A cold log that turns out writable must not keep the hook refused.

    Whether the probe row can be written IS the availability test, so a row that lands says the
    outage ended. Refusing anyway leaves the hook suppressed for the rest of the process even
    though every later invocation could be recorded.
    """

    @pytest.mark.asyncio
    async def test_a_landed_probe_lets_the_run_proceed(self, monkeypatch):
        import kiro_crew.hooks as H
        import kiro_crew.sandbox as sandbox_module

        hook = H.ScriptHook(id="rc", name="rc", event="Stop", matcher="*", command="true")
        spawned: list[str] = []

        async def _warm_that_does_not_take():
            return None

        async def _spawn(*_args, **_kwargs):
            spawned.append("spawn")
            return b"", False, b"", False

        monkeypatch.setattr(H, "sel_is_warm", lambda: False)
        monkeypatch.setattr(H, "warm_sel_singleton", _warm_that_does_not_take)
        monkeypatch.setattr(H, "_communicate_capped", _spawn)
        monkeypatch.setattr(H, "_script_hooks_capability_denied", lambda sk="", app="": None)
        monkeypatch.setattr(H, "_audit_gate_row", lambda *_a, **_k: True)
        # Windows has no supported sandbox backend, so without this the spawn gate refuses before
        # `_communicate_capped` and the assertion below reads as an audit refusal it is not.
        monkeypatch.setattr(sandbox_module, "_allow_unsandboxed_exec", lambda: True)

        result = await H.run_script_hook(hook, context="c", hook_event={})

        assert spawned, (
            "the audit log accepted the probe row, so it had recovered, yet the hook was still "
            f"refused (last_error={hook.last_error!r})"
        )
        assert hook.last_status != "blocked", hook.last_status
        assert result.exit_code != 2, result

    @pytest.mark.asyncio
    async def test_the_probe_row_does_not_claim_a_refusal_that_did_not_happen(self):
        """The row is the availability probe, so wording it as a denial would file a false one."""
        import inspect

        import kiro_crew.hooks as H

        body = inspect.getsource(H._audit_gate_row)
        assert "was not run" not in body, (
            "the probe row still says the hook was not run, so a landed row files a refusal "
            "immediately before the run it permits"
        )


class TestATimedOutRunSaysWhenItsAuditRowIsMissing:
    """A failed timeout audit must not look identical to a successful one.

    The marker is the ONLY signal the row is absent: the run itself already ended, so discarding
    the writer's outcome leaves an operator reading a timeout that appears fully recorded.
    """

    @staticmethod
    def _hook():
        import kiro_crew.hooks as H

        return H.ScriptHook(
            id="tm", name="tm", event="Stop", matcher="*", command="irrelevant", timeout=0.05
        )

    @pytest.mark.asyncio
    async def test_the_marker_is_appended_when_the_row_does_not_land(self, monkeypatch, fake_spawn):
        import kiro_crew.hooks as H

        fake_spawn["hang"] = True

        async def _audit_fails(*_args, **_kwargs):
            return False

        monkeypatch.setattr(H, "_audit_hook_invocation_now", _audit_fails)
        monkeypatch.setattr(H, "_script_hooks_capability_denied", lambda sk="", app="": None)

        hook = self._hook()
        result = await H.run_script_hook(hook, context="c", hook_event={})

        assert hook.last_status == "timeout", hook.last_status
        assert "(audit row not recorded)" in (hook.last_error or ""), (
            "the timeout audit failed and said nothing, so a missing row reads as a recorded one "
            f"(last_error={hook.last_error!r})"
        )
        assert "(audit row not recorded)" in (result.error or ""), result

    @pytest.mark.asyncio
    async def test_a_landed_row_adds_no_marker(self, monkeypatch, fake_spawn):
        """The positive control: without it the test above passes on an always-appended marker."""
        import kiro_crew.hooks as H

        fake_spawn["hang"] = True

        async def _audit_lands(*_args, **_kwargs):
            return True

        monkeypatch.setattr(H, "_audit_hook_invocation_now", _audit_lands)
        monkeypatch.setattr(H, "_script_hooks_capability_denied", lambda sk="", app="": None)

        hook = self._hook()
        await H.run_script_hook(hook, context="c", hook_event={})

        assert hook.last_status == "timeout", hook.last_status
        assert "(audit row not recorded)" not in (
            hook.last_error or ""
        ), f"a recorded timeout claims its row is missing (last_error={hook.last_error!r})"


class TestCancellationInsideTheTimeoutCleanup:
    """A cancellation during timeout cleanup must still reap and audit.

    CancelledError is a BaseException, and Python propagates one raised inside an
    except clause OUTWARD past that try's sibling clauses -- so the reap-and-audit
    CancelledError arm is unreachable from the timeout path. Both named consequences
    are pinned separately: the process must not survive, and the invocation must not
    go unaudited.
    """

    @staticmethod
    def _hook():
        import kiro_crew.hooks as H

        return H.ScriptHook(
            id="to", name="to", event="Stop", matcher="*", command="irrelevant", timeout=0.05
        )

    @pytest.fixture
    def cancelled_cleanup(self, monkeypatch, fake_spawn):
        """Cancel the timeout arm's first reap; record what the guard then does.

        The FIRST async kill raises, which is what drives control into the guard. Later
        calls record and succeed, so the guard's own reap is separately observable.
        """
        import kiro_crew.hooks as H

        # Hang the drain so the timeout arm is entered without a sleeping child.
        fake_spawn["hang"] = True
        seen = {"sync_kills": [], "async_kills": [], "rows": []}

        async def _async_kill(pid, _sig):
            if not seen["async_kills"]:
                seen["async_kills"].append(("cancelled", pid))
                raise asyncio.CancelledError()
            seen["async_kills"].append(("reaped", pid))
            return True

        monkeypatch.setattr(H.platform_compat, "kill_process_tree_async", _async_kill)
        monkeypatch.setattr(
            H.platform_compat,
            "kill_process_tree",
            lambda pid, sig: seen["sync_kills"].append(pid) or True,
        )
        monkeypatch.setattr(
            H,
            "_audit_hook_invocation_now",
            _appends_outcome_to(seen["rows"]),
        )
        monkeypatch.setattr(H, "sel_is_warm", lambda: True)
        return seen

    @pytest.mark.asyncio
    async def test_the_process_tree_is_still_reaped(self, cancelled_cleanup):
        import kiro_crew.hooks as H

        with pytest.raises(asyncio.CancelledError):
            await H.run_script_hook(self._hook(), context="c", hook_event={})

        reaped = [k for k in cancelled_cleanup["async_kills"] if k[0] == "reaped"]
        assert reaped, (
            "the guard ran no reap of its own, so a cancellation during timeout cleanup "
            f"can leave the spawned tree alive (calls={cancelled_cleanup['async_kills']!r})"
        )

    @pytest.mark.asyncio
    async def test_the_invocation_is_still_audited(self, cancelled_cleanup):
        import kiro_crew.hooks as H

        with pytest.raises(asyncio.CancelledError):
            await H.run_script_hook(self._hook(), context="c", hook_event={})

        assert "timeout" in cancelled_cleanup["rows"], (
            "the timed-out invocation filed no audit row "
            f"(rows={cancelled_cleanup['rows']!r}), so the run is unaccounted for"
        )

    @pytest.mark.asyncio
    async def test_the_reap_never_uses_the_blocking_kill(self, cancelled_cleanup):
        """The guard runs on the event loop, so it must not spawn a blocking taskkill.

        kill_process_tree shells out to ``taskkill /T /F`` with a 5s timeout on Windows,
        which freezes the gateway loop for as long as it runs.
        """
        import kiro_crew.hooks as H

        with pytest.raises(asyncio.CancelledError):
            await H.run_script_hook(self._hook(), context="c", hook_event={})

        assert not cancelled_cleanup["sync_kills"], (
            "the cancellation guard called the SYNCHRONOUS kill_process_tree "
            f"(pids={cancelled_cleanup['sync_kills']!r}), which blocks the event loop"
        )
        assert cancelled_cleanup["async_kills"], "no reap was attempted at all"


class TestNumericStringsDoNotEraseBookkeeping:
    """A numeric string in `hooks.json` must be parsed, not degraded to the default.

    `last_run` and `run_count` are the only record that a hook ever ran. Reading a
    hand-edited `"5"` as the default silently zeroes both, and the next save persists
    those zeros -- so the history is destroyed rather than merely misread, and
    `run_count` is monotonic so it never self-corrects.
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("5", 5.0),
            ("  5  ", 5.0),
            ("1.5", 1.5),
            ("-2", -2.0),
            ("1e3", 1000.0),
            (7, 7.0),
            (2.5, 2.5),
        ],
    )
    def test_a_finite_number_survives(self, raw, expected):
        import kiro_crew.hooks as H

        got = H._normalize_hook_number(raw, 0.0)

        assert got == expected, (
            f"{raw!r} normalised to {got!r} rather than {expected!r}; a real value read "
            "as the default erases the hook's run history on the next save"
        )

    @pytest.mark.parametrize(
        "raw",
        ["nan", "inf", "-inf", "NaN", "Infinity", "", "   ", "abc", "5abc", None, True, False, []],
    )
    def test_a_value_that_is_not_a_finite_number_falls_back(self, raw):
        import kiro_crew.hooks as H

        assert H._normalize_hook_number(raw, 99.0) == 99.0, (
            f"{raw!r} was accepted as a number; a non-finite or unparseable value must "
            "degrade to the default rather than poison the ordering comparisons"
        )

    def test_a_hand_edited_record_keeps_its_history(self, tmp_path):
        """The end-to-end consequence: the record survives a load and re-save."""
        import kiro_crew.hooks as H

        hook = H.ScriptHook.from_dict(
            {
                "id": "h",
                "name": "h",
                "event": "Stop",
                "matcher": "*",
                "command": "irrelevant",
                "last_run": "1750000000",
                "run_count": "5",
            }
        )

        assert (hook.last_run, hook.run_count) == (1750000000.0, 5), (
            f"a hand-edited record loaded as last_run={hook.last_run!r} "
            f"run_count={hook.run_count!r}, losing the run history"
        )
        assert (
            H.ScriptHook.from_dict(hook.to_dict()).run_count == 5
        ), "the zeroed value would be persisted by the next save, making the loss permanent"


class TestARolledBackUpdateDoesNotSurviveInAHeldReference:
    """A caller holding the stored hook must not keep an edit that failed to persist.

    Rolling back by REBINDING ``self._hooks`` is not enough: a reference obtained earlier still
    points at the mutated object. The test endpoint holds exactly such a reference across its
    ``await``, so a concurrent update to a destructive command whose save failed could then be
    executed -- irreversibly. The rollback therefore restores each stored hook in place.
    """

    def test_a_held_hook_is_restored_when_the_save_fails(self, tmp_path):
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(tmp_path)
        created = store.create(
            {
                "name": "probe",
                "event": H.HOOK_EVENT_SESSION_LANE_CHANGED,
                "command": "echo hello",
                "enabled": True,
            }
        )
        held = store.get(created.id)
        assert held is not None and held.command == "echo hello"

        with patch.object(H.ScriptHookStore, "_save", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                store.update(created.id, {"command": "rm -rf /tmp/everything"})

        assert store.get(created.id).command == "echo hello", "the store itself kept the edit"
        assert held.command == "echo hello", (
            "the HELD reference kept an uncommitted destructive command, so a caller between "
            "get() and its await can still execute it"
        )

    def test_the_store_still_commits_a_save_that_succeeds(self, tmp_path):
        """Positive control: else the assertion above passes on an update that never applies."""
        import kiro_crew.hooks as H

        store = H.ScriptHookStore(tmp_path)
        created = store.create(
            {
                "name": "probe",
                "event": H.HOOK_EVENT_SESSION_LANE_CHANGED,
                "command": "echo hello",
                "enabled": True,
            }
        )
        held = store.get(created.id)
        store.update(created.id, {"command": "echo goodbye"})

        assert store.get(created.id).command == "echo goodbye"
        assert held.command == "echo goodbye", "a committed edit must reach the held reference"


class TestAnAttendedEventIsNotDeniedByAnAuditOutage:
    """The five turn-lifecycle events neither file a start row nor are gated on one.

    Refusing there traded a guard hook's availability for symmetry with the unattended event,
    not for any reported defect -- and a refused ``PreToolUse`` returns ``-1``, which reads as
    ALLOW, so the guard would stop guarding at exactly the moment auditing was already broken.
    SEL's criterion is attendedness, and these fire inside an attended turn, so the row has no
    reader here and is not written. ``test_the_unattended_event_still_refuses`` below drives the
    same failing SEL double and does refuse, which is what proves the double fires at all.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "event",
        [
            "AgentSpawn",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "Stop",
        ],
    )
    async def test_an_unwritable_start_row_does_not_stop_an_attended_run(self, event, monkeypatch):
        import kiro_crew.hooks as H

        attempted: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                attempted.append(kw)
                raise OSError("audit volume is full")

        monkeypatch.setattr(H, "sel", lambda: _Sel())
        monkeypatch.setattr(H, "sel_is_warm", lambda: True)
        monkeypatch.setattr(H, "_script_hooks_capability_denied_async", lambda sk, app="": _none())

        hook = H.ScriptHook(
            id="a", name="a", event=event, matcher="*", command="true", enabled=True, timeout=5
        )
        result = await H.run_script_hook(hook, context="c", hook_event={"session_key": "s"})

        assert not [
            r for r in attempted if r.get("outcome") == "started"
        ], "an attended %s run filed a start row: only the gating event needs one" % (event,)
        assert "Refused" not in (
            result.error or ""
        ), "an attended %s run was denied by an audit outage: %r" % (event, result.error)
        assert not (hook.last_error or "").startswith(
            "Refused"
        ), "an attended %s run recorded a refusal on the hook: %r" % (event, hook.last_error)
        # exit_code -1 is NOT a refusal signal on its own: the sandbox-unavailable path returns
        # it too, so the discriminator is the refusal TEXT, never the bare code.
        assert not (
            result.exit_code == -1 and "Refused" in (result.error or "")
        ), "an attended %s run returned the refusal code AND reason: %r" % (event, result)

    @pytest.mark.asyncio
    async def test_the_unattended_event_still_refuses(self, monkeypatch):
        """The other side of the same switch, so neither half can drift alone."""
        import kiro_crew.hooks as H

        class _Sel:
            def log_tool_invocation(self, **kw):
                raise OSError("audit volume is full")

        monkeypatch.setattr(H, "sel", lambda: _Sel())
        monkeypatch.setattr(H, "sel_is_warm", lambda: True)
        monkeypatch.setattr(H, "_script_hooks_capability_denied_async", lambda sk, app="": _none())

        hook = H.ScriptHook(
            id="u",
            name="u",
            event=H.HOOK_EVENT_SESSION_LANE_CHANGED,
            matcher="*",
            command="true",
            enabled=True,
            timeout=5,
        )
        result = await H.run_script_hook(hook, context="c", hook_event={"session_key": "s"})

        assert "Refused" in (result.error or ""), result
        assert result.exit_code == -1, result


class TestADeletedHookDoesNotStillRun:
    """A delete landing between a caller's guard and the run must cancel the run.

    The Test endpoint's existence check and its ``run_one_and_fold`` call are separated by
    awaits -- reading the request body is network I/O -- so a concurrent delete republishes
    the snapshot while the handler still holds its own reference to the hook. Running that
    reference executes a hook the operator has already removed.
    """

    @staticmethod
    def _stored(H, tmp_path):
        store = H.ScriptHookStore(config_dir=tmp_path)
        hook = H.ScriptHook(
            id="gone", name="gone", event=H.HOOK_EVENT_SESSION_LANE_CHANGED, enabled=True
        )
        store._hooks["gone"] = hook
        store._publish_snapshot()
        return store, hook

    @pytest.mark.asyncio
    async def test_a_hook_deleted_after_the_guard_is_not_executed(self, tmp_path):
        import kiro_crew.hooks as H

        store, hook = self._stored(H, tmp_path)
        ran: list[str] = []

        async def fake_run(h, ctx=None, ev=None):
            ran.append(h.id)
            return H.ScriptHookResult(hook_id=h.id, hook_name=h.name, event=h.event)

        # The delete the operator issued while the handler was still reading the body.
        del store._hooks["gone"]
        store._publish_snapshot()
        assert store.get("gone") is None, "precondition: the hook is deleted"

        with patch.object(H, "run_script_hook", fake_run):
            result = await store.run_one_and_fold(hook, "ctx")

        assert ran == [], "a deleted hook was executed from the caller's stale reference: %r" % ran
        assert result.error == "hook no longer exists", result.error
        assert not result.succeeded, "a refused run must not report success"
        assert not result.blocked, "a refusal is not a PreToolUse block"

    @pytest.mark.asyncio
    async def test_a_live_hook_still_runs(self, tmp_path):
        """The control: the guard must not refuse a hook that is still committed."""
        import kiro_crew.hooks as H

        store, hook = self._stored(H, tmp_path)
        ran: list[str] = []

        async def fake_run(h, ctx=None, ev=None):
            ran.append(h.id)
            h.last_status = "ok"
            h.last_run = 1.0
            h.run_count += 1
            return H.ScriptHookResult(hook_id=h.id, hook_name=h.name, event=h.event)

        with patch.object(H, "run_script_hook", fake_run):
            result = await store.run_one_and_fold(hook, "ctx")

        assert ran == ["gone"], "the guard refused a hook that is still committed"
        assert result.error == "", result.error


class TestOneDefinitionOfStatusTag:
    """The surface and the hook must agree on what a status tag is.

    The tag manager's lightning toggle reads the field truthily (`!!t.status`), so a stricter
    compare in the backend would paint a hand-edited spelling as a status tag on screen while
    the drop path treated it as an ordinary label. Every base reader shares one predicate, and
    that predicate matches the surface rather than second-guessing it.
    """

    def test_a_truthy_non_bool_status_is_a_lane_tag(self):
        from kiro_crew.dashboard.chat_tags import _is_status_tag

        # These are status tags to the manager's toggle, so they are lanes here too.
        assert _is_status_tag({"status": 1}) is True
        assert _is_status_tag({"status": "yes"}) is True
        assert _is_status_tag({"status": True}) is True

    def test_a_falsy_status_is_not_a_lane_tag(self):
        from kiro_crew.dashboard.chat_tags import _is_status_tag

        assert _is_status_tag({"status": False}) is False
        assert _is_status_tag({"status": 0}) is False
        assert _is_status_tag({"status": ""}) is False
        assert _is_status_tag({}) is False

    def test_a_non_dict_entry_cannot_crash_the_read(self):
        """`load_tags` keeps any entry with a truthy id, so a hand-edited list can arrive here."""
        from kiro_crew.dashboard.chat_tags import _is_status_tag

        assert _is_status_tag(None) is False
        assert _is_status_tag(["x"]) is False

    @pytest.mark.asyncio
    async def test_a_hand_edited_integer_status_fires_the_lane_event(self, tmp_path):
        """End to end: the shape the surface paints as a status tag must reach the hook."""
        state = _make_state(tmp_path)
        state._tags = [
            {"id": "done", "name": "Done", "color": "#6b7280", "order": 0, "status": True},
            {"id": "junk", "name": "Junk", "color": "#6b7280", "order": 1, "status": 1},
        ]
        state._tags_authoritative = True
        seen: list[dict] = []

        async def _record(store, **kw):
            seen.append(kw)

        state.get_or_create_slot("s1")
        async with TestClient(TestServer(_make_tags_app(state))) as client:
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": ["done"]})
                await _settle()
                # Positive control first: without it, an assertion that the junk tag fires
                # nothing would pass on a dispatch that fires for nothing at all.
                assert len(seen) == 1, "a real status=True tag fired nothing: %r" % seen
                assert seen[0]["added"] == ["done"]

                resp = await client.put("/api/chat/slots/s1/tags", json={"tags": ["done", "junk"]})
                await _settle()

        assert resp.status == 200, await resp.text()
        assert len(seen) == 2, (
            "a hand-edited integer status fired nothing, so a tag the surface paints as a "
            "column moved a lane silently: %r" % seen
        )


class TestEveryPermittedRunFilesTheAllowedRow:
    """A permitted run files a permission-decision row, whichever event fired it.

    Attended events are the common path, so omitting the row there leaves the audit trail unable
    to distinguish a command that was permitted from one that never reached the gate. Denials are
    audited on every event; the grant must be too.
    """

    @staticmethod
    def _decisions(monkeypatch, H):
        seen: list[tuple] = []

        async def _record(sk, resource, decision, reason):
            seen.append((decision, resource))

        monkeypatch.setattr(H, "_audit_governance_hook_decision", _record, raising=False)
        return seen

    @pytest.mark.asyncio
    async def test_the_unattended_event_files_the_allowed_row(self, monkeypatch, tmp_path):
        import kiro_crew.hooks as H

        seen = self._decisions(monkeypatch, H)
        hook = H.ScriptHook(
            id="u", name="u", event=H.HOOK_EVENT_SESSION_LANE_CHANGED, enabled=True, command="true"
        )
        await H.run_script_hook(hook, "ctx", {"hook_event_name": hook.event})
        assert [d for d, _ in seen if d == "allowed"], (
            "the unattended event filed no allowed row, so the incident query cannot tell a "
            "permitted run from one that never reached the gate: %r" % seen
        )

    @pytest.mark.asyncio
    async def test_an_attended_event_also_files_the_allowed_row(self, monkeypatch, tmp_path):
        """The common path: a grant that runs a command must leave a permission-decision row."""
        import kiro_crew.hooks as H

        seen = self._decisions(monkeypatch, H)
        hook = H.ScriptHook(id="a", name="a", event=H.HOOK_EVENT_STOP, enabled=True, command="true")
        await H.run_script_hook(hook, "ctx", {"hook_event_name": hook.event})
        assert [d for d, _ in seen if d == "allowed"], (
            "an attended hook executed with no permission-decision row, so the audit trail "
            "cannot say what was allowed to run: %r" % seen
        )


class TestFiresFromFolderInheritance:
    """Closing-order step 1: a folder's status tag enters a lane with no tag-API call.

    The RFC of record names this site as one that must emit, and it needs no new authorization
    rule because the create handler already runs under a dashboard request.
    """

    @staticmethod
    def _folder_state(tmp_path, lane_id):
        state = _make_state(tmp_path)
        state._folders.append(
            {"id": "f-design", "name": "Design Review", "order": 0, "tags": [lane_id]}
        )
        state._tags = [
            {
                "id": lane_id,
                "name": lane_id.title(),
                "color": "#6b7280",
                "order": 0,
                "status": True,
            }
        ]
        state._tags_authoritative = True
        return state

    @staticmethod
    def _app(state):
        from aiohttp import web

        from kiro_crew.dashboard.chat import api_chat_slot_create

        @web.middleware
        async def _claims(request, handler):
            if "app" not in request:
                request["app"] = ""
            if "user" not in request:
                request["user"] = str(getattr(state, "owner_id", "") or "local-app")
            return await handler(request)

        app = web.Application(middlewares=[_claims])
        app["state"] = state
        app.router.add_post("/api/chat/slots", api_chat_slot_create)
        return app

    async def _create(self, state, seen):
        async def _record(store, **kw):
            seen.append(kw)

        async with TestClient(TestServer(self._app(state))) as client:
            with (
                patch("kiro_crew.dashboard.chat_tags.save_slot_off_loop"),
                patch("kiro_crew.dashboard.chat_tags.get_global_hook_store", lambda: _StoreStub()),
                patch("kiro_crew.hooks._fire_session_lane_changed", _record),
            ):
                resp = await client.post(
                    "/api/chat/slots", json={"name": "fresh", "folder_id": "f-design"}
                )
                await _settle()
        return resp

    @pytest.mark.asyncio
    async def test_a_folder_filed_slot_fires_the_lane_event(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._folder_state(tmp_path, "done")
        seen: list[dict] = []
        resp = await self._create(state, seen)

        assert resp.status == 200, await resp.text()
        assert state._slots["fresh"].tags == ["done"], "precondition: the tag must be inherited"
        assert len(seen) == 1, (
            "an inherited folder status tag entered a lane and fired nothing: %r" % seen
        )
        assert seen[0]["added"] == ["done"]
        assert seen[0]["removed"] == []
        assert seen[0]["slot_key"] == "fresh"

    @pytest.mark.asyncio
    async def test_a_folder_with_no_status_tag_stays_silent(self, tmp_path, monkeypatch):
        """The control: a plain label carries no lane meaning and must fire nothing."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = self._folder_state(tmp_path, "plain")
        state._tags[0]["status"] = False
        seen: list[dict] = []
        resp = await self._create(state, seen)

        assert resp.status == 200, await resp.text()
        assert state._slots["fresh"].tags == ["plain"], "precondition: the tag must be inherited"
        assert seen == [], "a non-status tag fired the lane event: %r" % seen


class TestLoadTagsLeavesBoardStatusSemanticsAlone:
    """This event observes the board's lanes; it must not redefine what a lane IS.

    `tags.json` is hand-editable and `bool("false")` is True, so a switched-off column
    persisted as that STRING reads as a live column to the board today. Repairing it at load
    would change which tags are columns for every board reader, which is a board change and
    not this event's scope. `_is_status_tag` therefore reads the field exactly as the board's
    own readers do, and this pins the loader leaving the persisted value untouched.
    """

    @staticmethod
    def _load(tmp_path, monkeypatch, status):
        import json as _json

        from kiro_crew.dashboard import state as ST

        monkeypatch.setattr(ST, "config_dir", lambda: tmp_path)
        (tmp_path / "tags.json").write_text(
            _json.dumps([{"id": "done", "name": "Done", "status": status}]), encoding="utf-8"
        )
        st = _make_state(tmp_path)
        st.load_tags()
        return st._tags[0]["status"]

    def test_a_stringy_status_is_persisted_verbatim(self, tmp_path, monkeypatch):
        for spelling in ("false", "maybe", "true"):
            got = self._load(tmp_path, monkeypatch, spelling)
            assert got == spelling, (
                "the loader rewrote a hand-edited status %r to %r, so this event changed which "
                "tags the board treats as columns" % (spelling, got)
            )

    def test_a_real_bool_is_persisted_verbatim(self, tmp_path, monkeypatch):
        for spelling in (True, False):
            got = self._load(tmp_path, monkeypatch, spelling)
            assert got is spelling, "the loader altered a boolean status %r -> %r" % (
                spelling,
                got,
            )


class TestEveryTagWriterIsWiredOrDeclaredSilent:
    """The per-writer emit is only safe while the writer set is CLOSED.

    A per-site emit is the shape that lets review miss a call site, so the set is pinned
    here rather than left to a reviewer's grep: a new writer breaks this test and forces the
    author to wire it or declare it. Each silent writer must also still be UNABLE to dispatch
    -- the gate admits only an owner dashboard request -- so a module that gains a request
    context stops being legitimately silent and this test says so.
    """

    @staticmethod
    def _writers() -> dict[str, int]:
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
        found: dict[str, int] = {}
        for path in root.rglob("*.py"):
            hits = sum(
                1
                for line in path.read_text(encoding="utf-8").splitlines()
                if "slot.tags.append" in line
            )
            if hits:
                found[path.name] = hits
        assert found, "found NO tag writers at all, so this test cannot prove anything"
        return found

    def test_the_writer_set_is_exactly_the_declared_one(self):
        assert self._writers() == {"chat_handlers.py": 1, "channel_slots.py": 2}, (
            "the set of slot.tags writers changed: wire the new one to the lane dispatch or "
            "declare it silent in the spec's writer table, then update this test: %r"
            % (self._writers(),)
        )

    def test_the_dashboard_writer_dispatches(self):
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src/kiro_crew/dashboard/chat_handlers.py"
        text = src.read_text(encoding="utf-8")
        assert "_dispatch_lane_changed" in text, (
            "the dashboard tag writer no longer reaches the lane dispatch, so an owner moving "
            "a card fires nothing"
        )

    def test_the_silent_writers_still_cannot_dispatch(self):
        from pathlib import Path

        src = Path(__file__).resolve().parents[1] / "src/kiro_crew/dashboard/channel_slots.py"
        text = src.read_text(encoding="utf-8")
        assert "request" not in text, (
            "channel filing gained a request context, so the owner-only gate could now admit "
            "it: decide whether this writer dispatches instead of leaving it silent"
        )


class TestCancellationDuringAPreSpawnAwaitIsStillBooked:
    """A run abandoned mid-gate must look abandoned, not un-attempted.

    The start-row arm already books a cancellation, so an earlier pre-spawn await that does
    not leaves the same abandonment recorded or unrecorded depending on WHERE the cancel
    landed -- and an operator reading the trail cannot tell the two apart.
    """

    @pytest.mark.asyncio
    async def test_a_cancel_in_the_capability_gate_books_the_run(self, monkeypatch):
        from kiro_crew import hooks as H

        hook = H.ScriptHook(
            id="h1",
            name="lane",
            event=HOOK_EVENT_SESSION_LANE_CHANGED,
            command="true",
            enabled=True,
        )

        async def _cancel_mid_gate(*_a, **_k):
            raise asyncio.CancelledError()

        monkeypatch.setattr(H, "_script_hooks_capability_denied_async", _cancel_mid_gate)

        rows: list[tuple] = []

        async def _record(sk, op, outcome, error=""):
            rows.append((op, outcome))
            return True

        monkeypatch.setattr(H, "_audit_hook_invocation_now", _record)

        with pytest.raises(asyncio.CancelledError):
            await H.run_script_hook(hook)

        assert hook.last_status == "cancelled", (
            "a run cancelled inside the capability gate left no bookkeeping, so the trail "
            "cannot tell it from a run never attempted: %r" % (hook.last_status,)
        )
        assert hook.run_count == 1, "the cancelled run was not counted: %r" % (hook.run_count,)
        assert [o for _, o in rows] == [
            "cancelled"
        ], "no cancelled outcome row was written for a pre-spawn cancellation: %r" % (rows,)
