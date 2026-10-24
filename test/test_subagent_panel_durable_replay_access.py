"""Who may read a persisted run, and the record each decision leaves.

Ownership and visibility both bound a rebuilt record before the row cap: the pre-cap
decision and both of its readings, the caller's filter, the saturated scan window, an app
token that cannot read another app's runs, the recheck on the loop and a lazily hydrated
slot that is not a breach. Redaction runs before clamping, and every grant and refusal on
the persisted listing leaves a SEL record. The shared helpers live in
``subagent_panel_durable_replay_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

import pytest
from subagent_panel_durable_replay_helpers import (  # noqa: F401 -- the fixture is requested by name
    DAY,
    TRUNC,
    agent_root_fixture,
    ids,
    panel,
    write_record,
)

from kiro_crew.dashboard import ws_event_scope as scope_module
from kiro_crew.dashboard.chat_utils import subagent_event_slot
from kiro_crew.dashboard.handlers import messaging
from kiro_crew.dashboard.ws_event_scope import (
    persisted_precap_denial_reason,
    persisted_precap_readings,
    persisted_replay_denial_reason,
    persisted_snapshot_denial_reason,
    slot_owner_snapshot,
    visible_subagent_slot_keys,
)
from kiro_crew.subagent_persistence import _PANEL_RESULT_CAP, _PANEL_TASK_CAP, read_panel_records


class TestAGrantIsAuditedNotOnlyARefusal:
    """Recording only refusals shows what was blocked and never what was released.

    Every refusal on this route leaves a SEL record, so a stream that omits the
    admissions cannot answer who was handed a persisted run's text -- which is the
    question an operator reviewing it has.
    """

    def decisions(self, monkeypatch) -> list[tuple[str, str, str]]:
        calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            messaging, "_audit_deny", lambda app, event, reason: calls.append((app, event, reason))
        )
        monkeypatch.setattr(
            messaging, "_audit_allow", lambda app, event: calls.append((app, event, "granted"))
        )
        return calls

    def request(self, state, caller: str, app: str = "", *, internal: bool = True):
        values: dict[str, object] = {"app": app, "internal_auth": internal}
        return SimpleNamespace(
            app={"state": state},
            headers={"X-Session-Key": caller},
            query={},
            get=lambda key, default=None: values.get(key, default),
        )

    def state(self, slot_app: str):
        slots = {"chat-1": SimpleNamespace(_app=slot_app)}
        return SimpleNamespace(
            subagents=SimpleNamespace(all_agents=[]), _slots=slots, get_slot=slots.get
        )

    def listing(
        self, monkeypatch, *, slot_app: str, caller: str, scope: object, internal: bool = True
    ):
        calls = self.decisions(monkeypatch)

        async def fake_scope(request, op):
            return scope, None

        monkeypatch.setattr(messaging, "internal_memory_scope", fake_scope)
        payload = asyncio.get_event_loop().run_until_complete(
            messaging.api_spawn_list(self.request(self.state(slot_app), caller, internal=internal))
        )
        return calls, json.loads(payload.text or "{}")

    def test_an_admitted_record_leaves_a_grant_record(self, agent_root, monkeypatch):
        write_record(agent_root, "grant11", app="", parent_session="dashboard:chat-1")
        calls, body = self.listing(
            monkeypatch, slot_app="", caller="dashboard:chat-1", scope=object()
        )
        assert [entry["id"] for entry in body["agents"]] == ["grant11"]
        assert [reason for _app, _event, reason in calls] == ["granted"]

    def test_a_refused_record_leaves_no_grant_record(self, agent_root, monkeypatch):
        """The two are exclusive, so a refusal must not also read as a release."""
        write_record(agent_root, "grant22", app="appA", parent_session="dashboard:chat-1")
        calls, body = self.listing(
            monkeypatch, slot_app="appB", caller="dashboard:chat-1", scope=object()
        )
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_owner_mismatch"]


class TestVisibilityBoundsTheCapNotOnlyOwnership:
    """Ownership and visibility are independent, and both belong before the cap.

    A record can name a slot the caller owns while carrying an event the caller
    never declared, and it can be visible under a declaration while belonging to
    another app. Sizing the cap on ownership alone lets a burst of records this
    socket may not see spend the slots its own visible runs need, and the cut count
    is then computed over records that were never its to receive.
    """

    def slot(self, app: str, origin: str = ""):
        return SimpleNamespace(_app=app, _origin=origin)

    def state(self, slots: dict):
        return SimpleNamespace(_slots=slots, get_slot=slots.get)

    def unrevoked(self, monkeypatch):
        """Pin the revocation answer, which is otherwise cache-warmth dependent.

        ``app_events_revoked`` reports NOT revoked on a cold cache and schedules a
        refresh, so an own-slot answer read twice in one process can legitimately
        differ. A test that does not pin it is measuring cache warmth.
        """
        monkeypatch.setattr(scope_module, "app_events_revoked", lambda _app: False)

    def test_the_dashboard_user_sees_every_live_slot(self, monkeypatch):
        self.unrevoked(monkeypatch)
        state = self.state({"chat-1": self.slot(""), "chat-2": self.slot("appA")})
        keys = visible_subagent_slot_keys(state, "", frozenset(), dashboard_user=True)
        assert keys == {"chat-1", "chat-2"}

    def test_an_app_sees_its_own_slot(self, monkeypatch):
        self.unrevoked(monkeypatch)
        state = self.state({"chat-1": self.slot("appA"), "chat-2": self.slot("appB")})
        keys = visible_subagent_slot_keys(state, "appA", frozenset(), dashboard_user=False)
        assert keys == {"chat-1"}

    def test_a_revoked_app_loses_even_its_own_slot(self, monkeypatch):
        """The own-slot branch is the one revocation has to reach."""
        monkeypatch.setattr(scope_module, "app_events_revoked", lambda _app: True)
        state = self.state({"chat-1": self.slot("appA")})
        keys = visible_subagent_slot_keys(state, "appA", frozenset(), dashboard_user=False)
        assert keys == set()

    def test_a_declaration_widens_it(self, monkeypatch):
        self.unrevoked(monkeypatch)
        state = self.state({"chat-1": self.slot("appA"), "chat-2": self.slot("appB")})
        keys = visible_subagent_slot_keys(
            state, "appA", frozenset({"subagent:all"}), dashboard_user=False
        )
        assert keys == {"chat-1", "chat-2"}

    def test_a_caller_that_is_neither_sees_nothing(self, monkeypatch):
        """Fail closed: no app claim and not the dashboard user admits no slot."""
        self.unrevoked(monkeypatch)
        state = self.state({"chat-1": self.slot("appA")})
        keys = visible_subagent_slot_keys(state, "", frozenset(), dashboard_user=False)
        assert keys == set()


class TestThePreCapDecisionIsBothBounds:
    """One function, two independent halves, so neither can be forgotten.

    A record can name a slot this client owns while being invisible to it, and it
    can be visible while belonging to another app. Sizing the cap on either half
    alone lets the other's records spend slots the client's own runs need.
    """

    def record(self, app: str, parent: str = "dashboard:chat-1") -> dict:
        return {"id": "pre111", "app": app, "parent_session": parent}

    def key(self, record: dict) -> str:
        return subagent_event_slot(str(record["parent_session"]))

    @pytest.mark.parametrize(
        ("app", "parent", "visible", "reason"),
        [
            pytest.param(
                "appA", "dashboard:chat-1", {"chat-1"}, "", id="test_both_bounds_satisfied_admits"
            ),
            pytest.param(
                "appA",
                "dashboard:chat-1",
                set(),
                "persisted_not_visible",
                id="test_invisible_is_refused_even_when_the_owner_matches",
            ),
            pytest.param(
                "appB",
                "dashboard:chat-1",
                {"chat-1"},
                "persisted_owner_mismatch",
                id="test_a_foreign_owner_is_refused_even_when_visible",
            ),
            # A declaration gap must not enter the stream as a cross-app breach.
            pytest.param(
                "appB",
                "dashboard:chat-1",
                set(),
                "persisted_not_visible",
                id="test_visibility_is_reported_first_when_both_fail",
            ),
            pytest.param(
                "appA",
                "dashboard:chat-9",
                {"chat-9"},
                "slot_missing",
                id="test_a_slot_no_snapshot_holds_is_slot_missing_when_visible",
            ),
        ],
    )
    def test_precap_denial_reason(self, app, parent, visible, reason):
        rec = self.record(app, parent=parent)
        assert (
            persisted_precap_denial_reason({"chat-1": "appA"}, visible, self.key(rec), rec)
            == reason
        )


class TestThePreCapReadingsAreBothTaken:
    """The pairing is the tested unit, not two inline calls at an unreachable site.

    A wiring that passed the owner map where the visible set belongs would type-check
    and pass every test of the decision function, because both are keyed on slot
    keys. Only asserting what each reading CARRIES separates them.
    """

    def state(self, slots: dict):
        return SimpleNamespace(_slots=slots, get_slot=slots.get)

    def test_the_owner_map_carries_apps_and_the_visible_set_carries_keys(self, monkeypatch):
        monkeypatch.setattr(scope_module, "app_events_revoked", lambda _app: False)
        slots = {
            "chat-1": SimpleNamespace(_app="appA", _origin=""),
            "chat-2": SimpleNamespace(_app="appB", _origin=""),
        }
        owners, visible = persisted_precap_readings(
            self.state(slots), "appA", frozenset(), dashboard_user=False
        )
        # The owner map spans every live slot and its VALUES are apps.
        assert owners == {"chat-1": "appA", "chat-2": "appB"}
        # The visible set is narrower than the owner map here, which is what makes
        # the two distinguishable: passing one for the other changes the answer.
        assert visible == {"chat-1"}
        assert visible != set(owners)

    def test_the_dashboard_user_reads_every_slot_as_visible(self, monkeypatch):
        monkeypatch.setattr(scope_module, "app_events_revoked", lambda _app: False)
        slots = {"chat-1": SimpleNamespace(_app="appA", _origin="")}
        owners, visible = persisted_precap_readings(
            self.state(slots), "", frozenset(), dashboard_user=True
        )
        assert visible == set(owners) == {"chat-1"}


class TestPersistedListingDenialsAreAudited:
    """An authorization refusal that emits nothing leaves no artifact to review.

    The repo's own scope gate audits every decision through ``_audit_deny``, and
    these two refusals are decisions of the same kind, so they go through it too.
    Each carries its own reason: a truncation must not borrow an ownership
    refusal's reason, and an ownership refusal must not read as a scope one.
    """

    def audited(self, monkeypatch) -> list[tuple[str, str, str]]:
        calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            messaging,
            "_audit_deny",
            lambda app, event, reason: calls.append((app, event, reason)),
        )
        return calls

    def warnings(self, monkeypatch) -> list[str]:
        """Collect the handler's own WARNING lines.

        The package logger does not propagate to root, so caplog sees nothing;
        the call itself is what this pins anyway.
        """
        lines: list[str] = []
        monkeypatch.setattr(
            messaging.logger,
            "warning",
            lambda msg, *a, **k: lines.append(str(msg) % a if a else str(msg)),
        )
        return lines

    def request(self, state, caller: str, app: str = "", *, internal: bool = True):
        headers = {"X-Session-Key": caller}
        values: dict[str, object] = {"internal_auth": internal}
        if app:
            values["app"] = app
        return SimpleNamespace(
            app={"state": state},
            headers=headers,
            query={},
            get=lambda key, default=None: values.get(key, default),
        )

    def state(self, slot_app: str):
        manager = SimpleNamespace(all_agents=[])
        slots = {"chat-1": SimpleNamespace(_app=slot_app)}
        return SimpleNamespace(subagents=manager, _slots=slots, get_slot=slots.get)

    def run_listing(
        self,
        monkeypatch,
        agent_root,
        *,
        slot_app: str,
        caller: str,
        scope: object,
        app: str = "",
        internal: bool = True,
    ):
        """Drive the listing end to end over one persisted record."""
        calls = self.audited(monkeypatch)

        async def fake_scope(request, op):
            return scope, None

        monkeypatch.setattr(messaging, "internal_memory_scope", fake_scope)
        state = self.state(slot_app)
        payload = asyncio.get_event_loop().run_until_complete(
            messaging.api_spawn_list(self.request(state, caller, app, internal=internal))
        )
        return calls, json.loads(payload.text or "{}")

    def test_a_stale_snapshot_is_overruled_by_the_loop_recheck(self, agent_root, monkeypatch):
        """The REST listing decides ownership on the loop, not in the worker thread.

        The snapshot is taken before the scan and can be out of date by the time
        the records come back, because slot keys are caller-supplied and another
        app can reclaim one mid-scan. Here the snapshot still says the caller owns
        the slot while live state says another app does, which is the shape of that
        race; the record must be withheld and audited on the live answer.
        """
        write_record(agent_root, "stale11", app="appA", parent_session="dashboard:chat-1")
        monkeypatch.setattr(messaging, "slot_owner_snapshot", lambda _state: {"chat-1": "appA"})
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="appB",
            caller="dashboard:chat-1",
            scope=object(),
        )
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_owner_mismatch"]

    def test_the_audit_identity_is_the_app_never_the_session_key(self, agent_root, monkeypatch):
        """The dedup registry behind the audit is keyed on it and never evicted.

        A per-run session id would leave one permanent entry per subagent run, which
        is the unbounded growth the retention rule forbids.
        """
        write_record(agent_root, "aud111", app="appA", parent_session="dashboard:chat-1")
        calls, _body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="appB",
            caller="subagent:ephemeral-run-id",
            scope=object(),
            app="owning-app",
        )
        assert [app for app, _event, _reason in calls] == ["owning-app"]
        assert "subagent:ephemeral-run-id" not in [app for app, _e, _r in calls]

    def test_an_absent_app_audits_under_a_fixed_literal_not_the_caller(
        self, agent_root, monkeypatch
    ):
        write_record(agent_root, "aud222", app="appA", parent_session="dashboard:chat-1")
        calls, _body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="appB",
            caller="subagent:another-run-id",
            scope=object(),
        )
        assert [app for app, _event, _reason in calls] == ["<owner>"]

    def test_an_owner_mismatch_is_audited_and_the_record_withheld(self, agent_root, monkeypatch):
        write_record(agent_root, "own111", app="appA", parent_session="dashboard:chat-1")
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="appB",
            caller="dashboard:chat-1",
            scope=object(),
        )
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_owner_mismatch"]

    def test_a_scope_mismatch_carries_its_own_reason(self, agent_root, monkeypatch):
        write_record(agent_root, "scp111", parent_session="dashboard:chat-1")
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="",
            caller="dashboard:other",
            scope=object(),
        )
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_scope_mismatch"]

    def test_an_internal_caller_on_the_default_store_is_still_bound(self, agent_root, monkeypatch):
        """The empty store is the COMMON internal case, not an exemption.

        ``internal_memory_scope`` answers ``scope.store or None``, so a verified
        internal caller whose execution record names the default store resolves
        the same ``None`` the dashboard owner does. Gating the session bound on
        that value therefore lifted it for nearly every attested caller, handing a
        subagent another session's persisted ``task``, ``parent`` and full
        ``result`` text -- which the live half of the same response withholds.
        """
        write_record(agent_root, "dflt11", parent_session="dashboard:chat-1")
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="",
            caller="subagent:someone-elses-run",
            scope=None,
            internal=True,
        )
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_scope_mismatch"]

    def test_the_dashboard_owner_still_reads_every_record(self, agent_root, monkeypatch):
        """Control for the bound above: it must narrow agents, not the operator.

        The owner arrives with no ``internal_auth`` and no store either, so if the
        bound keyed on the store it would be indistinguishable from the internal
        caller above -- this is the case that proves the new predicate separates
        them.
        """
        write_record(agent_root, "ownr11", parent_session="dashboard:chat-1")
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="",
            caller="dashboard:some-other-tab",
            scope=None,
            internal=False,
        )
        assert [entry["id"] for entry in body["agents"]] == ["ownr11"]
        assert [reason for _app, _event, reason in calls] == []

    def test_an_admitted_record_is_listed_and_audits_nothing(self, agent_root, monkeypatch):
        write_record(agent_root, "ok1111", parent_session="dashboard:chat-1")
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="",
            caller="dashboard:chat-1",
            scope=object(),
        )
        assert [entry["id"] for entry in body["agents"]] == ["ok1111"]
        assert calls == []

    def test_truncation_is_reported_and_audited_under_its_own_reason(
        self, agent_root, monkeypatch, caplog
    ):
        moment = time.time()
        for index in range(3):
            write_record(
                agent_root,
                f"tr{index:04d}",
                parent_session="dashboard:chat-1",
                mtime=moment - index,
            )
        monkeypatch.setattr(messaging, "PERSISTED_SUBAGENT_REPLAY_KEEP", 1)
        warned = self.warnings(monkeypatch)
        calls, body = self.run_listing(
            monkeypatch,
            agent_root,
            slot_app="",
            caller="dashboard:chat-1",
            scope=None,
        )
        assert len(body["agents"]) == 1
        # Said out loud to the OPERATOR, once. The client gets no count, because
        # no panel acts on one; silence about a cut tail is what is forbidden.
        said = [line for line in warned if "truncated" in line]
        assert len(said) == 1
        # The COUNT is the payload of the report; a line that says only that
        # something was cut is the same silence in a longer sentence.
        assert "2 eligible" in said[0]
        assert "persisted_overflow" not in body
        # And the report is the WARNING alone. A truncation refuses nobody, so it
        # is not a permission decision: filing it into the deny stream would put
        # a routine cap event beside the ownership refusals an operator reads
        # that stream to find.
        assert calls == []

    def test_a_saturated_window_is_reported_even_at_a_zero_count(self, agent_root, monkeypatch):
        """The case the count itself cannot see.

        Every candidate is rejected, so the row cap never fires and its count
        stays zero -- while admissible older folders were never inspected.
        Gating the report on the count alone calls that "nothing was cut".
        """
        moment = time.time()
        for index in range(12):
            write_record(
                agent_root,
                f"sw{index:04d}",
                parent_session="dashboard:other",
                mtime=moment - index,
            )
        monkeypatch.setattr(messaging, "PERSISTED_SUBAGENT_REPLAY_KEEP", 2)
        warned = self.warnings(monkeypatch)
        calls, body = self.run_listing(
            monkeypatch, agent_root, slot_app="", caller="dashboard:chat-1", scope=object()
        )
        assert body["agents"] == []
        said = [line for line in warned if "truncated" in line]
        assert len(said) == 1
        assert "scan window saturated" in said[0]
        # The deny stream carries one row per record the scan actually INSPECTED
        # and no truncation row: every reason in it is a permission decision. The
        # count is 8 rather than the 12 written, because the candidate window is
        # itself bounded -- which is exactly why a saturated window has to report
        # even at a zero cut count: the four it never opened are invisible to it.
        assert [reason for _a, _e, reason in calls] == ["persisted_scope_mismatch"] * 8

    def test_no_report_at_all_when_nothing_was_cut(self, agent_root, monkeypatch):
        write_record(agent_root, "fit111", parent_session="dashboard:chat-1")
        warned = self.warnings(monkeypatch)
        calls, body = self.run_listing(
            monkeypatch, agent_root, slot_app="", caller="dashboard:chat-1", scope=None
        )
        assert "persisted_overflow" not in body
        assert calls == []
        assert warned == []


class TestRedactionRunsBeforeClamping:
    """Clamping first cuts a credential's tail off and defeats the match.

    A value cut at the cap loses the suffix its pattern needs, so the consumers'
    own redaction cannot match the fragment that survives into the record. The
    native card path already requires this order; the persisted path follows it.
    """

    #: A value the redactors do match, so the ordering is testable at all.
    SECRET = "AKIAIOSFODNN7EXAMPLE"

    def straddling_task(self, cap: int) -> str:
        """A task whose credential starts before the cap and ends after it."""
        return "a" * (cap - 12) + self.SECRET + "b" * 50

    def test_a_credential_straddling_the_task_cap_is_redacted(self, agent_root):
        write_record(agent_root, "sec111", task=self.straddling_task(_PANEL_TASK_CAP))
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert self.SECRET not in record["task"]
        # A clamp-first ordering would leave this recognizable head behind.
        assert "AKIA" not in record["task"]

    def test_a_credential_straddling_the_result_cap_is_redacted(self, agent_root):
        write_record(agent_root, "sec222", result=self.straddling_task(_PANEL_RESULT_CAP))
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert self.SECRET not in record["result"]
        assert "AKIA" not in record["result"]

    def test_a_credential_in_the_error_detail_is_redacted(self, agent_root):
        write_record(
            agent_root,
            "sec333",
            tombstone_cause="error",
            outcome="failed",
            detail=f"provider rejected {self.SECRET}",
        )
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert self.SECRET not in record["error"]

    def test_a_credential_in_the_agent_name_is_redacted(self, agent_root):
        write_record(agent_root, "sec444", agent=self.SECRET)
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert self.SECRET not in record["agent"]

    def test_ordinary_text_survives_redaction_unchanged(self, agent_root):
        write_record(agent_root, "sec555", task="summarise the changelog")
        (record,) = panel(keep=10, max_age_secs=DAY)
        assert record["task"] == "summarise the changelog"

    def test_the_result_read_stays_bounded_despite_the_margin(self, agent_root):
        """The margin is finite: a huge file is still not read whole."""
        write_record(agent_root, "sec666", result="z" * (_PANEL_RESULT_CAP * 20))
        (record,) = panel(keep=10, max_age_secs=DAY, include_result=True)
        assert len(record["result"]) <= _PANEL_RESULT_CAP + len(TRUNC)


class TestSlotAbsentIsNotAnOwnershipBreach:
    """A lazily hydrated slot is ordinary, and must not be filed as a breach.

    Slots hydrate from history, so a reconnect can arrive before the slot exists.
    The live gate calls that ``slot_missing``; reporting it as an ownership
    mismatch would file every cold-start reconnect as a security event and dilute
    the stream a real cross-owner refusal has to stand out in.
    """

    def test_a_missing_slot_reports_slot_missing(self):
        state = SimpleNamespace(_slots={}, get_slot=lambda name: None)
        assert persisted_replay_denial_reason(state, "chat-1", {"app": ""}) == "slot_missing"

    def test_an_owner_difference_reports_an_ownership_mismatch(self):
        slots = {"chat-1": SimpleNamespace(_app="appB")}
        state = SimpleNamespace(_slots=slots, get_slot=slots.get)
        reason = persisted_replay_denial_reason(state, "chat-1", {"app": "appA"})
        assert reason == "persisted_owner_mismatch"

    def test_the_two_reasons_are_distinct(self):
        """The whole point: an operator can tell them apart in the audit stream."""
        empty = SimpleNamespace(_slots={}, get_slot=lambda name: None)
        slots = {"chat-1": SimpleNamespace(_app="appB")}
        owned = SimpleNamespace(_slots=slots, get_slot=slots.get)
        assert persisted_replay_denial_reason(empty, "chat-1", {"app": "appA"}) != (
            persisted_replay_denial_reason(owned, "chat-1", {"app": "appA"})
        )


class TestTheCallersFilterRunsBeforeTheCap:
    """Filtering after the cap lets a foreign record spend the caller's budget.

    Two harms, not one: a record the caller may not see occupies a slot its own
    runs need, so the caller's newest run vanishes while a stranger's is counted;
    and the overflow figure then describes a population that is not the caller's,
    which tells them how many foreign runs exist.
    """

    def mine(self, record: dict) -> bool:
        return record["app"] == "mine"

    def test_a_foreign_record_does_not_occupy_a_cap_slot(self, agent_root):
        moment = time.time()
        write_record(agent_root, "theirs1", app="theirs", mtime=moment - 1)
        write_record(agent_root, "theirs2", app="theirs", mtime=moment - 2)
        write_record(agent_root, "mine001", app="mine", mtime=moment - 3)
        result = read_panel_records(keep=2, max_age_secs=DAY, admit=self.mine)
        # Without the filter running first, the two foreign records would fill
        # the cap and this caller would see nothing of its own.
        assert ids(result.records) == ["mine001"]

    def test_a_foreign_record_is_not_counted_as_overflow(self, agent_root):
        moment = time.time()
        write_record(agent_root, "mine001", app="mine", mtime=moment - 1)
        for index in range(4):
            write_record(agent_root, f"their{index}", app="theirs", mtime=moment - 2 - index)
        result = read_panel_records(keep=1, max_age_secs=DAY, admit=self.mine)
        assert ids(result.records) == ["mine001"]
        assert result.overflow == 0

    def test_overflow_counts_only_the_callers_own_withheld_runs(self, agent_root):
        moment = time.time()
        for index in range(3):
            write_record(agent_root, f"mine{index:03d}", app="mine", mtime=moment - index)
        write_record(agent_root, "theirs1", app="theirs", mtime=moment - 10)
        result = read_panel_records(keep=1, max_age_secs=DAY, admit=self.mine)
        assert len(result.records) == 1
        assert result.overflow == 2

    def test_no_admit_filter_admits_everything(self, agent_root):
        write_record(agent_root, "any001", app="theirs")
        result = read_panel_records(keep=10, max_age_secs=DAY)
        assert ids(result.records) == ["any001"]

    def test_the_result_read_is_still_skipped_past_the_cap(self, agent_root, monkeypatch):
        from kiro_crew import subagent_persistence

        moment = time.time()
        for index in range(3):
            write_record(
                agent_root, f"mine{index:03d}", app="mine", result="payload", mtime=moment - index
            )
        reads: list[str] = []
        real = subagent_persistence._panel_result_text
        monkeypatch.setattr(
            subagent_persistence,
            "_panel_result_text",
            lambda d: (reads.append(d.name), real(d))[1],
        )
        result = read_panel_records(keep=1, max_age_secs=DAY, include_result=True, admit=self.mine)
        assert result.overflow == 2
        assert reads == ["mine000"]


class TestASaturatedScanWindowIsReportedOnItsOwn:
    """The candidate window is a SECOND bound, and it closes before admission.

    It keeps the newest folders by mtime, then validity and the caller's filter
    reject some of them. So a window filled entirely by records that are then
    rejected leaves admissible older folders uninspected while the row-cap count
    stays zero. Reporting only on a nonzero count calls that "nothing was cut".
    """

    def write_many(self, agent_root, count: int, *, app: str = "mine") -> None:
        moment = time.time()
        for index in range(count):
            write_record(agent_root, f"sat{index:04d}", app=app, mtime=moment - index)

    def test_saturation_is_flagged_even_when_the_row_count_is_zero(self, agent_root):
        # Every candidate is rejected, so overflow cannot see what was left out.
        self.write_many(agent_root, 12, app="theirs")
        result = read_panel_records(
            keep=2, max_age_secs=DAY, admit=lambda record: record["app"] == "mine"
        )
        assert result.records == []
        assert result.overflow == 0
        assert result.overflow_is_lower_bound is True

    def test_an_unsaturated_window_is_not_flagged(self, agent_root):
        self.write_many(agent_root, 2)
        result = read_panel_records(keep=10, max_age_secs=DAY)
        assert len(result.records) == 2
        assert result.overflow == 0
        assert result.overflow_is_lower_bound is False

    def test_the_flag_survives_alongside_a_real_count(self, agent_root):
        self.write_many(agent_root, 12)
        result = read_panel_records(keep=2, max_age_secs=DAY)
        assert len(result.records) == 2
        assert result.overflow > 0
        assert result.overflow_is_lower_bound is True


class TestAnAppTokenCannotReadAnotherAppsRuns:
    """An app caller and the dashboard owner both arrive with ``scope is None``.

    ``internal_memory_scope`` answers that for a non-internal caller AND for a
    verified session whose execution record is empty, so scope alone cannot tell
    an app token from the owner. The app CLAIM can: every transport publishes it
    only for a positively resolved app and leaves it absent for the person, so
    its presence is the positive signal and the app bound hangs off that.

    A transport flag cannot carry this, which is what these cases pin. The
    internal-secret arm is one of four arms that publish an app claim; the other
    three are the ordinary cookie and token transports, which set the claim and
    no flag. An app bound keyed to the flag is therefore absent on exactly the
    transport an installed app normally arrives over.
    """

    def audited(self, monkeypatch) -> list[tuple[str, str, str]]:
        calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            messaging,
            "_audit_deny",
            lambda app, event, reason: calls.append((app, event, reason)),
        )
        return calls

    def request(self, state, *, internal: bool, app: str):
        return SimpleNamespace(
            app={"state": state},
            headers={"X-Session-Key": "dashboard:chat-1"},
            query={},
            get=lambda key, default=None: (
                True if key == "internal_auth" and internal else (app if key == "app" else default)
            ),
        )

    def state(self, slot_app: str = ""):
        manager = SimpleNamespace(all_agents=[])
        slots = {"chat-1": SimpleNamespace(_app=slot_app)}
        return SimpleNamespace(subagents=manager, _slots=slots, get_slot=slots.get)

    def listing(self, monkeypatch, *, internal: bool, app: str, slot_app: str = ""):
        calls = self.audited(monkeypatch)

        async def fake_scope(request, op):
            return None, None

        monkeypatch.setattr(messaging, "internal_memory_scope", fake_scope)
        payload = asyncio.get_event_loop().run_until_complete(
            messaging.api_spawn_list(self.request(self.state(slot_app), internal=internal, app=app))
        )
        return calls, json.loads(payload.text or "{}")

    @pytest.mark.parametrize(
        ("agent_id", "internal"),
        [
            pytest.param("own111", True, id="test_an_app_token_is_refused_another_apps_record"),
            # The transport an installed app actually arrives over. The cookie and
            # token arms publish a validated app claim and set no internal flag, so
            # a bound keyed to the flag never runs here and every record is
            # admitted through ``scope is None`` -- another app's task, result and
            # error text, to a caller holding only its own app's token.
            pytest.param("own444", False, id="test_an_app_on_the_cookie_transport_is_refused_too"),
        ],
    )
    def test_another_apps_record_is_refused(self, agent_root, monkeypatch, agent_id, internal):
        write_record(agent_root, agent_id, app="theirs", parent_session="dashboard:chat-1")
        calls, body = self.listing(monkeypatch, internal=internal, app="mine")
        assert body["agents"] == []
        assert [reason for _app, _event, reason in calls] == ["persisted_app_mismatch"]

    @pytest.mark.parametrize(
        ("agent_id", "record_app", "internal", "caller_app", "slot_app"),
        [
            # The bound narrows to the caller's own app, it does not deny the app.
            pytest.param(
                "own555",
                "mine",
                False,
                "mine",
                "",
                id="test_an_app_on_the_cookie_transport_still_sees_its_own_record",
            ),
            # The slot is owned by the same app here because an internal caller
            # takes the session and slot-ownership bounds too, exactly as the live
            # half does. A record whose app differs from its slot's present owner
            # is the reused-key case those bounds exist for, not an app reading
            # its own run.
            pytest.param(
                "own222",
                "mine",
                True,
                "mine",
                "mine",
                id="test_an_app_token_still_sees_its_own_record",
            ),
            # The owner is not app-authenticated, so the bound does not apply.
            # Narrowing the owner here would empty the panel on a cold start: a
            # record's app is whatever spawned the run, which need not match the
            # app of the tab now reading it. An ABSENT claim is what marks the
            # person, which is why the bound reads the claim's presence rather
            # than treating an empty claim as an app named "".
            pytest.param(
                "own333",
                "theirs",
                False,
                "",
                "",
                id="test_the_owner_is_not_narrowed_by_the_app_bound",
            ),
        ],
    )
    def test_the_record_is_listed(
        self, agent_root, monkeypatch, agent_id, record_app, internal, caller_app, slot_app
    ):
        write_record(agent_root, agent_id, app=record_app, parent_session="dashboard:chat-1")
        calls, body = self.listing(
            monkeypatch, internal=internal, app=caller_app, slot_app=slot_app
        )
        assert [entry["id"] for entry in body["agents"]] == [agent_id]
        assert calls == []


class TestOwnershipIsRecheckedOnTheLoop:
    """The snapshot sizes the cap; the loop decides delivery.

    Slot keys are caller-supplied and are not namespaced by app, so another app
    can reclaim one WHILE the off-loop scan runs. A decision taken inside the
    worker thread would then be read from state the socket does not describe,
    so the admission the scan uses is a snapshot taken on the loop beforehand and
    every surviving record is checked again on the loop before its frame is kept.

    This exercises the two real functions in the order the replay calls them,
    with the owner changing in between.
    """

    def snapshot(self, state) -> dict:
        return slot_owner_snapshot(state)

    def refusal_of(self, snapshot: dict, record: dict) -> str:
        return persisted_snapshot_denial_reason(
            snapshot, subagent_event_slot(str(record.get("parent_session") or "")), record
        )

    def state(self, app: str):
        slots = {"chat-1": SimpleNamespace(_app=app)}
        return SimpleNamespace(_slots=slots, get_slot=slots.get)

    def record(self, app: str) -> dict:
        return {"id": "race111", "app": app, "parent_session": "dashboard:chat-1"}

    def test_an_owner_flip_mid_scan_is_caught_by_the_loop_recheck(self):
        state = self.state("appA")
        record = self.record("appA")
        taken = self.snapshot(state)
        assert self.refusal_of(taken, record) == ""
        # App B reclaims the slot while the scan is still running.
        state._slots["chat-1"]._app = "appB"
        reason = persisted_replay_denial_reason(
            state, subagent_event_slot(str(record["parent_session"])), record
        )
        assert reason == "persisted_owner_mismatch"

    def test_a_stable_owner_survives_both_checks(self):
        state = self.state("appA")
        record = self.record("appA")
        taken = self.snapshot(state)
        assert self.refusal_of(taken, record) == ""
        assert (
            persisted_replay_denial_reason(
                state, subagent_event_slot(str(record["parent_session"])), record
            )
            == ""
        )

    def test_the_snapshot_refuses_a_slot_it_does_not_hold(self):
        taken = self.snapshot(self.state("appA"))
        other = {"id": "race222", "app": "appA", "parent_session": "dashboard:chat-9"}
        assert self.refusal_of(taken, other) == "slot_missing"

    def test_the_snapshot_refuses_a_foreign_app_so_the_cap_is_not_spent(self):
        taken = self.snapshot(self.state("appA"))
        assert self.refusal_of(taken, self.record("appB")) == "persisted_owner_mismatch"

    def test_both_halves_name_the_same_refusal_for_the_same_state(self):
        """One decision, so the two halves cannot disagree about which refusal.

        A bool here would have forced the caller to withhold a record with no SEL
        record of why, which is the case a snapshot rejection normally is: the two
        checks apply the same equality, so most refusals never reach the loop half
        at all.
        """
        for owner, app, expected in (
            ("appA", "appB", "persisted_owner_mismatch"),
            ("", "appB", "persisted_owner_mismatch"),
            ("appA", "appA", ""),
            ("", "", ""),
        ):
            state = self.state(owner)
            record = self.record(app)
            slot = subagent_event_slot(str(record["parent_session"]))
            assert self.refusal_of(self.snapshot(state), record) == expected
            assert persisted_replay_denial_reason(state, slot, record) == expected
