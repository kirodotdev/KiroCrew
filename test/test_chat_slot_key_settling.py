"""A ``linked_session_key`` rebind may not move the key an arm is settling onto.

The settle resolves against a key, then the arm is transferred onto it; a writer landing
between the two leaves the arm owed to a binding nobody is on. Seven of the eight writers are
synchronous, and one transfer already runs inside ``async with slot._lock``, so the exclusion
is that the settle cannot SUSPEND rather than a lock. A rebind that still lands, across the
reset's own await, is observed afterwards by comparing the key and directory the settle read.
"""

from __future__ import annotations

import inspect
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config.paths import CWD_CLEARED
from kiro_crew.dashboard.chat_utils import bind_linked_session_key


def _slot(key: str = "dashboard:test", linked: str = "", claim_cwd: str = ""):
    slot = SimpleNamespace()
    slot.key = key
    slot.linked_session_key = linked
    slot.claim_cwd = claim_cwd
    return slot


class TestEveryKeyWriteGoesThroughOneHelper:
    """The INVARIANT: one writer of ``linked_session_key``, so every committed link passes
    a single place that records its crew-log class.

    Discovery is from source because an unexercised writer is invisible at runtime.
    """

    def test_no_key_writer_sits_in_a_module_that_serializes_nothing(self):
        """Completeness only: a NEW writer must not appear where nothing serializes it.

        Discovery is from source because an unexercised writer is invisible at runtime, but
        the assertion is a SUBSET -- writers may DISAPPEAR freely, which is what dismantling
        does, while one added in a module that reaches the key with no serialization in sight
        fails. The serialization point is resolved from the live object, so moving or renaming
        it does not read as a stray writer.
        """
        import pathlib
        import sys

        import kiro_crew

        root = pathlib.Path(kiro_crew.__file__).parent
        assign = re.compile(r"\.linked_session_key\s*=(?!=)")
        point = pathlib.Path(sys.modules[bind_linked_session_key.__module__].__file__).resolve()

        strays = []
        seen_point = False
        for path in sorted(root.rglob("*.py")):
            text = path.read_text(encoding="utf-8")
            if not assign.search(text):
                continue
            serialized = "bind_linked_session_key" in text
            for n, line in enumerate(text.splitlines(), 1):
                if "self.linked_session_key" in line or not assign.search(line):
                    continue
                if path.resolve() == point:
                    seen_point = True
                    continue
                if serialized:
                    continue
                strays.append(f"{path.relative_to(root).as_posix()}:{n}: {line.strip()}")

        assert seen_point, (
            "no write was found inside the serialization point, so this pattern no longer "
            "matches the real thing and would report every module as clean"
        )
        assert not strays, (
            "these modules reach linked_session_key with nothing serializing them, so a "
            "rebind can move the key while an arm settles:\n  " + "\n  ".join(strays)
        )

    def test_the_slot_factory_binds_its_keyword_only_when_it_mints_the_slot(self, tmp_path):
        """The keyword surface cannot rebind a live slot, and no census above can see it.

        ``get_or_create_slot(linked_session_key=...)`` sets the key without ever naming the
        attribute, so the dotted-assignment census is structurally blind to it. What keeps it
        safe is not the guard but the factory's own early return for an EXISTING slot, which
        precedes the bind: the keyword therefore binds only the slot it mints, where no arm
        can yet be settling. Moving that bind above the early return would turn this surface
        into a rebind the census still reports as clean.
        """
        from chat_test_helpers import _make_state

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("slack:C123.456", linked_session_key="slack:C123.456")
        assert slot.linked_session_key == "slack:C123.456", (
            "the keyword did not bind the slot it minted, so a channel-born tab surfaces "
            "unbound and answers from a session no channel reads"
        )

        again = state.get_or_create_slot(slot.key, linked_session_key="cron:job-9")
        assert again is slot
        assert slot.linked_session_key == "slack:C123.456", (
            "the factory rebound a LIVE slot from its keyword, a surface no census here "
            "matches, so it can move the key while an arm settles onto the one it left"
        )


class TestTheSingleWriterBindsImmediately:
    def test_a_writer_binds_the_key_it_was_given(self):
        slot = _slot(linked="slack:1111.0001")
        bind_linked_session_key(slot, "cron:job-9")
        assert slot.linked_session_key == "cron:job-9", (
            "a rebind was withheld, which would strand every cron and workflow injection "
            "on the binding it replaced"
        )


class TestTheSettleCannotSuspend:
    def test_the_settle_completes_without_yielding_to_the_loop(self):
        """An await between the key this reads and the transfer is the whole defect class.

        Asserted on the CALL: one handing back its answer rather than something to await has
        already finished, so nothing can be interleaved. That also covers a plain ``def``
        returning a coroutine, which a signature check alone misses.
        """
        from kiro_crew.dashboard.chat_runner import _settle_and_transfer_arm

        settled = _settle_and_transfer_arm(
            MagicMock(),
            _slot(linked="slack:1111.0001"),
            "dashboard:test",
            None,
            publish=False,
        )

        assert not inspect.isawaitable(settled), (
            "the settle handed back something to await, so a rebind can land between the "
            "key it read and the transfer that publishes the arm onto it"
        )
        assert settled == (None, "slack:1111.0001")


class TestARebindCannotLaunderAppAuthorizationIntoARetry:
    @pytest.mark.asyncio
    async def test_a_newer_same_key_change_during_the_reset_is_kept_not_cleared(self):
        """The teardown awaits, so a second project change can land on the SAME key.

        A key-only comparison reads no supersession and clears the flag, discarding the newer
        change while the arm still names the directory this reset left -- which the next
        claim is then refused by and retried into. So the directory is compared too.
        """
        from kiro_crew.dashboard import chat_runner

        slot = SimpleNamespace(
            key="dashboard:test",
            _app=None,
            linked_session_key="",
            _pending_reset_history_key="dashboard:test",
            claim_cwd="/project/b",
            forget_session_model_state=MagicMock(),
        )

        async def _reset(*_a, **_k):
            slot.claim_cwd = "/project/c"
            return True

        settle = MagicMock(return_value=(None, "dashboard:test"))
        with (
            patch.object(chat_runner, "_settle_and_transfer_arm", settle),
            patch.object(chat_runner, "subagents_attached_async", AsyncMock(return_value=False)),
            patch.object(chat_runner, "_broadcast_expired_oauth_banners", MagicMock()),
            patch.object(chat_runner, "_arm_pending_reset_retry", MagicMock()) as retry,
        ):
            state = MagicMock()
            state.sessions.reset = _reset
            await chat_runner._consume_pending_reset(state, slot)

        assert slot._pending_reset_history_key == "dashboard:test", (
            "the newer project change was cleared without being applied, so the session "
            "keeps the directory the user has already left"
        )
        retry.assert_called_once()
        assert settle.call_args_list[-1].args[3] == "/project/c", (
            "the arm was left naming the superseded directory, so the next claim stating "
            "the new one is refused and retried into it"
        )

    @pytest.mark.asyncio
    async def test_a_refused_teardown_still_re_settles_the_superseded_arm(self):
        """A refused reset is the WORSE case: the session stays live on the directory the
        slot has left while the arm still names it, so the next claim is retried THERE."""
        from kiro_crew.dashboard import chat_runner

        slot = SimpleNamespace(
            key="dashboard:test",
            _app=None,
            linked_session_key="",
            _pending_reset_history_key="dashboard:test",
            claim_cwd="/project/b",
            forget_session_model_state=MagicMock(),
        )

        async def _refuse(*_a, **_k):
            slot.claim_cwd = "/project/c"
            return False

        settle = MagicMock(return_value=(None, "dashboard:test"))
        with (
            patch.object(chat_runner, "_settle_and_transfer_arm", settle),
            patch.object(chat_runner, "subagents_attached_async", AsyncMock(return_value=False)),
            patch.object(chat_runner, "_broadcast_expired_oauth_banners", MagicMock()),
            patch.object(chat_runner, "_arm_pending_reset_retry", MagicMock()) as retry,
        ):
            state = MagicMock()
            state.sessions.reset = _refuse
            torn_down = await chat_runner._consume_pending_reset(state, slot)

        assert torn_down is False, "a refused teardown must not report one"
        assert settle.call_args_list[-1].args[3] == "/project/c", (
            "the refused path left the arm naming the superseded directory, so the next "
            "claim stating the new one is refused and retried into it"
        )
        assert slot._pending_reset_history_key == "dashboard:test"
        retry.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_rebound_foreign_key_is_dropped_rather_than_rearmed(self):
        """The settle gate fires only where the landed key differs from the ARMED one, so
        re-arming a rebound key makes the next consume skip the app check it exists to run."""
        from kiro_crew.dashboard import chat_runner

        slot = SimpleNamespace(
            key="dashboard:test",
            _app="spec-builder",
            _pending_reset_history_key="dashboard:test",
            claim_cwd=None,
        )
        rebound = MagicMock(return_value=(None, "slack:1111.0001"))
        with (
            patch.object(chat_runner, "_settle_and_transfer_arm", rebound),
            patch.object(chat_runner, "effective_session_key", lambda _s: "slack:1111.0001"),
            patch.object(chat_runner, "sel", MagicMock()),
            patch.object(chat_runner, "_arm_pending_reset_retry", MagicMock()) as retry,
        ):
            await chat_runner._consume_pending_reset(MagicMock(), slot)

        assert slot._pending_reset_history_key is None, (
            "the rebound foreign key was armed as this app's own, so the retry tears down "
            "slack:1111.0001 with no authorization check left to run"
        )
        retry.assert_not_called()


def test_a_spent_arm_is_reaped_only_where_no_start_can_still_read_its_generation():
    from kiro_crew.session import SessionManager
    from kiro_crew.session_allocation import SessionRegistryState

    mgr = object.__new__(SessionManager)
    mgr._allocation_state = SessionRegistryState()
    alloc = mgr._allocation_boundary()
    alloc._allocation_reservations["busy"] = {object()}
    for key in ("idle", "busy"):
        alloc.supersede_arm_for_new_slot(key)
    assert "idle" not in alloc.state.retire_arms
    assert alloc.state.retire_arms["busy"].generation == 1


def test_a_cleared_project_resumes_its_conversation_under_the_default_directory():
    """A clear moves the DIRECTORY. Dropping the resume SID as well replaced the native
    conversation on every later claim, because the cleared marker persists: the pool bypass
    already keeps a cleared claim off a warm child, so the SID needs no second defence."""
    from kiro_crew.session_allocation import SessionAllocationService

    src = inspect.getsource(SessionAllocationService._get_or_create_impl)
    assert "cwd_blocks_pool = cwd == CWD_CLEARED" in src, "read the wrong function"
    after_read = src.split("resume_sid = owner._session_map.get(key)", 1)[1]
    guard = after_read.split("cwd_blocks_pool", 1)[0]
    assert "clear_sid" not in guard, "the cleared-claim resume guard discards the conversation"


@pytest.mark.parametrize(
    "raw_bound,cwd,moved",
    [
        ("/a/b", None, False),
        ("", "/a/b", False),
        (None, "/a/b", False),
        ("/a/b/", "/a/b", False),
        ("/a/b", "/a/c", True),
        ("/a/b", CWD_CLEARED, False),
    ],
)
def test_the_reuse_comparison_moves_only_on_a_real_disagreement(raw_bound, cwd, moved):
    """Driven directly, because it runs on EVERY reuse and fails silently.

    A false move evicts a warm session, so the slot cold-starts each turn or exhausts its
    retry budget with nothing raised. The cases that must NOT move are the ones that cost: a
    claim stating no requirement, a provider reporting no readable directory, two spellings
    of one directory -- which is why both sides share one normalization -- and a CLEARED
    claim, whose directory only the cold start can name, since ``prepare_runtime``
    substitutes an enrolled member's configured workspace for an empty cwd.
    """
    from kiro_crew.session_allocation import cwd_moved_for_reuse

    assert cwd_moved_for_reuse(raw_bound, cwd) is moved


def test_a_stale_start_does_not_drain_what_a_forced_stop_parked():
    """Adoption EMPTIES the parked store, and a stale start is evicted moments later.

    Pinned on the source because reproducing it needs a forced stop to race a project
    change: the eviction unlinks the queue adoption moved those entries onto, so the retry
    finds the store already empty and the parked messages are gone with nothing logged.
    """
    from kiro_crew.session_allocation import SessionAllocationService

    src = inspect.getsource(SessionAllocationService._get_or_create_impl)
    guard = "if not stale_generation and adopt_parked_queue(session, key):"
    assert guard in src, "a stale start must be refused adoption at its registration"
    assert src.index("stale_generation = True") < src.index(guard), (
        "the stale verdict must be reached BEFORE adoption, or the guard reads an "
        "initial False that is not yet the answer"
    )


def test_a_cleared_project_retires_the_stored_directory_not_just_the_arm():
    """The arm is in MEMORY. The stored directory survives a restart, and a cwd-less claim
    restores it -- which every channel turn is, so persisting the retirement is the fix."""
    from kiro_crew.session import SessionManager
    from kiro_crew.session_allocation import SessionRegistryState

    mgr = object.__new__(SessionManager)
    mgr._allocation_state = SessionRegistryState()
    mgr._session_map = MagicMock()
    alloc = mgr._allocation_boundary()

    alloc.mark_retire_on_next_claim("dashboard:test", "/project/b")
    assert (
        mgr._session_map.clear_cwd.call_count == 0
    ), "a project MOVE does not retire the directory; the new one is stated by the claim"

    alloc.mark_retire_on_next_claim("dashboard:test", CWD_CLEARED)
    mgr._session_map.clear_cwd.assert_called_once_with("dashboard:test")


class TestTheChildrenProbeCoversTheKeyTheArmLandsOn:
    @pytest.mark.asyncio
    async def test_a_rebind_probes_children_on_the_key_the_arm_is_transferred_to(self):
        """The arm lands on the key the slot runs on NOW, so probing only the abandoned one
        arms a live session whose next claim evicts it, losing its attached sub-agents."""
        from kiro_crew.dashboard import chat_runner

        slot = SimpleNamespace(
            key="dashboard:test",
            _app=None,
            linked_session_key="cron:job-9",
            _pending_reset_history_key="dashboard:test",
            claim_cwd="/project/b",
            forget_session_model_state=MagicMock(),
        )
        probed: list[str] = []

        async def _attached(_state, _slot, probe_key, _why):
            probed.append(probe_key)
            return probe_key == "cron:job-9"

        settle = MagicMock(return_value=(None, "cron:job-9"))
        with (
            patch.object(chat_runner, "_settle_and_transfer_arm", settle),
            patch.object(chat_runner, "subagents_attached_async", _attached),
            patch.object(chat_runner, "_arm_pending_reset_retry", MagicMock()),
        ):
            await chat_runner._consume_pending_reset(MagicMock(), slot)

        assert "cron:job-9" in probed, (
            "the key the arm lands on was never probed for children, so the publish can "
            "evict a session holding attached sub-agents"
        )
        assert (
            settle.call_args_list[-1].kwargs["publish"] is False
        ), "children are attached to the key the arm would land on, so nothing may publish"


def test_a_cleared_arm_stores_the_requirement_rather_than_a_guess_at_it():
    """``resolved_cwd(CWD_CLEARED, key)`` answers the per-session default, which is a guess.

    ``prepare_runtime`` substitutes an enrolled member's configured workspace for an empty
    cwd, so an arm carrying that default contradicts the binding such a start reports: the
    start reads stale, and its retry re-states the default as a REAL path, which stops the
    substitution firing and raises ``project_identity_changed`` on every later send. Storing
    it unresolved also keeps ``resolved_cwd``'s ``workspace_root`` read and mkdir off the
    event loop the publishing callers run on.
    """
    from kiro_crew.session import SessionManager
    from kiro_crew.session_allocation import SessionRegistryState

    mgr = object.__new__(SessionManager)
    mgr._allocation_state = SessionRegistryState()
    mgr._session_map = MagicMock()
    alloc = mgr._allocation_boundary()
    alloc.mark_retire_on_next_claim("dashboard:test", CWD_CLEARED)

    assert alloc.state.retire_arms["dashboard:test"].cwd == CWD_CLEARED


def test_an_equivalent_cleared_rearm_does_not_supersede_an_in_flight_start():
    """The deferred-reset retry re-arms the same target every few seconds, and each
    generation bump rejects a cold start still resolving its own model. So recording and the
    same-key equivalence check must agree on the stored form: normalize on one side only and
    every pass reads as a new requirement. A DIFFERENT target must still supersede."""
    from kiro_crew.session import SessionManager
    from kiro_crew.session_allocation import SessionRegistryState

    mgr = object.__new__(SessionManager)
    mgr._allocation_state = SessionRegistryState()
    mgr._session_map = MagicMock()
    alloc = mgr._allocation_boundary()

    alloc.transfer_retire_arm("dashboard:test", "dashboard:test", CWD_CLEARED)
    armed = alloc.state.retire_arms["dashboard:test"].generation
    alloc.transfer_retire_arm("dashboard:test", "dashboard:test", CWD_CLEARED)
    assert alloc.state.retire_arms["dashboard:test"].generation == armed

    alloc.transfer_retire_arm("dashboard:test", "dashboard:test", "/a/b")
    assert alloc.state.retire_arms["dashboard:test"].generation > armed


def test_a_cleared_arm_is_satisfied_by_a_cleared_claim_not_by_a_resolved_path():
    """Only the cold start knows where a cleared project lands, so nothing earlier may guess.

    ``prepare_runtime`` substitutes an enrolled member's configured workspace for an empty
    cwd. An arm carrying a per-session default resolved before that therefore contradicts
    the binding, the registration refuses the start, and the retry re-states the resolved
    default -- which is non-empty, so the substitution does not fire and
    ``prepare_member_capabilities`` raises ``project_identity_changed`` on every later send.

    Pinned on the source because reaching it needs an enrolled member whose saved Parent
    names its configured workspace, plus a project clear racing that member's cold start.
    """
    from kiro_crew.session_allocation import SessionAllocationService

    src = inspect.getsource(SessionAllocationService._get_or_create_impl)
    anchor = "armed_cwd = claim_arm.cwd if claim_arm is not None else None"
    assert anchor in src, "read the wrong function"
    verdict = src.split(anchor, 1)[1].split("stale_generation = True", 1)[0]
    assert "cwd != CWD_CLEARED" in verdict, (
        "a cleared arm is compared against a path resolved here, so an enrolled member's "
        "own binding contradicts it and every send after the clear fails"
    )
