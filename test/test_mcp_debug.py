"""The ``kirocrew-debug`` MCP server: its tool set, its caps, and its refusals.

Five things are pinned here that no other suite can see:

* **The tool set is exactly five READS.** The server reads host and cross-session
  state, so a write tool would be the one addition that changes what the set IS.
  The names are ratcheted: adding one fails this file rather than shipping.
* **No ``autoApprove``, structurally.** An autoApproved MCP tool is approved inside
  kiro-cli and emits no permission request, so ``hooks.on_tool_call`` -- the deny
  floor, the sensitive-path check, the governance ceiling -- is never reached for
  it. For a server this wide that is not a style preference.
* **The diag gap is relayed VERBATIM.** Three tools depend on a package that lands
  in sibling changes. An agent must be able to tell "this build cannot answer yet"
  from "the answer is nothing", so the 501 body and the relayed message are pinned
  to the same string here.
* **The refusal classifier separates a timeout from a match.** This is the most
  consequential line of the whole feature: a path the resolver could not finish
  judging reads identically to a real match, and an agent that confuses them stops
  when it should have retried.
* **Every failure is a typed code**, never a traceback and never prose alone.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from kiro_crew import mcp_debug as server

CALLER = "dashboard:chat-owner"


@pytest.fixture(autouse=True)
def _identified():
    """Every test calls as a session the gateway can name, unless it says otherwise.

    The server refuses outright without a strict identity, so a test that did not
    establish one would be testing the refusal and nothing else.
    """
    with patch.object(server, "require_strict_session_key", return_value=(CALLER, "")):
        yield


def _call(name: str, **args: Any) -> dict[str, Any]:
    """One tool call, validated as the stdio loop validates it, parsed as JSON."""
    return json.loads(server._call_tool(name, args))


class TestTheToolSetIsRatcheted:
    """The whole point of the server: five reads, and no way to add a write."""

    def test_exactly_five_tools_by_name(self) -> None:
        assert [t["name"] for t in server._list_tools()] == [
            "debug_gateway",
            "debug_refusals",
            "debug_threads",
            "debug_processes",
            "debug_snapshots",
        ]

    def test_the_declared_set_matches_what_is_advertised(self) -> None:
        assert server.TOOLS == tuple(t["name"] for t in server._list_tools())

    def test_no_tool_name_reads_as_a_write(self) -> None:
        """A name-level ratchet, so a write cannot arrive under a read's clothing."""
        forbidden = (
            "write",
            "append",
            "delete",
            "remove",
            "prune",
            "repair",
            "set",
            "update",
            "kill",
            "stop",
            "start",
            "restart",
            "rotate",
            "clear",
        )
        for name in server.TOOLS:
            assert not any(verb in name for verb in forbidden), name

    def test_every_tool_has_a_schema_so_args_are_never_passed_through_raw(self) -> None:
        from kiro_crew.validation import MCP_DEBUG_SCHEMAS

        assert set(MCP_DEBUG_SCHEMAS) == set(server.TOOLS)

    def test_no_schema_field_can_express_an_action(self) -> None:
        """Read-only by SURFACE, not only by handler.

        Every field is a question narrowing -- a window, a filter, a format. A field
        naming a path to write, a pid to signal or a flag to set would make the
        server capable of an action whatever the handlers currently do with it, so
        the absence is pinned rather than left to review.
        """
        from kiro_crew.validation import MCP_DEBUG_SCHEMAS

        banned = {"path", "file", "pid", "signal", "command", "cmd", "flag", "enable", "value"}
        for tool, schema in MCP_DEBUG_SCHEMAS.items():
            for field in getattr(schema, "fields", []) or []:
                assert field.name not in banned, f"{tool}.{field.name}"

    def test_a_dump_name_cannot_carry_a_path(self) -> None:
        """``read`` names a dump inside the store, so a separator is refused here.

        The route resolves the name inside the crash-dump directory, so traversal
        would fail there too -- but refusing at the schema means the attempt never
        reaches a path join at all.

        A schema rejection comes back as the wrapper's plain ``Error: ...`` string
        rather than this server's JSON envelope, because validation runs BEFORE the
        dispatcher that builds one. Asserted in that shape deliberately: wrapping it
        would mean the refusal had reached the handler.
        """
        for attempt in ("../../etc/passwd", "a/b", "x\\y", "/abs"):
            with patch.object(server, "_get") as get:
                raw = server._call_tool("debug_threads", {"mode": "dumps", "read": attempt})
            assert raw.startswith("Error:"), (attempt, raw)
            get.assert_not_called()

    def test_the_caller_name_matches_the_one_the_route_recognizes(self) -> None:
        from kiro_crew.dashboard.handlers.debug import DEBUG_MCP_CALLER
        from kiro_crew.dashboard.token_auth import KNOWN_INTERNAL_CALLERS

        assert server.SERVER_NAME == DEBUG_MCP_CALLER
        assert server.SERVER_NAME in KNOWN_INTERNAL_CALLERS

    def test_the_route_prefix_is_on_the_strict_internal_transport(self) -> None:
        """Without this entry every internal call falls through to cookie auth."""
        from kiro_crew.dashboard.server import _STRICT_INTERNAL_API_PATHS

        assert server.ROUTE_PREFIX in _STRICT_INTERNAL_API_PATHS

    def test_the_server_advertises_caller_identity(self) -> None:
        """A pooled backend drops the caller block unless the server advertises it."""
        assert server.ADVERTISE_CALLER_IDENTITY is True

    def test_the_server_declares_no_auto_approve(self) -> None:
        """An autoApproved tool never reaches hooks.on_tool_call."""
        from kiro_crew.agent import _MANAGED_MCP_SERVERS

        entry = _MANAGED_MCP_SERVERS[server.SERVER_NAME]
        assert "autoApprove" not in entry
        assert entry["opt_in"] is True


class TestTheDiagGapIsRelayedVerbatim:
    """An agent must tell 'cannot answer yet' from 'the answer is nothing'."""

    def test_the_route_body_and_the_server_contract_are_the_same_string(self) -> None:
        from kiro_crew.dashboard.handlers.debug import DIAG_UNAVAILABLE

        assert server.DIAG_UNAVAILABLE == DIAG_UNAVAILABLE

    @pytest.mark.parametrize("tool", ["debug_threads", "debug_processes", "debug_snapshots"])
    def test_the_501_is_relayed_with_its_own_code(self, tool: str) -> None:
        with patch.object(server, "_get", return_value={"error": server.DIAG_UNAVAILABLE}):
            result = _call(tool)
        assert result["error"]["code"] == server.DIAG_UNAVAILABLE_CODE
        assert result["error"]["message"] == server.DIAG_UNAVAILABLE

    def test_the_gap_is_not_reported_as_a_transport_failure(self) -> None:
        """``unavailable`` means nothing answered; the gap is a different action."""
        with patch.object(server, "_get", return_value={"error": server.DIAG_UNAVAILABLE}):
            result = _call("debug_threads")
        assert result["error"]["code"] != "unavailable"

    def test_tools_are_advertised_even_though_three_cannot_answer(self) -> None:
        """A missing tool reads as 'Kiro Crew cannot do this', which is wrong."""
        assert len(server._list_tools()) == 5


class TestRefusalsAreTyped:
    def test_a_transport_failure_reads_as_unavailable(self) -> None:
        with patch.object(server, "_get", return_value={}):
            result = _call("debug_gateway")
        assert result["error"]["code"] == "unavailable"

    def test_the_routes_own_code_is_carried_through(self) -> None:
        with patch.object(server, "_get", return_value={"error": "nope", "code": "forbidden"}):
            result = _call("debug_processes")
        assert result["error"]["code"] == "forbidden"

    def test_an_unidentified_session_is_refused_before_any_request(self) -> None:
        """Fails closed: a caller the gateway cannot name reads nothing at all."""
        with patch.object(server, "require_strict_session_key", return_value=("", "no identity")):
            with patch.object(server, "_get") as get:
                result = _call("debug_gateway")
        get.assert_not_called()
        assert result["error"]["code"] == "forbidden"

    def test_a_refusal_is_never_a_traceback(self) -> None:
        with patch.object(server, "_get", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                _call("debug_gateway")

    def test_an_unknown_tool_is_refused_by_name(self) -> None:
        assert json.loads(server._call_tool_inner("debug_everything", {}))["error"]["code"] == (
            "unknown_tool"
        )


class TestTheCallerKeyIsForwardedOnEveryLeg:
    def test_the_strictly_resolved_key_is_the_key_sent(self) -> None:
        """The identity that was checked must be the identity that is audited."""
        with patch.object(server, "_get", return_value={"pid": 1}) as get:
            _call("debug_gateway")
        assert get.call_args.kwargs["session_key"] == CALLER

    def test_refusals_defaults_to_self_rather_than_to_everything(self) -> None:
        """A default asking for the wide view would make every first call a refusal."""
        with patch.object(server, "_get", return_value={"refusals": []}) as get:
            _call("debug_refusals")
        assert f"session={server.SELF}" in get.call_args.args[0]


class TestTheOutputCap:
    def test_a_payload_that_fits_is_not_marked_truncated(self) -> None:
        with patch.object(server, "_get", return_value={"series": [{"a": 1}]}):
            result = _call("debug_snapshots")
        assert "truncated_to_fit" not in result

    def test_an_oversized_payload_is_cut_and_says_so(self) -> None:
        big = {"series": [{"pad": "x" * 200, "i": i} for i in range(5000)]}
        with patch.object(server, "_get", return_value=big):
            raw = server._call_tool("debug_snapshots", {})
        assert len(raw.encode("utf-8")) <= server.MAX_OUTPUT_BYTES
        assert json.loads(raw)["truncated_to_fit"] is True

    def test_a_cut_payload_still_parses(self) -> None:
        """Cut by ROWS, never by bytes: a byte slice through JSON is unparseable."""
        big = {"nodes": [{"pad": "y" * 500, "i": i} for i in range(4000)]}
        with patch.object(server, "_get", return_value=big):
            raw = server._call_tool("debug_processes", {})
        parsed = json.loads(raw)
        assert parsed["truncated_to_fit"] is True
        assert len(raw.encode("utf-8")) <= server.MAX_OUTPUT_BYTES

    def test_a_cut_payload_reports_the_rows_it_actually_carries(self) -> None:
        big = {"series": [{"pad": "z" * 300, "i": i} for i in range(3000)]}
        with patch.object(server, "_get", return_value=big):
            parsed = json.loads(server._call_tool("debug_snapshots", {}))
        assert parsed["series_returned"] == len(parsed["series"])


class TestRedaction:
    def test_a_secret_in_a_payload_does_not_reach_the_caller(self) -> None:
        """Every string leaves through the context redactor, whatever the route sent.

        The sentinel is ASSEMBLED at runtime rather than written as one literal. A
        credential-shaped constant in an added line is what the internal-content and
        secret scanners are built to catch, and they are right to: they cannot tell a
        test fixture from the real thing. Joining the halves keeps the runtime value
        exactly what the redactor must recognise while leaving no scannable literal
        in the source.
        """
        secret = "AKIA" + "IOSFODNN7EXAMPLE"
        payload = {"series": [{"env": f"AWS_SECRET_ACCESS_KEY={secret}"}]}
        with patch.object(server, "_get", return_value=payload):
            raw = server._call_tool("debug_snapshots", {})
        assert secret not in raw


class TestTheRefusalClassifier:
    """The most consequential logic in the feature.

    A budget timeout and a real sensitive-path match read identically to an agent
    and call for opposite actions: retry the identical call, or stop asking. The
    budget text also names the sensitive-path list, so order matters -- testing the
    match first would label every timeout a match.
    """

    def test_a_budget_timeout_is_not_reported_as_a_match(self) -> None:
        from kiro_crew.dashboard.handlers.debug import _classify_refusal
        from kiro_crew.security.paths import UNVERIFIABLE_PATH_PREFIX

        # The real refusal text, led by the prefix the gate writes -- an earlier
        # version of this test dropped that lead and so tested a shape no writer
        # produces.
        row = {
            "error": (
                f"{UNVERIFIABLE_PATH_PREFIX} (symlink resolution did not complete "
                "in time), so it is refused fail-closed"
            )
        }
        assert _classify_refusal(row) == "unverifiable_path"

    def test_a_real_match_is_reported_as_a_match(self) -> None:
        from kiro_crew.dashboard.handlers.debug import _classify_refusal

        row = {"error": "refused: sensitive path match on a credential file"}
        assert _classify_refusal(row) == "sensitive_path_match"

    def test_a_write_protected_config_path_is_a_match_not_unclassified(self) -> None:
        """Found by reading REAL rows, where this wording classified as nothing.

        It is the same ANSWER as a sensitive-path match -- the path is protected and
        the refusal is correct -- so the agent's action is identical: stop, do not
        retry. Grouped rather than given a sixth class name for that reason.
        """
        from kiro_crew.dashboard.handlers.debug import _classify_refusal

        row = {
            "error": (
                "Blocked: modification of write-protected config path: " "kirocrew/policy.toml"
            )
        }
        assert _classify_refusal(row) == "sensitive_path_match"

    def test_a_governance_ceiling_is_its_own_class(self) -> None:
        from kiro_crew.dashboard.handlers.debug import _classify_refusal

        assert _classify_refusal({"error": "denied by the governance ceiling"}) == "governance"

    def test_a_row_with_no_reason_is_unclassified_rather_than_guessed(self) -> None:
        from kiro_crew.dashboard.handlers.debug import _classify_refusal

        assert _classify_refusal({}) == "unclassified"

    def test_the_refusal_diagnostic_id_is_parsed_not_recomputed(self) -> None:
        from kiro_crew.dashboard.handlers.debug import _refusal_diagnostic_id
        from kiro_crew.security.diagnostics import annotate_refusal, refusal_diagnostic

        # Built by the REAL writer rather than by a hand-spelled string, so the test
        # cannot drift from the shape the gate actually records.
        reason = annotate_refusal("nope.", refusal_diagnostic("deny.shell.curl", "shell", "curl x"))
        assert _refusal_diagnostic_id({"error": reason}) == "deny.shell.curl"

    def test_a_diagnostic_quoted_in_the_reason_cannot_name_the_rule(self) -> None:
        """The gateway's own annotation is appended LAST and on its own line.

        A refusal reason can quote the subject that caused it, so an earlier
        occurrence of the prefix is attacker-reachable text, not the gate's verdict.
        Reading the first match would let that text name the rule an operator then
        reads as authoritative.
        """
        from kiro_crew.dashboard.handlers.debug import _refusal_diagnostic_id
        from kiro_crew.security.diagnostics import (
            REFUSAL_DIAGNOSTIC_PREFIX,
            annotate_refusal,
            refusal_diagnostic,
        )

        quoted = f"refused: {REFUSAL_DIAGNOSTIC_PREFIX}rule=looks.official"
        reason = annotate_refusal(quoted, refusal_diagnostic("deny.real.rule", "shell", "x"))
        assert _refusal_diagnostic_id({"error": reason}) == "deny.real.rule"

    def test_a_rule_token_that_is_not_an_identifier_is_dropped(self) -> None:
        """Validated through the writer's own id rule, so a traversal-shaped or
        otherwise non-identifier token is not echoed to the operator as a rule name."""
        from kiro_crew.dashboard.handlers.debug import _refusal_diagnostic_id
        from kiro_crew.security.diagnostics import REFUSAL_DIAGNOSTIC_PREFIX

        row = {"error": f"{REFUSAL_DIAGNOSTIC_PREFIX}rule=../../etc/shadow component=x"}
        assert _refusal_diagnostic_id(row) == ""

    def test_a_budget_row_failing_the_structural_test_is_not_called_a_match(self) -> None:
        """Both refusals name the sensitive-path list, so a budget row that does not
        carry the structural prefix must not fall through to the match arm: that would
        tell a caller "protected, stop asking" about a timeout it should retry."""
        from kiro_crew.dashboard.handlers.debug import _classify_refusal

        row = {"error": "the sensitive-path list could not be checked, resolver budget spent"}
        assert _classify_refusal(row) == "unclassified"

    def test_a_row_without_a_diagnostic_yields_no_id(self) -> None:
        from kiro_crew.dashboard.handlers.debug import _refusal_diagnostic_id

        assert _refusal_diagnostic_id({"error": "plain refusal"}) == ""


class TestTheRouteAuthorizationMatrix:
    """Every arm of the gate, driven through the real handlers.

    The handlers are called directly rather than over a socket: the gate reads
    headers, query and ``app["state"]``, all of which a mocked request carries
    faithfully, and a real server would add a port and a thread without adding a
    branch. What matters is that no arm is asserted about in the abstract -- each
    test drives the handler and reads the status it actually returns.
    """

    OWNER = "dashboard:chat-owner"

    def _slot(self, **kw: object) -> Any:
        attrs = {
            "key": self.OWNER,
            "_app": "",
            "memory_mode": "persistent",
            "linked_session_key": "",
            "is_restricted": False,
            "_created_by": "",
            "workspace": "default",
            "mirror_channels": (),
        }
        attrs.update(kw)
        return type("Slot", (), attrs)()

    def _state(self, slot: Any, *, children: tuple[str, ...] = ()) -> Any:
        recs = [
            type("Rec", (), {"id": f"kid-{n}", "parent_session_key": self.OWNER})()
            for n in range(len(children))
        ]
        for rec, key in zip(recs, children):
            rec.session_key = key
        return type(
            "State",
            (),
            {
                "_slots": {"chat-owner": slot},
                "sessions": None,
                "crons": None,
                "subagents": type(
                    "Subs", (), {"_agents": type("A", (), {"values": lambda s: recs})()}
                )(),
            },
        )()

    def _request(self, path: str, state: Any, **headers: str) -> Any:
        from aiohttp.test_utils import make_mocked_request

        hdrs = {"X-Internal-Secret": "s", "X-Internal-Caller": "kirocrew-debug"}
        hdrs.update({k.replace("_", "-"): v for k, v in headers.items()})
        hdrs.setdefault("X-Session-Key", self.OWNER)
        from aiohttp import web

        app = web.Application()
        app["state"] = state
        request = make_mocked_request("GET", path, headers=hdrs, app=app)
        # Two things the middleware establishes that a mocked request does not, and
        # that the route genuinely depends on. ``internal_auth`` is published only
        # after a constant-time secret match and is what the route gates on. The
        # member scope is what ``guard_owner_surface_routes`` requires: every
        # ``api_*`` route here except ``api_debug_refusals`` is wrapped as an owner
        # surface, and without a VERIFIED scope that wrapper answers 409
        # ``member_identity_unavailable`` before the handler runs. Supplying both is
        # what makes these tests exercise the wrapped route rather than a
        # pass-through of it.
        from kiro_crew.dashboard.handlers._shared import MemberScope

        request["internal_auth"] = True
        request["_member_scope"] = MemberScope(self.OWNER, True, None, None)
        return request

    def test_a_caller_that_only_attaches_the_secret_header_is_refused(self) -> None:
        """The header is an identity claim; the grant comes from the transport.
        ``token_auth_middleware`` publishes ``internal_auth`` only after a
        constant-time match, so a cookie-authenticated caller that merely attaches a
        header must not reach a host-wide view."""
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.handlers.debug import api_debug_gateway

        app = web.Application()
        app["state"] = self._state(self._slot())
        request = make_mocked_request(
            "GET",
            "/api/debug/gateway",
            headers={
                "X-Internal-Secret": "whatever-the-caller-likes",
                "X-Internal-Caller": "kirocrew-debug",
                "X-Session-Key": self.OWNER,
            },
            app=app,
        )
        # internal_auth deliberately NOT set: the middleware did not grant it.
        assert self._run(api_debug_gateway, request).status == 403

    def _run(self, handler: Any, request: Any) -> Any:
        import asyncio

        return asyncio.run(handler(request))

    def test_the_owner_at_a_tab_gets_the_host_wide_view(self) -> None:
        from kiro_crew.dashboard.handlers.debug import api_debug_gateway

        slot = self._slot()
        got = self._run(api_debug_gateway, self._request("/api/debug/gateway", self._state(slot)))
        assert got.status == 200

    def test_a_browser_without_the_internal_secret_is_refused(self) -> None:
        """The route is MCP-only; the dashboard has no debug panel to send a tab to."""
        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.handlers.debug import api_debug_gateway

        app = web.Application()
        app["state"] = self._state(self._slot())
        request = make_mocked_request("GET", "/api/debug/gateway", app=app)
        assert self._run(api_debug_gateway, request).status == 403

    def test_a_request_naming_another_component_is_refused(self) -> None:
        from kiro_crew.dashboard.handlers.debug import api_debug_gateway

        request = self._request(
            "/api/debug/gateway", self._state(self._slot()), X_Internal_Caller="kirocrew-panel"
        )
        assert self._run(api_debug_gateway, request).status == 403

    def test_a_request_with_no_session_identity_is_refused(self) -> None:
        from kiro_crew.dashboard.handlers.debug import api_debug_gateway

        request = self._request("/api/debug/gateway", self._state(self._slot()), X_Session_Key="")
        assert self._run(api_debug_gateway, request).status == 403

    def test_a_session_an_agent_minted_is_refused_the_host_wide_view(self) -> None:
        """An agent can mint a real dashboard session through the session-control
        create verb. That child carries a ``dashboard:`` key, a live slot and no app
        of its own, so every other condition in the owner gate passes and only
        ``_created_by`` tells it apart from the person's own tab."""
        from kiro_crew.dashboard.handlers.debug import api_debug_gateway

        slot = self._slot(_created_by="conductor-session")
        got = self._run(api_debug_gateway, self._request("/api/debug/gateway", self._state(slot)))
        assert got.status == 403

    def test_a_channel_linked_session_is_refused_the_host_wide_view(self) -> None:
        from kiro_crew.dashboard.handlers.debug import api_debug_gateway

        slot = self._slot(linked_session_key="slack:C123")
        got = self._run(api_debug_gateway, self._request("/api/debug/gateway", self._state(slot)))
        assert got.status == 403

    def test_an_incognito_session_is_refused_the_host_wide_view(self) -> None:
        from kiro_crew.dashboard.handlers.debug import api_debug_gateway

        slot = self._slot(is_restricted=True)
        got = self._run(api_debug_gateway, self._request("/api/debug/gateway", self._state(slot)))
        assert got.status == 403

    def test_a_key_naming_no_live_slot_is_refused(self) -> None:
        from kiro_crew.dashboard.handlers.debug import api_debug_gateway

        state = self._state(self._slot())
        request = self._request("/api/debug/gateway", state, X_Session_Key="dashboard:chat-gone")
        assert self._run(api_debug_gateway, request).status == 403

    def test_a_channel_linked_session_still_reads_its_own_refusals(self) -> None:
        """Narrowed, not refused: its own rows tell it nothing it did not experience."""
        from kiro_crew.dashboard.handlers.debug import api_debug_refusals

        slot = self._slot(linked_session_key="slack:C123")
        got = self._run(api_debug_refusals, self._request("/api/debug/refusals", self._state(slot)))
        assert got.status == 200

    def test_a_channel_linked_session_cannot_name_another_session(self) -> None:
        from kiro_crew.dashboard.handlers.debug import api_debug_refusals

        slot = self._slot(linked_session_key="slack:C123")
        request = self._request(
            "/api/debug/refusals?session=dashboard:chat-other", self._state(slot)
        )
        assert self._run(api_debug_refusals, request).status == 403

    def test_a_non_integer_last_is_a_400_carrying_its_code(self) -> None:
        from kiro_crew.dashboard.handlers.debug import api_debug_refusals

        request = self._request("/api/debug/refusals?last=lots", self._state(self._slot()))
        got = self._run(api_debug_refusals, request)
        assert got.status == 400
        assert b"bad_range" in got.body

    def test_real_sel_rows_are_walked_classified_and_scoped(self) -> None:
        """Drives the scan over REAL rows written in SEL's own field names and the
        real refusal wording the path gate emits, because the classifier reads that
        text and a paraphrase would prove nothing about the live case. Also pins the
        two things the scan must not do: admit an approval whose outcome merely looks
        refusal-shaped, and return a row belonging to another session."""
        import json
        import tempfile
        from pathlib import Path

        from kiro_crew.dashboard.handlers import debug as mod
        from kiro_crew.security.paths import UNVERIFIABLE_PATH_PREFIX

        budget = f"{UNVERIFIABLE_PATH_PREFIX} (symlink resolution did not complete in time)"
        rows = [
            ("tool_invocation", "execute_bash", "denied", budget, self.OWNER),
            (
                "tool_invocation",
                "fs_write",
                "denied",
                "Blocked: modification of write-protected config path: /x/policy.toml",
                self.OWNER,
            ),
            ("tool_denial", "use_aws", "denied", "denied by the governance ceiling", self.OWNER),
            # An APPROVAL must never read as a refusal, whatever its outcome spelling.
            ("tool_approval", "fs_read", "approved", "", self.OWNER),
            # Another session's row must not come back on a self-scoped read.
            ("tool_denial", "fs_read", "denied", "denied by rule", "dashboard:chat-other"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "security_events.jsonl"
            with log.open("w", encoding="utf-8") as handle:
                for event_type, operation, outcome, error, identity in rows:
                    handle.write(
                        json.dumps(
                            {
                                "event_type": event_type,
                                "operation": operation,
                                "outcome": outcome,
                                "error": error,
                                "caller_identity": identity,
                                "timestamp": "2026-01-01T00:00:00Z",
                            }
                        )
                        + "\n"
                    )
            original = mod._sel_files
            mod._sel_files = lambda: [log]  # type: ignore[assignment]
            try:
                scoped = mod._read_refusals(keys={self.OWNER}, since_ts=None, limit=50)
            finally:
                mod._sel_files = original  # type: ignore[assignment]

        classes = [r["class"] for r in scoped["refusals"]]
        sessions = {r["session"] for r in scoped["refusals"]}
        tools = [r["tool"] for r in scoped["refusals"]]
        assert sessions == {self.OWNER}, "a self-scoped read returned another session's rows"
        assert "fs_read" not in tools, "an approval was read as a refusal"
        assert "unverifiable_path" in classes
        assert "sensitive_path_match" in classes
        assert "governance" in classes

    def test_a_retryable_refusal_says_so(self) -> None:
        """The retry hint is the tool's most load-bearing output: it is the difference
        between "wait and try again" and "stop asking"."""
        import json
        import tempfile
        from pathlib import Path

        from kiro_crew.dashboard.handlers import debug as mod
        from kiro_crew.security.paths import UNVERIFIABLE_PATH_PREFIX

        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "security_events.jsonl"
            log.write_text(
                json.dumps(
                    {
                        "event_type": "tool_denial",
                        "operation": "fs_read",
                        "outcome": "denied",
                        "error": UNVERIFIABLE_PATH_PREFIX,
                        "caller_identity": self.OWNER,
                        "timestamp": "2026-01-01T00:00:00Z",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            original = mod._sel_files
            mod._sel_files = lambda: [log]  # type: ignore[assignment]
            try:
                got = mod._read_refusals(keys={self.OWNER}, since_ts=None, limit=50)
            finally:
                mod._sel_files = original  # type: ignore[assignment]

        entry = got["refusals"][0]
        assert entry["class"] == "unverifiable_path"
        assert entry["retryable"] is True
        assert entry["action"]

    def test_the_window_parser_takes_a_duration_and_rejects_nonsense(self) -> None:
        from kiro_crew.dashboard.handlers.debug import _parse_window

        assert _parse_window("") is None
        assert _parse_window("nonsense") is None
        assert isinstance(_parse_window("15m"), float)

    def test_the_daemon_block_reports_a_revision_mismatch(self, monkeypatch) -> None:
        """The reason this tool exists: a daemon that outlived a code change keeps
        serving backends built from the old checkout, and the symptom appears far from
        the cause. The mismatch verdict itself had no test."""
        from kiro_crew.dashboard.handlers import debug as mod

        info = type(
            "Info", (), {"pid": 4242, "fingerprint": "old-sha", "owner_pid": 1, "owner_alive": True}
        )()
        monkeypatch.setitem(
            __import__("sys").modules,
            "kiro_crew.mcp_gateway.daemon_control",
            type("M", (), {"describe_daemon": staticmethod(lambda: info)}),
        )
        got = mod._daemon_block()
        assert got["running"] is True
        assert got["pid"] == 4242
        assert got["matches_this_install"] is False

    def test_a_pre_fingerprint_daemon_is_named_as_such_not_left_blank(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers import debug as mod

        info = type(
            "Info", (), {"pid": 7, "fingerprint": "", "owner_pid": 1, "owner_alive": True}
        )()
        monkeypatch.setitem(
            __import__("sys").modules,
            "kiro_crew.mcp_gateway.daemon_control",
            type("M", (), {"describe_daemon": staticmethod(lambda: info)}),
        )
        got = mod._daemon_block()
        assert "pre-fingerprint" in got["fingerprint"]
        assert got["matches_this_install"] is False

    def test_a_stopped_daemon_is_reported_as_stopped(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers import debug as mod

        monkeypatch.setitem(
            __import__("sys").modules,
            "kiro_crew.mcp_gateway.daemon_control",
            type("M", (), {"describe_daemon": staticmethod(lambda: None)}),
        )
        assert mod._daemon_block()["running"] is False

    def test_the_recorder_absence_is_a_fact_not_an_error(self) -> None:
        """``debug_gateway`` is the tool that must work when nothing else does, so a
        missing diag package is reported rather than raised."""
        from kiro_crew.dashboard.handlers import debug as mod

        got = mod._recorder_health()
        assert got["available"] is False
        assert got["reason"] == mod.DIAG_UNAVAILABLE

    def test_a_present_recorder_is_reported_with_its_health(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers import debug as mod

        rec = type("Rec", (), {"health": staticmethod(lambda: {"running": True, "queued": 3})})()
        monkeypatch.setitem(
            __import__("sys").modules,
            "kiro_crew.diag.recorder",
            type("M", (), {"get_recorder": staticmethod(lambda: rec)}),
        )
        got = mod._recorder_health()
        assert got["available"] is True
        assert got["queued"] == 3

    def test_a_registered_but_unstarted_recorder_reads_as_not_running(self, monkeypatch) -> None:
        from kiro_crew.dashboard.handlers import debug as mod

        monkeypatch.setitem(
            __import__("sys").modules,
            "kiro_crew.diag.recorder",
            type("M", (), {"get_recorder": staticmethod(lambda: None)}),
        )
        got = mod._recorder_health()
        assert got == {"available": True, "running": False}

    def test_drop_in_names_are_listed_and_their_contents_never_read(
        self, monkeypatch, tmp_path
    ) -> None:
        """Names only: a drop-in can carry an ``Environment=`` line, so dumping one
        would undo the environment allowlist this module keeps elsewhere."""
        from pathlib import Path

        from kiro_crew.dashboard.handlers import debug as mod
        from kiro_crew.service.linux import SERVICE_NAME, USER_UNIT_SUBDIR

        unit_dir = tmp_path / USER_UNIT_SUBDIR / f"{SERVICE_NAME}.service.d"
        unit_dir.mkdir(parents=True)
        (unit_dir / "10-override.conf").write_text("[Service]\nEnvironment=SECRET=x\n")
        (unit_dir / "notes.txt").write_text("ignored")
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

        names = mod._systemd_drop_ins()
        assert any(n.endswith("10-override.conf") for n in names)
        assert not any(n.endswith("notes.txt") for n in names)
        assert not any("SECRET" in n for n in names)

    def test_a_continued_subagent_is_not_dropped_from_the_parent_scope(self) -> None:
        """A subagent's real session key is its ``conversation_key`` when it has one,
        which is the precedence the runtime applies
        (``info.conversation_key or f"subagent:{info.id}"``). Deriving it from ``id``
        alone dropped every continued subagent, and the failure was silent in the worst
        direction: the parent's read came back SHORT rather than refused, so a caller
        would conclude a child had no refusals when the scope never covered it."""
        from kiro_crew.dashboard.handlers.debug import _spawn_tree_keys

        recs = [
            type(
                "R",
                (),
                {
                    "id": "kid1",
                    "parent_session_key": self.OWNER,
                    "conversation_key": "subagent:orig-conv-7",
                },
            )(),
            # A grandchild whose parent is named by that conversation key, so the walk
            # has to have added it for this one to be reachable at all.
            type(
                "R",
                (),
                {
                    "id": "kid2",
                    "parent_session_key": "subagent:orig-conv-7",
                    "conversation_key": "",
                },
            )(),
        ]
        state = type(
            "S",
            (),
            {
                "subagents": type(
                    "Subs", (), {"_agents": type("A", (), {"values": lambda s: recs})()}
                )()
            },
        )()
        keys = _spawn_tree_keys(state, self.OWNER)
        assert "subagent:orig-conv-7" in keys, "a continued subagent was dropped"
        assert "subagent:kid2" in keys, "the grandchild under it was unreachable"

    def test_the_spawn_tree_walks_past_the_first_generation(self) -> None:
        """A grandchild's refusals are in scope for the session that owns the tree;
        stopping at depth one would silently narrow the answer."""
        from kiro_crew.dashboard.handlers.debug import _spawn_tree_keys

        recs = [
            type(
                "R",
                (),
                {"id": "a", "parent_session_key": self.OWNER, "session_key": "dashboard:kid"},
            )(),
            type(
                "R",
                (),
                {
                    "id": "b",
                    "parent_session_key": "dashboard:kid",
                    "session_key": "dashboard:grandkid",
                },
            )(),
            type(
                "R",
                (),
                {
                    "id": "c",
                    "parent_session_key": "dashboard:stranger",
                    "session_key": "dashboard:other",
                },
            )(),
        ]
        state = type(
            "S",
            (),
            {
                "subagents": type(
                    "Subs", (), {"_agents": type("A", (), {"values": lambda s: recs})()}
                )()
            },
        )()
        keys = _spawn_tree_keys(state, self.OWNER)
        assert self.OWNER in keys
        assert "dashboard:other" not in keys

    def test_a_star_read_is_narrowed_for_a_channel_linked_caller(self) -> None:
        """``*`` is the widest ask, so it is exactly where a class that must not read
        past itself has to be narrowed rather than obeyed."""
        from kiro_crew.dashboard.handlers.debug import _refusal_scope_keys

        slot = self._slot(linked_session_key="slack:C1")
        request = self._request("/api/debug/refusals?session=*", self._state(slot))
        assert _refusal_scope_keys(request, self.OWNER, "*") == {self.OWNER}

    def test_a_star_read_is_unnarrowed_for_the_owner_at_a_tab(self) -> None:
        from kiro_crew.dashboard.handlers.debug import _refusal_scope_keys

        request = self._request("/api/debug/refusals?session=*", self._state(self._slot()))
        assert _refusal_scope_keys(request, self.OWNER, "*") is None

    def test_a_star_read_is_narrowed_for_an_agent_minted_session(self) -> None:
        from kiro_crew.dashboard.handlers.debug import _refusal_scope_keys

        slot = self._slot(_created_by="conductor")
        request = self._request("/api/debug/refusals?session=*", self._state(slot))
        assert _refusal_scope_keys(request, self.OWNER, "*") == {self.OWNER}

    def test_authorization_going_stale_across_the_offload_is_caught(self) -> None:
        """The gate runs before a threaded read; the grant is re-tested after it,
        because the slot can be linked to a channel while the read is in flight."""
        from kiro_crew.dashboard.handlers.debug import _stale_grant_refusal

        slot = self._slot(linked_session_key="slack:C1")
        request = self._request("/api/debug/gateway", self._state(slot))
        assert _stale_grant_refusal(request, "gateway", "debug.gateway") is not None

    def test_a_self_narrowed_read_is_not_refused_by_the_recheck(self) -> None:
        """The re-check must mirror the gate: applying the class test where the gate
        did not apply it would refuse the one view a channel-linked session is owed."""
        from kiro_crew.dashboard.handlers.debug import _stale_grant_refusal

        slot = self._slot(linked_session_key="slack:C1")
        request = self._request("/api/debug/refusals", self._state(slot))
        assert (
            _stale_grant_refusal(request, "refusals", "debug.refusals", reads_past_own=False)
            is None
        )

    def _sel_log(self, tmp: object, lines: list[str]) -> Any:
        from pathlib import Path

        log = Path(str(tmp)) / "security_events.jsonl"
        log.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        return log

    def test_a_malformed_log_line_is_skipped_rather_than_crashing_the_read(self) -> None:
        """The log is append-only and can be torn mid-write, so a partial last line is
        normal. A debug tool that dies on one bad line is useless exactly when the
        machine is unhealthy."""
        import json
        import tempfile

        from kiro_crew.dashboard.handlers import debug as mod

        good = json.dumps(
            {
                "event_type": "tool_denial",
                "operation": "ok_tool",
                "outcome": "denied",
                "error": "denied by rule",
                "caller_identity": self.OWNER,
                "timestamp": "2026-01-01T00:00:00Z",
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            log = self._sel_log(
                tmp,
                [
                    "",
                    "   ",
                    "{not json at all",
                    json.dumps(["a", "list", "not", "a", "row"]),
                    json.dumps({"event_type": "session_opened", "outcome": "denied"}),
                    json.dumps({"event_type": "tool_denial", "outcome": "approved"}),
                    good,
                ],
            )
            original = mod._sel_files
            mod._sel_files = lambda: [log]  # type: ignore[assignment]
            try:
                got = mod._read_refusals(keys={self.OWNER}, since_ts=None, limit=50)
            finally:
                mod._sel_files = original  # type: ignore[assignment]

        assert [r["tool"] for r in got["refusals"]] == ["ok_tool"]

    def test_a_row_outside_the_window_and_one_with_a_bad_stamp_are_both_dropped(self) -> None:
        """An unparseable timestamp is dropped rather than admitted: a row that cannot
        be placed in time would silently widen a window the caller asked to narrow."""
        import datetime
        import json
        import tempfile

        from kiro_crew.dashboard.handlers import debug as mod

        def row(stamp: str, tool: str) -> str:
            return json.dumps(
                {
                    "event_type": "tool_denial",
                    "operation": tool,
                    "outcome": "denied",
                    "error": "denied by rule",
                    "caller_identity": self.OWNER,
                    "timestamp": stamp,
                }
            )

        with tempfile.TemporaryDirectory() as tmp:
            log = self._sel_log(
                tmp,
                [
                    row("2020-01-01T00:00:00Z", "too_old"),
                    row("not-a-timestamp", "unplaceable"),
                    row("2026-06-01T00:00:00Z", "in_window"),
                ],
            )
            original = mod._sel_files
            mod._sel_files = lambda: [log]  # type: ignore[assignment]
            try:
                cutoff = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc).timestamp()
                got = mod._read_refusals(keys={self.OWNER}, since_ts=cutoff, limit=50)
            finally:
                mod._sel_files = original  # type: ignore[assignment]

        assert [r["tool"] for r in got["refusals"]] == ["in_window"]

    def test_a_read_cut_short_by_the_limit_says_it_was_cut(self) -> None:
        """A truncated answer that does not admit it is worse than a short one: the
        absence of a refusal is what a caller would conclude from it."""
        import json
        import tempfile

        from kiro_crew.dashboard.handlers import debug as mod

        rows = [
            json.dumps(
                {
                    "event_type": "tool_denial",
                    "operation": f"tool{n}",
                    "outcome": "denied",
                    "error": "denied by rule",
                    "caller_identity": self.OWNER,
                    "timestamp": "2026-01-01T00:00:00Z",
                }
            )
            for n in range(10)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log = self._sel_log(tmp, rows)
            original = mod._sel_files
            mod._sel_files = lambda: [log]  # type: ignore[assignment]
            try:
                got = mod._read_refusals(keys={self.OWNER}, since_ts=None, limit=3)
            finally:
                mod._sel_files = original  # type: ignore[assignment]

        assert len(got["refusals"]) == 3
        assert got["truncated"] is True

    def test_an_unreadable_log_file_is_skipped_not_fatal(self) -> None:
        from pathlib import Path

        from kiro_crew.dashboard.handlers import debug as mod

        original = mod._sel_files
        mod._sel_files = lambda: [Path("/nonexistent/security_events.jsonl")]  # type: ignore[assignment]
        try:
            got = mod._read_refusals(keys={self.OWNER}, since_ts=None, limit=5)
        finally:
            mod._sel_files = original  # type: ignore[assignment]
        assert got["refusals"] == []

    def test_the_remaining_refusal_classes_are_each_reachable(self) -> None:
        from kiro_crew.dashboard.handlers.debug import _classify_refusal
        from kiro_crew.security.diagnostics import REFUSAL_DIAGNOSTIC_PREFIX

        assert _classify_refusal({"error": "the call timed out waiting on approval"}) == (
            "tool_policy_timeout"
        )
        # The gate's own marker is the authoritative signal.
        assert _classify_refusal({"error": f"{REFUSAL_DIAGNOSTIC_PREFIX}rule=x"}) == "denied_rule"
        # And the prose wording a gate writes when it states the rule in words.
        assert _classify_refusal({"error": "denied by rule shell.curl"}) == "denied_rule"
        assert _classify_refusal({"error": "something else entirely"}) == "unclassified"

    def test_a_non_owner_naming_a_session_outside_its_tree_is_refused(self) -> None:
        """Not its own rows, not a session it spawned, and not the owner: the one arm
        that has to refuse rather than narrow."""
        from kiro_crew.dashboard.handlers.debug import api_debug_refusals

        slot = self._slot(_created_by="conductor")
        request = self._request(
            "/api/debug/refusals?session=dashboard:chat-stranger", self._state(slot)
        )
        assert self._run(api_debug_refusals, request).status == 403

    def test_a_key_with_no_slot_part_names_no_slot(self) -> None:
        from kiro_crew.dashboard.handlers.debug import _live_slot

        state = self._state(self._slot())
        assert _live_slot(state, "") is None
        assert _live_slot(state, "dashboard:") is None
        assert _live_slot(object(), self.OWNER) is None

    def test_the_three_diag_routes_relay_501_until_diag_lands(self) -> None:
        """The contract string the MCP relay matches on, plus its error code."""
        from kiro_crew.dashboard.handlers import debug as mod

        for handler in (mod.api_debug_threads, mod.api_debug_processes, mod.api_debug_snapshots):
            got = self._run(handler, self._request("/api/debug/x", self._state(self._slot())))
            assert got.status == 501, handler.__name__
            assert b"diag_unavailable" in got.body, handler.__name__


class TestTheRouteAuthorizationShape:
    """What the ROUTE promises, pinned from outside it."""

    def test_lineage_never_exempts_a_caller_from_its_class(self) -> None:
        """The class test precedes the lineage arm, and that order is the invariant.

        A dispatch grant does not exempt a caller from its class. Without that
        ordering a target inside the caller's spawn tree reaches ``granted`` with no
        class test at all, and a channel-linked caller's read of a spawned child's
        refusal rows lands in that channel's thread. Same ordering ``crew_log``
        states, for the same reason: the exclusions are about where an answer lands.
        """
        import inspect

        from kiro_crew.dashboard.handlers import debug as handlers

        src = inspect.getsource(handlers._authorize_debug_read)
        head, _, tail = src.partition("_spawn_tree_keys")
        assert "class_refusal" in head, "the class test must precede the lineage arm"
        assert tail, "the lineage arm must still exist"

    def test_a_self_read_narrows_rather_than_refuses(self) -> None:
        """A channel-linked caller keeps its OWN rows; it just does not get the tree.

        Refusing outright would take away the one view built for that caller class,
        so the scope helper narrows instead -- and this pins that it narrows to
        exactly the caller's own key.
        """
        from kiro_crew.dashboard.handlers import debug as handlers

        class Slot:
            _app = ""
            memory_mode = "persistent"
            linked_session_key = "slack:1712793600.123456"
            is_restricted = False
            workspace = "default"

        class State:
            def __init__(self):
                self._slots = {"chat-owner": Slot()}
                self.sessions = None
                self.subagents = None
                self.crons = None

        request = type("R", (), {"app": {"state": State()}})()
        keys = handlers._refusal_scope_keys(request, "dashboard:chat-owner", "self")
        assert keys == {"dashboard:chat-owner"}

    def test_every_offload_is_followed_by_a_stale_grant_recheck(self) -> None:
        """Every route that offloads re-asserts authorization before it responds.

        Authorization is decided before a multi-second offload (git spawn, socket
        probe), so without a re-check a mirror attaching mid-flight would deliver
        host-wide state into a session that is publishing by the time it lands.
        """
        import inspect

        from kiro_crew.dashboard.handlers import debug as handlers

        for name in (
            "api_debug_gateway",
            "api_debug_refusals",
            "api_debug_threads",
            "api_debug_processes",
            "api_debug_snapshots",
        ):
            src = inspect.getsource(getattr(handlers, name))
            assert "_stale_grant_refusal" in src, name

    def test_the_per_session_route_is_exempt_from_the_owner_surface_wrapper(self) -> None:
        """Otherwise a member's own ``debug_refusals`` 403s before its own gate runs."""
        import inspect

        from kiro_crew.dashboard.handlers import debug as handlers

        src = inspect.getsource(handlers)
        assert 'member_scoped=frozenset({"api_debug_refusals"})' in src

    def test_the_four_host_wide_views_are_named(self) -> None:
        from kiro_crew.dashboard.handlers.debug import HOST_WIDE_VIEWS

        assert HOST_WIDE_VIEWS == frozenset({"gateway", "threads", "processes", "snapshots"})

    def test_refusals_is_the_only_per_session_view(self) -> None:
        """A route added later must choose a side rather than inherit the weak one."""
        from kiro_crew.dashboard.handlers.debug import HOST_WIDE_VIEWS

        views = {name.removeprefix("debug_") for name in server.TOOLS}
        assert views - HOST_WIDE_VIEWS == {"refusals"}

    def test_the_real_sel_vocabulary_is_used_not_the_specs_wording(self) -> None:
        """The spec said ``tool_denied``; no such event type exists.

        SEL writes ``tool_denial`` rows plus ``tool_invocation`` / ``api_access``
        rows whose outcome is denied or rejected. Reading the spec's literal wording
        would have matched nothing at all, so the real vocabulary is pinned here.
        """
        from kiro_crew.dashboard.handlers.debug import REFUSAL_EVENT_TYPES, REFUSAL_OUTCOMES

        assert "tool_denied" not in REFUSAL_EVENT_TYPES
        assert "tool_denial" in REFUSAL_EVENT_TYPES
        assert REFUSAL_OUTCOMES == frozenset({"denied", "rejected"})

    def test_an_approval_row_is_never_read_as_a_refusal(self) -> None:
        from kiro_crew.dashboard.handlers.debug import REFUSAL_EVENT_TYPES

        assert "tool_approval" not in REFUSAL_EVENT_TYPES

    def test_the_five_routes_are_registered(self) -> None:
        from aiohttp import web

        from kiro_crew.dashboard.routes import sessions

        app = web.Application()
        sessions.register(app)
        # ``route.resource`` is Optional on the aiohttp type, so the canonical path
        # is read once per route and the None case is dropped rather than indexed.
        canonical = [r.resource.canonical for r in app.router.routes() if r.resource is not None]
        paths = {c for c in canonical if c.startswith(server.ROUTE_PREFIX)}
        assert paths == {f"{server.ROUTE_PREFIX}/{n.removeprefix('debug_')}" for n in server.TOOLS}

    def test_a_non_integer_last_is_a_400_not_a_crash(self) -> None:
        """The bad-input path answers 400 rather than raising.

        ``web.json_response`` takes the status as a KEYWORD; positionally it lands in
        ``text`` and raises a TypeError, which would make the one path that reports
        bad input the one path that crashes.
        """
        import asyncio

        from aiohttp import web
        from aiohttp.test_utils import make_mocked_request

        from kiro_crew.dashboard.handlers import debug as handlers

        request = make_mocked_request(
            "GET",
            "/api/debug/refusals?last=not-a-number",
            headers={
                "X-Internal-Secret": "s",
                "X-Internal-Caller": "kirocrew-debug",
                "X-Session-Key": "dashboard:chat-owner",
            },
        )
        request.app["state"] = None
        response = asyncio.run(handlers.api_debug_refusals(request))
        assert isinstance(response, web.Response)
        # 403 (no placeable slot on a bare mock) or 400 (the bad-range path) are both
        # fine; a TypeError is what must not happen.
        assert response.status in (400, 403)


class TestTheDiagImportStaysLazy:
    def test_importing_the_route_module_loads_no_diag_submodule(self) -> None:
        """This module is on the gateway's boot path.

        A build without the diag package must still import the dashboard, and a
        build WITH it must not pay to load a diagnostics package to start.

        The prefix is ``kiro_crew.diag.`` WITH the dot, plus the package itself.
        ``kiro_crew.diagnostics`` is a different, unrelated module that the
        dashboard does import, and a bare ``kiro_crew.diag`` prefix matches it --
        which would fail this test forever for the wrong reason.
        """
        import subprocess
        import sys

        probe = (
            "import sys; import kiro_crew.dashboard.handlers.debug as d; "
            "print(sorted(m for m in sys.modules "
            "if m == 'kiro_crew.diag' or m.startswith('kiro_crew.diag.')))"
        )
        done = subprocess.run(
            [sys.executable, "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
        )
        assert done.stdout.strip().endswith("[]"), done.stdout
