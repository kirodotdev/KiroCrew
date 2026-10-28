"""Shared Crew contract records: round trips, legacy reads and forward compatibility."""

from __future__ import annotations

import pytest

from kiro_crew.shared_crew import SCHEMA_VERSION, ContractError
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
