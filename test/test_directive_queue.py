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

import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew import session_directive
from kiro_crew.dashboard import directive_queue
from kiro_crew.dashboard.handlers.sessions import api_session_directive


@pytest.fixture(autouse=True)
def _clean_queue():
    """Every test starts and ends with an empty store — the module holds process
    state, so a leaked record would make a later test pass for the wrong reason."""
    directive_queue.reset()
    yield
    directive_queue.reset()


def _dg(args, kind: str = "monitor_start") -> str:
    return session_directive.call_input_digest(kind, args)


def _pub(sess: str, kind: str, args, *, called_with=None) -> str:
    """Park *args* the way the tool does: keyed by the digest of the tool name and
    what the model CALLED it with (``called_with``, defaulting to *args* itself)."""
    return directive_queue.publish(
        sess, kind, args, _dg(args if called_with is None else called_with, kind)
    )


def _claim(sess: str, args, kind: str = "monitor_start", **kw):
    """Claim the way the consumer does: by the digest of the resolved directive tool
    and the tool_call's rawInput."""
    return directive_queue.claim(sess, _dg(args, kind), **kw)


class TestPublishAndClaim:
    def test_a_published_directive_is_claimable(self):
        _pub("sess-a", "monitor_start", {"message": "check CI"})
        rec = _claim("sess-a", {"message": "check CI"})
        assert rec is not None
        assert rec["kind"] == "monitor_start"
        assert rec["args"] == {"message": "check CI"}

    def test_claim_is_single_consume(self):
        """Two consumers racing one session must not both apply the same record —
        that is two armed loops from one request."""
        _pub("sess-a", "monitor_start", {"message": "x"})
        assert _claim("sess-a", {"message": "x"}) is not None
        assert _claim("sess-a", {"message": "x"}) is None

    def test_claim_of_an_unknown_session_is_none_not_an_error(self):
        assert _claim("never-published", {}) is None

    def test_unknown_kind_is_refused(self):
        """The only legitimate publishers are Kiro Crew's own directive tools, so
        an unrecognized kind means the request did not come from one."""
        with pytest.raises(ValueError, match="unknown directive kind"):
            _pub("sess-a", "rm_rf_everything", {})

    def test_empty_session_key_is_refused(self):
        with pytest.raises(ValueError, match="session_key required"):
            _pub("", "monitor_start", {})

    def test_empty_digest_is_refused(self):
        """A record nothing can claim is worse than a 400 to the tool: the model
        would be told the directive was requested and nothing could ever apply it."""
        with pytest.raises(ValueError, match="input_digest required"):
            directive_queue.publish("sess-a", "monitor_start", {}, "")

    def test_empty_digest_claims_nothing(self):
        _pub("sess-a", "monitor_start", {"message": "x"})
        assert directive_queue.claim("sess-a", "") is None
        assert directive_queue.depth("sess-a") == 1

    def test_args_are_copied_not_aliased(self):
        """The publisher's dict must not stay live inside the store: a later
        mutation would change what gets applied."""
        args = {"message": "original"}
        _pub("sess-a", "monitor_start", args)
        args["message"] = "mutated after publish"
        rec = _claim("sess-a", {"message": "original"})
        assert rec is not None
        assert rec["args"]["message"] == "original"


class TestCorrelation:
    """The record is unforgeable in CONTENT but its TARGET is a header; the call
    input is bound to the right session but is model-authored. A directive applies
    only where both agree, which is what these lock in.

    The selector is the digest of the tool CALL's raw arguments, never anything
    read out of the tool RESULT -- so nothing here constructs a result body."""

    def test_a_record_parked_for_another_session_cannot_be_activated(self):
        """The cross-session attack: a same-uid caller over TCP (or on Windows,
        where there is no AF_UNIX peer to attest) names the victim's session. The
        victim's own model never makes a call with that input, so nothing applies."""
        _pub("victim", "reset_conversation", {})
        # The victim's turn is doing something else entirely.
        assert _claim("victim", {"message": "mine"}) is None
        assert directive_queue.depth("victim") == 1

    def test_a_mismatched_input_does_not_claim(self):
        _pub("sess-a", "monitor_start", {"message": "real"})
        assert _claim("sess-a", {"message": "forged"}) is None
        assert directive_queue.depth("sess-a") == 1

    def test_the_records_own_kind_is_what_gets_applied(self):
        _pub("sess-a", "monitor_start", {"message": "x"})
        rec = _claim("sess-a", {"message": "x"})
        assert rec is not None and rec["kind"] == "monitor_start"

    def test_the_same_arguments_to_a_different_tool_do_not_claim(self):
        """Every no-argument tool hashes ``{}``. A planted ``reset_conversation({})``
        must not be claimable by the victim's own ``resource_status({})`` frame, or
        by any other directive tool called with identical arguments: the tool name
        is part of the key."""
        _pub("sess-a", "reset_conversation", {})
        assert _claim("sess-a", {}, kind="monitor_stop") is None
        assert _claim("sess-a", {}, kind="autonudge_stop") is None
        assert directive_queue.depth("sess-a") == 1
        assert _claim("sess-a", {}, kind="reset_conversation") is not None

    def test_key_order_does_not_defeat_the_match(self):
        """The two sides serialize independently, so the digest must be about the
        arguments rather than about emission order."""
        _pub("sess-a", "monitor_start", {"a": 1, "b": 2})
        assert _claim("sess-a", {"b": 2, "a": 1}) is not None

    def test_the_tool_call_frames_meta_block_does_not_defeat_the_match(self):
        """The consumer digests the ACP frame's rawInput, which on KAS carries a
        ``_meta`` block the MCP server never sees (kiro-agent strips it before
        callTool). The digest must ignore it or every KAS directive misses."""
        _pub("sess-a", "monitor_start", {"message": "x"})
        frame_input = {"message": "x", "_meta": {"_isValid": True, "_activePath": []}}
        assert _claim("sess-a", frame_input) is not None

    def test_validated_args_may_differ_from_the_call_input(self):
        """The record CARRIES the validated payload (defaults added, a reason
        clamped) but is KEYED by what the model sent. The consumer only ever sees
        the latter, so that is what it must be able to claim on."""
        called_with = {"reason": "stop " + "x" * 2000}
        validated = {"reason": "stop " + "x" * 100 + " [truncated]"}
        _pub("sess-a", "autonudge_stop", validated, called_with=called_with)
        assert (
            _claim("sess-a", validated, kind="autonudge_stop") is None
        ), "the validated shape is not the key"
        rec = _claim("sess-a", called_with, kind="autonudge_stop")
        assert rec is not None and rec["args"] == validated

    def test_the_result_body_plays_no_part(self):
        """The whole point of this key. A backend may re-serialise, duplicate, cap
        or offload the tool RESULT; none of that reaches the claim, which needs only
        the call input. Encode the worst case (a marker-less, truncated result) and
        show the claim never looks at it."""
        args = {"message": "watch " + "y" * 400}
        out = session_directive.encode("monitor_start", args, "human line")
        mangled = json.dumps({"response": out[: len(out) // 2], "message": out[: len(out) // 3]})
        assert (
            not session_directive.has_marker(mangled)
            or session_directive.decode(mangled, "monitor_start") is None
        )
        _pub("sess-a", "monitor_start", args)
        assert _claim("sess-a", args) is not None

    def test_a_record_from_an_abandoned_turn_is_not_claimable_by_a_later_turn(self):
        """A cancelled turn leaves its record parked. Bounding the claim to the
        claiming turn is what stops a stale reset/project/loop landing later."""
        _pub("sess-a", "monitor_start", {"message": "x"})
        later_turn_started = time.monotonic() + 1.0
        assert _claim("sess-a", {"message": "x"}, not_before=later_turn_started) is None

    def test_a_record_from_this_turn_is_claimable(self):
        this_turn_started = time.monotonic()
        _pub("sess-a", "monitor_start", {"message": "x"})
        assert _claim("sess-a", {"message": "x"}, not_before=this_turn_started) is not None

    def test_a_sibling_record_survives_a_claim_that_matches_neither(self):
        """Two directives in one turn: a lookup for something else must not drain
        the queue, or the sibling frame finds nothing."""
        _pub("sess-a", "monitor_start", {"message": "first"})
        _pub("sess-a", "suggest_followup", {"items": []})
        assert _claim("sess-a", {"questions": []}) is None
        assert directive_queue.depth("sess-a") == 2

    def test_each_frame_claims_its_own_record(self):
        _pub("sess-a", "monitor_start", {"message": "first"})
        _pub("sess-a", "suggest_followup", {"items": []})
        assert _claim("sess-a", {"message": "first"}) is not None
        assert directive_queue.depth("sess-a") == 1
        assert _claim("sess-a", {"items": []}, kind="suggest_followup") is not None
        assert directive_queue.depth("sess-a") == 0

    def test_two_identical_directives_are_consumed_one_per_frame(self):
        _pub("sess-a", "monitor_start", {"message": "same"})
        _pub("sess-a", "monitor_start", {"message": "same"})
        assert _claim("sess-a", {"message": "same"}) is not None
        assert _claim("sess-a", {"message": "same"}) is not None
        assert _claim("sess-a", {"message": "same"}) is None


class TestSessionIsolation:
    """The concurrency case: several chat slots arming at once.

    Records are keyed by the session that published them, and a directive only
    ever affects the session that claims it — so nothing can land in the wrong
    slot. Nothing here is shared between the two sessions, which is the point.
    """

    def test_two_sessions_do_not_see_each_others_records(self):
        _pub("slot-a", "monitor_start", {"message": "for A"})
        _pub("slot-b", "monitor_start", {"message": "for B"})

        rec_a = _claim("slot-a", {"message": "for A"})
        rec_b = _claim("slot-b", {"message": "for B"})

        assert rec_a is not None and rec_a["args"]["message"] == "for A"
        assert rec_b is not None and rec_b["args"]["message"] == "for B"

    def test_a_session_cannot_claim_a_record_parked_for_another(self):
        _pub("slot-b", "monitor_start", {"message": "for B"})
        assert _claim("slot-a", {"message": "for B"}) is None
        assert directive_queue.depth("slot-b") == 1

    def test_one_session_claiming_does_not_drain_another(self):
        _pub("slot-a", "monitor_start", {"message": "for A"})
        _pub("slot-b", "suggest_followup", {"items": []})

        _claim("slot-a", {"message": "for A"})

        assert directive_queue.depth("slot-b") == 1

    def test_discard_is_scoped_to_one_session(self):
        _pub("slot-a", "monitor_start", {"message": "a"})
        _pub("slot-b", "monitor_start", {"message": "b"})

        assert directive_queue.discard("slot-a") == 1

        assert _claim("slot-a", {"message": "a"}) is None
        assert _claim("slot-b", {"message": "b"}) is not None


class TestDiscard:
    def test_discard_drops_without_applying(self):
        """The kiro-cli path applies from the verified marker; the out-of-band
        twin must be retired or the effect lands twice."""
        _pub("sess-a", "monitor_start", {"message": "x"})
        assert directive_queue.discard("sess-a") == 1
        assert _claim("sess-a", {"message": "x"}) is None

    def test_discard_of_nothing_is_zero(self):
        assert directive_queue.discard("sess-a") == 0

    def test_discard_of_empty_key_is_zero(self):
        assert directive_queue.discard("") == 0


class TestBounds:
    def test_the_queue_is_capped_and_keeps_the_newest(self):
        """An unclaimed queue must not grow without limit, and a live session's
        most recent intent is the one worth keeping."""
        for i in range(directive_queue.MAX_PER_SESSION + 3):
            _pub("sess-a", "monitor_start", {"message": f"m{i}"})

        assert directive_queue.depth("sess-a") == directive_queue.MAX_PER_SESSION
        # The three oldest were dropped, so m2 is gone and m3 survives.
        assert _claim("sess-a", {"message": "m2"}) is None
        assert _claim("sess-a", {"message": "m3"}) is not None
        newest = f"m{directive_queue.MAX_PER_SESSION + 2}"
        assert _claim("sess-a", {"message": newest}) is not None

    def test_a_bucket_whose_records_all_expired_is_deleted_not_left_empty(self):
        """The unbounded-growth case. Only the dashboard consumer claims -- a
        channel session (Slack/Discord, driven by the marker path) never calls in
        here, so its bucket has no read path and would live for the gateway's whole
        lifetime. Reclaim therefore runs on PUBLISH."""
        _pub("channel-sess", "monitor_start", {"message": "x"})
        assert "channel-sess" in directive_queue._pending

        with patch.object(
            directive_queue.time,
            "monotonic",
            return_value=time.monotonic() + directive_queue.MAX_AGE_SECS + 1,
        ):
            _pub("someone-else", "monitor_start", {"message": "y"})

        assert "channel-sess" not in directive_queue._pending

    def test_the_publishing_session_is_never_swept_out_from_under_itself(self):
        """A publish must not lose the record it is parking, even when that
        session's earlier records are all expired."""
        _pub("sess-a", "monitor_start", {"message": "old"})
        with patch.object(
            directive_queue.time,
            "monotonic",
            return_value=time.monotonic() + directive_queue.MAX_AGE_SECS + 1,
        ):
            _pub("sess-a", "monitor_start", {"message": "new"})
            assert directive_queue.depth("sess-a") == 1
            rec = _claim("sess-a", {"message": "new"})
        assert rec is not None

    def test_a_fresh_bucket_for_another_session_is_not_swept(self):
        _pub("sess-a", "monitor_start", {"message": "a"})
        _pub("sess-b", "monitor_start", {"message": "b"})
        assert directive_queue.depth("sess-a") == 1
        assert directive_queue.depth("sess-b") == 1

    def test_the_session_count_is_capped_and_keeps_the_most_recent(self):
        """The backstop: more distinct sessions publishing inside one expiry window
        than the cap allows. Whole buckets go, least-recently-published first."""
        for i in range(directive_queue.MAX_SESSIONS + 5):
            _pub(f"sess-{i:04d}", "monitor_start", {"message": "x"})

        assert len(directive_queue._pending) <= directive_queue.MAX_SESSIONS
        # The publisher of the newest record is always retained.
        newest = f"sess-{directive_queue.MAX_SESSIONS + 4:04d}"
        assert newest in directive_queue._pending
        # The oldest buckets were the ones evicted.
        assert "sess-0000" not in directive_queue._pending

    def test_a_stale_record_is_dropped_rather_than_applied(self):
        """A directive belongs to the turn that asked for it. Applying one long
        after that turn ended would arm a loop nobody is waiting on."""
        _pub("sess-a", "monitor_start", {"message": "x"})
        with patch.object(
            directive_queue.time,
            "monotonic",
            return_value=time.monotonic() + directive_queue.MAX_AGE_SECS + 1,
        ):
            assert _claim("sess-a", {"message": "x"}) is None

    def test_a_fresh_record_survives_the_age_check(self):
        _pub("sess-a", "monitor_start", {"message": "x"})
        with patch.object(
            directive_queue.time,
            "monotonic",
            return_value=time.monotonic() + (directive_queue.MAX_AGE_SECS / 2),
        ):
            assert _claim("sess-a", {"message": "x"}) is not None


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
    """The body names the CALL (tool + raw_args). The gateway re-runs the tool to
    derive the payload and computes the claim key itself; nothing the caller sends
    is stored as-is."""

    @staticmethod
    def _body(tool: str, raw_args: dict) -> dict:
        return {"tool": tool, "raw_args": raw_args}

    # Session keys here are dashboard-shaped on purpose: the gateway derives the
    # directive AS the declared session, and monitor_start's own short-circuit
    # refuses a key no loop can bind to. That refusal is the tool behaving, so a
    # bare "slot-a" would 400 here for a reason unrelated to the route.

    @pytest.mark.asyncio
    async def test_a_non_nudgeable_session_key_is_refused_by_the_tool_itself(self):
        resp = await api_session_directive(
            _request(
                {"X-Session-Key": "cron:job-1"}, self._body("monitor_start", {"message": "go"})
            )
        )
        assert resp.status == 400
        assert directive_queue.depth("cron:job-1") == 0

    @pytest.mark.asyncio
    async def test_happy_path_parks_the_derived_record_for_the_declared_session(self):
        from kiro_crew import mcp_core

        raw = {"message": "go"}
        resp = await api_session_directive(
            _request({"X-Session-Key": "dashboard:slot-a"}, self._body("monitor_start", raw))
        )
        assert resp.status == 200
        rec = _claim("dashboard:slot-a", raw)
        assert rec is not None
        # The record holds what the TOOL derives from the raw call (validated,
        # defaults added), which is the same thing the victim's own call parks.
        kind, args = mcp_core.derive_directive("monitor_start", raw, "dashboard:slot-a")
        assert (rec["kind"], rec["args"]) == (kind, args)
        assert args["message"] == "go" and "max_cycles" in args

    @pytest.mark.asyncio
    async def test_a_caller_supplied_payload_is_ignored(self):
        """The substitution attack: pair a payload of the attacker's choosing with
        the key of a call the victim will make. The body's ``args``/``kind``/
        ``input_digest`` fields are not read at all -- only the call is."""
        raw = {"message": "victim's own monitor"}
        resp = await api_session_directive(
            _request(
                {"X-Session-Key": "dashboard:victim"},
                {
                    **self._body("monitor_start", raw),
                    "kind": "reset_conversation",
                    "args": {},
                    "input_digest": _dg({}, "reset_conversation"),
                },
            )
        )
        assert resp.status == 200
        assert _claim("dashboard:victim", {}, kind="reset_conversation") is None
        rec = _claim("dashboard:victim", raw)
        assert rec is not None and rec["kind"] == "monitor_start"
        assert rec["args"]["message"] == "victim's own monitor"

    @pytest.mark.asyncio
    async def test_a_structured_monitor_call_derives_under_the_requests_identity(self):
        """monitor_watch / monitor_stop / monitor_update refuse without a strict
        session identity. The gateway has one -- the kernel-verified X-Session-Key --
        and must run the derivation AS that session, or every structured-monitor
        directive on an out-of-band backend is 400'd and never parked."""
        raw = {
            "kind": "github_pull_request",
            "target": "https://github.com/acme/widgets/pull/7",
            "objective": "review_ready",
        }
        resp = await api_session_directive(
            _request({"X-Session-Key": "dashboard:chat-9-9"}, self._body("monitor_watch", raw))
        )
        assert resp.status == 200, resp.text
        rec = _claim("dashboard:chat-9-9", raw, kind="monitor_watch")
        assert rec is not None and rec["kind"] == "monitor_watch"
        assert rec["args"]["target"] == "https://github.com/acme/widgets/pull/7"

    @pytest.mark.asyncio
    async def test_a_call_that_derives_no_directive_is_400_and_parks_nothing(self):
        # monitor_start with no message: the tool's own validation refuses.
        resp = await api_session_directive(
            _request({"X-Session-Key": "dashboard:slot-a"}, self._body("monitor_start", {}))
        )
        assert resp.status == 400
        assert directive_queue.depth("dashboard:slot-a") == 0

    @pytest.mark.asyncio
    async def test_missing_session_key_is_400_with_a_code(self):
        resp = await api_session_directive(
            _request({}, self._body("monitor_start", {"message": "x"}))
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_a_pre_call_input_body_is_named_as_a_stale_backend(self, caplog):
        """The body shape an MCP server from BEFORE this protocol sends.

        A pooled ``kirocrew mcp-core`` that outlived a code change kept POSTing
        ``{kind, args}``; read as an empty ``tool`` it was refused ``not_derivable``,
        which pointed an operator at the directive tools when the fault was a
        stale process. The refusal now names the process and the fix.
        """
        import json as _json

        with caplog.at_level("WARNING", logger="kiro_crew.dashboard.handlers.sessions"):
            resp = await api_session_directive(
                _request(
                    {"X-Session-Key": "dashboard:slot-a"},
                    {"kind": "monitor_start", "args": {"message": "go"}},
                )
            )
        assert resp.status == 400
        assert _json.loads(resp.text)["code"] == "stale_mcp_backend"
        assert directive_queue.depth("dashboard:slot-a") == 0
        msg = " ".join(r.getMessage() for r in caplog.records)
        assert "stale_mcp_backend" in msg
        assert "kirocrew restart" in msg
        assert "not_derivable" not in msg

    @pytest.mark.asyncio
    async def test_unknown_tool_is_400_and_parks_nothing(self):
        resp = await api_session_directive(
            _request({"X-Session-Key": "dashboard:slot-a"}, self._body("not_a_directive", {}))
        )
        assert resp.status == 400
        assert directive_queue.depth("dashboard:slot-a") == 0

    @pytest.mark.asyncio
    async def test_a_non_directive_core_tool_cannot_be_derived_into_one(self):
        # A real kirocrew-core tool that is not a directive tool: derive refuses
        # before ever running it.
        resp = await api_session_directive(
            _request({"X-Session-Key": "dashboard:slot-a"}, self._body("learn_list", {}))
        )
        assert resp.status == 400
        assert directive_queue.depth("dashboard:slot-a") == 0

    @pytest.mark.asyncio
    async def test_cookie_caller_cannot_park_a_directive_for_a_chosen_session(self):
        """The locality re-assert. On a ``local_only=False`` deployment the auth
        middleware admits a cookie/token caller onto this strict route, and such
        a caller picks its own ``X-Session-Key`` — which would be a cross-session
        mutation once the named turn's consumer applies the record. 403, and the
        queue must stay empty."""
        resp = await api_session_directive(
            _request(
                {"X-Session-Key": "dashboard:victim-slot"},
                self._body("monitor_start", {"message": "go"}),
                local=False,
            )
        )
        assert resp.status == 403
        assert directive_queue.depth("dashboard:victim-slot") == 0

    @pytest.mark.asyncio
    async def test_kernel_verified_peer_without_the_secret_is_accepted(self):
        """``peer_verified`` alone is enough: the kernel attested the AF_UNIX
        peer's ancestry resolves to the DECLARED key, which is stronger evidence
        for this route than the shared secret."""
        req = _request(
            {"X-Session-Key": "dashboard:slot-a"},
            self._body("monitor_start", {"message": "go"}),
            local=False,
        )
        req.get = {"peer_verified": True}.get
        resp = await api_session_directive(req)
        assert resp.status == 200
        assert directive_queue.depth("dashboard:slot-a") == 1

    @pytest.mark.asyncio
    async def test_malformed_body_is_400_and_parks_nothing(self):
        req = _request({"X-Session-Key": "dashboard:slot-a"}, None)
        req.json = AsyncMock(side_effect=ValueError("not json"))
        resp = await api_session_directive(req)
        assert resp.status == 400
        assert directive_queue.depth("dashboard:slot-a") == 0

    @pytest.mark.asyncio
    async def test_non_dict_raw_args_degrade_to_empty_not_a_crash(self):
        resp = await api_session_directive(
            _request(
                {"X-Session-Key": "dashboard:slot-a"},
                self._body("reset_conversation", "not-a-dict"),
            )
        )
        assert resp.status == 200
        rec = _claim("dashboard:slot-a", {}, kind="reset_conversation")
        assert rec is not None
        assert rec["args"] == {}


class TestEmitHelperPublishes:
    """``control._emit_directive`` must report the CALL as well as return the
    marker — the marker alone is what a provider-less backend cannot use."""

    def test_emit_posts_the_call_not_the_payload(self):
        from kiro_crew import mcp_core
        from kiro_crew.mcp_tools import control

        posted: list[tuple] = []
        with patch.object(mcp_core, "_post", side_effect=lambda p, b, **kw: posted.append((p, b))):
            out = mcp_core._call_tool("monitor_start", {"message": "hi"})
        assert len(posted) == 1
        path, body = posted[0]
        assert path == "/api/session-directive"
        assert body == {"tool": "monitor_start", "raw_args": {"message": "hi"}}
        assert "args" not in body and "input_digest" not in body
        from kiro_crew import session_directive

        assert session_directive.has_marker(out)
        assert control is not None

    def test_capture_mode_records_instead_of_posting(self):
        from kiro_crew import mcp_core

        posted: list[tuple] = []
        with patch.object(mcp_core, "_post", side_effect=lambda p, b, **kw: posted.append((p, b))):
            derived = mcp_core.derive_directive(
                "monitor_start", {"message": "hi"}, "dashboard:chat-1-1"
            )
        assert posted == [], "derivation must never reach the gateway route"
        assert derived is not None
        kind, args = derived
        assert kind == "monitor_start" and args["message"] == "hi"

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
