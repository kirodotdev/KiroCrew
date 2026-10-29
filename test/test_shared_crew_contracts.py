"""Shared Crew contract records: round trips, legacy reads, forward compatibility,
and the operation state machine."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path

import pytest

from kiro_crew.shared_crew import SCHEMA_VERSION, ContractError
from kiro_crew.shared_crew.admission import (
    CAPACITY_HOLDING,
    TASKQ_STATE_MAP,
    TERMINAL,
    TRANSITIONS,
    AcceptedWork,
    AdmissionRequest,
    CapacityResponse,
    CapacityScope,
    InvalidTransition,
    OperationKind,
    OperationState,
    ReasonCode,
    RuntimeOwnershipRef,
    ScopeUsage,
    StaleRevision,
    error_body,
    http_status,
    idempotency_scope,
    is_legal_transition,
    retry_after_header,
)
from kiro_crew.shared_crew.collab import (
    DecisionKind,
    DecisionState,
    ResolveOutcome,
    SharedDecision,
    classify_response,
)
from kiro_crew.shared_crew.contracts import (
    SESSION_ATTRIBUTION_META_KEY,
    UNKNOWN_PRINCIPAL_ID,
    ActionOutcome,
    ActionRecord,
    ActionType,
    ActorContext,
    AttributionAvailability,
    AuthSnapshot,
    Capabilities,
    CapabilityState,
    DisplaySnapshot,
    EnforcementLevel,
    Principal,
    PrincipalKind,
    Provenance,
    SessionAttribution,
    SessionQuery,
    Transport,
    keep_creator,
    principal_id_for,
    principal_or_unknown,
)
from kiro_crew.taskq import model as taskq_model

REPO = Path(__file__).resolve().parent.parent
ALICE = Principal(
    id=principal_id_for("tailscale", "example.ts.net", "alice@example.com"),
    kind=PrincipalKind.HUMAN,
    provider="tailscale",
    issuer="example.ts.net",
    subject="alice@example.com",
    provenance=Provenance.TAILSCALE_SERVE,
    display=DisplaySnapshot(name="Alice", login="alice@example.com", captured_at=10.0),
)
BOB = Principal(
    id=principal_id_for("tailscale", "example.ts.net", "bob@example.com"),
    kind=PrincipalKind.HUMAN,
    provider="tailscale",
    issuer="example.ts.net",
    subject="bob@example.com",
    provenance=Provenance.TAILSCALE_DIRECT,
)


def _work(state: OperationState = OperationState.QUEUED, **kw: object) -> AcceptedWork:
    base: dict[str, object] = dict(
        work_id="w1",
        scope=idempotency_scope(ALICE.id, "chat-1", "key-00000001"),
        budget_key=ALICE.id,
        session_key="chat-1",
        operation=OperationKind.CHAT_TURN,
        state=state,
        revision=0,
        accepted_at=100.0,
        expires_at=200.0,
    )
    base.update(kw)
    return AcceptedWork(**base)  # type: ignore[arg-type]


class TestPrincipal:
    def test_id_is_stable_and_opaque(self) -> None:
        a = principal_id_for("tailscale", "t", "alice@example.com")
        assert a == principal_id_for("tailscale", "t", "alice@example.com")
        assert "alice" not in a
        assert a != principal_id_for("enterprise", "t", "alice@example.com")

    def test_round_trip(self) -> None:
        assert Principal.from_dict(ALICE.to_dict()) == ALICE
        assert ALICE.to_dict()["schema_version"] == SCHEMA_VERSION

    def test_rename_keeps_identity(self) -> None:
        renamed = Principal.from_dict({**ALICE.to_dict(), "display": {"name": "Al"}})
        assert renamed.id == ALICE.id

    def test_newer_version_and_display_keys_survive_a_rewrite(self) -> None:
        raw = {**ALICE.to_dict(), "schema_version": 2}
        raw["display"] = {**raw["display"], "future_label": "x"}
        decoded = Principal.from_dict(raw)
        out = decoded.to_dict()
        assert out["schema_version"] == 2
        assert out["display"]["future_label"] == "x"
        assert "future_label" not in decoded.to_public_dict()["display"]
        assert decoded == ALICE

    def test_overflowing_number_reads_as_unknown(self) -> None:
        raw = {**ALICE.to_dict(), "display": {"name": "A", "captured_at": 10**400}}
        assert principal_or_unknown(raw).is_unknown

    def test_unknown_fields_are_preserved(self) -> None:
        raw = {**ALICE.to_dict(), "future_field": {"x": 1}, "schema_version": 2}
        out = Principal.from_dict(raw).to_dict()
        assert out["future_field"] == {"x": 1}

    def test_unknown_kind_from_newer_writer_reads_as_unknown(self) -> None:
        p = Principal.from_dict({**ALICE.to_dict(), "kind": "team"})
        assert p.kind is PrincipalKind.UNKNOWN

    def test_public_dict_omits_subject_and_issuer(self) -> None:
        public = ALICE.to_public_dict()
        assert set(public) == {"id", "kind", "display"}

    @pytest.mark.parametrize("bad", ["", "alice", "p_UPPER", "p_" + "a" * 65, "p_x/y"])
    def test_malformed_id_refused(self, bad: str) -> None:
        with pytest.raises(ContractError):
            Principal(id=bad, kind=PrincipalKind.HUMAN)

    def test_damaged_or_missing_record_displays_unknown(self) -> None:
        assert principal_or_unknown(None).id == UNKNOWN_PRINCIPAL_ID
        assert principal_or_unknown({"id": 3}).is_unknown
        assert principal_or_unknown("alice").is_unknown

    def test_system_principals_are_bounded_per_component(self) -> None:
        assert Principal.system("cron").id == Principal.system("cron").id
        assert Principal.system("cron").id != Principal.system("heartbeat").id
        assert Principal.system("cron").kind is PrincipalKind.SYSTEM


class TestActorAndCrew:
    def test_actor_to_dict(self) -> None:
        actor = ActorContext(
            principal=ALICE,
            request_id="r1",
            transport=Transport.WEBSOCKET,
            correlation_id="c1",
            auth=AuthSnapshot(token_subject="owner", is_dashboard_user=True, pin_scope="login"),
            parent_execution_id="e0",
        )
        body = actor.to_dict()
        assert (body["transport"], body["parent_execution_id"]) == ("websocket", "e0")
        assert body["auth"]["pin_scope"] == "login" and body["principal"]["id"] == ALICE.id


class TestSessionAttribution:
    def test_legacy_metadata_reads_as_unknown(self) -> None:
        attr = SessionAttribution.from_metadata({"created_by": "chat-9", "title": "x"})
        assert attr.availability is AttributionAvailability.LEGACY_MISSING
        assert attr.creator.is_unknown

    def test_damaged_attribution_reads_as_legacy(self) -> None:
        attr = SessionAttribution.from_metadata({SESSION_ATTRIBUTION_META_KEY: "garbage"})
        assert attr.availability is AttributionAvailability.LEGACY_MISSING

    def test_round_trip_and_unknown_field_preservation(self) -> None:
        attr = SessionAttribution(
            creator=ALICE, availability=AttributionAvailability.RECORDED, created_at=5.0
        )
        raw = {**attr.to_dict(), "origin_host": "h1"}
        back = SessionAttribution.from_metadata({SESSION_ATTRIBUTION_META_KEY: raw})
        assert back == attr
        assert back.to_dict()["origin_host"] == "h1"

    def test_creator_is_write_once(self) -> None:
        first = SessionAttribution(creator=ALICE, availability=AttributionAvailability.RECORDED)
        second = SessionAttribution(creator=BOB, availability=AttributionAvailability.RECORDED)
        assert keep_creator(first, second).creator == ALICE
        assert keep_creator(None, second).creator == BOB

    def test_legacy_is_never_backfilled_from_viewer(self) -> None:
        legacy = SessionAttribution.legacy()
        viewer = SessionAttribution(creator=BOB, availability=AttributionAvailability.RECORDED)
        assert keep_creator(legacy, viewer).creator.is_unknown


class TestActionRecord:
    def test_to_dict_names_the_responder(self) -> None:
        rec = ActionRecord(
            action_id="a1",
            action_type=ActionType.APPROVAL,
            session_key="chat-1",
            initiator=ALICE,
            responder=BOB,
            at=1.0,
            outcome=ActionOutcome.APPLIED,
            causal_action_id="a0",
        )
        body = rec.to_dict()
        assert body["responder"]["id"] == BOB.id and body["causal_action_id"] == "a0"


class TestCapabilities:
    def test_defaults_report_nothing_unbacked(self) -> None:
        caps = Capabilities()
        assert caps.attribution is CapabilityState.UNAVAILABLE
        assert caps.containment_level is EnforcementLevel.NONE
        assert caps.membership_link == ""

    def test_round_trip(self) -> None:
        caps = Capabilities(
            attribution=CapabilityState.AVAILABLE,
            containment=CapabilityState.DEGRADED,
            containment_level=EnforcementLevel.BEST_EFFORT,
            membership_link="tailscale_admin",
        )
        assert caps.to_dict()["containment_level"] == "best_effort"

    def test_arbitrary_link_refused(self) -> None:
        with pytest.raises(ContractError):
            Capabilities(membership_link="https://evil.example")


class TestSessionQuery:
    def test_defaults(self) -> None:
        q = SessionQuery.from_query({})
        assert (q.creator, q.limit) == ("all", 50)

    def test_limit_is_clamped(self) -> None:
        assert SessionQuery.from_query({"limit": "100000"}).limit == 200
        assert SessionQuery.from_query({"limit": "0"}).limit == 1

    @pytest.mark.parametrize(
        "bad", [{"creator": "alice@example.com"}, {"cursor": "a b"}, {"limit": "x"}]
    )
    def test_malformed_refused(self, bad: dict[str, str]) -> None:
        with pytest.raises(ContractError):
            SessionQuery.from_query(bad)

    def test_mine_uses_viewer_and_never_matches_for_unknown_viewer(self) -> None:
        q = SessionQuery.from_query({"creator": "mine"})
        assert q.matches(ALICE, viewer=ALICE)
        assert not q.matches(BOB, viewer=ALICE)
        assert not q.matches(Principal.unknown(), viewer=Principal.unknown())

    def test_unknown_and_principal_filters(self) -> None:
        assert SessionQuery.from_query({"creator": "unknown"}).matches(Principal.unknown(), ALICE)
        assert SessionQuery.from_query({"creator": BOB.id}).matches(BOB, ALICE)


class TestStateMachine:
    def test_every_state_has_a_row(self) -> None:
        assert set(TRANSITIONS) == set(OperationState)

    def test_terminal_states_have_no_exit(self) -> None:
        for state in TERMINAL:
            assert TRANSITIONS[state] == frozenset()

    def test_rejected_only_from_submitted(self) -> None:
        sources = {s for s, targets in TRANSITIONS.items() if OperationState.REJECTED in targets}
        assert sources == {OperationState.SUBMITTED}

    def test_queued_work_cannot_start_running_without_admission(self) -> None:
        assert not is_legal_transition(OperationState.QUEUED, OperationState.RUNNING)

    def test_running_work_cannot_become_cancelled_without_cancelling(self) -> None:
        assert not is_legal_transition(OperationState.RUNNING, OperationState.CANCELLED)

    def test_every_taskq_state_is_mapped(self) -> None:
        assert set(TASKQ_STATE_MAP) == set(taskq_model.STATES)
        for queue_state in taskq_model.TERMINAL:
            assert TASKQ_STATE_MAP[queue_state] in TERMINAL

    def test_accepted_work_never_holds_unaccepted_states(self) -> None:
        for state in (OperationState.SUBMITTED, OperationState.REJECTED):
            with pytest.raises(ContractError):
                _work(state)

    def test_advance_bumps_revision(self) -> None:
        nxt = _work().advance(OperationState.ADMITTED, expected_revision=0)
        assert (nxt.state, nxt.revision) == (OperationState.ADMITTED, 1)

    def test_advance_refuses_stale_revision(self) -> None:
        with pytest.raises(StaleRevision) as exc:
            _work(revision=3).advance(OperationState.ADMITTED, expected_revision=2)
        assert exc.value.code == "stale_revision"

    def test_advance_refuses_illegal_transition(self) -> None:
        with pytest.raises(InvalidTransition):
            _work().advance(OperationState.COMPLETED, expected_revision=0)

    def test_capacity_holding_states_are_not_terminal(self) -> None:
        assert not CAPACITY_HOLDING & TERMINAL

    def test_accepted_work_round_trip_and_extra(self) -> None:
        work = _work(
            OperationState.RUNNING,
            reason=ReasonCode.CANCELLED_BY_USER,
            runtime=RuntimeOwnershipRef(execution_id="e1", lease="l", tenancy_epoch=2),
            extra={"lane_c_field": 1},
        )
        raw = work.to_dict()
        assert raw["lane_c_field"] == 1
        assert AcceptedWork.from_dict(raw) == work

    def test_public_dict_hides_scope_and_lease(self) -> None:
        public = _work(
            runtime=RuntimeOwnershipRef(execution_id="e", lease="secret")
        ).to_public_dict()
        assert "scope" not in public and "runtime" not in public and "budget_key" not in public


class TestAdmission:
    def test_request_round_trip(self) -> None:
        req = AdmissionRequest(
            idempotency_key="key-00000001",
            budget_key=ALICE.id,
            session_key="chat-1",
            operation=OperationKind.SUBAGENT,
            submitted_at=1.0,
            expires_at=2.0,
            parent_execution_id="e0",
        )
        assert req.to_dict()["parent_execution_id"] == "e0"

    def test_idempotency_scope_separates_principals_and_sessions(self) -> None:
        a = idempotency_scope(ALICE.id, "s1", "key-00000001")
        assert a != idempotency_scope(BOB.id, "s1", "key-00000001")
        assert a != idempotency_scope(ALICE.id, "s2", "key-00000001")

    @pytest.mark.parametrize("bad", ["short", "has space1234", "x" * 129])
    def test_malformed_idempotency_key_refused(self, bad: str) -> None:
        with pytest.raises(ContractError):
            idempotency_scope(ALICE.id, "s", bad)

    def test_expiry_before_submit_refused(self) -> None:
        with pytest.raises(ContractError):
            AdmissionRequest(
                idempotency_key="key-00000001",
                budget_key=ALICE.id,
                session_key="s",
                operation=OperationKind.CHAT_TURN,
                submitted_at=2.0,
                expires_at=1.0,
            )

    def test_capacity_round_trip_and_error_envelope(self) -> None:
        cap = CapacityResponse(
            principal=ScopeUsage(active=2, queued=1, active_limit=2, queue_limit=4),
            crew=ScopeUsage(active=3, active_limit=8),
            enforcement=EnforcementLevel.HARD,
            limiting_scope=CapacityScope.PRINCIPAL,
            reason=ReasonCode.PRINCIPAL_ACTIVE_LIMIT,
            retry_after_seconds=5,
        )
        assert "eta" not in str(cap.to_dict())
        body = error_body(ReasonCode.PRINCIPAL_ACTIVE_LIMIT, "busy", capacity=cap)
        assert body["code"] == "principal_active_limit" and body["error"] == "busy"
        assert http_status(ReasonCode.PRINCIPAL_ACTIVE_LIMIT) == 429
        assert retry_after_header(cap) == {"Retry-After": "5"}
        assert retry_after_header(None) == {}

    def test_every_reason_has_an_http_status(self) -> None:
        for reason in ReasonCode:
            assert 400 <= http_status(reason) < 600


class TestSharedDecision:
    def _pending(self) -> SharedDecision:
        return SharedDecision(
            decision_id="d1",
            kind=DecisionKind.APPROVAL,
            session_key="chat-1",
            state=DecisionState.PENDING,
            revision=0,
            initiator=ALICE,
            created_at=1.0,
        )

    def test_first_response_wins_then_replay_then_conflict(self) -> None:
        pending = self._pending()
        assert classify_response(pending, "resp-1") is ResolveOutcome.WON
        resolved = replace(
            pending,
            state=DecisionState.RESOLVED,
            revision=1,
            responder=BOB,
            response_id="resp-1",
            outcome="allow_once",
            resolved_at=2.0,
        )
        assert classify_response(resolved, "resp-1") is ResolveOutcome.REPLAYED
        assert classify_response(resolved, "resp-2") is ResolveOutcome.ALREADY_RESOLVED
        assert SharedDecision.from_dict(resolved.to_dict()) == resolved
        assert resolved.to_public_dict()["responder"]["id"] == BOB.id

    def test_resolved_without_responder_refused(self) -> None:
        with pytest.raises(ContractError):
            replace(self._pending(), state=DecisionState.RESOLVED)

    def test_expired_is_not_pending(self) -> None:
        expired = replace(self._pending(), state=DecisionState.EXPIRED)
        assert classify_response(expired, "r") is ResolveOutcome.NOT_PENDING


def test_typescript_transition_table_matches_python() -> None:
    """``website/src/api/sharedCrew.ts`` mirrors ``admission.TRANSITIONS``."""
    source = (REPO / "website" / "src" / "api" / "sharedCrew.ts").read_text(encoding="utf-8")
    block = source.split("OPERATION_TRANSITIONS", 1)[1].split("}", 1)[0]
    parsed = {
        state: frozenset(re.findall(r"'([a-z_]+)'", targets))
        for state, targets in re.findall(r"(\w+): \[([^\]]*)\]", block)
    }
    expected = {s.value: frozenset(t.value for t in targets) for s, targets in TRANSITIONS.items()}
    assert parsed == expected
