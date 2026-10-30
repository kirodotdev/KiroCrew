"""People/setup modes, Unattributed filtering, trigger attribution and audit references.

Covers ``shared_crew.crew_mode`` (personal / network / invalid and the startup
refusal of an unusable team configuration), the ``unattributed`` session filter
with its older ``unknown`` spelling, the trigger label of unattended work
(``shared_crew.trigger``), and the workload identity, trigger and causal join an
audit record carries.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

import pytest

from kiro_crew.history import ConversationLog
from kiro_crew.shared_crew import (
    access,
    audit,
    crew_mode,
    people,
    trigger,
)
from kiro_crew.shared_crew.admission import AcceptedWork, OperationKind, OperationState
from kiro_crew.shared_crew.attribution import public_attribution_from_meta, trigger_of_public
from kiro_crew.shared_crew.contracts import (
    ActionOutcome,
    ActionType,
    ActorContext,
    AttributionAvailability,
    Principal,
    Provenance,
    SessionAttribution,
    SessionQuery,
    Transport,
)
from kiro_crew.shared_crew.identity import local_operator_principal, tailnet_principal
from kiro_crew.shared_crew.session_query import query_sessions

ALICE = tailnet_principal("alice@example.com", "a", Provenance.TAILSCALE_SERVE)
OPERATOR = local_operator_principal("local-app")
NO_PATHS: dict[str, dict[str, str]] = {
    "tailscale": {"state": "unavailable", "reason": "not_configured"},
    "kiro_login": {"state": "unavailable", "reason": "not_in_this_build"},
    "systems_manager": {"state": "unavailable", "reason": "not_in_this_build"},
}
TAILNET_ON = {**NO_PATHS, "tailscale": {"state": "available", "reason": ""}}


@pytest.fixture(autouse=True)
def _clean():
    access._reset_setup_paths_for_tests()
    trigger.set_scheduled_namer(None)
    people.reset_for_tests()
    yield
    access._reset_setup_paths_for_tests()
    trigger.set_scheduled_namer(None)
    people.reset_for_tests()


# ── modes ──


class TestModes:
    def test_personal_when_no_team_path_and_nobody_else(self):
        mode, problem = crew_mode.resolve_mode(NO_PATHS, others_seen=False)
        assert (mode, problem) == (crew_mode.MODE_PERSONAL, None)

    def test_network_when_a_path_is_on_or_someone_else_was_seen(self):
        assert crew_mode.resolve_mode(TAILNET_ON, others_seen=False)[0] == "network"
        degraded = {**NO_PATHS, "tailscale": {"state": "degraded", "reason": "tailnet_unreachable"}}
        assert crew_mode.resolve_mode(degraded, others_seen=False)[0] == "network"
        assert crew_mode.resolve_mode(NO_PATHS, others_seen=True)[0] == "network"

    def test_an_unreadable_tailnet_allowlist_refuses_team_mode(self):
        access.note_tailnet(
            enabled=True, host="crew.example.ts.net", identity_config_unreadable=True
        )
        assert crew_mode.resolve_mode(access.setup_paths_dict(), others_seen=False)[0] == "invalid"
        access.note_tailnet(enabled=True, host="crew.example.ts.net")
        assert crew_mode.resolve_mode(access.setup_paths_dict(), others_seen=False)[0] == "network"

    def test_startup_validation_logs_a_machine_readable_reason(self, caplog):
        access.note_tailnet(enabled=True, host="h", identity_config_unreadable=True)
        with caplog.at_level(logging.ERROR, logger="kiro_crew.shared_crew.crew_mode"):
            problem = crew_mode.validate_at_startup()
        assert problem is not None
        assert "network_identity_config_unreadable" in caplog.text
        access.note_tailnet(enabled=True, host="h")
        assert crew_mode.validate_at_startup() is None

    def test_others_observed_counts_network_people_and_other_operators(self):
        assert crew_mode.others_observed(OPERATOR, [OPERATOR, Principal.unknown()]) is False
        assert crew_mode.others_observed(OPERATOR, [ALICE]) is True
        assert crew_mode.others_observed(OPERATOR, [local_operator_principal("other")]) is True
        assert crew_mode.others_observed(OPERATOR, [Principal.system("automation")]) is False

    def test_mode_view_keeps_the_console_and_workload_keys_constant(self):
        personal = crew_mode.mode_view(OPERATOR, [], setup_paths=NO_PATHS, crew_id="g1")
        assert personal["mode"] == "personal"
        assert personal["console"] == {"state": "not_offered", "url": ""}
        assert personal["crew_id"] == "g1"
        assert personal["systems_manager_attribution"] == "unavailable"
        assert personal["workload_identity_ref"] == "unavailable"
        invalid = crew_mode.mode_view(
            OPERATOR, [], setup_paths=NO_PATHS, crew_id="g1", network_identity_unreadable=True
        )
        assert invalid["mode"] == "invalid"
        assert invalid["team_config"]["reason"] == "network_identity_config_unreadable"


# ── triggers ──


class TestTriggers:
    def test_cron_created_by_names_the_schedule(self):
        trigger.set_scheduled_namer({"abc12345": "Nightly"}.get)
        found = trigger.trigger_of(key="dashboard:tab", created_by="cron:abc12345")
        assert found is not None and (found.kind, found.ref, found.name) == (
            "scheduled",
            "abc12345",
            "Nightly",
        )

    @pytest.mark.parametrize(
        "key", ["cron:abc12345", "cron_abc12345", "cron:abc12345:run", "dashboard_cron_abc12345"]
    )
    def test_cron_namespace_keys_in_either_spelling(self, key):
        found = trigger.trigger_of(key=key)
        assert found is not None and found.kind == "scheduled" and found.ref == "abc12345"
        assert found.name == ""

    def test_webhook_and_system(self):
        hook = trigger.trigger_of(key="hook:github:1")
        assert hook is not None and (hook.kind, hook.name) == ("webhook", "github")
        folded = trigger.trigger_of(key="hook_github_1")
        assert folded is not None and (folded.kind, folded.name) == ("webhook", "")
        system = trigger.trigger_of(key="startup", origin="system")
        assert system is not None and system.kind == "system"

    @pytest.mark.parametrize(
        "key,created_by", [("dashboard:tab", ""), ("cron:NOTHEX!", ""), ("tab", "dashboard:other")]
    )
    def test_a_person_s_session_has_no_trigger(self, key, created_by):
        assert trigger.trigger_of(key=key, created_by=created_by) is None

    def test_a_failing_name_lookup_only_loses_the_name(self):
        def boom(_ref: str) -> str:
            raise RuntimeError("down")

        trigger.set_scheduled_namer(boom)
        found = trigger.trigger_of(key="cron:abc12345")
        assert found is not None and found.name == ""

    def test_public_attribution_names_the_trigger_never_for_a_person(self):
        legacy = public_attribution_from_meta({"created_by": "cron:abc12345"}, key="dashboard:t")
        assert trigger_of_public(legacy) == {
            "kind": "scheduled",
            "ref": "abc12345",
            "name": "",
        }
        person = SessionAttribution(
            creator=ALICE, availability=AttributionAvailability.RECORDED, created_at=1.0
        )
        meta = {"attribution": person.to_dict(), "created_by": "cron:abc12345"}
        assert trigger_of_public(public_attribution_from_meta(meta, key="cron:abc12345")) is None


# ── Unattributed filter over real catalog rows ──


def _write(
    base, key: str, *, creator: Principal | None, mtime: float, extra: dict | None = None
) -> None:
    meta: dict[str, Any] = {"_type": "metadata", "title": key}
    if creator is not None:
        meta["attribution"] = SessionAttribution(
            creator=creator, availability=AttributionAvailability.RECORDED, created_at=mtime
        ).to_dict()
    meta.update(extra or {})
    path = base / f"{key}.jsonl"
    path.write_text(
        json.dumps(meta) + "\n" + json.dumps({"role": "user", "content": "x"}) + "\n",
        encoding="utf-8",
    )
    os.utime(path, (mtime, mtime))


@pytest.fixture
def catalog(tmp_path):
    base = tmp_path / "sessions"
    base.mkdir()
    for i in range(3):
        _write(base, f"dashboard_a{i}", creator=ALICE, mtime=1000.0 + i)
    for i in range(4):
        _write(base, f"dashboard_l{i}", creator=None, mtime=2000.0 + i)
    _write(base, "dashboard_c0", creator=None, mtime=3000.0, extra={"created_by": "cron:abc12345"})
    _write(base, "cron_def67890", creator=None, mtime=3001.0)
    return ConversationLog(base)


@pytest.mark.parametrize("spelling", ["unattributed", "unknown"])
def test_unattributed_filter_counts_and_pages_before_search(catalog, spelling):
    rows = catalog.list_sessions()
    query = SessionQuery.from_query({"creator": spelling, "limit": "3"})
    assert query.creator == "unattributed"
    page = query_sessions(rows, query, ALICE)
    assert page.total_matching == 4 and page.hidden_by_filter == 5
    assert {i["key"] for i in page.items} <= {f"dashboard_l{i}" for i in range(4)}
    assert page.next_cursor
    rest = query_sessions(
        rows, SessionQuery.from_query({"creator": spelling, "cursor": page.next_cursor}), ALICE
    )
    assert len(page.items) + len(rest.items) == 4
    searched = query_sessions(
        rows, SessionQuery.from_query({"creator": spelling, "q": "l1"}), ALICE
    )
    assert [i["key"] for i in searched.items] == ["dashboard_l1"]
    assert searched.total_matching + searched.hidden_by_filter == 1


def test_scheduled_sessions_list_under_all_with_their_trigger(catalog):
    page = query_sessions(
        catalog.list_sessions(), SessionQuery.from_query({"creator": "all"}), ALICE
    )
    triggers = {i["key"]: trigger_of_public(i["attribution"]) for i in page.items}
    assert triggers["dashboard_c0"]["ref"] == "abc12345"
    assert triggers["cron_def67890"]["ref"] == "def67890"
    assert triggers["dashboard_l0"] is None and triggers["dashboard_a0"] is None


def test_filters_change_visibility_only(catalog):
    rows = catalog.list_sessions()
    every = {
        i["key"]
        for i in query_sessions(rows, SessionQuery.from_query({"limit": "200"}), ALICE).items
    }
    union: set[str] = set()
    for creator in ("mine", "unattributed"):
        page = query_sessions(
            rows, SessionQuery.from_query({"creator": creator, "limit": "200"}), ALICE
        )
        union |= {i["key"] for i in page.items}
    assert union < every and every - union == {"dashboard_c0", "cron_def67890"}


# ── the mode route ──


# ── audit ──


def _actor(principal: Principal) -> ActorContext:
    return ActorContext(principal=principal, request_id="r", transport=Transport.HTTP)


class TestAudit:
    def test_every_record_names_the_crew_workload_as_unavailable(self):
        record = audit.new_action_record(
            ActionType.MESSAGE,
            session_key="dashboard:t",
            initiator=_actor(ALICE),
            outcome=ActionOutcome.ACCEPTED,
        )
        meta = audit.security_event(record).metadata
        assert meta["workload_identity_ref"] == "unavailable"
        assert meta["initiator"]["id"] == ALICE.id and meta["trigger"] is None

    def test_unattended_work_names_its_trigger_never_a_person(self):
        record = audit.new_action_record(
            ActionType.MESSAGE,
            session_key="cron:abc12345",
            initiator=_actor(Principal.system("automation")),
            outcome=ActionOutcome.ACCEPTED,
        )
        meta = audit.security_event(record).metadata
        assert meta["trigger"] == {"kind": "scheduled", "ref": "abc12345", "name": ""}
        assert meta["initiator"]["kind"] == "system"
        assert meta["workload_identity_ref"] == "unavailable"
        human = audit.new_action_record(
            ActionType.MESSAGE,
            session_key="cron:abc12345",
            initiator=_actor(ALICE),
            outcome=ActionOutcome.ACCEPTED,
        )
        assert audit.security_event(human).metadata["trigger"] is None

    def test_causal_join_work_session_actor_survives_a_round_trip(self):
        work = AcceptedWork(
            work_id="w1",
            scope="root",
            budget_key=ALICE.id,
            session_key="dashboard:t",
            operation=OperationKind.CHAT_TURN,
            state=OperationState.QUEUED,
            revision=1,
            accepted_at=1.0,
            expires_at=100.0,
        )
        bob = tailnet_principal("bob@example.com", "b", Provenance.TAILSCALE_SERVE)
        cancel = audit.new_action_record(
            ActionType.CANCEL,
            session_key=work.session_key,
            initiator=_actor(bob),
            outcome=ActionOutcome.APPLIED,
            target=work.work_id,
        )
        follow = audit.new_action_record(
            ActionType.CANCEL,
            session_key=work.session_key,
            initiator=_actor(bob),
            outcome=ActionOutcome.APPLIED,
            target=work.work_id,
            causal_action_id=cancel.action_id,
        )
        back = cancel
        assert (back.target, back.session_key) == (work.work_id, work.session_key)
        # The actor who cancelled stays distinct from the person the work is charged to.
        assert back.initiator.id == bob.id != work.budget_key
        assert back.extra["workload_identity_ref"] == "unavailable"
        assert follow.causal_action_id == back.action_id
        event = audit.security_event(back).metadata
        assert (event["target"], event["session_key"], event["initiator"]["id"]) == (
            "w1",
            "dashboard:t",
            bob.id,
        )
        other = audit.new_action_record(
            ActionType.CANCEL,
            session_key="dashboard:other",
            initiator=_actor(bob),
            outcome=ActionOutcome.APPLIED,
            target="w1",
        )
        assert other.target == work.work_id and other.session_key != work.session_key


def test_no_external_requested_by_attribution_is_emitted():
    """Human attribution stays in Crew records; nothing tells a downstream system who asked.

    External "Requested by <person>" output is suspended pending product
    reconciliation, so no source file under ``src/kiro_crew`` may build one.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1] / "src" / "kiro_crew"
    # Case-sensitive: the outbound label is "Requested by <person>"; prose such as
    # "requested by me" in a filter description is not attribution output.
    pattern = re.compile(r"[\"']Requested by\b|requested_by_human|on_behalf_of_human")
    hits = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if pattern.search(p.read_text(encoding="utf-8", errors="replace"))
    ]
    assert hits == []
