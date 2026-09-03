"""Provider-neutral session-directive delivery.

The marker path can only be trusted when the provider stamps the tool call with
``_meta.kiro`` identity. A backend that omits it leaves the forgery gate with no
trusted source, so the gate refuses every directive and the whole control plane
(loops, project changes, cards) fails closed. These cover the out-of-band path
that carries the validated payload to the gateway instead, and — the part that
matters most with several chat slots live at once — that a record can only ever
be claimed by the session it was published for.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import session_directive
from kiro_crew.dashboard import directive_queue
from kiro_crew.dashboard.chat_utils import _redact_tool_field
from kiro_crew.dashboard.handlers.sessions import api_session_directive


@pytest.fixture(autouse=True)
def _clean_queue():
    """Every test starts and ends with an empty store — the module holds process
    state, so a leaked record would make a later test pass for the wrong reason."""
    directive_queue.reset()
    yield
    directive_queue.reset()


class TestPublishAndClaim:
    def test_a_published_directive_is_claimable(self):
        directive_queue.publish("sess-a", "monitor_start", {"message": "check CI"})
        rec = directive_queue.claim("sess-a", "monitor_start", {"message": "check CI"})
        assert rec is not None
        assert rec["kind"] == "monitor_start"
        assert rec["args"] == {"message": "check CI"}

    def test_claim_is_single_consume(self):
        """Two consumers racing one session must not both apply the same record —
        that is two armed loops from one request."""
        directive_queue.publish("sess-a", "monitor_start", {"message": "x"})
        assert directive_queue.claim("sess-a", "monitor_start", {"message": "x"}) is not None
        assert directive_queue.claim("sess-a", "monitor_start", {"message": "x"}) is None

    def test_claim_of_an_unknown_session_is_none_not_an_error(self):
        assert directive_queue.claim("never-published", "monitor_start", {}) is None

    def test_unknown_kind_is_refused(self):
        """The only legitimate publishers are Kiro Crew's own directive tools, so
        an unrecognized kind means the request did not come from one."""
        with pytest.raises(ValueError, match="unknown directive kind"):
            directive_queue.publish("sess-a", "rm_rf_everything", {})

    def test_empty_session_key_is_refused(self):
        with pytest.raises(ValueError, match="session_key required"):
            directive_queue.publish("", "monitor_start", {})

    def test_args_are_copied_not_aliased(self):
        """The publisher's dict must not stay live inside the store: a later
        mutation would change what gets applied."""
        args = {"message": "original"}
        directive_queue.publish("sess-a", "monitor_start", args)
        args["message"] = "mutated after publish"
        rec = directive_queue.claim("sess-a", "monitor_start", {"message": "original"})
        assert rec is not None
        assert rec["args"]["message"] == "original"


class TestCorrelation:
    """The record is unforgeable in CONTENT but its TARGET is a header; the marker
    is bound to the right session but is model-visible text. A directive applies
    only where both agree, which is what these lock in."""

    def test_a_record_parked_for_another_session_cannot_be_activated(self):
        """The cross-session attack: a same-uid caller over TCP (or on Windows,
        where there is no AF_UNIX peer to attest) names the victim's session. The
        victim's own turn never emits that payload, so nothing applies."""
        directive_queue.publish("victim", "reset_conversation", {})
        # The victim's turn is doing something else entirely.
        assert directive_queue.claim("victim", "monitor_start", {"message": "mine"}) is None
        # And the planted record is still not applicable by its own kind unless
        # the victim's marker names it -- which is the whole point.
        assert directive_queue.depth("victim") == 1

    def test_a_mismatched_payload_does_not_claim(self):
        directive_queue.publish("sess-a", "monitor_start", {"message": "real"})
        assert directive_queue.claim("sess-a", "monitor_start", {"message": "forged"}) is None
        assert directive_queue.depth("sess-a") == 1

    def test_a_mismatched_kind_does_not_claim(self):
        directive_queue.publish("sess-a", "monitor_start", {"message": "x"})
        assert directive_queue.claim("sess-a", "reset_conversation", {"message": "x"}) is None
        assert directive_queue.depth("sess-a") == 1

    def test_key_order_does_not_defeat_the_match(self):
        """The two channels serialize independently, so the comparison must be
        about the payload rather than about emission order."""
        directive_queue.publish("sess-a", "monitor_start", {"a": 1, "b": 2})
        assert directive_queue.claim("sess-a", "monitor_start", {"b": 2, "a": 1}) is not None

    def test_an_unknown_kind_claims_nothing(self):
        directive_queue.publish("sess-a", "monitor_start", {"message": "x"})
        assert directive_queue.claim("sess-a", "rm_rf_everything", {"message": "x"}) is None
        assert directive_queue.depth("sess-a") == 1

    def test_a_redacted_marker_must_not_be_used_as_the_selector(self):
        """The consumer redacts the tool output before broadcasting it, and that
        scrubber rewrites credential-shaped runs anywhere in the string -- the
        marker's JSON included. The record was parked with the args the tool
        validated, so selecting on the REDACTED copy compares a scrubbed payload
        against a raw one and loses a legitimate directive. Pins the asymmetry so
        the consumer cannot drift back to peeking at the redacted text."""
        args = {"message": "watch with token ghp_A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"}
        raw_out = session_directive.encode("monitor_start", args, "human line")
        redacted_out = _redact_tool_field(raw_out)

        # The premise: redaction really does alter this marker.
        assert redacted_out != raw_out

        # Selecting on the redacted copy finds nothing -- the defect.
        sel_redacted = session_directive.peek(redacted_out)
        assert sel_redacted is not None
        directive_queue.publish("sess-a", "monitor_start", args)
        assert directive_queue.claim("sess-a", sel_redacted[0], sel_redacted[1]) is None

        # Selecting on the raw provider text finds the record -- the fix.
        sel_raw = session_directive.peek(raw_out)
        assert sel_raw is not None
        assert directive_queue.claim("sess-a", sel_raw[0], sel_raw[1]) is not None

    def test_a_truncated_marker_must_not_be_used_as_the_selector(self):
        """Same asymmetry by the other route: the redactor also caps the field, so
        a long-but-legal directive can lose its marker tail entirely."""
        args = {"message": "x" * 400}
        raw_out = session_directive.encode("monitor_start", args, "human line")
        truncated_out = raw_out[: len(raw_out) // 2]

        directive_queue.publish("sess-a", "monitor_start", args)
        assert session_directive.peek(truncated_out) is None
        sel_raw = session_directive.peek(raw_out)
        assert sel_raw is not None
        assert directive_queue.claim("sess-a", sel_raw[0], sel_raw[1]) is not None

    def test_a_record_from_an_abandoned_turn_is_not_claimable_by_a_later_turn(self):
        """A cancelled turn leaves its record parked. Bounding the claim to the
        claiming turn is what stops a stale reset/project/loop landing later."""
        directive_queue.publish("sess-a", "monitor_start", {"message": "x"})
        later_turn_started = time.monotonic() + 1.0
        assert (
            directive_queue.claim(
                "sess-a", "monitor_start", {"message": "x"}, not_before=later_turn_started
            )
            is None
        )

    def test_a_record_from_this_turn_is_claimable(self):
        this_turn_started = time.monotonic()
        directive_queue.publish("sess-a", "monitor_start", {"message": "x"})
        assert (
            directive_queue.claim(
                "sess-a", "monitor_start", {"message": "x"}, not_before=this_turn_started
            )
            is not None
        )

    def test_a_sibling_record_survives_a_claim_that_matches_neither(self):
        """Two directives in one turn: a lookup for something else must not drain
        the queue, or the sibling frame finds nothing."""
        directive_queue.publish("sess-a", "monitor_start", {"message": "first"})
        directive_queue.publish("sess-a", "suggest_followup", {"items": []})
        assert directive_queue.claim("sess-a", "ask_question", {}) is None
        assert directive_queue.depth("sess-a") == 2

    def test_each_frame_claims_its_own_record(self):
        directive_queue.publish("sess-a", "monitor_start", {"message": "first"})
        directive_queue.publish("sess-a", "suggest_followup", {"items": []})
        assert directive_queue.claim("sess-a", "monitor_start", {"message": "first"}) is not None
        assert directive_queue.depth("sess-a") == 1
        assert directive_queue.claim("sess-a", "suggest_followup", {"items": []}) is not None
        assert directive_queue.depth("sess-a") == 0

    def test_two_identical_directives_are_consumed_one_per_frame(self):
        directive_queue.publish("sess-a", "monitor_start", {"message": "same"})
        directive_queue.publish("sess-a", "monitor_start", {"message": "same"})
        assert directive_queue.claim("sess-a", "monitor_start", {"message": "same"}) is not None
        assert directive_queue.claim("sess-a", "monitor_start", {"message": "same"}) is not None
        assert directive_queue.claim("sess-a", "monitor_start", {"message": "same"}) is None


class TestSessionIsolation:
    """The concurrency case: several chat slots arming at once.

    Records are keyed by the session that published them, and a directive only
    ever affects the session that claims it — so nothing can land in the wrong
    slot. Nothing here is shared between the two sessions, which is the point.
    """

    def test_two_sessions_do_not_see_each_others_records(self):
        directive_queue.publish("slot-a", "monitor_start", {"message": "for A"})
        directive_queue.publish("slot-b", "monitor_start", {"message": "for B"})

        rec_a = directive_queue.claim("slot-a", "monitor_start", {"message": "for A"})
        rec_b = directive_queue.claim("slot-b", "monitor_start", {"message": "for B"})

        assert rec_a is not None and rec_a["args"]["message"] == "for A"
        assert rec_b is not None and rec_b["args"]["message"] == "for B"

    def test_a_session_cannot_claim_a_record_parked_for_another(self):
        directive_queue.publish("slot-b", "monitor_start", {"message": "for B"})
        assert directive_queue.claim("slot-a", "monitor_start", {"message": "for B"}) is None
        assert directive_queue.depth("slot-b") == 1

    def test_one_session_claiming_does_not_drain_another(self):
        directive_queue.publish("slot-a", "monitor_start", {"message": "for A"})
        directive_queue.publish("slot-b", "suggest_followup", {"items": []})

        directive_queue.claim("slot-a", "monitor_start", {"message": "for A"})

        assert directive_queue.depth("slot-b") == 1

    def test_discard_is_scoped_to_one_session(self):
        directive_queue.publish("slot-a", "monitor_start", {"message": "a"})
        directive_queue.publish("slot-b", "monitor_start", {"message": "b"})

        assert directive_queue.discard("slot-a") == 1

        assert directive_queue.claim("slot-a", "monitor_start", {"message": "a"}) is None
        assert directive_queue.claim("slot-b", "monitor_start", {"message": "b"}) is not None


class TestDiscard:
    def test_discard_drops_without_applying(self):
        """The kiro-cli path applies from the verified marker; the out-of-band
        twin must be retired or the effect lands twice."""
        directive_queue.publish("sess-a", "monitor_start", {"message": "x"})
        assert directive_queue.discard("sess-a") == 1
        assert directive_queue.claim("sess-a", "monitor_start", {"message": "x"}) is None

    def test_discard_of_nothing_is_zero(self):
        assert directive_queue.discard("sess-a") == 0

    def test_discard_of_empty_key_is_zero(self):
        assert directive_queue.discard("") == 0


class TestBounds:
    def test_the_queue_is_capped_and_keeps_the_newest(self):
        """An unclaimed queue must not grow without limit, and a live session's
        most recent intent is the one worth keeping."""
        for i in range(directive_queue.MAX_PER_SESSION + 3):
            directive_queue.publish("sess-a", "monitor_start", {"message": f"m{i}"})

        assert directive_queue.depth("sess-a") == directive_queue.MAX_PER_SESSION
        # The three oldest were dropped, so m2 is gone and m3 survives.
        assert directive_queue.claim("sess-a", "monitor_start", {"message": "m2"}) is None
        assert directive_queue.claim("sess-a", "monitor_start", {"message": "m3"}) is not None
        newest = f"m{directive_queue.MAX_PER_SESSION + 2}"
        assert directive_queue.claim("sess-a", "monitor_start", {"message": newest}) is not None

    def test_a_bucket_whose_records_all_expired_is_deleted_not_left_empty(self):
        """The unbounded-growth case. Only the dashboard consumer claims -- a
        channel session (Slack/Discord, driven by the marker path) never calls in
        here, so its bucket has no read path and would live for the gateway's whole
        lifetime. Reclaim therefore runs on PUBLISH."""
        directive_queue.publish("channel-sess", "monitor_start", {"message": "x"})
        assert "channel-sess" in directive_queue._pending

        with patch.object(
            directive_queue.time,
            "monotonic",
            return_value=time.monotonic() + directive_queue.MAX_AGE_SECS + 1,
        ):
            directive_queue.publish("someone-else", "monitor_start", {"message": "y"})

        assert "channel-sess" not in directive_queue._pending

    def test_the_publishing_session_is_never_swept_out_from_under_itself(self):
        """A publish must not lose the record it is parking, even when that
        session's earlier records are all expired."""
        directive_queue.publish("sess-a", "monitor_start", {"message": "old"})
        with patch.object(
            directive_queue.time,
            "monotonic",
            return_value=time.monotonic() + directive_queue.MAX_AGE_SECS + 1,
        ):
            directive_queue.publish("sess-a", "monitor_start", {"message": "new"})
            assert directive_queue.depth("sess-a") == 1
            rec = directive_queue.claim("sess-a", "monitor_start", {"message": "new"})
        assert rec is not None

    def test_a_fresh_bucket_for_another_session_is_not_swept(self):
        directive_queue.publish("sess-a", "monitor_start", {"message": "a"})
        directive_queue.publish("sess-b", "monitor_start", {"message": "b"})
        assert directive_queue.depth("sess-a") == 1
        assert directive_queue.depth("sess-b") == 1

    def test_the_session_count_is_capped_and_keeps_the_most_recent(self):
        """The backstop: more distinct sessions publishing inside one expiry window
        than the cap allows. Whole buckets go, least-recently-published first."""
        for i in range(directive_queue.MAX_SESSIONS + 5):
            directive_queue.publish(f"sess-{i:04d}", "monitor_start", {"message": "x"})

        assert len(directive_queue._pending) <= directive_queue.MAX_SESSIONS
        # The publisher of the newest record is always retained.
        newest = f"sess-{directive_queue.MAX_SESSIONS + 4:04d}"
        assert newest in directive_queue._pending
        # The oldest buckets were the ones evicted.
        assert "sess-0000" not in directive_queue._pending

    def test_a_stale_record_is_dropped_rather_than_applied(self):
        """A directive belongs to the turn that asked for it. Applying one long
        after that turn ended would arm a loop nobody is waiting on."""
        directive_queue.publish("sess-a", "monitor_start", {"message": "x"})
        with patch.object(
            directive_queue.time,
            "monotonic",
            return_value=time.monotonic() + directive_queue.MAX_AGE_SECS + 1,
        ):
            assert directive_queue.claim("sess-a", "monitor_start", {"message": "x"}) is None

    def test_a_fresh_record_survives_the_age_check(self):
        directive_queue.publish("sess-a", "monitor_start", {"message": "x"})
        with patch.object(
            directive_queue.time,
            "monotonic",
            return_value=time.monotonic() + (directive_queue.MAX_AGE_SECS / 2),
        ):
            assert directive_queue.claim("sess-a", "monitor_start", {"message": "x"}) is not None


def _request(headers: dict, body: object, can_read: bool = True, local: bool = True):
    """A request double for the endpoint.

    ``local`` models what the auth middleware stamped on the request: an
    internal-secret / kernel-verified peer caller (True) versus a
    cookie-authenticated browser caller on a ``local_only=False`` deployment
    (False). The ``.get`` mapping is backed by a REAL dict on purpose — a bare
    ``MagicMock.get`` returns a truthy mock, which would silently satisfy the
    handler's locality re-assert and make every test here vacuous.
    """
    req = MagicMock(spec=web.Request)
    req.headers = headers
    req.can_read_body = can_read
    req.json = AsyncMock(return_value=body)
    req.app = {"state": MagicMock()}
    _scope: dict = {"internal_auth": True} if local else {}
    req.get = _scope.get
    return req


class TestEndpoint:
    @pytest.mark.asyncio
    async def test_happy_path_parks_the_record_for_the_declared_session(self):
        resp = await api_session_directive(
            _request(
                {"X-Session-Key": "slot-a"},
                {"kind": "monitor_start", "args": {"message": "go"}},
            )
        )
        assert resp.status == 200
        rec = directive_queue.claim("slot-a", "monitor_start", {"message": "go"})
        assert rec is not None
        assert rec["args"] == {"message": "go"}

    @pytest.mark.asyncio
    async def test_missing_session_key_is_400_with_a_code(self):
        resp = await api_session_directive(_request({}, {"kind": "monitor_start", "args": {}}))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_unknown_kind_is_400_and_parks_nothing(self):
        resp = await api_session_directive(
            _request({"X-Session-Key": "slot-a"}, {"kind": "not_a_directive"})
        )
        assert resp.status == 400
        assert directive_queue.depth("slot-a") == 0

    @pytest.mark.asyncio
    async def test_cookie_caller_cannot_park_a_directive_for_a_chosen_session(self):
        """The locality re-assert. On a ``local_only=False`` deployment the auth
        middleware admits a cookie/token caller onto this strict route, and such
        a caller picks its own ``X-Session-Key`` — which would be a cross-session
        mutation once the named turn's consumer applies the record. 403, and the
        queue must stay empty."""
        resp = await api_session_directive(
            _request(
                {"X-Session-Key": "victim-slot"},
                {"kind": "monitor_start", "args": {"message": "go"}},
                local=False,
            )
        )
        assert resp.status == 403
        assert directive_queue.depth("victim-slot") == 0

    @pytest.mark.asyncio
    async def test_kernel_verified_peer_without_the_secret_is_accepted(self):
        """``peer_verified`` alone is enough: the kernel attested the AF_UNIX
        peer's ancestry resolves to the DECLARED key, which is stronger evidence
        for this route than the shared secret."""
        req = _request(
            {"X-Session-Key": "slot-a"},
            {"kind": "monitor_start", "args": {"message": "go"}},
            local=False,
        )
        req.get = {"peer_verified": True}.get
        resp = await api_session_directive(req)
        assert resp.status == 200
        assert directive_queue.depth("slot-a") == 1

    @pytest.mark.asyncio
    async def test_malformed_body_is_400_and_parks_nothing(self):
        req = _request({"X-Session-Key": "slot-a"}, None)
        req.json = AsyncMock(side_effect=ValueError("not json"))
        resp = await api_session_directive(req)
        assert resp.status == 400
        assert directive_queue.depth("slot-a") == 0

    @pytest.mark.asyncio
    async def test_non_dict_args_degrade_to_empty_not_a_crash(self):
        resp = await api_session_directive(
            _request(
                {"X-Session-Key": "slot-a"},
                {"kind": "reset_conversation", "args": "not-a-dict"},
            )
        )
        assert resp.status == 200
        rec = directive_queue.claim("slot-a", "reset_conversation", {})
        assert rec is not None
        assert rec["args"] == {}


class TestEmitHelperPublishes:
    """``control._emit_directive`` must park the payload as well as return the
    marker — the marker alone is what a provider-less backend cannot use."""

    def test_emit_publishes_and_still_returns_the_marker(self):
        from kiro_crew.mcp_tools import control

        posted: list[tuple] = []

        with patch.object(
            control.mcp_core, "_post", side_effect=lambda p, b: posted.append((p, b))
        ):
            out = control._emit_directive("monitor_start", {"message": "hi"}, "Monitor requested.")

        assert posted == [
            ("/api/session-directive", {"kind": "monitor_start", "args": {"message": "hi"}})
        ]
        from kiro_crew import session_directive

        assert session_directive.has_marker(out)

    def test_a_refused_oversized_directive_is_never_published(self):
        """``encode`` refuses an over-limit payload and tells the model nothing was
        applied. Publishing anyway would leave a record that contradicts that."""
        from kiro_crew import session_directive
        from kiro_crew.mcp_tools import control

        posted: list[tuple] = []
        huge = {"message": "x" * (session_directive.MAX_DIRECTIVE_CHARS + 100)}

        with patch.object(
            control.mcp_core, "_post", side_effect=lambda p, b: posted.append((p, b))
        ):
            out = control._emit_directive("monitor_start", huge, "Monitor requested.")

        assert session_directive.is_refusal(out)
        assert posted == []

    def test_a_publish_failure_does_not_break_the_tool(self):
        """An older gateway with no such route, or one that is down, must not turn
        a working tool call into an error — the marker path may still work."""
        from kiro_crew import session_directive
        from kiro_crew.mcp_tools import control

        with patch.object(control.mcp_core, "_post", side_effect=RuntimeError("gateway down")):
            out = control._emit_directive("monitor_start", {"message": "hi"}, "Monitor requested.")

        assert session_directive.has_marker(out)
