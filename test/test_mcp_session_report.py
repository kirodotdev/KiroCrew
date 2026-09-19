"""Per-session MCP report accumulation (:mod:`kiro_crew.acp.mcp_session_report`)."""

from __future__ import annotations

import inspect

from kiro_crew.acp import session_handle
from kiro_crew.acp.mcp_session_report import (
    _BUCKET_CAP,
    _ERROR_CAP,
    _NAME_CAP,
    STATE_CONNECTED_WITHOUT_PROVENANCE,
    KasMcpReadiness,
    McpSessionReport,
    roster_names,
    server_name_of,
)
from kiro_crew.acp.types import (
    EVENT_MCP_SERVER_INITIALIZED,
    METHOD_KAS_MCP_STATUS,
    METHOD_KAS_TOOLS_CHANGED,
    METHOD_MCP_OAUTH_REQUEST,
    METHOD_MCP_SERVER_INIT_FAILURE,
    METHOD_MCP_SERVER_INITIALIZED,
    METHOD_SESSION_UPDATE,
    JsonRpcMessage,
)


def _frame(method: str, name: str = "", **params: object) -> JsonRpcMessage:
    body: dict[str, object] = dict(params)
    if name:
        body["serverName"] = name
    return JsonRpcMessage(method=method, params=body)


def _ready(name: str) -> JsonRpcMessage:
    return _frame(METHOD_MCP_SERVER_INITIALIZED, name)


def _failed(name: str, error: str = "boom") -> JsonRpcMessage:
    return _frame(METHOD_MCP_SERVER_INIT_FAILURE, name, error=error)


def _oauth(name: str) -> JsonRpcMessage:
    return _frame(METHOD_MCP_OAUTH_REQUEST, name)


class TestEmptyState:
    def test_fresh_report_has_no_payload(self):
        # None, not a dict of empty lists: a consumer must be able to tell
        # "nothing was reported" from "nothing started".
        assert McpSessionReport().payload() is None
        assert McpSessionReport().empty is True

    def test_roster_alone_makes_it_non_empty(self):
        r = McpSessionReport()
        r.begin_session([{"name": "kirocrew-core"}])
        assert r.empty is False
        payload = r.payload()
        assert payload is not None
        assert payload["configured"] == ["kirocrew-core"]
        assert payload["ready"] == []


class TestBuckets:
    def test_initialized_lands_in_ready(self):
        r = McpSessionReport()
        assert r.record_frame(_ready("github-mcp"), owned=True) is True
        assert r.payload() == {
            "configured": [],
            "unresolved_refs": [],
            "ready": ["github-mcp"],
            "failed": [],
            "awaiting_auth": [],
            "failures": {},
        }

    def test_failure_records_its_reason(self):
        r = McpSessionReport()
        r.record_frame(_failed("slack-mcp", "spawn ENOENT"), owned=True)
        payload = r.payload()
        assert payload is not None
        assert payload["failed"] == ["slack-mcp"]
        assert payload["failures"] == {"slack-mcp": "spawn ENOENT"}

    def test_oauth_lands_in_awaiting_auth(self):
        r = McpSessionReport()
        r.record_frame(_oauth("builder-mcp"), owned=True)
        payload = r.payload()
        assert payload is not None
        assert payload["awaiting_auth"] == ["builder-mcp"]

    def test_failure_then_initialized_moves_and_clears_the_reason(self):
        # The normal shape of a server that needed authorization. Keeping the
        # stale reason would leave the panel showing a failure for a server
        # that is now up -- the misleading-evidence class this view removes.
        r = McpSessionReport()
        r.record_frame(_failed("builder-mcp", "401 unauthorized"), owned=True)
        assert r.record_frame(_ready("builder-mcp"), owned=True) is True
        assert r.payload() == {
            "configured": [],
            "unresolved_refs": [],
            "ready": ["builder-mcp"],
            "failed": [],
            "awaiting_auth": [],
            "failures": {},
        }

    def test_initialized_then_failure_moves_to_failed(self):
        r = McpSessionReport()
        r.record_frame(_ready("slack-mcp"), owned=True)
        r.record_frame(_failed("slack-mcp", "died"), owned=True)
        payload = r.payload()
        assert payload is not None
        assert payload["ready"] == []
        assert payload["failed"] == ["slack-mcp"]
        assert payload["failures"] == {"slack-mcp": "died"}

    def test_oauth_after_failure_clears_the_reason(self):
        r = McpSessionReport()
        r.record_frame(_failed("builder-mcp", "401"), owned=True)
        r.record_frame(_oauth("builder-mcp"), owned=True)
        payload = r.payload()
        assert payload is not None
        assert payload["awaiting_auth"] == ["builder-mcp"]
        assert payload["failures"] == {}

    def test_a_name_is_never_in_two_buckets(self):
        r = McpSessionReport()
        for frame in (_oauth("x"), _failed("x"), _ready("x"), _failed("x"), _ready("x")):
            r.record_frame(frame, owned=True)
        payload = r.payload()
        assert payload is not None
        seen = payload["ready"] + payload["failed"] + payload["awaiting_auth"]
        assert seen == ["x"]

    def test_repeat_of_the_same_state_is_not_a_change(self):
        r = McpSessionReport()
        assert r.record_frame(_ready("a"), owned=True) is True
        assert r.record_frame(_ready("a"), owned=True) is False

    def test_first_seen_order_is_preserved(self):
        r = McpSessionReport()
        for name in ("c", "a", "b"):
            r.record_frame(_ready(name), owned=True)
        payload = r.payload()
        assert payload is not None
        assert payload["ready"] == ["c", "a", "b"]


class TestFramesThatMustBeIgnored:
    def test_non_registration_frame_is_ignored(self):
        r = McpSessionReport()
        assert r.record_frame(_frame(METHOD_SESSION_UPDATE, "noise"), owned=True) is False
        assert r.payload() is None

    def test_nameless_frame_is_ignored(self):
        # A nameless row would render as a real server that failed.
        r = McpSessionReport()
        assert (
            r.record_frame(_frame(METHOD_MCP_SERVER_INIT_FAILURE, error="x"), owned=True) is False
        )
        assert r.payload() is None

    def test_unowned_frame_is_refused(self):
        # The runtime fans a session-less frame out to every registered session.
        # At most one produced it, so crediting THIS session with it would put
        # another session's server in this session's own view.
        r = McpSessionReport()
        assert r.record_frame(_ready("someone-elses-server"), owned=False) is False
        assert r.payload() is None

    def test_owned_frame_is_recorded(self):
        r = McpSessionReport()
        assert r.record_frame(_ready("mine"), owned=True) is True

    def test_a_sessionless_frame_is_unowned_even_with_the_fanout_flag_clear(self):
        # The original defect: ownership was read off ``fanout_no_owner``, which
        # the runtime sets only once MORE THAN ONE queue is registered. A lone
        # registered session therefore received a co-tenant's sessionless frame
        # with the flag CLEAR and published that server as its own. The report
        # must refuse on the caller's ownership answer, not on the flag.
        r = McpSessionReport()
        msg = _ready("a-co-tenants-server")
        assert msg.fanout_no_owner is False
        assert r.record_frame(msg, owned=False) is False
        assert r.payload() is None


class TestRoster:
    def test_wire_shape_is_accepted(self):
        assert roster_names([{"name": "a", "command": "x"}, {"name": "b"}]) == ("a", "b")

    def test_duplicates_collapse_and_junk_is_tolerated(self):
        assert roster_names([{"name": "a"}, {"name": "a"}, "nope", {}, None]) == ("a",)

    def test_non_list_is_empty(self):
        assert roster_names(None) == ()
        assert roster_names({"name": "a"}) == ()

    def test_roster_is_capped(self):
        assert (
            len(roster_names([{"name": f"s{i}"} for i in range(_BUCKET_CAP + 25)])) == _BUCKET_CAP
        )


class TestSanitization:
    def test_server_name_alternate_spelling(self):
        assert server_name_of(JsonRpcMessage(method="m", params={"name": "alt"})) == "alt"

    def test_server_name_strips_newlines_and_controls(self):
        # An embedded newline would forge a line in the gateway log; ESC would
        # inject terminal escapes. A server name is config-derived, so an
        # installed app chooses it.
        got = server_name_of(JsonRpcMessage(method="m", params={"serverName": "a\nb\x1b[31mc"}))
        assert "\n" not in got
        assert "\x1b" not in got
        assert got == "a b[31mc"

    def test_server_name_is_capped(self):
        long = "x" * (_NAME_CAP + 50)
        assert (
            len(server_name_of(JsonRpcMessage(method="m", params={"serverName": long})))
            == _NAME_CAP
        )

    def test_failure_text_is_redacted(self):
        # A failing server's startup error can carry a credential; this payload
        # reaches a browser.
        r = McpSessionReport()
        r.record_frame(_failed("s", "denied for AKIAIOSFODNN7EXAMPLE"), owned=True)
        payload = r.payload()
        assert payload is not None
        assert "AKIAIOSFODNN7EXAMPLE" not in payload["failures"]["s"]

    def test_failure_text_is_capped(self):
        r = McpSessionReport()
        r.record_frame(_failed("s", "y" * (_ERROR_CAP + 100)), owned=True)
        payload = r.payload()
        assert payload is not None
        assert len(payload["failures"]["s"]) == _ERROR_CAP

    def test_empty_failure_text_records_no_reason(self):
        r = McpSessionReport()
        r.record_frame(_failed("s", ""), owned=True)
        payload = r.payload()
        assert payload is not None
        assert payload["failed"] == ["s"]
        assert payload["failures"] == {}

    def test_a_retry_that_fails_without_a_reason_drops_the_earlier_one(self):
        # The original defect: the store was only WRITTEN when the new failure
        # carried text, so a retry that failed silently left the first attempt's
        # reason standing and the panel presented it as this failure's own.
        r = McpSessionReport()
        r.record_frame(_failed("s", "spawn ENOENT"), owned=True)
        assert (r.payload() or {})["failures"] == {"s": "spawn ENOENT"}
        r.record_frame(_failed("s", ""), owned=True)
        payload = r.payload()
        assert payload is not None
        assert payload["failed"] == ["s"], "the server is still failed"
        assert payload["failures"] == {}, "but carries no reason it did not give"


class TestSupersetSemantics:
    def test_a_reported_server_need_not_be_in_the_roster(self):
        # The backend starts the agent spec's own servers as well as the ones
        # Kiro Crew injects, so reports are a SUPERSET of the roster. This is
        # how a spec-declared server (github-mcp) proves it started.
        r = McpSessionReport()
        r.begin_session([{"name": "kirocrew-core"}])
        r.record_frame(_ready("github-mcp"), owned=True)
        payload = r.payload()
        assert payload is not None
        assert payload["configured"] == ["kirocrew-core"]
        assert payload["ready"] == ["github-mcp"]

    def test_an_ownerless_event_is_refused_like_an_ownerless_frame(self):
        # An MCP notification carries no sessionId, so on a shared runtime it is
        # fanned out to every co-tenant. Recording it would credit this session
        # with another session's server. Same rule the frame path applies.
        r = McpSessionReport()
        assert r.record_event(EVENT_MCP_SERVER_INITIALIZED, "shared", fanout_no_owner=True) is False
        assert r.payload() is None
        # The identical event, owned, IS recorded — so the refusal is the flag's
        # doing and not a broken event path.
        assert r.record_event(EVENT_MCP_SERVER_INITIALIZED, "shared") is True

    def test_a_long_name_is_not_truncated_into_a_non_matching_one(self):
        # The frontend matches these names exactly against the configured list, so
        # a truncated name matches nothing and reads "no report" for a server that
        # did report. The bound only exists to stop a pathological name.
        r = McpSessionReport()
        name = "a" * 100
        r.record_frame(_ready(name), owned=True)
        payload = r.payload()
        assert payload is not None
        assert payload["ready"] == [name]

    def test_a_report_never_spans_two_session_attempts(self):
        # A failed session/load drains its notifications before failing, so its
        # frames are already in the report when the client falls back to
        # session/new. Carrying a READY server across would publish a false
        # all-clear for a session that never came up.
        r = McpSessionReport()
        r.begin_session([{"name": "old"}])
        r.record_frame(_ready("old"), owned=True)
        r.record_frame(_failed("gone", "boom"), owned=True)
        r.record_frame(_oauth("pending"), owned=True)

        r.begin_session([{"name": "fresh"}])

        payload = r.payload()
        assert payload is not None
        assert payload["configured"] == ["fresh"]
        for bucket in ("ready", "failed", "awaiting_auth"):
            assert payload[bucket] == [], f"{bucket} survived the new session attempt"
        assert payload["failures"] == {}

    def test_a_started_session_reports_even_with_nothing_in_it(self):
        # The kiro-cli path sends an EMPTY wire roster by design (servers arrive
        # via --agent), so a session whose frames have not landed yet has nothing
        # in any bucket. Returning None there is indistinguishable from "no
        # session", and the consumer falls back to host-configured green dots —
        # the false all-clear this view exists to remove.
        r = McpSessionReport()
        assert r.payload() is None, "no session yet is genuinely unknown"

        r.begin_session([])

        payload = r.payload()
        assert payload is not None
        assert payload["configured"] == []
        assert payload["ready"] == []

    def test_bucket_is_capped(self):
        r = McpSessionReport()
        for i in range(_BUCKET_CAP + 25):
            r.record_frame(_ready(f"s{i}"), owned=True)
        payload = r.payload()
        assert payload is not None
        assert len(payload["ready"]) == _BUCKET_CAP

    def test_a_dropped_failure_does_not_leave_its_reason_behind(self):
        # The reasons dict must obey the same cap as the bucket: storing a reason
        # for a name the cap dropped grows this payload without bound in exactly
        # the case the cap exists to bound.
        r = McpSessionReport()
        for i in range(_BUCKET_CAP):
            r.record_frame(_failed(f"s{i}", f"boom {i}"), owned=True)
        r.record_frame(_failed("overflow", "boom overflow"), owned=True)
        payload = r.payload()
        assert payload is not None
        assert "overflow" not in payload["failed"]
        assert "overflow" not in payload["failures"]
        assert len(payload["failures"]) == _BUCKET_CAP

    def test_a_tracked_server_transitioning_into_a_full_bucket_is_not_erased(self):
        # The original bug's exact shape: a server the report already tracked
        # (here: failed) recovers while the ready bucket is full. Removing it
        # from ``failed`` and then refusing it at the full ``ready`` made a
        # real server vanish from the report entirely — the worst direction,
        # since an absent server reads as "no claim" while in truth the
        # report still tracks it. The transition must land: the tracked
        # server moves, and the oldest ready entry is evicted to make room.
        r = McpSessionReport()
        for i in range(_BUCKET_CAP):
            r.record_frame(_ready(f"s{i}"), owned=True)
        r.record_frame(_failed("mover", "boom"), owned=True)
        r.record_frame(_ready("mover"), owned=True)
        payload = r.payload()
        assert payload is not None
        assert "mover" in payload["ready"]
        assert "mover" not in payload["failed"]
        assert "mover" not in payload["failures"]
        assert len(payload["ready"]) == _BUCKET_CAP

    def test_eviction_cleans_the_evicted_servers_failure_reason(self):
        # The evicted entry degrades to "no report"; leaving its reason in the
        # dict would keep presenting evidence about a server the buckets no
        # longer describe, and would let the dict outgrow the cap.
        r = McpSessionReport()
        for i in range(_BUCKET_CAP):
            r.record_frame(_failed(f"s{i}", f"boom {i}"), owned=True)
        r.record_frame(_ready("mover"), owned=True)
        r.record_frame(_failed("mover", "moved boom"), owned=True)
        payload = r.payload()
        assert payload is not None
        assert "mover" in payload["failed"]
        assert payload["failures"]["mover"] == "moved boom"
        # s0 was the oldest failed entry; its slot and reason both went.
        assert "s0" not in payload["failed"]
        assert "s0" not in payload["failures"]
        assert len(payload["failures"]) == _BUCKET_CAP


class TestEventOwnershipReachesTheReport:
    """Gating on ownership is dead code unless the event carries it.

    The MCP registration notifications name no session, so the runtime fans them
    out — but their event constructions omitted ``runtime_global``, leaving the
    flag False on exactly the traffic it exists to mark. That is worse than
    absent: a consumer gating on it would look correct and never fire.
    """

    def test_every_mcp_event_passes_the_frames_ownership_through(self):
        src = inspect.getsource(session_handle)
        for kind in (
            "EVENT_MCP_OAUTH_REQUEST",
            "EVENT_MCP_SERVER_INITIALIZED",
            "EVENT_MCP_SERVER_INIT_FAILURE",
        ):
            marker = f"kind={kind},"
            assert marker in src, f"{kind} is no longer yielded — re-point this guard"
            window = src[src.index(marker) : src.index(marker) + 400]
            assert "runtime_global=not self._owns_mcp_frame(msg)" in window, (
                f"{kind} is yielded without the frame's ownership provenance, so a "
                "consumer gating on runtime_global would silently never fire"
            )
            assert "runtime_global=msg.fanout_no_owner" not in window, (
                f"{kind} is back to deriving ownership from the runtime's fan-out "
                "counter, which is only set once MORE THAN ONE queue is registered "
                "— a lone session is handed a co-tenant's sessionless frame unmarked "
                "and publishes that server as its own"
            )

    def test_every_runner_call_site_forwards_it_to_the_report(self):
        from kiro_crew.dashboard import chat_runner

        src = inspect.getsource(chat_runner)
        calls = src.count("_record_session_mcp_event(\n") - src.count(
            "def _record_session_mcp_event(\n"
        )
        assert calls == 3, f"expected 3 call sites, found {calls} — re-point this guard"
        forwarded = src.count("fanout_no_owner=event.runtime_global")
        assert forwarded == calls, (
            "a _record_session_mcp_event call site does not forward the event's "
            "ownership, so that MCP kind still contaminates every co-tenant's report"
        )


_SID = "sess_ed78c259-a634-4c69-86de-65ca2c8056bc"
_CLIENT = {"kiro": {"resource": {"resourceType": "mcpServer", "source": {"origin": "client"}}}}
_GLOBAL = {"kiro": {"resource": {"resourceType": "mcpServer", "source": {"origin": "global"}}}}


def _status(*servers: dict, sid: str | None = _SID) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_MCP_STATUS, params={"sessionId": sid, "servers": list(servers)}
    )


def _tags(*tags: str, sid: str | None = _SID) -> JsonRpcMessage:
    return JsonRpcMessage(
        method=METHOD_KAS_TOOLS_CHANGED,
        params={"sessionId": sid, "tags": [{"source": "mcp", "tag": tag} for tag in tags]},
    )


def _core(status: str, meta: dict | None = None, *, catalog: bool = True, **extra: object) -> dict:
    """One ``kirocrew-core`` status entry; ``meta=None`` reproduces the 2.18.0 wire.

    ``catalog=False`` drops the connected entry's ``tools`` list, so exposure can
    only come from a later tag frame.
    """
    entry: dict = {"name": "kirocrew-core", "status": status, **extra}
    if status == "connected" and catalog:
        entry["tools"] = [{"name": "ping", "description": "probe", "disabled": False}]
    if meta is not None:
        entry["_meta"] = meta
    return entry


class TestKasReadinessProvenance:
    """The origin filter against the released wire shapes it has to read.

    Captured frames (``bugfix-kas-compat57/2.18.0-agent-probe.json``,
    ``bugfix-repair58-fable/2.18.0-newload-*.json``): released kiro-cli 2.18.0
    emits ``_kiro/mcp/status`` and ``_kiro/tools/didChange`` under the session's
    own id but NO ``_meta`` on any status entry. On it a session-level
    ``mcpServers`` injection wins over a same-named global server on new and
    load, while an agent-block declaration is shadowed by the global one on new
    and coexists with it under one name on load. 2.20.0 stamps every entry with
    ``origin: client``.
    """

    def test_captured_2_18_0_agent_only_wire_refuses_with_the_compatibility_limit(self):
        ready = KasMcpReadiness(_SID, ("kirocrew-core",))
        ready.record(_status(_core("connecting")))
        assert ready.pending == "kirocrew-core: connecting"
        assert not ready.failure
        ready.record(_status(_core("connected")))
        ready.record(_tags("@kirocrew-core/ping"))
        assert ready.states["kirocrew-core"] == STATE_CONNECTED_WITHOUT_PROVENANCE
        failure = ready.failure
        assert failure.startswith(f"kirocrew-core: {STATE_CONNECTED_WITHOUT_PROVENANCE}")
        # Actionable: names what the backend lacks and what satisfies the barrier.
        assert "reports no MCP server origin" in failure
        assert "_meta.kiro.resource.source.origin" in failure
        # The tag is exposure evidence and is recorded as such; it never turns
        # the refused server into a satisfied requirement.
        assert "kirocrew-core" in ready.advertised
        assert ready.pending.startswith(f"kirocrew-core: {STATE_CONNECTED_WITHOUT_PROVENANCE}")

    def test_captured_2_18_0_injected_wire_is_ready_on_connected_catalog(self):
        # ``2.18.0-newload-global+session.json``: the injected server's own
        # catalog (``session_ping``) is what connects, on new and on load, and
        # that catalog on the connected entry is the exposure evidence.
        ready = KasMcpReadiness(_SID, ("kirocrew-core",), injected=frozenset({"kirocrew-core"}))
        ready.record(_status(_core("connecting")))
        assert ready.pending == "kirocrew-core: connecting"
        ready.record(_status(_core("connected")))
        assert not ready.pending and not ready.failure

    def test_injection_exempts_only_the_injected_name(self):
        ready = KasMcpReadiness(
            _SID,
            ("kirocrew-core", "kirocrew-dashboard"),
            injected=frozenset({"kirocrew-dashboard"}),
        )
        ready.record(
            _status(_core("connected"), {"name": "kirocrew-dashboard", "status": "connected"})
        )
        ready.record(_tags("@kirocrew-core/ping", "@kirocrew-dashboard/ping"))
        assert ready.states["kirocrew-dashboard"] == "connected"
        assert ready.failure.startswith("kirocrew-core: connected without provenance")

    def test_injection_does_not_waive_exposure_or_failure_states(self):
        ready = KasMcpReadiness(_SID, ("kirocrew-core",), injected=frozenset({"kirocrew-core"}))
        ready.record(_status(_core("connected", catalog=False)))
        assert ready.pending == "kirocrew-core: tools not advertised"
        ready.record(_status(_core("connected")))
        assert not ready.pending
        ready.record(_status(_core("failed", errorMessage="boom")))
        assert ready.failure == "kirocrew-core: failed (boom)"

    def test_injection_is_not_consulted_on_a_stamping_backend(self):
        # With provenance present the explicit origin decides, injected or not:
        # a same-named global entry is still skipped.
        ready = KasMcpReadiness(_SID, ("kirocrew-core",), injected=frozenset({"kirocrew-core"}))
        ready.record(_status(_core("connected", _GLOBAL)))
        ready.record(_tags("@kirocrew-core/ping"))
        assert ready.pending == "kirocrew-core: unreported"
        assert not ready.failure

    def test_the_limit_outranks_a_backend_error_text_on_the_same_entry(self):
        ready = KasMcpReadiness(_SID, ("kirocrew-core",))
        ready.record(_status(_core("connected", errorMessage="irrelevant")))
        assert "reports no MCP server origin" in ready.failure

    def test_stamped_client_entry_without_catalog_waits_for_the_tag(self):
        ready = KasMcpReadiness(_SID, ("kirocrew-core",))
        ready.record(_status(_core("connected", _CLIENT, catalog=False)))
        assert ready.pending == "kirocrew-core: tools not advertised"
        ready.record(_tags("@kirocrew-core/ping"))
        assert not ready.pending and not ready.failure

    def test_explicit_global_origin_is_skipped_not_refused(self):
        # A backend that speaks provenance and reports a same-named global
        # server: the private declaration may still report, so stay pending.
        ready = KasMcpReadiness(_SID, ("kirocrew-core",))
        ready.record(_status(_core("connected", _GLOBAL)))
        ready.record(_tags("@kirocrew-core/ping"))
        assert ready.pending == "kirocrew-core: unreported"
        assert not ready.failure

    def test_unstamped_entry_on_a_stamping_backend_is_skipped_not_refused(self):
        # Provenance is a property of the SNAPSHOT: when any entry carries it,
        # an unstamped required entry is foreign, not legacy.
        ready = KasMcpReadiness(_SID, ("kirocrew-core",))
        ready.record(
            _status(_core("connected"), {"name": "other", "status": "connected", "_meta": _CLIENT})
        )
        assert ready.pending == "kirocrew-core: unreported"
        assert not ready.failure

    def test_legacy_snapshot_for_another_or_no_session_is_ignored(self):
        ready = KasMcpReadiness(_SID, ("kirocrew-core",))
        ready.record(_status(_core("connected"), sid="sess_other"))
        ready.record(_status(_core("connected"), sid=None))
        assert ready.pending == "kirocrew-core: unreported"
        assert not ready.failure

    def test_a_later_stamped_snapshot_replaces_the_refusal(self):
        # Snapshots are full state, so the terminal reading is only as durable
        # as the wire that produced it; a stamped client snapshot recovers.
        ready = KasMcpReadiness(_SID, ("kirocrew-core",))
        ready.record(_status(_core("connected")))
        assert ready.failure
        ready.record(_status(_core("connected", _CLIENT)))
        assert not ready.failure
        assert ready.errors == {}


class TestKasReadinessExposure:
    """Exposure evidence across the released wires that disagree about it.

    Captured kiro-cli 2.22.0 (KAS 0.66.0) off the production relay spawn with a
    session-level ``kirocrew-core`` injection: the connected status entry carries
    ``origin: client`` and an 82-entry ``tools`` catalog, and EVERY
    ``_kiro/tools/didChange`` snapshot of the session lists only ``builtin``
    tags (``read``, ``write``, ``shell``, ``web``) -- with or without
    ``tool_search`` in the agent's ``tools``. No MCP tag ever arrives. A barrier
    that needed one timed out every KAS session start on that release with
    ``kirocrew-core: tools not advertised``.
    """

    def _builtin_tags(self) -> JsonRpcMessage:
        return JsonRpcMessage(
            method=METHOD_KAS_TOOLS_CHANGED,
            params={
                "sessionId": _SID,
                "tags": [
                    {"source": "builtin", "tag": "read", "description": "read-file tools"},
                    {"source": "builtin", "tag": "shell", "description": "run-commands tools"},
                ],
            },
        )

    def test_captured_2_22_0_wire_is_ready_on_the_connected_catalog_alone(self):
        ready = KasMcpReadiness(_SID, ("kirocrew-core",))
        ready.record(self._builtin_tags())
        ready.record(_status(_core("connecting", _CLIENT)))
        assert ready.pending == "kirocrew-core: connecting"
        ready.record(_status(_core("connected", _CLIENT)))
        # The builtin-only snapshot that arrives WITH the connected status on
        # that release must not retract what the catalog just established.
        ready.record(self._builtin_tags())
        assert not ready.pending and not ready.failure

    def test_an_empty_catalog_is_not_exposure(self):
        ready = KasMcpReadiness(_SID, ("kirocrew-core",))
        ready.record(_status(_core("connected", _CLIENT, catalog=False, tools=[])))
        assert ready.pending == "kirocrew-core: tools not advertised"

    def test_a_reconnect_still_needs_fresh_evidence(self):
        ready = KasMcpReadiness(_SID, ("kirocrew-core",))
        ready.record(_status(_core("connected", _CLIENT)))
        assert not ready.pending
        ready.record(_status(_core("connecting", _CLIENT)))
        assert ready.pending == "kirocrew-core: connecting"
        # Reconnected without a catalog: the earlier catalog is stale.
        ready.record(_status(_core("connected", _CLIENT, catalog=False)))
        assert ready.pending == "kirocrew-core: tools not advertised"
        ready.record(_tags("@kirocrew-core/ping"))
        assert not ready.pending

    def test_a_tag_frame_adds_exposure_without_clearing_the_catalog_evidence(self):
        ready = KasMcpReadiness(_SID, ("kirocrew-core", "kirocrew-dashboard"))
        ready.record(
            _status(
                _core("connected", _CLIENT),
                {"name": "kirocrew-dashboard", "status": "connected", "_meta": _CLIENT},
            )
        )
        assert ready.pending == "kirocrew-dashboard: tools not advertised"
        ready.record(_tags("@kirocrew-dashboard/ping"))
        assert not ready.pending
        # A later full tag snapshot without the dashboard tag retracts the TAG
        # evidence, as before this change; the core's catalog evidence stays.
        ready.record(self._builtin_tags())
        assert ready.pending == "kirocrew-dashboard: tools not advertised"
        assert "kirocrew-core" in ready.advertised

    def test_a_full_tag_snapshot_retracts_a_dropped_tag(self):
        ready = KasMcpReadiness(_SID, ("kirocrew-core",))
        ready.record(_status(_core("connected", _CLIENT, catalog=False)))
        ready.record(_tags("@kirocrew-core/ping"))
        assert not ready.pending
        ready.record(_tags("@other/ping"))
        assert ready.pending == "kirocrew-core: tools not advertised"
        # Tag evidence also dies with the connection.
        ready.record(_tags("@kirocrew-core/ping"))
        assert not ready.pending
        ready.record(_status(_core("connecting", _CLIENT)))
        ready.record(_status(_core("connected", _CLIENT, catalog=False)))
        assert ready.pending == "kirocrew-core: tools not advertised"


class TestKasStatusReport:
    def test_status_transitions_keep_external_failure_visible(self):
        report = McpSessionReport()
        report.begin_session([{"name": "external"}])

        def status(value, **extra):
            return _frame(
                METHOD_KAS_MCP_STATUS,
                sessionId="owned-session",
                servers=[{"name": "external", "status": value, **extra}],
            )

        failed = status("failed", errorMessage="external failed")
        assert not report.record_frame(failed, owned=False)
        assert report.payload()["failed"] == []
        assert report.record_frame(failed, owned=True)
        assert report.payload()["failures"] == {"external": "external failed"}
        report.include_configured(("kirocrew-core",))
        assert report.payload()["configured"] == ["external", "kirocrew-core"]
        assert report.payload()["failed"] == ["external"]
        assert report.record_frame(status("connecting"), owned=True)
        assert report.payload()["failed"] == []
        assert report.payload()["failures"] == {}
        assert report.record_frame(status("connecting", failedAuthorization=True), owned=True)
        assert report.payload()["awaiting_auth"] == ["external"]
        assert report.record_frame(status("connected"), owned=True)
        assert report.payload()["ready"] == ["external"]
        assert report.payload()["awaiting_auth"] == []
        assert report.record_frame(status("connecting"), owned=True)
        assert report.payload()["ready"] == []

    def test_roster_extension_preserves_reference_diagnostics(self):
        report = McpSessionReport()
        report.begin_session([{"name": "external"}])
        report.record_unresolved_refs(["@missing/tool"])
        report.include_configured(("kirocrew-core", "external"))
        assert report.payload()["configured"] == ["external", "kirocrew-core"]
        assert report.payload()["unresolved_refs"] == ["@missing/tool"]
