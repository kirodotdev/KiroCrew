"""Verdict engine + hazard ledger for stub/share recommendations."""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kiro_crew.mcp_gateway import hazards, shareability
from kiro_crew.mcp_gateway import verdict_cache as vc
from kiro_crew.mcp_gateway.hashing import ENV_SCRUB_PREFIXES
from kiro_crew.mcp_gateway.record_json import finite_float
from kiro_crew.mcp_gateway.shareability import ShareEvidence, Strength, assess


def _ident(**over: object) -> vc.Identity:
    base: dict = {
        "command_args_hash": "cmd1",
        "env_hash": "env1",
        "binary_version": "1.0.0",
    }
    base.update(over)
    return vc.Identity(**base)  # type: ignore[arg-type]


def _verdict() -> vc.CachedPreflight:
    return vc.CachedPreflight(ran=True, caller_sensitive=False, reasons=(), evaluated_at=1.0)


def _codes(verdict: shareability.ShareVerdict) -> set[str]:
    return {r.code for r in verdict.reasons}


class TestRefutationOutranksEverything:
    def test_observed_hazard_beats_a_positive_declaration(self) -> None:
        """A server may declare caller-identity and still be caught misbehaving.

        This ordering is the whole point of the ledger: a declaration is a
        promise, an observed hazard is what actually happened.
        """
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                capabilities={"experimental": {shareability.CALLER_IDENTITY_CAPABILITY: {}}},
                observed_hazards=(hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,),
            )
        )
        assert verdict.strength is Strength.REFUTED
        assert not verdict.recommend_stub
        assert not verdict.recommend_share
        assert _codes(verdict) == {"observed_hazard"}


class TestDisqualifiers:
    def test_rotating_secret_env_is_reported_even_without_a_probe(self) -> None:
        """A config fact stands whether or not the server could be started.

        It is no longer a disqualification. A secret-prefixed key is never
        forwarded into a SHARED backend at all -- ``_declared_non_secret_env``
        drops it because ``ENV_SCRUB_PREFIXES`` makes the pool hash non-injective
        over these keys, so no single value is correct -- which means the pooled
        backend receives NOBODY's secret rather than the wrong session's. Nothing
        crosses tenants. What breaks is a server that authenticates FROM declared
        env, and it breaks loudly at its own auth layer.

        And a server following the documented pattern (read the credential from
        disk) declares the key without needing it at runtime, so it pools fine --
        the case a disqualification got wrong, and the expensive kind of mistake
        for a layer whose job is to say yes.

        Reported BEFORE the probe gate on purpose: on a host where the probe
        cannot run, naming the config fact beats saying only "unknown".
        """
        verdict = assess(
            ShareEvidence(name="x", probe_ok=False, declared_env_names=("AWS_SECRET_ACCESS_KEY",))
        )
        assert verdict.strength is Strength.UNKNOWN
        assert _codes(verdict) == {"not_probed", "rotating_secret_env"}
        rotating = [r for r in verdict.reasons if r.code == "rotating_secret_env"]
        assert [r.detail for r in rotating] == ["AWS_SECRET_ACCESS_KEY"]

    def test_rotating_secret_env_is_a_note_that_follows_the_rewriter(self) -> None:
        """Not a disqualifier, and still not auto-shared. Both halves matter.

        Not disqualifying, because it is not a leak: a secret-prefixed key is never
        forwarded into a shared backend at all, so a pooled backend receives
        NOBODY's secret rather than the wrong session's. A server reading its
        credential from disk -- the documented pattern -- declares the key without
        consuming it and pools perfectly well.

        Still not auto-shared, and this is agreement rather than caution. The
        rewriter ALREADY refuses to pool such an entry: ``_withheld_env_count``
        counts the keys a shared backend would not receive, a non-zero count leaves
        the entry unwrapped, and ``_stub_eligibility`` reports that as
        ``pooling_blocked_by_env``. Recommending a share the rewriter will decline
        would have the page promise work the broker never does. When that guard
        changes, this withholding goes with it.
        """
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                declared_env_names=("OAUTH_TOKEN",),
                capabilities={"experimental": {shareability.CALLER_IDENTITY_CAPABILITY: {}}},
                preflight_ran=True,
            )
        )
        assert verdict.strength is Strength.DECLARED
        assert verdict.recommend_stub is True
        assert verdict.recommend_share is False
        assert "rotating_secret_env" in _codes(verdict)

    def test_a_named_identity_key_stops_withholding_the_share(self) -> None:
        """The withholding follows the guard, in BOTH directions.

        The test above pins that the page declines a share the rewriter declines.
        Once ``mcp_gateway.pool_identity_env`` names the key the rewriter DOES pool
        the entry (``_withheld_env_count`` stops counting it, because the key is
        now inside ``effective_env_hash`` and really is forwarded). If the note
        survived that, the page would decline a share the broker performs -- the
        same two-components-disagreeing failure, mirrored.
        """
        evidence = ShareEvidence(
            name="x",
            probe_ok=True,
            has_tools=True,
            declared_env_names=("OAUTH_TOKEN",),
            capabilities={"experimental": {shareability.CALLER_IDENTITY_CAPABILITY: {}}},
            preflight_ran=True,
        )
        verdict = assess(evidence, {"OAUTH_TOKEN"})
        assert verdict.recommend_share is True
        assert "rotating_secret_env" not in _codes(verdict)
        # A DIFFERENT name in the list must not silence this key's note.
        assert "rotating_secret_env" in _codes(assess(evidence, {"OTHER_TOKEN"}))

    def test_a_broker_gap_does_not_borrow_that_withholding(self) -> None:
        """The two must not collapse into one rule.

        ``rotating_secret_env`` withholds because a live guard declines the work.
        A broker gap has no such guard -- pooling proceeds and log verbosity simply
        follows the last caller's level -- so it must stay pure information.
        Sharing one flag between them would silently re-introduce the disqualifier
        this change removed.
        """
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={
                    "experimental": {shareability.CALLER_IDENTITY_CAPABILITY: {}},
                    "logging": {},
                },
                preflight_ran=True,
            )
        )
        assert verdict.recommend_share is True
        assert "degrades_when_shared" in _codes(verdict)

    def test_rotating_prefixes_cover_every_pool_key_exclusion(self) -> None:
        """Ratchet: hashing's scrub list is what makes co-tenants disagree.

        If ``hashing`` grows a prefix this module does not know about, a server
        authenticating from that variable would be recommended for sharing
        while the pool key ignores its value — the exact incorrectness this
        disqualifier exists to prevent.
        """
        for prefix in ENV_SCRUB_PREFIXES:
            assert shareability.rotating_secret_env((prefix + "_X",)) == (prefix + "_X",), prefix

    def test_a_session_bound_server_is_never_recommended(self) -> None:
        """The disqualifier is the identity mechanism, not the authorship.

        ``kirocrew-cron`` is the real shape: one of ours, and still reading its
        channel identity from process env. The sibling test below pins the other
        half -- that being ours is NOT itself a disqualifier.

        Why this one stayed a disqualifier when three others became notes: on a
        shared backend such a server reads EMPTY, and empty is not benign here
        because the consumer treats it as privileged.
        ``mcp_cron._check_cron_job_ownership`` returns None -- allow -- when the
        session key is falsy, so a pooled cron skips the ownership check entirely
        and one session could list, pause or remove another's jobs. That is a
        cross-session authorization failure rather than a lost feature.

        It is also the one case the "share and retreat" posture cannot cover: both
        hazard codes are routing-shaped, so serving the wrong session's data emits
        no unroutable frame and produces no ledger entry. No retreat exists to fall
        back on, which is what earns the gate.
        """
        verdict = assess(
            ShareEvidence(
                name="kirocrew-cron", session_bound_by_construction=True, probe_ok=True
            )
        )
        assert verdict.strength is Strength.DISQUALIFIED
        assert _codes(verdict) == {"session_bound_by_construction"}
        assert verdict.recommend_share is False

    def test_a_managed_server_that_consumes_the_caller_block_is_not_disqualified(self) -> None:
        """Regression: ``kirocrew-core`` was disqualified for being ours.

        It advertises the caller-identity extension and resolves the session from
        the injected caller block, which is exactly the property that makes a
        shared backend correct — so the verdict must be the positive one. Keying
        the disqualifier on the name inverted the answer for the one server in the
        set that was built for pooling.
        """
        verdict = assess(
            ShareEvidence(
                name="kirocrew-core",
                session_bound_by_construction=False,
                probe_ok=True,
                capabilities={"experimental": {shareability.CALLER_IDENTITY_CAPABILITY: {}}},
                preflight_ran=True,
            )
        )
        assert verdict.strength is Strength.DECLARED
        assert verdict.recommend_share
        assert "first_party_session_scoped" not in _codes(verdict)

    def test_non_stdio_is_out_of_scope_not_unsafe(self) -> None:
        verdict = assess(ShareEvidence(name="x", is_stdio=False, probe_ok=True))
        assert _codes(verdict) == {"not_stdio"}

    def test_resources_subscribe_no_longer_produces_a_degradation_note(self) -> None:
        """Subscriptions survive pooling, so there is nothing to warn about.

        The broker keeps a ``uri -> {stub_uuid}`` table
        (``backend._resource_subscriptions``) and routes each
        ``notifications/resources/updated`` to exactly the stubs subscribed to
        its URI. The degradation table's contract is delete-not-relax: an
        entry leaves the moment the broker learns to attribute the frames it
        covers, and this test pins ``resources.subscribe``'s absence. A
        subscribe-capable server is otherwise unremarkable evidence and lands
        on the ordinary no-objection tier.
        """
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={"resources": {"subscribe": True}},
            )
        )
        assert verdict.strength is Strength.NO_OBJECTION
        assert verdict.recommend_stub is True
        assert verdict.recommend_share is False
        notes = [r for r in verdict.reasons if r.code == "degrades_when_shared"]
        assert notes == []

    def test_list_changed_alone_is_not_a_disqualifier(self) -> None:
        """Those notifications are global broadcasts, safe to fan out."""
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={
                    "tools": {"listChanged": True},
                    "prompts": {"listChanged": True},
                    "resources": {"listChanged": True},
                },
            )
        )
        assert verdict.strength is Strength.NO_OBJECTION

    def test_declaring_logging_is_detected_but_does_not_disqualify(self) -> None:
        """Two claims, and only one of them survived the epistemics change.

        STILL TRUE: in MCP an empty object ADVERTISES a capability rather than
        withholding it, so ``{"logging": {}}`` means ``logging/setLevel`` is
        supported. Testing truthiness here (an earlier version of this module)
        read that server as one that does not log at all, so the ``present`` mode
        must keep detecting both shapes.

        NO LONGER TRUE: that this disqualifies. The cost of pooling a logging
        server is that the last caller's level wins for everyone, and that a log
        notification tied to one caller's in-flight call is dropped rather than
        broadcast. That is log volume and lost log lines. No co-tenant ever
        receives another tenant's content, so there is nothing here to condemn
        the server for -- and what it predicts is precisely what
        ``HAZARD_UNATTRIBUTABLE_NOTIFICATION`` records if it ever happens for
        real, on evidence worth more than this guess.
        """
        for caps in ({"logging": {}}, {"logging": {"level": "info"}}):
            verdict = assess(
                ShareEvidence(name="x", probe_ok=True, has_tools=True, capabilities=caps)
            )
            assert verdict.strength is Strength.NO_OBJECTION, caps
            assert verdict.recommend_stub is True, caps
            notes = [r for r in verdict.reasons if r.code == "degrades_when_shared"]
            assert [r.detail for r in notes] == ["logging_level"], caps

    def test_absent_logging_key_does_not_disqualify(self) -> None:
        verdict = assess(
            ShareEvidence(name="x", probe_ok=True, has_tools=True, capabilities={"tools": {}})
        )
        assert verdict.strength is Strength.NO_OBJECTION


class TestUnknownVersusNoObjection:
    def test_never_probed_is_unknown_not_recommendable(self) -> None:
        verdict = assess(ShareEvidence(name="x", probe_ok=False))
        assert verdict.strength is Strength.UNKNOWN
        assert not verdict.recommend_stub
        assert _codes(verdict) == {"not_probed"}

    def test_probe_ok_with_no_capabilities_object_is_still_unknown(self) -> None:
        """``capabilities=None`` means we never saw a handshake result."""
        verdict = assess(ShareEvidence(name="x", probe_ok=True, capabilities=None))
        assert verdict.strength is Strength.UNKNOWN


class TestPositiveDeclaration:
    def test_caller_identity_plus_passed_preflight_recommends_both(self) -> None:
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={"experimental": {shareability.CALLER_IDENTITY_CAPABILITY: {}}},
                preflight_ran=True,
            )
        )
        assert verdict.strength is Strength.DECLARED
        assert verdict.recommend_stub and verdict.recommend_share
        assert "declares_caller_identity" in _codes(verdict)
        assert "preflight_passed" in _codes(verdict)

    def test_declaration_without_a_run_preflight_does_not_grant_sharing(self) -> None:
        """A promise nobody has tested is not evidence.

        The whole point of anticipating is that a real session must not be the
        first thing to discover the server was wrong about itself.
        """
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={"experimental": {shareability.CALLER_IDENTITY_CAPABILITY: {}}},
                preflight_ran=None,
            )
        )
        assert verdict.strength is Strength.DECLARED
        assert verdict.recommend_stub is True
        assert verdict.recommend_share is False
        assert "preflight_not_run" in _codes(verdict)

    def test_a_divergence_does_not_beat_the_declaration(self) -> None:
        """The inversion this refactor is about, on its sharpest case.

        This test used to assert the opposite, on the reasoning that a
        measurement outranks a promise. It does -- when the measurement measured
        something. Two spawns under two different ``clientInfo`` values cannot:
        an answer computed from the caller and an answer that varies for the
        server's own reasons both produce a difference, and the variable is never
        isolated.

        For a server that ADVERTISES caller-identity the reading is weaker still,
        because answering two callers differently is the advertised behaviour
        working. So the divergence rides along as a note and the declaration
        stands. The thing that can still overrule a declaration is an entry in
        the hazard ledger, which is an event rather than an inference.
        """
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={"experimental": {shareability.CALLER_IDENTITY_CAPABILITY: {}}},
                preflight_ran=True,
                preflight_caller_sensitive=True,
            )
        )
        assert verdict.strength is Strength.DECLARED
        assert verdict.recommend_stub is True
        assert "handshake_not_reproducible" in _codes(verdict)
        assert "declares_caller_identity" in _codes(verdict)
        # And it withholds NOTHING. This layer's job is to turn pooling on for an
        # operator who never got round to it, so "no" is its failure mode, not its
        # caution -- and a note that is re-derived every pass would make
        # eligibility flap with the last sample. Pure information.
        assert verdict.recommend_share is True
        # And it does NOT also claim the pass found nothing. Both reasons on one
        # row would read as "answered identically" and "did not answer
        # identically" about the same server.
        assert "preflight_passed" not in _codes(verdict)

    def test_logging_informs_but_does_not_withhold_sharing(self) -> None:
        """Noisier logs are not worth refusing pooling over.

        The cost of pooling a logging server is a shared verbosity level and some
        dropped call-scoped log lines: degraded, not broken. Withholding the
        automatic action here would trade something valuable for something cheap,
        which is the trade this layer exists to stop making.
        """
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={
                    "experimental": {shareability.CALLER_IDENTITY_CAPABILITY: {}},
                    "logging": {},
                },
                preflight_ran=True,
            )
        )
        assert verdict.strength is Strength.DECLARED
        assert verdict.recommend_share is True
        assert "degrades_when_shared" in _codes(verdict)

    def test_a_broker_gap_is_reported_but_does_not_withhold_sharing(self) -> None:
        """The note names OUR missing feature, so it cannot be the server's cost.

        ``logging/setLevel`` is process-global, but a proxy can emit at the
        finest level any tenant asked for and filter DOWN per stub, giving each
        tenant the verbosity it requested from one process. The broker does not
        do that today -- that is the defect, and it is ours. Withholding pooling
        here would charge the operator for work we have not done, which is the
        failure mode of a layer whose whole job is to say yes.

        The note still ships, because until the broker learns to attribute it,
        log verbosity really does follow the last caller's level once pooled,
        and the operator is entitled to know that before pressing a bulk action.
        The degradation table's contract is delete-not-relax: an entry leaves
        the moment the broker attributes the frames it covers, as
        ``resources.subscribe``'s absence demonstrates.
        """
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={
                    "experimental": {shareability.CALLER_IDENTITY_CAPABILITY: {}},
                    "logging": {},
                },
                preflight_ran=True,
            )
        )
        assert verdict.strength is Strength.DECLARED
        assert verdict.recommend_stub is True
        assert verdict.recommend_share is True
        assert "degrades_when_shared" in _codes(verdict)


class TestNoObjectionSplitsStubFromShare:
    def test_absence_of_evidence_recommends_stub_but_not_share(self) -> None:
        """The load-bearing product decision.

        Stubbing keeps the backend 1:1 with the session — same topology as no
        gateway — and is what unlocks server-authored UI. Sharing is the step
        that introduces co-tenancy. On weak evidence we offer the safe half.
        """
        verdict = assess(ShareEvidence(name="x", probe_ok=True, has_tools=True, capabilities={}))
        assert verdict.strength is Strength.NO_OBJECTION
        assert verdict.recommend_stub is True
        assert verdict.recommend_share is False

    def test_read_only_annotations_are_reported_as_supporting_evidence(self) -> None:
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={},
                protocol_version="2025-06-18",
                tool_annotations=[{"readOnlyHint": True}, {"readOnlyHint": True}],
            )
        )
        assert "all_tools_read_only" in _codes(verdict)

    def test_one_writing_tool_removes_the_read_only_claim(self) -> None:
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={},
                tool_annotations=[{"readOnlyHint": True}, {"readOnlyHint": False}],
            )
        )
        assert "all_tools_read_only" not in _codes(verdict)

    def test_missing_annotations_are_reported_as_unavailable_not_as_writes(self) -> None:
        """An older protocol version cannot send annotations; say so."""
        verdict = assess(
            ShareEvidence(
                name="x",
                probe_ok=True,
                has_tools=True,
                capabilities={},
                protocol_version="2024-11-05",
            )
        )
        codes = _codes(verdict)
        assert "no_tool_annotations" in codes
        assert "all_tools_read_only" not in codes
        detail = next(r.detail for r in verdict.reasons if r.code == "no_tool_annotations")
        assert detail == "2024-11-05"


class TestMeasurementCanEarnAVerdict:
    """The rung that keeps a third-party server from being stuck for ever.

    ``kirocrew.caller-identity`` is our own extension and the MCP base protocol has
    no equivalent, so a third-party server cannot reach ``DECLARED`` no matter how
    well it behaves. Before this tier the pre-flight could only ever take a verdict
    away (``caller_sensitive_initialize``); provoking a server and finding NO
    divergence recorded nothing.

    What it must NOT do is recommend sharing -- see
    ``test_a_measurement_does_not_recommend_sharing``.
    """

    def test_a_passed_preflight_earns_its_own_tier_without_any_declaration(self) -> None:
        verdict = assess(
            ShareEvidence(
                name="third-party",
                probe_ok=True,
                has_tools=True,
                capabilities={},
                preflight_ran=True,
            )
        )
        assert verdict.strength is Strength.MEASURED
        assert verdict.recommend_stub is True
        assert "preflight_passed" in _codes(verdict)

    def test_a_measurement_does_not_recommend_sharing(self) -> None:
        """The limit that keeps this tier honest, not caution.

        The pre-flight compares the HANDSHAKE and never makes a tool call. A
        server whose state is process-global -- one browser context, one database
        connection, one working directory -- replays that handshake identically for
        two callers and still cannot serve two sessions: on a shared backend one
        caller reads state another caller wrote. So a measurement is a fact about
        DETERMINISM while a declaration is a claim about ISOLATION, and only the
        second is grounds for co-tenancy.

        Nothing catches this afterwards either: the hazard ledger's codes describe
        frames the gateway could not route, not state handed to the wrong session,
        so a wrong ``recommend_share`` here would never be refuted.
        """
        verdict = assess(
            ShareEvidence(
                name="stateful-but-deterministic",
                probe_ok=True,
                has_tools=True,
                capabilities={},
                preflight_ran=True,
            )
        )
        assert verdict.strength is Strength.MEASURED
        assert verdict.recommend_share is False

    def test_only_a_declaration_plus_a_measurement_recommends_sharing(self) -> None:
        """The contrast, pinned in one place so the two tiers cannot converge."""
        declared = assess(
            ShareEvidence(
                name="declares-it",
                probe_ok=True,
                has_tools=True,
                capabilities={"experimental": {shareability.CALLER_IDENTITY_CAPABILITY: {}}},
                preflight_ran=True,
            )
        )
        assert declared.strength is Strength.DECLARED
        assert declared.recommend_share is True

    def test_an_unrun_preflight_still_reads_as_absence_of_evidence(self) -> None:
        """``preflight_ran=None`` is "nobody asked", not "asked and found nothing"."""
        verdict = assess(
            ShareEvidence(name="third-party", probe_ok=True, has_tools=True, capabilities={})
        )
        assert verdict.strength is Strength.NO_OBJECTION
        assert verdict.recommend_share is False
        assert "no_objection_found" in _codes(verdict)

    def test_a_preflight_that_could_not_run_does_not_promote(self) -> None:
        """A pre-flight blocked by the moment (missing credential, dead tunnel).

        ``evaluate`` reports that as ``ran=False`` and deliberately does not cache
        it, so it must not read as supporting evidence here either.
        """
        verdict = assess(
            ShareEvidence(
                name="third-party",
                probe_ok=True,
                has_tools=True,
                capabilities={},
                preflight_ran=False,
            )
        )
        assert verdict.strength is Strength.NO_OBJECTION
        assert verdict.recommend_share is False

    def test_a_degradation_note_does_not_lower_the_tier(self) -> None:
        """A note travels with the verdict; it does not replace it.

        Previously this server was DISQUALIFIED and the clean measurement was
        discarded, because the note was modelled as an objection that outranked
        it. Both facts are now reported at once: the measurement earned the tier,
        and the operator still gets told what pooling would cost.
        """
        verdict = assess(
            ShareEvidence(
                name="third-party",
                probe_ok=True,
                has_tools=True,
                capabilities={"logging": {}},
                preflight_ran=True,
            )
        )
        assert verdict.strength is Strength.MEASURED
        assert "degrades_when_shared" in _codes(verdict)
        assert "preflight_passed" in _codes(verdict)

    def test_a_divergence_does_not_earn_measured(self) -> None:
        """MEASURED's whole content is that something was ruled OUT.

        A pass that ran and saw the handshake differ ruled nothing out, so it must
        not collect the tier that claims otherwise, and must not claim
        ``preflight_passed`` either. It lands on NO_OBJECTION carrying the note:
        no durable objection exists, and one sample looked odd.
        """
        verdict = assess(
            ShareEvidence(
                name="third-party",
                probe_ok=True,
                has_tools=True,
                capabilities={},
                preflight_ran=True,
                preflight_caller_sensitive=True,
            )
        )
        assert verdict.strength is Strength.NO_OBJECTION
        assert "handshake_not_reproducible" in _codes(verdict)
        assert "preflight_passed" not in _codes(verdict)
        assert verdict.recommend_stub is True
        assert verdict.recommend_share is False

    def test_measured_still_reports_the_supporting_annotation_evidence(self) -> None:
        """The extra reasons are the same set; only the headline tier changes."""
        verdict = assess(
            ShareEvidence(
                name="third-party",
                probe_ok=True,
                has_tools=True,
                capabilities={},
                protocol_version="2025-06-18",
                tool_annotations=[{"readOnlyHint": True}],
                preflight_ran=True,
            )
        )
        assert verdict.strength is Strength.MEASURED
        assert "all_tools_read_only" in _codes(verdict)


class TestHazardLedger:
    def test_record_flush_and_reload_round_trip(self, tmp_path) -> None:
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        assert led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST) is True
        assert led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST) is False
        led.flush()

        fresh = hazards.load_ledger(tmp_path)
        assert fresh.codes_for_name("srv") == (hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,)
        assert fresh.codes_for_name("other") == ()

    def test_unknown_code_is_refused(self, tmp_path) -> None:
        """The vocabulary is a UI contract; a typo must not disqualify forever."""
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        assert led.record("srv", "made_up") is False
        assert led.codes_for_name("srv") == ()
        led.flush()
        assert not hazards.ledger_path(tmp_path).exists()

    def test_missing_file_reads_as_no_hazards(self, tmp_path) -> None:
        assert hazards.load_ledger(tmp_path).as_dict() == {}

    def test_corrupt_file_reads_as_no_hazards(self, tmp_path) -> None:
        hazards.ledger_path(tmp_path).write_text("{not json", encoding="utf-8")
        assert hazards.load_ledger(tmp_path).as_dict() == {}

    def test_future_schema_is_ignored_rather_than_guessed(self, tmp_path) -> None:
        hazards.ledger_path(tmp_path).write_text(
            json.dumps({"schema": 99, "servers": {"srv": {"codes": ["whatever"]}}}),
            encoding="utf-8",
        )
        assert hazards.load_ledger(tmp_path).as_dict() == {}

    def test_unknown_codes_are_dropped_on_read(self, tmp_path) -> None:
        hazards.ledger_path(tmp_path).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "servers": {
                        "a": {"codes": ["made_up"], "count": 3},
                        "b": {"codes": [hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION]},
                    },
                }
            ),
            encoding="utf-8",
        )
        led = hazards.load_ledger(tmp_path)
        assert led.codes_for_name("a") == ()
        assert led.codes_for_name("b") == (hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION,)

    def test_flush_is_a_no_op_when_clean(self, tmp_path) -> None:
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.flush()
        assert not hazards.ledger_path(tmp_path).exists()

    def test_an_observation_during_the_write_is_not_marked_persisted(self, tmp_path) -> None:
        """The flush runs off-loop while ``record`` keeps running on it.

        Clearing dirty unconditionally would drop an observation that landed
        mid-write: the ledger would look clean while the file lacked the entry, so
        a hazard the gateway really saw would never withdraw a recommendation.
        """
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST)

        real_write = hazards.atomic_write

        def racing_write(path, content, **kw):  # noqa: ANN001
            # Simulate the loop recording while this thread is mid-write.
            led.record("other", hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)
            return real_write(path, content, **kw)

        hazards.atomic_write = racing_write  # type: ignore[assignment]
        try:
            led.flush()
        finally:
            hazards.atomic_write = real_write  # type: ignore[assignment]

        assert led._dirty is True, "the concurrent observation was marked persisted"
        led.flush()
        assert hazards.load_ledger(tmp_path).codes_for_name("other") == (
            hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION,
        )

    def test_concurrent_flushes_cannot_revert_the_file(self, tmp_path) -> None:
        """Two flush callers race, and the loser must not be the newer snapshot.

        The periodic sweep and the shutdown flush both run in worker threads and
        can overlap — cancellation starts the second while the first is mid-write.
        Each builds its payload from the in-memory records BEFORE writing, so
        without serialization the thread that writes last can be the one that read
        first, silently reverting an observation already on disk.
        """
        import threading

        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("first", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST)

        real_write = hazards.atomic_write
        entered = threading.Event()
        release = threading.Event()

        def slow_write(path, text):
            # Hold the first writer inside the critical section long enough for a
            # second flush to attempt an overlap.
            if not entered.is_set():
                entered.set()
                release.wait(2)
            return real_write(path, text)

        hazards.atomic_write = slow_write  # type: ignore[assignment]
        try:
            t = threading.Thread(target=led.flush)
            t.start()
            assert entered.wait(2), "the first flush never reached the write"
            # A newer observation lands, then a second flush races the first.
            led.record("second", hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)
            second = threading.Thread(target=led.flush)
            second.start()
            release.set()
            t.join(5)
            second.join(5)
        finally:
            hazards.atomic_write = real_write  # type: ignore[assignment]
            release.set()

        on_disk = hazards.load_ledger(tmp_path).as_dict()
        assert "first" in on_disk
        assert "second" in on_disk, "the newer observation was reverted by an older flush"

    def test_ledger_feeds_the_verdict(self, tmp_path) -> None:
        """End to end: what gatewayd observed withdraws the recommendation."""
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)
        led.flush()

        observed = hazards.load_ledger(tmp_path).codes_for_name("srv")
        verdict = assess(
            ShareEvidence(
                name="srv", probe_ok=True, capabilities={}, observed_hazards=observed
            )
        )
        assert verdict.strength is Strength.REFUTED


class TestHostileRecordNumbers:
    """A record file is not a protocol message -- nothing upstream validates it.

    Both records are loaded during gateway startup, so a loader that raises
    takes the daemon down before it binds its socket. ``isinstance(v, (int,
    float))`` reads as a sufficient guard and is not: JSON parses a bare number
    into ``int``, which has no size limit, and ``float()`` of a large enough one
    raises ``OverflowError`` -- an ``ArithmeticError``, so it escapes a guard
    spelled ``except (TypeError, ValueError)``.
    """

    #: 310 digits: past the largest representable double, still valid JSON.
    HUGE = "9" * 310

    def test_overflowing_int_is_not_a_float_conversion_away(self) -> None:
        """The premise, stated as a test so a Python change cannot silently void it."""
        import json

        parsed = json.loads(f'{{"n": {self.HUGE}}}')["n"]
        assert isinstance(parsed, int)
        with pytest.raises(OverflowError):
            float(parsed)
        assert not issubclass(OverflowError, (TypeError, ValueError))

    def test_the_string_form_yields_infinity_instead_of_raising(self) -> None:
        """The other half: no exception, but a timestamp nothing can order."""
        assert float(self.HUGE) == float("inf")
        assert finite_float(float("inf")) == 0.0
        assert finite_float(float("nan")) == 0.0

    def test_a_huge_timestamp_does_not_take_the_ledger_down(self, tmp_path) -> None:
        hazards.ledger_path(tmp_path).write_text(
            '{"schema": 1, "servers": {"srv": {"codes": ["%s"], "count": 1,'
            ' "lastSeen": %s}}}'
            % (hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION, self.HUGE),
            encoding="utf-8",
        )

        led = hazards.load_ledger(tmp_path)

        assert led.codes_for_name("srv") == (hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION,)

    def test_a_huge_timestamp_does_not_take_the_verdict_cache_down(self, tmp_path) -> None:
        # A current-schema identity, so the loader's schema check keeps the row
        # and this test goes on exercising the TIMESTAMP, which is its subject.
        ident = vc.Identity(
            command_args_hash="c", env_hash="e", binary_version="b"
        ).as_str()
        payload = json.dumps(
            {
                "entries": {
                    "srv": {
                        "ran": True,
                        "callerSensitive": False,
                        "reasons": [],
                        "evaluatedAt": 0,
                        "identity": ident,
                    }
                }
            }
        ).replace('"evaluatedAt": 0', '"evaluatedAt": %s' % self.HUGE)
        vc.cache_path(tmp_path).write_text(payload, encoding="utf-8")

        cache = vc.load_cache(tmp_path)

        assert len(cache) == 1

    def test_bool_is_not_accepted_as_a_number(self) -> None:
        """``bool`` is an ``int`` subclass, so a JSON ``true`` would read as 1.0."""
        assert finite_float(True) == 0.0
        assert finite_float(False) == 0.0


class TestNameCollisionDoesNotInheritEvidence:
    """One server owns one row, so the reader is a direct lookup.

    The ambiguity that matters is one NAME covering two different launches in the
    merged agent config: the rows merge, and serving the measured definition's
    verdict to the merged row would hand an unmeasured one its
    ``recommend_share``. That is decided in the row loop, where both definitions
    are visible — not here.
    """

    @staticmethod
    def _write_row(tmp_path: Path, name: str, ident: vc.Identity) -> None:
        cache = vc.VerdictCache(vc.cache_path(tmp_path))
        cache.put(name, ident, vc.CachedPreflight(True, False, (), 1.0))
        cache.flush()

    def test_a_stored_row_is_served_by_name(self, tmp_path, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers import mcp as handler

        self._write_row(tmp_path, "srv", _ident(command_args_hash="cmdA"))
        monkeypatch.setattr(handler, "records_dir", lambda *_a, **_k: tmp_path)

        _observed, preflights = handler._load_shareability_state()

        assert preflights.get("srv") == (True, False)

    def test_a_changed_identity_overwrites_rather_than_accumulating(
        self, tmp_path, monkeypatch
    ) -> None:
        """Editing a server's command leaves ONE row, carrying the new measurement.

        Keying by identity would have left the superseded row behind, which is what
        forced a size cap, an eviction policy, and a newest-wins rule on every
        reader. There is no history to sort through now.
        """
        from kiro_crew.dashboard.handlers import mcp as handler

        cache = vc.VerdictCache(vc.cache_path(tmp_path))
        cache.put(
            "srv",
            _ident(command_args_hash="old"),
            vc.CachedPreflight(True, True, ("caller_sensitive_initialize",), 1.0),
        )
        cache.put(
            "srv",
            _ident(command_args_hash="new"),
            vc.CachedPreflight(True, False, (), 2.0),
        )
        cache.flush()
        assert len(vc.load_cache(tmp_path)) == 1
        monkeypatch.setattr(handler, "records_dir", lambda *_a, **_k: tmp_path)

        _observed, preflights = handler._load_shareability_state()

        assert preflights.get("srv") == (True, False), "the newest measurement wins"

    def test_the_same_definition_in_many_agents_is_not_ambiguous(self) -> None:
        """Listing one server in several agents is the ordinary case.

        Withholding on agent COUNT rather than on distinct launches would cost a
        correct recommendation for every server an operator shares across agents,
        which is most of them. The row keys on the launch hash for that reason.
        """
        from kiro_crew.mcp_gateway.hashing import hash_command

        assert len({hash_command("/bin/a", ["--x"]) for _ in range(5)}) == 1
        assert len({hash_command("/bin/a", []), hash_command("/bin/b", [])}) == 2


class TestNoBlockingRecordIoOnTheLoop:
    """One invariant, not one assertion per site.

    Every filesystem touch of the two shareability records happens off the event
    loop. Three separate rounds of this same finding — the row builder, the
    evaluation pass, then daemon startup — were each closed by patching the
    instance that was reported, because the rule named the blocking helpers in a
    hand-written list. A helper missing from that list was invisible, so the
    fourth instance (deriving cache keys, the most expensive of them) passed a
    green ratchet.

    So the blocking set is DERIVED from the modules themselves: any helper that
    reaches a filesystem or subprocess primitive counts, whether or not anyone
    remembered to list it.
    """

    #: Modules whose helpers own the record files and the binaries behind them.
    RECORD_MODULES = (
        "kiro_crew.mcp_gateway.hazards",
        "kiro_crew.mcp_gateway.verdict_cache",
        "kiro_crew.mcp_gateway.evaluate",
    )

    #: Primitives that make a helper blocking. Deliberately spelled as bare
    #: attribute/function names, because that is how they appear at the call
    #: site regardless of how the module imported them.
    IO_PRIMITIVES = frozenset(
        {
            "open",
            "atomic_write",
            "read_text",
            "write_text",
            "read_bytes",
            "write_bytes",
            "mkdir",
            "unlink",
            "replace",
            "stat",
            "which",
            "run",
            "Popen",
            "binary_fingerprint",
            "communicate",
        }
    )

    #: (module, async function) pairs that reach the records.
    SITES = (
        ("kiro_crew.dashboard.handlers.mcp", "api_mcp_gateway_servers"),
        ("kiro_crew.dashboard.handlers.mcp", "_evaluate_shareability"),
        ("kiro_crew.mcp_gateway.evaluate", "evaluate_new_servers"),
    )

    @classmethod
    def _blocking_helpers(cls) -> set[str]:
        """Names of helpers in the record modules that reach an IO primitive.

        Transitive on purpose. ``load_cache`` touches no primitive itself — it
        delegates to ``VerdictCache.load``, which does. A derivation that only
        looked one level deep, or only at module-level functions, would call the
        public entry points non-blocking and wave through exactly the calls this
        rule exists to stop.
        """
        import ast
        import importlib
        import inspect

        calls: dict[str, set[str]] = {}
        module_level: set[str] = set()
        for module_name in cls.RECORD_MODULES:
            mod = importlib.import_module(module_name)
            tree = ast.parse(inspect.getsource(mod))
            module_level.update(
                node.name
                for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            )
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                called: set[str] = set()
                for inner in ast.walk(node):
                    if not isinstance(inner, ast.Call):
                        continue
                    func = inner.func
                    if isinstance(func, ast.Attribute):
                        called.add(func.attr)
                    elif isinstance(func, ast.Name):
                        called.add(func.id)
                calls.setdefault(node.name, set()).update(called)

        blocking = {n for n, c in calls.items() if c & cls.IO_PRIMITIVES}
        while True:
            grown = {n for n, c in calls.items() if c & blocking}
            if not grown <= blocking:
                blocking |= grown
                continue
            # Methods drive the transitivity above but are not reported: a bare
            # method name is not unique to these modules, and matching one by
            # name alone flagged ``KiroCrewConfig.load()`` — an unrelated loader
            # the whole codebase calls on the loop by design. What another module
            # can actually reach is a module-level entry point, so that is what
            # the caller scans for.
            return blocking & module_level

    def test_the_blocking_set_is_actually_derived(self) -> None:
        """Guard the guard: an empty derivation would pass every check below."""
        found = self._blocking_helpers()
        assert {"load_cache", "load_ledger", "identity_for"} <= found, found

    @pytest.mark.parametrize("module_name,func_name", SITES)
    def test_every_record_touch_is_offloaded(self, module_name: str, func_name: str) -> None:
        import importlib
        import inspect

        blocking = self._blocking_helpers() | {"flush", "install_sink"}
        mod = importlib.import_module(module_name)
        src = inspect.getsource(getattr(mod, func_name))
        for line in src.splitlines():
            code = line.split("#")[0]
            if "to_thread" in code:
                continue
            for name in blocking:
                assert f"{name}(" not in code, (
                    f"{module_name}.{func_name}: {name} does blocking IO and runs "
                    f"on the event loop -- offload it\n    {line.strip()}"
                )

    def test_the_key_derivation_refuses_to_run_on_the_loop(self) -> None:
        """Belt and braces: the ratchet reads source, this catches a live call.

        A reviewer can restructure the call into a shape the line scan does not
        recognise; this fires regardless of how it is spelled.
        """
        import asyncio

        from kiro_crew.mcp_gateway import evaluate

        async def call_it() -> None:
            evaluate.identity_for(
                SimpleNamespace(name="s", command="/bin/true", args=[], env={})
            )

        with pytest.raises(RuntimeError, match="must not run on the event loop"):
            asyncio.run(call_it())

    def test_gatewayd_installs_the_sink_off_loop(self) -> None:
        """Startup counts too: a slow ledger would delay socket readiness."""
        import inspect

        from kiro_crew.mcp_gateway import gatewayd

        src = inspect.getsource(gatewayd.run_gatewayd)
        line = next(ln for ln in src.splitlines() if "install_sink" in ln)
        assert "to_thread" in line, line.strip()

    def test_the_expensive_modules_stay_off_the_gateway_boot_path(self) -> None:
        """``evaluate`` must not be imported at module scope by the handler.

        The handler is on the gateway's boot path, and ``evaluate`` pulls in
        ``preflight`` -> ``mcp_discovery`` and ``stub`` (the stub PROCESS entry
        point). Measured on this tree, hoisting it added 8 modules to that path
        and pushed a startup loop-responsiveness ceiling over on Windows. The
        top-level-imports convention loses to a measured startup regression —
        this is a boot-path decision, NOT an unproven circular-import excuse.

        Asserted at module scope rather than by importing and counting, so the
        test states the rule instead of re-measuring a machine-dependent number.
        """
        import ast
        import inspect

        from kiro_crew.dashboard.handlers import mcp as handler

        tree = ast.parse(inspect.getsource(handler))
        banned = {
            "kiro_crew.mcp_gateway.evaluate",
            "kiro_crew.mcp_gateway.preflight",
            "kiro_crew.mcp_gateway.stub",
        }
        for node in tree.body:  # module scope only
            if isinstance(node, ast.ImportFrom) and node.module in banned:
                raise AssertionError(
                    f"{node.module} imported at module scope (line {node.lineno}) -- "
                    "that puts the stub/preflight chain on the gateway boot path"
                )

    def test_the_new_modules_have_no_function_local_imports(self) -> None:
        """Hoisted to module scope, and proven cycle-free in both load orders.

        A function-local import here is not a style nit: it hides a real cycle if
        one exists, and the convention is to prove the absence instead.
        """
        import ast
        import inspect

        from kiro_crew.mcp_gateway import evaluate, preflight, seed, verdict_cache

        for mod in (shareability, hazards, preflight, verdict_cache, seed, evaluate):
            tree = ast.parse(inspect.getsource(mod))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                for inner in ast.walk(node):
                    if isinstance(inner, (ast.Import, ast.ImportFrom)):
                        raise AssertionError(
                            f"{mod.__name__}.{node.name} imports at function scope "
                            f"(line {inner.lineno}) -- hoist it to module scope"
                        )

    def test_assess_server_is_pure(self) -> None:
        """It takes the loaded state as arguments and opens nothing."""
        import inspect

        from kiro_crew.dashboard.handlers import mcp as handler

        src = inspect.getsource(handler._assess_server)
        for forbidden in ("load_cache", "load_ledger", "KiroCrewConfig", "open("):
            assert forbidden not in src, f"{forbidden} moved back into the row path"
        params = inspect.signature(handler._assess_server).parameters
        assert "preflight" in params, "preflight must be passed in, not looked up"
        assert "observed_hazards" in params

    def test_the_loader_is_awaited_off_loop_exactly_once(self) -> None:
        """One ``to_thread`` call, outside the row loop."""
        import inspect

        from kiro_crew.dashboard.handlers import mcp as handler

        src = inspect.getsource(handler.api_mcp_gateway_servers)
        assert src.count("_load_shareability_state") == 1
        assert "asyncio.to_thread(_load_shareability_state)" in src
        # The call must precede the loop that builds rows, or it is per-row again.
        assert src.index("_load_shareability_state") < src.index("for name in sorted(rows)")

    def test_cache_exposes_names(self) -> None:
        cache = vc.VerdictCache(vc.cache_path(Path("/nonexistent")))
        cache.put("a", _ident(), _verdict())
        cache.put("b", _ident(command_args_hash="other"), _verdict())
        assert cache.server_names() == {"a", "b"}


class TestHazardInvalidation:
    """A hazard must survive what does not change the program, and only that.

    The ledger's neighbour — the pre-flight cache — re-measures when a server's
    launch identity changes. Without the same rule here a single observation
    disqualifies a server for ever, so wiring a pooling refusal to this ledger
    would strand a server on evidence about a version it no longer runs.
    """

    @staticmethod
    def _ident(binary: str = "v1") -> str:
        return hazards.launch_identity("cmd1", "env1", binary)

    def test_an_upgrade_discards_the_prior_observation(self, tmp_path) -> None:
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST, self._ident("v1"))
        assert led.codes_for("srv", self._ident("v1")) != ()

        # Same path, same args, new bytes: the evidence described the old build.
        assert led.codes_for("srv", self._ident("v2")) == ()

    def test_the_discard_is_a_replacement_not_an_accumulation(self, tmp_path) -> None:
        """A new identity starts clean — it does not inherit the old codes.

        Inheriting would make an upgrade look like it exhibited behaviour it
        never showed, which is the same permanence bug wearing a new identity.
        """
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST, self._ident("v1"))
        led.record("srv", hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION, self._ident("v2"))

        assert led.codes_for("srv", self._ident("v2")) == (
            hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION,
        )

    def test_a_draining_backend_cannot_erase_the_new_builds_evidence(
        self, tmp_path
    ) -> None:
        """Two identities are live at once during a blue/green drain.

        The pool keeps the outgoing build serving its attached stubs while the
        incoming one starts, so BOTH can still record. Collapsing them into one
        row per name let a late frame from the draining backend overwrite what
        the new build had already observed — and that reads as "nothing observed"
        for the build actually being kept, the permissive direction.
        """
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        new = self._ident("v2")
        old = self._ident("v1")

        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST, new)
        # The outgoing backend emits one more frame AFTER the new build recorded.
        led.record("srv", hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION, old)

        assert led.codes_for("srv", new) == (
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
        ), "the new build's evidence survived a late frame from the old one"
        assert led.codes_for("srv", old) == (
            hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION,
        ), "each identity keeps its own observations"

        led.flush()
        reloaded = hazards.load_ledger(tmp_path)
        assert reloaded.codes_for("srv", new) == (
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
        )

    def test_an_unchanged_server_keeps_its_hazard(self, tmp_path) -> None:
        """The direction that must NOT clear — otherwise the ledger is useless.

        Two codes under ONE identity, because a single observation cannot tell a
        correct implementation from one that resets on every record: both would
        end up holding exactly the code just written.
        """
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST, self._ident())
        led.record("srv", hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION, self._ident())
        led.flush()

        reloaded = hazards.load_ledger(tmp_path)
        assert reloaded.codes_for("srv", self._ident()) == (
            hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION,
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
        ), "observations under one identity accumulate, they do not replace"

    def test_identity_is_not_the_preflight_identity(self) -> None:
        """The two stores must not share an invalidation trigger.

        ``verdict_cache.Identity`` folds in the pre-flight schema, because a
        measurement is void when the way we measure changes. A hazard is an
        observation of real traffic and stays true however the prober evolves, so
        sharing that field would let a schema bump erase witnessed evidence.
        """
        launch = hazards.launch_identity("cmd1", "env1", "1.0.0")
        preflight = vc.Identity("cmd1", "env1", "1.0.0").as_str()
        assert launch != preflight
        assert str(vc.SCHEMA) not in launch.split("\u0000")

    def test_a_legacy_row_reads_as_unobserved_but_still_shows(self, tmp_path) -> None:
        """A v1 file still loads; its rows just cannot be attributed.

        They must not be deleted — that would discard real evidence to add a
        field — and must not be trusted for a launch either, so they land under
        the empty identity: invisible to the checked read, still surfaced by the
        name-only one so the dashboard keeps showing the withdrawal.
        """
        (tmp_path / hazards.HAZARDS_FILENAME).write_text(
            json.dumps(
                {
                    "schema": 1,
                    "servers": {
                        "srv": {
                            "codes": [hazards.HAZARD_UNROUTABLE_SERVER_REQUEST],
                            "count": 1,
                            "lastSeen": 1.0,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        led = hazards.load_ledger(tmp_path)
        assert led.codes_for("srv", self._ident()) == ()
        assert led.codes_for_name("srv") == (
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
        )

    def test_an_older_reader_still_sees_the_evidence(self, tmp_path) -> None:
        """The written file must not lock out a build that predates identities.

        Make Live can put an earlier worktree back in front of the same data
        home, so a version-gated shape change would read as "future, ignore"
        there and silently drop every observation. The writer therefore keeps the
        schema and emits the flat union alongside the nested map.
        """
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST, self._ident("v1"))
        led.record("srv", hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION, self._ident("v2"))
        led.flush()

        raw = json.loads((tmp_path / hazards.HAZARDS_FILENAME).read_text())
        assert raw["schema"] == 1, "a bump would make an older reader ignore the file"
        entry = raw["servers"]["srv"]
        # What a reader that knows nothing about identities parses:
        assert entry["codes"] == [
            hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION,
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
        ], "the flat union carries every code an older reader would need"
        # ...and the shape this build actually reads, unaffected by it:
        assert set(entry["identities"]) == {self._ident("v1"), self._ident("v2")}

    def test_clear_drops_evidence_that_is_still_current(self, tmp_path) -> None:
        """The escape hatch identity cannot provide.

        A frame this gateway misattributed is our defect; fixing it changes
        nothing about the server, so without an explicit clear the record would
        stand for ever against a server that never misbehaved.
        """
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST, self._ident())
        led.flush()

        assert led.clear("srv") is True
        assert led.clear("srv") is False, "a second clear reports nothing to forget"
        led.flush()
        assert hazards.load_ledger(tmp_path).codes_for_name("srv") == ()

    def test_nothing_expires_by_age(self, tmp_path) -> None:
        """Ageing an observation out would manufacture a safety nothing observed.

        A server that personalises state per caller does not become safe because
        time passed, so a very old hazard on an UNCHANGED identity still stands.
        """
        led = hazards.HazardLedger(hazards.ledger_path(tmp_path))
        led.record("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST, self._ident())
        # Backdate well past any plausible expiry window.
        led._records["srv"][self._ident()].last_seen = 1.0
        led.flush()

        assert hazards.load_ledger(tmp_path).codes_for("srv", self._ident()) == (
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
        )

    def test_the_recording_site_stamps_the_pool_key(self) -> None:
        """The identity must come from the launch, with no IO on the loop.

        ``PoolKey`` already carries the three fingerprints, which is what makes
        stamping free at a site that runs on the event loop.
        """
        from kiro_crew.mcp_gateway import backend as backend_mod

        src = inspect.getsource(backend_mod.Backend._record_hazard)
        assert "launch_identity" in src
        assert "key.command_args_hash" in src and "key.binary_version" in src
        for forbidden in ("open(", "read_bytes", "sha256", "to_thread"):
            assert forbidden not in src, f"{forbidden} would put IO on the loop"


class TestRecordsDirIsNotGatedOnABroker:

    """A machine that never enabled stubbing must still get verdicts.

    Deriving the records directory from ``mcp_gateway.socket_path`` made the
    whole feature inert for exactly its audience: that field is empty until a
    broker is configured, and the recommendation exists to tell an operator
    whether configuring one is safe.
    """

    def test_unconfigured_socket_still_resolves_a_directory(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.mcp_gateway.rewriter import records_dir, runtime_dir

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        assert records_dir("") == runtime_dir()
        assert records_dir("") == tmp_path / "mcp-gateway"

    def test_configured_socket_wins_so_writer_and_reader_agree(self, tmp_path) -> None:
        """gatewayd writes beside its real socket; the page must read there."""
        from kiro_crew.mcp_gateway.rewriter import records_dir

        sock = tmp_path / "custom" / "gateway.sock"
        assert records_dir(str(sock)) == sock.parent
        # gatewayd passes a Path, the dashboard passes a str — both must work.
        assert records_dir(sock) == sock.parent

    def test_an_empty_path_does_not_resolve_to_the_cwd(self, monkeypatch, tmp_path) -> None:
        """``Path("")`` is ``PosixPath(".")`` and therefore truthy.

        Branching on the Path instead of its string form would put the hazard
        ledger in whatever directory the process happened to start in.
        """
        from kiro_crew.mcp_gateway.rewriter import records_dir, runtime_dir

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        assert records_dir(Path("")) == runtime_dir()
        assert records_dir(None) == runtime_dir()  # type: ignore[arg-type]
        assert records_dir(".") == runtime_dir()
        # But a REAL relative socket keeps its own directory — the guard is on
        # the input being bare ".", not on the parent resolving to ".".
        assert records_dir("gateway.sock") == Path(".")

    def test_the_default_socket_lives_in_that_same_directory(self, monkeypatch, tmp_path) -> None:
        from kiro_crew.mcp_gateway.rewriter import default_socket_path, runtime_dir

        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
        assert default_socket_path().parent == runtime_dir()

    def test_neither_reader_nor_trigger_short_circuits_on_an_empty_socket(self) -> None:
        """Ratchet: the early `if not socket_path: return` must not come back."""
        import inspect

        from kiro_crew.dashboard.handlers import mcp as handler

        for fn in (handler._load_shareability_state, handler._evaluate_shareability):
            src = inspect.getsource(fn)
            assert "records_dir" in src, f"{fn.__name__} must use the shared resolver"
            assert "if not socket_path" not in src, f"{fn.__name__} regained the broker gate"


@pytest.mark.parametrize(
    "evidence",
    [
        ShareEvidence(name="a", probe_ok=False),
        ShareEvidence(name="b", probe_ok=True, capabilities={}),
        ShareEvidence(name="c", is_stdio=False),
    ],
)
def test_to_dict_is_json_serialisable(evidence: ShareEvidence) -> None:
    """The verdict crosses an HTTP boundary; it must survive json.dumps."""
    json.dumps(assess(evidence).to_dict())


class TestBackendRecordsHazards:
    """The writer side. Without these the ledger is a declaration, not a signal."""

    @staticmethod
    def _backend(server_name: str, *, exclusive_token: str = "", refcount: int = 2) -> object:
        """A Backend-shaped stand-in for the fields ``_record_hazard`` reads.

        Deliberately not a MagicMock: the point is to pin that the method reads
        ``pool_key.server_name``, the three launch fingerprints,
        ``exclusive_token`` and ``refcount`` — the REAL field names on
        ``Backend`` and ``PoolKey`` — so a rename cannot leave this passing
        against a mock that would answer to anything. That the fingerprints are
        already ON the pool key is what lets the recording site stamp an identity
        without doing IO on the event loop.

        ``refcount`` defaults to 2 because that is the only state in which a
        hazard means anything: two clients attached, so an unattributable frame
        could have reached the wrong one.
        """
        from kiro_crew.mcp_gateway.backend import Backend

        obj = object.__new__(Backend)
        obj.pool_key = SimpleNamespace(  # type: ignore[attr-defined]
            server_name=server_name,
            command_args_hash="cmd1",
            effective_env_hash="env1",
            binary_version="v1",
        )
        obj.exclusive_token = exclusive_token  # type: ignore[attr-defined]
        obj.refcount = refcount  # type: ignore[attr-defined]
        return obj

    def test_shared_backend_records(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setattr(hazards, "_sink", hazards.HazardLedger(hazards.ledger_path(tmp_path)))
        backend = self._backend("srv")
        backend._record_hazard(hazards.HAZARD_UNROUTABLE_SERVER_REQUEST)  # type: ignore[attr-defined]
        hazards.flush_sink()
        assert hazards.load_ledger(tmp_path).codes_for_name("srv") == (
            hazards.HAZARD_UNROUTABLE_SERVER_REQUEST,
        )

    def test_exclusive_backend_does_not_record(self, tmp_path, monkeypatch) -> None:
        """A 1:1 backend legitimately owns its single client.

        The same frame there proves nothing about shareability, so recording it
        would disqualify servers for behaviour that is correct when unshared.
        """
        monkeypatch.setattr(hazards, "_sink", hazards.HazardLedger(hazards.ledger_path(tmp_path)))
        backend = self._backend("srv", exclusive_token="stub-uuid")
        backend._record_hazard(hazards.HAZARD_UNROUTABLE_SERVER_REQUEST)  # type: ignore[attr-defined]
        hazards.flush_sink()
        assert hazards.load_ledger(tmp_path).as_dict() == {}

    @pytest.mark.parametrize("refcount", [0, 1])
    def test_a_pooled_backend_with_one_client_does_not_record(
        self, tmp_path, monkeypatch, refcount: int
    ) -> None:
        """Being poolable is not the same as currently serving two clients.

        A pooled backend serves exactly one client from the moment it starts
        until a second stub attaches, and none at all between sessions. An
        unattributable frame in either state had no second tenant to reach, so it
        says nothing about shareability — and a hazard is permanent, so recording
        one here would kill a server for behaviour that is correct.
        """
        monkeypatch.setattr(hazards, "_sink", hazards.HazardLedger(hazards.ledger_path(tmp_path)))
        backend = self._backend("srv", refcount=refcount)
        backend._record_hazard(hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION)  # type: ignore[attr-defined]
        hazards.flush_sink()
        assert hazards.load_ledger(tmp_path).as_dict() == {}

    def test_no_sink_installed_is_a_silent_no_op(self, monkeypatch) -> None:
        """The stub process and unit tests never install a sink."""
        monkeypatch.setattr(hazards, "_sink", None)
        assert hazards.record_observed("srv", hazards.HAZARD_UNROUTABLE_SERVER_REQUEST) is False
        hazards.flush_sink()  # must not raise

    def test_both_observation_sites_call_the_recorder(self) -> None:
        """Ratchet: the hazard sites must stay wired to the ledger.

        Three sites: the unattributable request-scoped notification drop, the
        unroutable server->client request recycle, and the terminal
        resources/updated drop for a URI nobody subscribed to.

        Asserted on the source because reproducing either frame end to end
        needs a live shared backend and a misbehaving server; the value here is
        catching a future edit that deletes the call while keeping the log line.
        """
        from pathlib import Path as _Path

        import kiro_crew.mcp_gateway.backend as backend_mod

        src = _Path(backend_mod.__file__).read_text(encoding="utf-8")
        assert src.count("self._record_hazard(") == 3, "expected exactly three hazard sites"
        assert "hazards.HAZARD_UNATTRIBUTABLE_NOTIFICATION" in src
        assert "hazards.HAZARD_UNROUTABLE_SERVER_REQUEST" in src
