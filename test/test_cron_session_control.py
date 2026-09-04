"""Cron callers on the session-control surface (issue #8332).

A cron job's own slot is admitted as a SOURCE even though nobody is watching it,
and is bounded by the same ``created_by`` fence a crew member is. These tests pin
the four halves of that contract:

* the admission (a ``cron-`` caller passes the unattended refusal; a
  ``workflow-`` caller still does not),
* the fence (a cron reaches the sessions it created and nothing else, including
  another job's tab),
* the link exemption (a ``cron:<job_id>`` link is not a channel link, while a
  real channel link still refuses),
* the origin of what it creates (``SlotOrigin.CRON``, so a cron cannot launder
  its output into the ``slots:user`` scope its own slot is kept out of).

Assertions run against REAL slot objects for the same reason the rest of the
suite does: the guards read ``linked_session_key`` / ``_created_by`` / ``_origin``
off the production class, and a permissive double would let a dead guard look
alive.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from chat_test_helpers import _make_state

from kiro_crew.dashboard import create_rate_limit
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.dashboard.state import SlotOrigin
from kiro_crew.members import DM_SLOT_KEY_PREFIX

JOB_ID = "2c3b2e25"
CRON_SLOT = f"cron-{JOB_ID}"
CRON_KEY = f"cron:{JOB_ID}"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    """Default to the shipped (enabled) state without reading config."""
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


@pytest.fixture(autouse=True)
def _fresh_create_budget():
    """The per-caller create-rate window is process-wide module state."""
    create_rate_limit.reset_for_tests()
    yield
    create_rate_limit.reset_for_tests()


def _cron_tab(state, job_id: str = JOB_ID, *, created_by: str = ""):
    """A cron job's own tab, as ``inject_cron_result_to_dashboard`` mints it.

    Registers the owning job too, because ownership is read from the JOB and an
    unfindable one is refused (`cron_owner_unverifiable`) -- a cron tab whose job
    the registry cannot produce is not a caller. Note what the mint does NOT pass:
    no ``app=``, which is exactly why the `_app` check cannot see an app's cron.

    The agent is left empty on purpose: the child inherits the caller's, and a
    name that does not resolve in the test config is refused (``agent_unresolved``)
    before the gates under test are reached.
    """
    jobs = list(state.crons.list_jobs.return_value or [])
    jobs.append(SimpleNamespace(id=job_id, created_by=created_by))
    state.crons.list_jobs.return_value = jobs
    return state.get_or_create_slot(
        f"cron-{job_id}",
        linked_session_key=f"cron:{job_id}",
        origin=SlotOrigin.CRON,
    )


# ── Predicates ───────────────────────────────────────────────────────────────


class TestCronCallerPredicate:
    def test_a_cron_slot_key_is_a_cron_caller(self):
        assert sc._cron_caller(CRON_SLOT)

    def test_workflow_and_ordinary_and_member_slots_are_not(self):
        assert not sc._cron_caller("workflow-run7")
        assert not sc._cron_caller("chat-1-abc")
        assert not sc._cron_caller(DM_SLOT_KEY_PREFIX + "radar")
        assert not sc._cron_caller("")

    def test_workflow_is_still_an_unattended_prefix(self):
        """The refusal set keeps workflow, so a later prefix is refused by default."""
        assert "workflow-" in sc.UNATTENDED_SLOT_PREFIXES
        assert "cron-" in sc.UNATTENDED_SLOT_PREFIXES


class TestOwnershipFencedPredicate:
    """One predicate covers every fenced caller, so admission cannot outrun the fence."""

    def test_both_exempted_caller_classes_are_fenced(self, tmp_path):
        state = _make_state(tmp_path)
        assert sc._caller_is_ownership_fenced(state, CRON_SLOT)
        assert sc._caller_is_ownership_fenced(state, DM_SLOT_KEY_PREFIX + "radar")

    def test_a_human_created_caller_is_not_fenced(self, tmp_path):
        """A person's own tab reaches get_or_create_slot directly and stays unattributed."""
        state = _make_state(tmp_path)
        state.get_or_create_slot("chat-1")
        assert not sc._caller_is_ownership_fenced(state, "chat-1")

    def test_a_session_a_fenced_caller_created_is_fenced_too(self, tmp_path):
        """The deputy hole: the child has a chat- key but inherits the creator's agent."""
        state = _make_state(tmp_path)
        child = state.get_or_create_slot("chat-9")
        child._created_by = CRON_SLOT
        assert sc._caller_is_ownership_fenced(state, "chat-9")

    def test_a_grandchild_is_fenced_without_walking_the_chain(self, tmp_path):
        """`_created_by` is written only by create_session, so depth needs no walk."""
        state = _make_state(tmp_path)
        child = state.get_or_create_slot("chat-9")
        child._created_by = CRON_SLOT
        grandchild = state.get_or_create_slot("chat-10")
        grandchild._created_by = "chat-9"
        assert sc._caller_is_ownership_fenced(state, "chat-10")

    def test_sticky_attendance_does_not_release_the_fence(self, tmp_path):
        """`_human_seen` records that a human EVER typed, not who authored this turn.

        Releasing on it would hand the creator its deputy back for the price of the
        user glancing at the tab once: cron creates the child, the user types into
        it, and every later cron-authored turn in that child runs unfenced.
        """
        state = _make_state(tmp_path)
        child = state.get_or_create_slot("chat-9")
        child._created_by = CRON_SLOT
        child._human_seen = True
        assert sc._caller_is_ownership_fenced(state, "chat-9")


class TestCreatedChildIsNotAnUnfencedDeputy:
    """A cron must not get an unfenced deputy by creating one (GPT, head a0c88b594)."""

    def test_the_child_cannot_reach_a_session_it_did_not_create(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _cron_tab(state)
        child = state.get_or_create_slot("chat-9", workspace=caller.workspace)
        child._created_by = CRON_SLOT
        state.get_or_create_slot("chat-2", workspace=caller.workspace)

        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state, caller_session_key="chat-9", target="chat-2", operation="read"
            )
        assert exc.value.code == "not_creator"
        assert "agent-created session" in exc.value.message

    def test_the_child_still_reaches_its_own_children(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _cron_tab(state)
        child = state.get_or_create_slot("chat-9", workspace=caller.workspace)
        child._created_by = CRON_SLOT
        grandchild = state.get_or_create_slot("chat-10", workspace=caller.workspace)
        grandchild._created_by = "chat-9"

        resolved = sc.authorize_target(
            state, caller_session_key="chat-9", target="chat-10", operation="send"
        )
        assert resolved is grandchild


# ── Creator eligibility ──────────────────────────────────────────────────────


class TestRefuseIneligibleCreator:
    def test_a_cron_link_does_not_refuse(self, tmp_path):
        """A cron tab's link names its own run transcript, not a channel thread."""
        state = _make_state(tmp_path)
        sc._refuse_ineligible_creator(state, _cron_tab(state))

    def test_a_real_channel_link_still_refuses(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-9", linked_session_key="slack:C123:171.2")
        with pytest.raises(sc.SessionControlError) as exc:
            sc._refuse_ineligible_creator(state, slot)
        assert exc.value.code == "linked_session_caller"

    def test_an_app_scoped_cron_tab_is_still_refused(self, tmp_path):
        """The link exemption widens one refusal, not the set."""
        state = _make_state(tmp_path)
        slot = _cron_tab(state)
        slot._app = "some-app"
        with pytest.raises(sc.SessionControlError) as exc:
            sc._refuse_ineligible_creator(state, slot)
        assert exc.value.code == "app_scoped_caller"


class TestAppOwnedCron:
    """An app must not escape its confinement through its own scheduled job.

    ``inject_cron_result_to_dashboard`` mints the tab without ``app=``, so the
    ``_app`` refusal cannot see an app's cron. Ownership is read from the job.
    """

    def test_an_app_owned_cron_cannot_create_a_session(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _cron_tab(state, created_by="app:issue-radar")
        with pytest.raises(sc.SessionControlError) as exc:
            sc._refuse_ineligible_creator(state, caller)
        assert exc.value.code == "app_owned_cron_caller"

    def test_an_app_owned_cron_cannot_control_a_session_it_created(self, tmp_path):
        """Refused on the control path too, so the two caller halves stay mirrors."""
        state = _make_state(tmp_path)
        caller = _cron_tab(state, created_by="app:issue-radar")
        worker = state.get_or_create_slot("chat-7", workspace=caller.workspace)
        worker._created_by = CRON_SLOT

        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state, caller_session_key=CRON_KEY, target="chat-7", operation="send"
            )
        assert exc.value.code == "app_owned_cron_caller"

    def test_an_app_owned_cron_is_refused_before_the_target_is_resolved(self, tmp_path):
        """A caller refused for its own identity must learn nothing from the attempt.

        Resolving first makes the refusal an existence oracle: a guessed target
        answers `target_not_found` when it does not exist and this refusal when it
        does, so a caller allowed to touch nothing could enumerate session keys and
        titles by the shape of the error. Same code either way is the assertion.

        Mutation guard: move the refusal back below `_resolve_slot` and the
        nonexistent-target case answers `target_not_found` instead.
        """
        state = _make_state(tmp_path)
        caller = _cron_tab(state, created_by="app:issue-radar")
        state.get_or_create_slot("chat-7", workspace=caller.workspace)

        codes = []
        for target in ("chat-7", "chat-does-not-exist"):
            with pytest.raises(sc.SessionControlError) as exc:
                sc.authorize_target(
                    state, caller_session_key=CRON_KEY, target=target, operation="read"
                )
            codes.append(exc.value.code)
        assert codes == ["app_owned_cron_caller", "app_owned_cron_caller"]

    def test_a_user_owned_cron_is_unaffected(self, tmp_path):
        """`created_by` also carries a Slack user id, which is not an app."""
        state = _make_state(tmp_path)
        caller = _cron_tab(state, created_by="U0123ABCD")
        sc._refuse_ineligible_creator(state, caller)

    def test_a_job_authored_by_an_app_session_is_refused(self, tmp_path):
        """The second spelling of app ownership.

        `mcp_cron.cron_add` records the calling session in `session_key` and never
        writes `created_by`, so an app-scoped session's job carries its authority
        only there. Reading `created_by` alone let it through.

        Mutation guard: drop the session_key branch and this passes silently.
        """
        state = _make_state(tmp_path)
        app_session = state.get_or_create_slot("chat-42", app="issue-radar")
        caller = _cron_tab(state, created_by="")
        state.crons.list_jobs.return_value = [
            SimpleNamespace(id=JOB_ID, created_by="", session_key="dashboard:chat-42")
        ]
        assert app_session._app == "issue-radar"

        with pytest.raises(sc.SessionControlError) as exc:
            sc._refuse_ineligible_creator(state, caller)
        assert exc.value.code == "app_owned_cron_caller"

    def test_a_job_authored_by_an_ordinary_session_is_unaffected(self, tmp_path):
        state = _make_state(tmp_path)
        state.get_or_create_slot("chat-42")
        caller = _cron_tab(state, created_by="")
        state.crons.list_jobs.return_value = [
            SimpleNamespace(id=JOB_ID, created_by="", session_key="dashboard:chat-42")
        ]
        sc._refuse_ineligible_creator(state, caller)

    def test_a_job_whose_authoring_session_has_closed_fails_closed(self, tmp_path):
        """The confinement escape GPT flagged (backend-security-controls).

        ``mcp_cron.cron_add`` records an app's authority ONLY in ``session_key``
        (it never writes ``created_by``). The app then closes its session, so the
        owning slot is gone and its ``_app`` tag is unreadable. Before this fix the
        ``owning_slot is not None`` guard failed open and returned ``None``, so the
        app's own cron could mint a persistent, non-app, sidebar-visible session --
        precisely the confinement the ``_app`` refusal exists to prevent, reached
        through a closed session the gate can no longer inspect.

        "Cannot verify the owner is not an app" must fail closed, the same
        direction the missing-job and unreadable-registry cases already take.

        Mutation guard: restore the ``owning_slot is not None and`` short-circuit
        (drop the unresolvable-owner refusal) and this passes silently, letting the
        escape back in.
        """
        state = _make_state(tmp_path)
        caller = _cron_tab(state, created_by="")
        # The job names a session that no longer has a live slot.
        state.crons.list_jobs.return_value = [
            SimpleNamespace(id=JOB_ID, created_by="", session_key="dashboard:chat-gone")
        ]
        with pytest.raises(sc.SessionControlError) as exc:
            sc._refuse_ineligible_creator(state, caller)
        assert exc.value.code == "cron_owner_unverifiable"

    def test_an_unfindable_job_fails_closed(self, tmp_path):
        """A tab whose job the registry cannot produce proves no ownership."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot(
            CRON_SLOT, linked_session_key=CRON_KEY, origin=SlotOrigin.CRON
        )
        with pytest.raises(sc.SessionControlError) as exc:
            sc._refuse_ineligible_creator(state, slot)
        assert exc.value.code == "cron_owner_unverifiable"

    def test_an_unreadable_registry_fails_closed(self, tmp_path):
        """ "Could not verify" must not read as "has no owner"."""
        state = _make_state(tmp_path)
        caller = _cron_tab(state)
        state.crons.list_jobs.side_effect = RuntimeError("registry unavailable")
        with pytest.raises(sc.SessionControlError) as exc:
            sc._refuse_ineligible_creator(state, caller)
        assert exc.value.code == "cron_owner_unverifiable"

    def test_an_ordinary_caller_needs_no_job_lookup(self, tmp_path):
        """The lookup is scoped to cron callers; nothing else pays for it."""
        state = _make_state(tmp_path)
        state.crons.list_jobs.side_effect = AssertionError("must not be consulted")
        sc._refuse_ineligible_creator(state, state.get_or_create_slot("chat-1"))


class TestCreateSessionAdmission:
    def test_a_workflow_caller_is_still_refused_as_unattended(self, tmp_path):
        state = _make_state(tmp_path)
        state.get_or_create_slot("workflow-run7", linked_session_key="workflow:run7")
        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(sc.create_session(state, caller_session_key="workflow:run7"))
        assert exc.value.code == "unattended_caller"

    def test_a_cron_caller_passes_the_unattended_refusal(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        caller = _cron_tab(state)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)

        result = asyncio.run(sc.create_session(state, caller_session_key=CRON_KEY))

        assert state.get_slot(result["target"]) is not None

    def test_the_global_switch_still_gates_a_cron_caller(self, tmp_path, monkeypatch):
        """Unlike a member, a cron gets no bypass of ``agent.session_control``."""
        state = _make_state(tmp_path)
        _cron_tab(state)
        monkeypatch.setattr(sc, "session_control_enabled", lambda: False)
        with pytest.raises(sc.SessionControlError) as exc:
            asyncio.run(sc.create_session(state, caller_session_key=CRON_KEY))
        assert exc.value.code == "session_control_disabled"

    def test_the_created_session_is_attributed_to_the_job(self, tmp_path, monkeypatch):
        """``_created_by`` is the fence's only input, so the create must write it."""
        state = _make_state(tmp_path)
        caller = _cron_tab(state)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)

        result = asyncio.run(sc.create_session(state, caller_session_key=CRON_KEY))

        assert state.get_slot(result["target"])._created_by == CRON_SLOT


class TestCreatedSessionOrigin:
    """A cron must not reach ``slots:user`` by creating a session and writing there."""

    def test_a_cron_creates_a_cron_origin_child(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        caller = _cron_tab(state)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)

        result = asyncio.run(sc.create_session(state, caller_session_key=CRON_KEY))

        child = state.get_slot(result["target"])
        assert child._origin == SlotOrigin.CRON, "a USER label would enter slots:user"

    def test_an_ordinary_caller_still_creates_a_user_origin_child(self, tmp_path, monkeypatch):
        """The narrowing is scoped to cron lineage; nothing else changes."""
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1", origin=SlotOrigin.USER)
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: caller.workspace)

        result = asyncio.run(sc.create_session(state, caller_session_key=slot_history_key(caller)))

        assert state.get_slot(result["target"])._origin == SlotOrigin.USER

    def test_a_grandchild_of_a_cron_stays_cron_origin(self, tmp_path, monkeypatch):
        """The tag must follow authority, not the caller's key prefix.

        A child inherits its creator's agent, so a cron's child can itself call
        this verb -- and its caller key is a plain `chat-`. Keying only on the
        prefix mints that grandchild USER, which `ws_event_scope` then delivers to
        any app holding `slots:user`: the two-hop version of the laundering route
        the CRON tag exists to close.

        Mutation guard: drop the `_origin` half of the condition and this fails.
        """
        state = _make_state(tmp_path)
        cron = _cron_tab(state)
        child = state.get_or_create_slot("chat-9", workspace=cron.workspace, origin=SlotOrigin.CRON)
        child._created_by = CRON_SLOT
        monkeypatch.setattr(sc, "_workspace_name_for_dir", lambda cfg, ws_dir: child.workspace)

        result = asyncio.run(sc.create_session(state, caller_session_key="chat-9"))

        grandchild = state.get_slot(result["target"])
        assert grandchild._origin == SlotOrigin.CRON, "cron output would enter slots:user"


# ── The fence ────────────────────────────────────────────────────────────────


class TestAuthorizeTargetCronPath:
    def _authorize(self, state, target: str, operation: str = "send"):
        return sc.authorize_target(
            state, caller_session_key=CRON_KEY, target=target, operation=operation
        )

    def test_a_cron_reaches_a_session_it_created(self, tmp_path):
        state = _make_state(tmp_path)
        caller = _cron_tab(state)
        worker = state.get_or_create_slot("chat-7", workspace=caller.workspace)
        worker._created_by = CRON_SLOT

        assert self._authorize(state, "chat-7") is worker

    def test_a_cron_cannot_reach_a_session_it_did_not_create(self, tmp_path):
        """The user's own conversation, which is what the old refusal protected."""
        state = _make_state(tmp_path)
        caller = _cron_tab(state)
        state.get_or_create_slot("chat-7", workspace=caller.workspace)

        with pytest.raises(sc.SessionControlError) as exc:
            self._authorize(state, "chat-7")
        assert exc.value.code == "not_creator"
        assert "scheduled run" in exc.value.message

    def test_a_cron_cannot_reach_another_jobs_tab(self, tmp_path):
        """``unattended_target`` stands: a cron tab is never addressable."""
        state = _make_state(tmp_path)
        _cron_tab(state)
        other = _cron_tab(state, "99887766")
        other._created_by = CRON_SLOT  # even if it somehow carried our attribution

        with pytest.raises(sc.SessionControlError) as exc:
            self._authorize(state, other.key)
        assert exc.value.code == "unattended_target"

    def test_a_cron_tab_with_a_real_channel_link_is_still_refused(self, tmp_path):
        """The exemption is the cron link, not the field being non-empty."""
        state = _make_state(tmp_path)
        caller = _cron_tab(state)
        caller.linked_session_key = "slack:C123:171.2"
        worker = state.get_or_create_slot("chat-7", workspace=caller.workspace)
        worker._created_by = CRON_SLOT

        with pytest.raises(sc.SessionControlError) as exc:
            sc.authorize_target(
                state,
                caller_session_key=caller.key,
                target="chat-7",
                operation="send",
            )
        assert exc.value.code == "linked_session_caller"

    def test_an_unowned_target_fails_closed(self, tmp_path):
        """An ownerless rehydrate must not read as ours."""
        state = _make_state(tmp_path)
        caller = _cron_tab(state)
        worker = state.get_or_create_slot("chat-7", workspace=caller.workspace)
        worker._created_by = ""

        with pytest.raises(sc.SessionControlError) as exc:
            self._authorize(state, "chat-7")
        assert exc.value.code == "not_creator"
