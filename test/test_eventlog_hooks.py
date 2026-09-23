"""Tests for the best-effort per-member event-log hook helpers.

These must not depend on the real event-log service: its bodies may still
raise NotImplementedError while it is being filled in concurrently, which is
exactly the case emit() has to swallow.
"""

from __future__ import annotations

import kiro_crew.eventlog.service as svc_mod
from kiro_crew import eventlog_hooks
from kiro_crew.members import member_slot_key, slug_for_name


def test_member_slug_for_slot_bare_key():
    slug = slug_for_name("Review Agent")
    assert eventlog_hooks.member_slug_for_slot(member_slot_key(slug)) == slug


def test_member_slug_for_slot_prefixed_keys():
    slug = slug_for_name("Review Agent")
    base = member_slot_key(slug)
    assert eventlog_hooks.member_slug_for_slot("dashboard_" + base) == slug
    assert eventlog_hooks.member_slug_for_slot("dashboard:" + base) == slug


def test_member_slug_for_slot_rejects_non_member():
    assert eventlog_hooks.member_slug_for_slot("chat-42-1700000000") is None
    assert eventlog_hooks.member_slug_for_slot("cron-abc") is None
    assert eventlog_hooks.member_slug_for_slot("") is None
    assert eventlog_hooks.member_slug_for_slot(None) is None
    assert eventlog_hooks.member_slug_for_slot(1234) is None


def test_emit_swallows_a_raising_service(monkeypatch):
    class _Raiser:
        def ensure(self, slug, name):
            raise NotImplementedError("service not filled in yet")

        def append(self, slug, type, data):
            raise NotImplementedError

    monkeypatch.setattr(svc_mod, "get_service", lambda: _Raiser())
    # Must not raise despite ensure() blowing up.
    eventlog_hooks.emit("some-slug", "Some Name", "member/message", {"ts": 1.0, "preview": "hi"})


def test_emit_calls_ensure_then_append(monkeypatch):
    calls: list[tuple] = []

    class _Recorder:
        def ensure(self, slug, name):
            calls.append(("ensure", slug, name))

        def append(self, slug, type, data):
            calls.append(("append", slug, type, data))

    monkeypatch.setattr(svc_mod, "get_service", lambda: _Recorder())
    eventlog_hooks.emit("slug-a", "Name A", "member/message", {"ts": 2.0, "preview": "x"})
    assert calls[0] == ("ensure", "slug-a", "Name A")
    assert calls[1][0] == "append" and calls[1][1] == "slug-a"
    assert calls[1][2] == "member/message"


def test_emit_no_slug_is_noop(monkeypatch):
    def _boom():
        raise AssertionError("get_service must not be called for an empty slug")

    monkeypatch.setattr(svc_mod, "get_service", _boom)
    eventlog_hooks.emit("", "Name", "member/message", {})
    eventlog_hooks.emit(None, "Name", "member/message", {})


# ---------------------------------------------------------------------------
# member_name_for_slug: turning a slot's slug back into the member's exact NAME
# ---------------------------------------------------------------------------
class _Cfg:
    """Minimal stand-in for the config object these helpers read."""

    def __init__(self, agents):
        self.agents = agents


def test_member_name_for_slug_returns_the_exact_configured_name():
    """The event log stores the NAME, so a slug has to round-trip back to it.

    Storing the slug instead would be lossy: the slug is a lowercased, punctuation
    folded form, and the log is what the member timeline renders.
    """
    name = "Review-Agent"
    cfg = _Cfg({name: object(), "Other-Agent": object()})

    assert eventlog_hooks.member_name_for_slug(cfg, slug_for_name(name)) == name


def test_member_name_for_slug_declines_a_name_outside_the_agent_grammar():
    """A roster name with a space resolves to None, and that is the contract.

    The primary resolver skips any name failing the agent-name grammar (which
    allows only alphanumerics, hyphens and underscores), so such a row is not
    addressable here -- it cannot have been created through the validated CRUD
    surface, and a hand-edited config row must not become addressable by writing
    it. Worth pinning because the slug itself round-trips fine, so the None looks
    surprising until you know it is the grammar talking.
    """
    spaced = "Review Agent"
    cfg = _Cfg({spaced: object()})

    assert eventlog_hooks.member_name_for_slug(cfg, slug_for_name(spaced)) is None


def test_member_name_for_slug_is_none_without_a_slug():
    """Called on every slot, most of which are not members, so this is the hot path."""
    cfg = _Cfg({"Review Agent": object()})

    assert eventlog_hooks.member_name_for_slug(cfg, "") is None
    assert eventlog_hooks.member_name_for_slug(cfg, None) is None


def test_member_name_for_slug_is_none_when_no_member_matches():
    """An unknown slug must read as "not a member", never as a guess."""
    cfg = _Cfg({"Review Agent": object()})

    assert eventlog_hooks.member_name_for_slug(cfg, "nobody-by-that-slug") is None


def test_member_name_for_slug_falls_back_to_a_direct_scan(monkeypatch):
    """The handler resolver is an optional import, so its absence cannot be fatal.

    ``dashboard.handlers.members`` pulls in the whole dashboard package; a CLI-only
    or partially installed process can fail that import, and the hook still has to
    answer. The fallback scans ``cfg.agents`` through ``members.slug_for_name``.
    """
    import kiro_crew.dashboard.handlers.members as members_handler

    def _unavailable(cfg, slug):
        raise RuntimeError("resolver unavailable in this process")

    monkeypatch.setattr(members_handler, "_member_names_for_slug", _unavailable)
    name = "Review Agent"
    cfg = _Cfg({name: object()})

    assert eventlog_hooks.member_name_for_slug(cfg, slug_for_name(name)) == name


def test_member_name_for_slug_skips_a_name_that_cannot_be_slugged(monkeypatch):
    """One unsluggable roster entry must not hide the members after it.

    The scan is ordered, so a raise on entry one would otherwise lose entry two --
    the same "one broken peer must not mask healthy ones" rule the doctor sections
    follow.
    """
    import kiro_crew.dashboard.handlers.members as members_handler
    from kiro_crew import members as members_mod

    monkeypatch.setattr(
        members_handler,
        "_member_names_for_slug",
        lambda cfg, slug: (_ for _ in ()).throw(RuntimeError("resolver unavailable")),
    )
    real_slug_for_name = members_mod.slug_for_name

    def _explode_on_first(name):
        if name == "Broken Entry":
            raise ValueError("cannot slug this name")
        return real_slug_for_name(name)

    monkeypatch.setattr(members_mod, "slug_for_name", _explode_on_first)
    wanted = "Review Agent"
    cfg = _Cfg({"Broken Entry": object(), wanted: object()})

    assert eventlog_hooks.member_name_for_slug(cfg, real_slug_for_name(wanted)) == wanted


def test_member_name_for_slug_survives_a_config_without_agents(monkeypatch):
    """A config shape this helper cannot read is "no member", not a crash."""
    import kiro_crew.dashboard.handlers.members as members_handler

    monkeypatch.setattr(
        members_handler,
        "_member_names_for_slug",
        lambda cfg, slug: (_ for _ in ()).throw(RuntimeError("resolver unavailable")),
    )

    assert eventlog_hooks.member_name_for_slug(object(), "review-agent") is None


class TestV2MemberSlotKeys:
    """A member who opts into a memory store still gets durable events.

    ``dm_slot_key`` appends ``.memory-<store>`` for such a member, and a reader
    that treats the whole tail as the slug hands ``validate_slug`` a string with
    a ``.`` in it. That is refused, the hook answers None, and every message,
    slot and patrol event for that member is dropped -- silently, because each
    emit site wraps itself in a best-effort except.
    """

    def _v2_slot_key(self, slug: str, store: str) -> str:
        from kiro_crew import members as members_mod

        return members_mod.DM_SLOT_KEY_PREFIX + slug + members_mod.MEMORY_STORE_SLOT_SUFFIX + store

    def test_a_memory_store_slot_resolves_to_the_members_slug(self):
        from kiro_crew import eventlog_hooks

        key = self._v2_slot_key("alice", "research")
        assert eventlog_hooks.member_slug_for_slot(key) == "alice", (
            "a V2 member's slot key did not resolve to their slug, so every "
            "durable event for that member is dropped"
        )

    def test_a_dashboard_prefixed_memory_store_slot_also_resolves(self):
        from kiro_crew import eventlog_hooks

        key = "dashboard_" + self._v2_slot_key("alice", "research")
        assert eventlog_hooks.member_slug_for_slot(key) == "alice"

    def test_the_suffix_belongs_to_the_slot_not_the_slug(self):
        """The shared parser is the one place this rule is spelled."""
        from kiro_crew import members as members_mod

        key = self._v2_slot_key("alice", "research")
        assert members_mod.slug_from_dm_slot_key(key) == "alice"
        assert members_mod.slug_from_dm_slot_key("chat-42-1700000000") is None


class TestStartupReconcileHonoursMemberId:
    """Reconciliation must read the log the member's own identity names."""

    def test_an_explicit_member_id_decides_which_log_is_reconciled(self):
        from kiro_crew import members as members_mod

        class _Agent:
            # An attribute, not a dict key: member_slug reads it with getattr,
            # so a mapping fixture silently yields the folded name instead.
            member_id = "alice-two"

        class _Cfg:
            agents = {"Alice": _Agent()}

        cfg = _Cfg()
        assert members_mod.member_slug("Alice", cfg) == "alice-two", (
            "member_slug ignored the explicit member_id, so reconciliation "
            "would fold the name and read a different log"
        )
        assert members_mod.slug_for_name("Alice") != "alice-two"


class TestEmitAnswersWhetherTheEventLanded:
    """A dropped event is reported and its outcome is returned, not discarded.

    This log is the projections' only input, so an append that does not land is a
    transition missing from history for good. At debug that is indistinguishable
    from a transition that never happened, which is the one distinction a
    projection built from this log exists to make.
    """

    def test_a_failed_append_is_reported_and_answered(self, caplog, monkeypatch):
        import logging

        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog import service as service_module

        class _Unusable:
            def ensure(self, *args, **kwargs):
                raise OSError("no space left on device")

        monkeypatch.setattr(service_module, "get_service", lambda: _Unusable())
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.eventlog_hooks"):
            landed = eventlog_hooks.emit("alice", "Alice", "member/message", {"text": "hi"})

        assert landed is False
        reported = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert reported, "a dropped event was not reported above debug"
        assert "DROPPED" in reported[0] and "alice" in reported[0]

    def test_a_landed_append_answers_true_and_is_not_reported(self, caplog):
        # CONTROL. Without this, an emit that answered False unconditionally, or one
        # that announced every call, would satisfy the test above while saying
        # nothing about whether the event reached the log.
        import logging

        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.eventlog.types import MEMBER_MESSAGE

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.eventlog_hooks"):
            landed = eventlog_hooks.emit("alice", "Alice", MEMBER_MESSAGE, {"text": "hi"})

        assert landed is True
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert get_service().last_seq("alice") >= 0


class TestThePendingQueueIsBounded:
    """The outstanding-append set is a retained field, so it has a ceiling.

    One worker drains it in order, and each queued item holds a closure carrying
    the event's own data, so a burst arriving faster than the filesystem retires it
    would grow without limit.
    """

    def test_submissions_past_the_cap_are_refused_and_reported_once(self, caplog, monkeypatch):
        import logging
        import threading

        from kiro_crew import eventlog_hooks

        release = threading.Event()
        ran: list[int] = []

        def _block() -> None:
            release.wait(timeout=10)
            ran.append(1)

        monkeypatch.setattr(eventlog_hooks, "MAX_PENDING_APPENDS", 4)
        try:
            with caplog.at_level(logging.DEBUG, logger="kiro_crew.eventlog_hooks"):
                for _ in range(12):
                    eventlog_hooks.submit(_block)
                pending = len(eventlog_hooks._inflight)
        finally:
            release.set()
            eventlog_hooks.drain_for_shutdown()

        assert pending <= 4
        reported = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert reported, "an overflowing append queue was not reported"
        assert "DROPPED" in reported[0]
        # ONE line per episode, not one per dropped append: a deep burst would
        # otherwise turn a single fault into thousands of lines.
        assert len(reported) == 1

    def test_submissions_within_the_cap_all_run_and_are_not_reported(self, caplog):
        # CONTROL. Without this, a submit that refused everything would satisfy the
        # test above while dropping every event the gateway ever records.
        import logging

        from kiro_crew import eventlog_hooks

        ran: list[int] = []
        with caplog.at_level(logging.DEBUG, logger="kiro_crew.eventlog_hooks"):
            for _ in range(5):
                eventlog_hooks.submit(lambda: ran.append(1))
            eventlog_hooks.drain_for_shutdown()

        assert len(ran) == 5
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


class TestThisProcessDoesNotRetainAMemberLogsWriteLease:
    """Measured, because a reviewer read the cached handle as a standing exclusion.

    The write lease is taken lazily on a handle's first WRITE and released when that
    handle is dropped. ``MemberLog.append`` reloads at the end of the same call,
    which replaces the handle the claim was bound to, so the lease does not outlive
    the append that took it. A second process is therefore refused only while an
    append is actually in flight, not for the life of this one.
    """

    def test_no_lease_is_held_after_ensure_append_or_read(self):
        from kiro_crew.crew_log import lease
        from kiro_crew.eventlog.service import get_service
        from kiro_crew.eventlog.types import ACTIVITY_RECORD

        svc = get_service()
        svc.ensure("probe-member", "Probe Member")
        assert not lease._held, "ensure left a lease held"

        svc.append("probe-member", ACTIVITY_RECORD, {"member": "Probe Member", "ts": "x"})
        assert not lease._held, "append left a lease held"

        svc.append("probe-member", ACTIVITY_RECORD, {"member": "Probe Member", "ts": "y"})
        assert not lease._held, "a second append left a lease held"

        svc.history("probe-member", before=None, limit=None)
        assert not lease._held, "a read took a lease at all"

    def test_the_lease_module_can_report_a_holder_at_all(self):
        # CONTROL. Asserting an EMPTY holder table proves nothing unless that table
        # is capable of being non-empty, which is exactly the shape a pin can be
        # vacuous in: a table that is always empty would satisfy the test above.
        from kiro_crew.crew_log import lease
        from kiro_crew.crew_log.schema import KIND_MEMBER
        from kiro_crew.eventlog.log import MemberLog
        from kiro_crew.eventlog.service import get_service

        get_service().ensure("probe-member", "Probe Member")
        lease_path = MemberLog("probe-member").path.parent / lease.LEASE_FILE
        key = lease.acquire(lease_path, kind=KIND_MEMBER, unit_id="probe-member")
        try:
            assert lease._held, "the holder table cannot report a holder"
        finally:
            lease.release(key)
        assert not lease._held


class TestARefusedSubmitLeavesTheTransitionsRetryable:
    """The checkpoint is the only record of what is still unwritten.

    `submit` is bounded, so it can refuse. Advancing the checkpoint past a refused
    handover does not lose one event -- it loses every future retry of it, because
    the next broadcast compares against the advanced checkpoint and computes no
    transitions at all.
    """

    def test_submit_answers_false_past_the_ceiling(self, monkeypatch):
        import kiro_crew.eventlog_hooks as hooks

        monkeypatch.setattr(hooks, "MAX_PENDING_APPENDS", 0)
        monkeypatch.setattr(hooks, "_overflowing", False)
        assert hooks.submit(lambda: None) is False

    def test_submit_answers_true_when_queued(self):
        # CONTROL. Without this, a submit that always answered False would satisfy
        # the test above while making every caller retry forever and never advance.
        import threading

        import kiro_crew.eventlog_hooks as hooks

        landed = threading.Event()
        assert hooks.submit(landed.set) is True
        assert landed.wait(5.0)

    def test_a_refused_handover_keeps_the_checkpoint_so_the_next_call_retries(self, monkeypatch):
        """Drives the REAL method twice and counts the handovers it makes.

        Asserted on what reaches `submit`, not on the checkpoint field: a caller that
        advanced the checkpoint but resent anyway would be correct, and one that held
        the checkpoint but computed an empty set would not.
        """
        from kiro_crew.dashboard import state as state_module

        st = state_module.DashboardState.__new__(state_module.DashboardState)
        st._member_driven_slots_seen = {}
        st._member_slots_retry = []
        st._member_slots_unconfirmed = {}
        st._broadcast = lambda *a, **k: None
        # Serialization is the rest of this method's job and is not what is under
        # test; stubbing it keeps the REAL transition bookkeeping running.
        st.serialize_slots = lambda *a, **k: []
        # `member-<slug>` is case (a) in the method's own comment: a member's pinned
        # DM slot, whose slug is pure string work on the loop.
        st._slots = {"slot-1": _StubSlot("slot-1", "member-alice")}

        handovers: list[object] = []
        accept = {"ok": False}

        def _fake_submit(fn):
            handovers.append(fn)
            return accept["ok"]

        # Patched on the real module: state.py imports eventlog_hooks INSIDE the
        # method, so there is no module-level attribute to stand in for.
        import kiro_crew.eventlog_hooks as hooks_module

        monkeypatch.setattr(hooks_module, "submit", _fake_submit)

        accept["ok"] = False
        st._do_slots_broadcast()
        assert len(handovers) == 1, "the first pass must hand the transitions over"
        assert st._member_driven_slots_seen == {}, (
            "a refused handover must leave the checkpoint where it was, "
            "or nothing ever recomputes the transitions"
        )

        accept["ok"] = True
        st._do_slots_broadcast()
        assert len(handovers) == 2, "the retry is the property being pinned"
        assert st._member_driven_slots_seen != {}

        # A third pass has nothing left to say.
        st._do_slots_broadcast()
        assert len(handovers) == 2


class _StubSlot:
    """Smallest thing the real `_do_slots_broadcast` reads off a slot."""

    def __init__(self, key: str, created_by: str) -> None:
        self.key = key
        self._created_by = created_by
        self.memory_store = ""


class TestAnAppendThatDidNotLandIsRecomputed:
    """Queue acceptance is not the same claim as a completed write.

    `submit` answering True means the closure is QUEUED. The append itself runs
    later, on the worker, and can still fail there -- at which point the checkpoint
    already counts the transition as handed over. Without a report back, that
    transition is lost for the life of the process, because every later broadcast
    compares against a checkpoint claiming it was written.
    """

    @staticmethod
    def _state(monkeypatch, emit_results, handovers):
        from kiro_crew.dashboard import state as state_module

        st = state_module.DashboardState.__new__(state_module.DashboardState)
        st._member_driven_slots_seen = {}
        st._member_slots_retry = []
        st._member_slots_unconfirmed = {}
        st._broadcast = lambda *a, **k: None
        st.serialize_slots = lambda *a, **k: []
        st._slots = {"slot-1": _StubSlot("slot-1", "member-alice")}

        import kiro_crew.eventlog_hooks as hooks_module

        def _run_now(fn):
            handovers.append(fn)
            fn()  # the ordered executor runs it later; here, inline and observable
            return True

        monkeypatch.setattr(hooks_module, "submit", _run_now)
        monkeypatch.setattr(
            hooks_module, "emit", lambda *a, **k: emit_results.pop(0) if emit_results else True
        )
        return st

    def test_a_failed_append_is_offered_again_on_the_next_broadcast(self, monkeypatch):
        handovers: list[object] = []
        # First append fails, every later one lands.
        st = self._state(monkeypatch, [False], handovers)

        st._do_slots_broadcast()
        assert len(handovers) == 1
        assert [e[0] for e in st._member_slots_retry] == ["slot-1"], (
            "a worker that could not write must say so, or the loss is permanent. A "
            "failed OPEN is retained VERBATIM rather than as a checkpoint correction: "
            "the correction only recomputes the open while the slot is STILL open, so "
            "a slot that closes inside the window loses both of its transitions."
        )
        assert (
            st._member_slots_unconfirmed == {}
        ), "an open is not reported through the checkpoint map any more"

        st._do_slots_broadcast()
        assert len(handovers) == 2, "the failed transition must be recomputed and re-queued"
        assert st._member_slots_retry == []

        # Third pass: it landed, so there is nothing left to say.
        st._do_slots_broadcast()
        assert len(handovers) == 2

    def test_an_open_that_failed_survives_the_slot_closing_before_the_retry(self, monkeypatch):
        """The window a checkpoint correction cannot express.

        Recording a failed OPEN as `unconfirmed[key] = None` pops the key from the
        checkpoint so the next comparison recomputes the open. That works only while
        the slot is STILL open. If it closes first, `current` lacks the key too, the
        comparison yields NOTHING, and neither the open nor the close reaches the
        ledger -- the slot's whole episode is absent from the log for the life of the
        process, with no later event to correct it. So a failed open is retained
        verbatim instead, which does not depend on the slot's present state.

        Both transitions must be written, and the open must come FIRST: a close with no
        preceding open is not a history anyone can read.
        """
        import kiro_crew.eventlog_hooks as hooks_module
        from kiro_crew.dashboard import state as state_module
        from kiro_crew.eventlog.types import SLOT_CLOSED, SLOT_OPENED

        st = state_module.DashboardState.__new__(state_module.DashboardState)
        st._member_driven_slots_seen = {}
        st._member_slots_retry = []
        st._member_slots_unconfirmed = {}
        st._broadcast = lambda *a, **k: None
        st.serialize_slots = lambda *a, **k: []
        st._slots = {"slot-1": _StubSlot("slot-1", "member-alice")}

        emitted: list[str] = []
        fail_opens = {"on": True}

        def _emit(slug, name, etype, data):
            if fail_opens["on"] and etype == SLOT_OPENED:
                return False  # refused: nothing is written
            emitted.append(etype)
            return True

        monkeypatch.setattr(hooks_module, "submit", lambda fn: (fn(), True)[1])
        monkeypatch.setattr(hooks_module, "emit", _emit)

        # The open is refused.
        st._do_slots_broadcast()
        assert emitted == [], "the refused open must not count as written"

        # The slot CLOSES before anything retried the open, and the open now lands.
        fail_opens["on"] = False
        st._slots = {}
        st._do_slots_broadcast()

        assert emitted == [SLOT_OPENED, SLOT_CLOSED], (
            "the episode must reach the log as OPENED then CLOSED; got "
            f"{emitted!r} -- an empty list is the old defect (both transitions lost), "
            "and a lone close is a history with no beginning"
        )
        assert st._member_slots_retry == []
        assert st._member_slots_unconfirmed == {}

        # And it settles: nothing is re-emitted for ever.
        st._do_slots_broadcast()
        assert emitted == [SLOT_OPENED, SLOT_CLOSED], emitted

    def test_an_append_that_landed_is_not_offered_again(self, monkeypatch):
        # CONTROL. Without this, reporting EVERY append as unconfirmed would satisfy
        # the test above while re-appending the same transition on every broadcast.
        handovers: list[object] = []
        st = self._state(monkeypatch, [], handovers)

        st._do_slots_broadcast()
        assert len(handovers) == 1
        assert st._member_slots_unconfirmed == {}

        st._do_slots_broadcast()
        assert len(handovers) == 1


class TestAFailedCloseIsRetriedToo:
    """The two directions need OPPOSITE corrections, which is why a key is not enough.

    A failed OPEN is retried by leaving the key out of the checkpoint. A failed
    CLOSE cannot be: the checkpoint has already moved past that key, so removing it
    changes nothing and the next comparison computes no transition at all. The key
    has to go BACK for the close to be recomputed.
    """

    def test_a_failed_close_is_offered_again(self, monkeypatch):
        import kiro_crew.eventlog_hooks as hooks_module
        from kiro_crew.dashboard import state as state_module
        from kiro_crew.eventlog.types import SLOT_CLOSED

        st = state_module.DashboardState.__new__(state_module.DashboardState)
        st._member_driven_slots_seen = {}
        st._member_slots_retry = []
        st._member_slots_unconfirmed = {}
        st._broadcast = lambda *a, **k: None
        st.serialize_slots = lambda *a, **k: []
        st._slots = {"slot-1": _StubSlot("slot-1", "member-alice")}

        emitted: list[str] = []
        fail_closes = {"on": False}

        def _emit(slug, name, etype, data):
            emitted.append(etype)
            return not (fail_closes["on"] and etype == SLOT_CLOSED)

        monkeypatch.setattr(hooks_module, "submit", lambda fn: (fn(), True)[1])
        monkeypatch.setattr(hooks_module, "emit", _emit)

        # Open it, successfully.
        st._do_slots_broadcast()
        assert emitted == ["slot/opened"], emitted

        # Now close it, and make the close fail.
        fail_closes["on"] = True
        st._slots = {}
        st._do_slots_broadcast()
        assert emitted == ["slot/opened", "slot/closed"], emitted
        assert st._member_slots_unconfirmed.get("slot-1") is not None, (
            "a failed CLOSE must record the identity to restore, not just the key: "
            "the checkpoint has already moved past it"
        )

        # The retry is the property the key-only version could not express.
        fail_closes["on"] = False
        st._do_slots_broadcast()
        assert emitted == ["slot/opened", "slot/closed", "slot/closed"], emitted
        assert st._member_slots_unconfirmed == {}

        # And it settles.
        st._do_slots_broadcast()
        assert emitted == ["slot/opened", "slot/closed", "slot/closed"], emitted


class TestAReportArrivingDuringTheDrainIsNotErased:
    """The worker writes its failure reports from a thread; the drain runs on the loop.

    A drain that COPIES the map and then clears it leaves a window: a report the worker
    records between those two statements is wiped without ever being applied, so the
    comparison recomputes nothing for that slot and the checkpoint advances past the
    transition for good. Draining by repeated pop closes it -- each pop either returns
    an entry, which is therefore applied, or finds none and leaves later arrivals for
    the next pass.
    """

    class _RacyMap(dict):
        """Lands one concurrent worker write at the instant the drain reads the map.

        Hooks BOTH read shapes so the fixture is not written against one
        implementation: ``clear`` is what a copy-then-clear drain calls after taking
        its snapshot, and ``popitem`` is what a pop drain calls instead.
        """

        def __init__(self, *args, injected=None, **kwargs):
            super().__init__(*args, **kwargs)
            self._injected = injected
            self._fired = False

        def _inject(self):
            if not self._fired and self._injected is not None:
                self._fired = True
                key, value = self._injected
                dict.__setitem__(self, key, value)

        def popitem(self):
            item = dict.popitem(self)
            self._inject()
            return item

        def clear(self):
            self._inject()
            dict.clear(self)

    def test_a_failure_recorded_mid_drain_is_still_applied(self, monkeypatch):
        import kiro_crew.eventlog_hooks as hooks_module
        from kiro_crew.dashboard import state as state_module

        st = state_module.DashboardState.__new__(state_module.DashboardState)
        # The checkpoint has already advanced past both closes, which is what makes a
        # dropped correction permanent rather than merely late.
        st._member_driven_slots_seen = {}
        st._member_slots_retry = []
        st._member_slots_unconfirmed = self._RacyMap(
            {"slot-1": ("member-alice", "")},
            injected=("slot-2", ("member-bob", "")),
        )
        st._broadcast = lambda *a, **k: None
        st.serialize_slots = lambda *a, **k: []
        st._slots = {}

        closed: list[str] = []

        def _emit(slug, name, etype, data):
            closed.append(str(data.get("slot_key")))
            return True

        monkeypatch.setattr(hooks_module, "submit", lambda fn: (fn(), True)[1])
        monkeypatch.setattr(hooks_module, "emit", _emit)

        st._do_slots_broadcast()

        assert "slot-1" in closed, "precondition: the entry present before the drain applied"
        assert "slot-2" in closed, (
            "a failure the worker recorded during the drain was erased before it was "
            "applied, so that slot's close is never recomputed"
        )

    def test_nothing_is_recomputed_when_no_failure_was_recorded(self, monkeypatch):
        """CONTROL, and it has to be able to FAIL.

        A slot that is open, confirmed, and reported by nobody as failed must produce
        NO transition at all. An earlier version of this control left every field
        empty, which made it pass under any implementation -- so it certified nothing.
        Here the checkpoint and the live set agree on one slot, so a drain that
        invented a correction, or applied one unconditionally, would drop that slot
        out of the checkpoint and the comparison would re-emit it as an OPEN.
        """
        import kiro_crew.eventlog_hooks as hooks_module
        from kiro_crew.dashboard import state as state_module

        st = state_module.DashboardState.__new__(state_module.DashboardState)
        st._member_driven_slots_seen = {"slot-1": ("member-alice", "")}
        st._member_slots_retry = []
        st._member_slots_unconfirmed = {}
        st._broadcast = lambda *a, **k: None
        st.serialize_slots = lambda *a, **k: []
        st._slots = {"slot-1": _StubSlot("slot-1", "member-alice")}

        handed: list[object] = []
        emitted: list[str] = []

        def _emit(slug, name, etype, data):
            emitted.append(f"{etype}:{data.get('slot_key')}")
            return True

        monkeypatch.setattr(hooks_module, "submit", lambda fn: (handed.append(fn), fn(), True)[2])
        monkeypatch.setattr(hooks_module, "emit", _emit)

        st._do_slots_broadcast()

        assert handed == [], (
            "the checkpoint already matched the live set, so a correction was invented "
            f"where none was reported: {emitted}"
        )
        assert emitted == [], emitted


class TestAReportArrivingAfterADrainIsStillHeard:
    """The worker closure captures the unconfirmed map by REFERENCE.

    A drain that rebinds the attribute to a fresh dictionary leaves an in-flight
    worker writing into an object nothing reads, so its failure report vanishes and
    the retry it exists for never happens. Draining in place keeps every worker's
    reference live.
    """

    def test_a_worker_in_flight_across_a_drain_is_still_heard(self, monkeypatch):
        from kiro_crew.dashboard import state as state_module

        st = state_module.DashboardState.__new__(state_module.DashboardState)
        st._member_driven_slots_seen = {}
        st._member_slots_retry = []
        st._member_slots_unconfirmed = {}
        st._broadcast = lambda *a, **k: None
        st.serialize_slots = lambda *a, **k: []
        st._slots = {"slot-1": _StubSlot("slot-1", "member-alice")}

        import kiro_crew.eventlog_hooks as hooks_module

        deferred: list[object] = []
        monkeypatch.setattr(hooks_module, "submit", lambda fn: (deferred.append(fn), True)[1])
        monkeypatch.setattr(hooks_module, "emit", lambda *a, **k: False)

        # Pass 1 queues a worker that has NOT run yet; it holds the map from now.
        st._do_slots_broadcast()
        assert len(deferred) == 1

        # An earlier failure is outstanding, which is what makes the next pass DRAIN.
        # Without something to drain the rebind never happens and this pin proves
        # nothing -- the first version of it passed against the broken code for
        # exactly that reason.
        st._member_slots_unconfirmed["slot-earlier"] = None
        st._slots = {}
        st._do_slots_broadcast()

        # Only now does the in-flight worker report.
        deferred[0]()
        assert st._member_slots_unconfirmed or st._member_slots_retry, (
            "the in-flight worker wrote into a detached container, so its failure "
            "was lost and nothing will ever recompute that transition"
        )


class TestALiveReopenSurvivesTheStartupReconcile:
    """A closer is decided from a snapshot and written afterwards.

    Reconcile runs concurrent with the gateway going live, so a slot it read as open
    can be legitimately reopened before its closer lands. The log is append-only with
    no compaction, so a closer landing after a live open is a permanent regression --
    and a later restart reads the state as closed and never reopens it.
    """

    def test_a_closer_is_not_written_once_the_state_it_closes_is_gone(self, tmp_path, monkeypatch):
        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.eventlog import types

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        svc_mod.set_service(None)
        svc = svc_mod.get_service()
        svc.ensure("ivy", "Ivy")

        wrote = svc.append_closer_if_still_applies(
            "ivy",
            types.SLOT_CLOSED,
            {"slot_key": "s1", "reason": "interrupted"},
            still_applies=lambda _values, _observed: False,
        )

        assert wrote is None
        assert not [
            e
            for e in svc.history("ivy", before=None, limit=None)
            if e.get("type") == types.SLOT_CLOSED
        ], "a closer landed for state that had already been reopened"

    def test_a_closer_that_still_applies_is_written(self, tmp_path, monkeypatch):
        # CONTROL. Refusing every closer would satisfy the test above and leave every
        # interrupted slot open for good.
        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.eventlog import types

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        svc_mod.set_service(None)
        svc = svc_mod.get_service()
        svc.ensure("ivy", "Ivy")

        wrote = svc.append_closer_if_still_applies(
            "ivy",
            types.SLOT_CLOSED,
            {"slot_key": "s1", "reason": "interrupted"},
            still_applies=lambda _values, _observed: True,
        )

        assert wrote is not None
        assert wrote["type"] == types.SLOT_CLOSED

    def test_the_predicate_reads_the_current_projection_not_the_callers(
        self, tmp_path, monkeypatch
    ):
        # The point of the recheck is that it sees state the caller could not. A
        # predicate handed the caller's own snapshot would close nothing it should not
        # and also nothing it should -- it would just be the same stale answer again.
        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.eventlog import types

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        svc_mod.set_service(None)
        svc = svc_mod.get_service()
        svc.ensure("ivy", "Ivy")
        seen: list[dict] = []

        svc.append_closer_if_still_applies(
            "ivy",
            types.SLOT_CLOSED,
            {"slot_key": "s1", "reason": "interrupted"},
            still_applies=lambda values, _observed: bool(seen.append(values)) or True,
        )

        assert len(seen) == 1, "the predicate was not consulted"
        assert isinstance(seen[0], dict), f"the predicate saw {type(seen[0])}, not projections"

    def test_a_patrol_rearmed_between_the_decision_and_the_write_keeps_its_arm(
        self, tmp_path, monkeypatch
    ):
        """The state is not the episode.

        Reading the CURRENT projection is necessary but not sufficient: a patrol that
        stopped and was re-armed inside the window reads `armed` exactly like the
        episode the reconcile meant to close. Closing that one stamps the NEW episode
        with the OLD one's STOPPED, and because the log is append-only with no
        compaction the live patrol then reads as stopped for good -- a later restart
        sees `stopped` and has no reason to reopen it.
        """
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.eventlog import types

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        svc_mod.set_service(None)
        svc = svc_mod.get_service()
        svc.ensure("ivy", "Ivy")

        # The episode the reconcile decides on.
        svc.append("ivy", types.PATROL_STARTED, {"slot_key": "s1"})
        observed = svc.snapshot("ivy").get("values", {})
        assert (observed.get(types.PROJ_WAKE) or {}).get("patrol") == "armed"

        # The window: that episode ends and a NEW one is armed.
        svc.append("ivy", types.PATROL_STOPPED, {"slot_key": "s1", "reason": "user"})
        svc.append("ivy", types.PATROL_STARTED, {"slot_key": "s2"})

        current = svc.snapshot("ivy").get("values", {})
        assert (current.get(types.PROJ_WAKE) or {}).get("patrol") == "armed", (
            "precondition: the re-armed patrol must read `armed`, which is exactly "
            "why a state-only predicate cannot tell it from the original episode"
        )

        wrote = svc.append_closer_if_still_applies(
            "ivy",
            types.PATROL_STOPPED,
            {"slot_key": "s1", "reason": "interrupted"},
            still_applies=eventlog_hooks._patrol_is_still_armed_at,
            observed=observed,
        )

        assert wrote is None, (
            "a STOPPED closer for the finished episode landed on the re-armed patrol, "
            "so the live patrol now reads as stopped and nothing reopens it"
        )
        assert (svc.snapshot("ivy").get("values", {}).get(types.PROJ_WAKE) or {}).get(
            "patrol"
        ) == "armed", "the re-armed patrol was closed by the previous episode's closer"

    def test_an_unchanged_episode_is_still_closed(self, tmp_path, monkeypatch):
        # CONTROL for the test above. Refusing whenever the slug's sequence advanced
        # would satisfy it and leave every genuinely interrupted patrol armed for
        # good -- which is exactly the failure mode of gating on `asOfSeq`.
        from kiro_crew import eventlog_hooks
        from kiro_crew.eventlog import service as svc_mod
        from kiro_crew.eventlog import types

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        svc_mod.set_service(None)
        svc = svc_mod.get_service()
        svc.ensure("ivy", "Ivy")

        svc.append("ivy", types.PATROL_STARTED, {"slot_key": "s1"})
        observed = svc.snapshot("ivy").get("values", {})

        # An UNRELATED event advances the slug's sequence without touching the wake
        # projection. The closer must still land.
        svc.append("ivy", types.SLOT_OPENED, {"slot_key": "other"})

        wrote = svc.append_closer_if_still_applies(
            "ivy",
            types.PATROL_STOPPED,
            {"slot_key": "s1", "reason": "interrupted"},
            still_applies=eventlog_hooks._patrol_is_still_armed_at,
            observed=observed,
        )

        assert wrote is not None, (
            "the closer was refused for an episode that never changed; an unrelated "
            "event advanced the slug's sequence, which must not count as a re-arm"
        )
        assert wrote["type"] == types.PATROL_STOPPED

    def test_the_reconcile_hands_the_guard_what_it_decided_on(self):
        # The two tests above prove the PREDICATE tells a re-arm from an unchanged
        # episode. They still pass when the reconcile forgets to pass `observed`,
        # because the guard defaults it to {} -- and the predicate then compares the
        # live wake against an empty dict, refuses every closer, and silently leaves
        # every interrupted patrol armed. So pin the wiring too.
        import ast
        import pathlib

        import kiro_crew

        src = (pathlib.Path(kiro_crew.__file__).parent / "eventlog_hooks.py").read_text(
            encoding="utf-8"
        )
        calls = [
            n
            for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "append_closer_if_still_applies"
        ]
        assert calls, "the reconcile no longer uses the guarded append"
        for call in calls:
            kwargs = {k.arg for k in call.keywords}
            assert "observed" in kwargs, (
                f"the guarded append at line {call.lineno} passes no `observed`, so "
                "the predicate compares against {} and refuses every closer"
            )


class TestTheDrainWaitsForAReservationNotJustARegisteredFuture:
    """`submit` releases the lock to call `pool.submit`, so for that call's length an
    append is counted in `_reserved` and absent from `_inflight`.

    A drain reading only the futures answers "nothing remains" there, and the
    force-exit path that asked goes straight to `os._exit` -- the append dies queued,
    with no replay to recover it.
    """

    def test_a_drain_inside_the_window_does_not_report_success(self, monkeypatch):
        import concurrent.futures

        import kiro_crew.eventlog_hooks as hooks

        monkeypatch.setattr(hooks, "_overflowing", False)
        monkeypatch.setattr(hooks, "_reserved", 0)
        hooks._inflight.clear()

        answers: list[bool] = []

        class _Pool:
            def submit(self, fn):
                # The caller is between its reservation and its registration. This is
                # the window, reproduced the same way the ceiling test reproduces it.
                answers.append(hooks.drain_for_shutdown(timeout=0.05))
                f: concurrent.futures.Future = concurrent.futures.Future()
                f.set_result(None)
                return f

        monkeypatch.setattr(hooks, "io_executor", lambda: _Pool())
        assert hooks.submit(lambda: None) is True

        assert answers == [False], (
            "the drain reported a complete log while an append was reserved and not "
            "yet registered, so a force exit here loses it with no replay"
        )

    def test_a_drain_with_nothing_outstanding_still_returns_at_once(self, monkeypatch):
        # CONTROL. Polling until the deadline whatever the state would satisfy the
        # test above and add the full drain timeout to every clean shutdown.
        import time

        import kiro_crew.eventlog_hooks as hooks

        monkeypatch.setattr(hooks, "_reserved", 0)
        hooks._inflight.clear()

        started = time.monotonic()
        answer = hooks.drain_for_shutdown(timeout=5.0)
        elapsed = time.monotonic() - started

        assert answer is True
        assert elapsed < 0.5, f"a clean drain took {elapsed:.2f}s, so it polled needlessly"


class TestTheCeilingHoldsWhenTwoCallersInterleave:
    """Checking under the lock is not the same as CLAIMING under it.

    The future does not exist until `submit` returns, so the set cannot be added to
    while the lock is held. A check alone therefore lets several callers each pass a
    count that was true for all of them and then each add, putting the outstanding
    set past the ceiling by one per caller. The interleaving is reproduced exactly
    here: the pool re-enters `submit` before the first future is registered.
    """

    def test_a_second_caller_arriving_mid_submit_is_refused(self, monkeypatch):
        import concurrent.futures

        import kiro_crew.eventlog_hooks as hooks

        monkeypatch.setattr(hooks, "MAX_PENDING_APPENDS", 1)
        monkeypatch.setattr(hooks, "_overflowing", False)
        monkeypatch.setattr(hooks, "_reserved", 0)
        hooks._inflight.clear()

        inner: list[bool] = []

        class _Pool:
            def submit(self, fn):
                # The first caller is between its reservation and its registration --
                # exactly the window the ceiling has to survive.
                if not inner:
                    inner.append(hooks.submit(lambda: None))
                f: concurrent.futures.Future = concurrent.futures.Future()
                f.set_result(None)
                return f

        monkeypatch.setattr(hooks, "io_executor", lambda: _Pool())
        assert hooks.submit(lambda: None) is True
        assert inner == [False], (
            "the second caller was accepted while the first was still unregistered, "
            "so the outstanding set can exceed MAX_PENDING_APPENDS"
        )
        hooks._inflight.clear()

    def test_the_reservation_is_released_so_the_ceiling_does_not_shrink(self, monkeypatch):
        # CONTROL. Without this, never releasing a reservation would satisfy the test
        # above while lowering the ceiling by one on every append for the life of the
        # process, until nothing could be queued at all.
        import kiro_crew.eventlog_hooks as hooks

        monkeypatch.setattr(hooks, "MAX_PENDING_APPENDS", 2)
        monkeypatch.setattr(hooks, "_overflowing", False)
        monkeypatch.setattr(hooks, "_reserved", 0)
        hooks._inflight.clear()
        for _ in range(5):
            assert hooks.submit(lambda: None) is True
            hooks._inflight.clear()
        assert hooks._reserved == 0
