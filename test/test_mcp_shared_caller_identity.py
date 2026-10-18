"""The caller identity ``run_mcp_stdio_loop`` reads, and the tool policy it applies.

Each ``tools/call`` is judged by the identity its own frame carries: the loop hands
that caller block to the policy read, advertises its caller-identity and in-flight
capabilities only when opted in, and shows the tool its caller, or a bare tenant
nonce. A call whose policy cannot be resolved is refused: the refusal quotes the
gateway's reason when it gave one, defanged, redacted and bounded, and names the
cause and a retry when the gateway could not be reached. A reason that names no
agent, or a gateway that declines the caller (``policy_forbidden``), lets the call
run, audited; failed and excluded calls are audited under the caller's session.
``_resolve_tool_policy`` keeps one policy per session. The shared loop harness,
request builders and framing reset live in ``mcp_shared_helpers.py``.
"""

from __future__ import annotations

import io
import json

from mcp_shared_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _framing_undetected,
    _initialize,
    _LoopHarness,
    _slow_then_echo,
    _tools_call,
    _tools_call_with_caller,
)

import kiro_crew.mcp_shared as mcp_shared

# --- Caller-identity extension through the stdio loop -----------------------


#: What the loop advertises under ``capabilities.experimental`` when it is
#: started with ``reports_inflight=True`` (only ``kirocrew-core``), independent
#: of the caller-identity opt-in: the in-flight report.
_LOOP_OWN_CAPABILITIES: dict = {mcp_shared.INFLIGHT_CAPABILITY_KEY: {}}


def _tools_call_with_tenant(req_id, tool_name: str, nonce: str) -> dict:
    """A forwarded call as an UNNAMED co-tenant receives it: nonce, no identity."""
    from kiro_crew.mcp_caller import build_tenant_meta

    msg = _tools_call(req_id, tool_name)
    msg["params"]["_meta"] = build_tenant_meta(nonce)
    return msg


class TestStdioLoopCallerIdentity:
    def test_tool_policy_uses_only_the_current_request_identity(self, monkeypatch):
        from kiro_crew.mcp_caller import CallerContext, build_caller_meta

        seen = []
        harness = _LoopHarness(monkeypatch, lambda _name, _args: "ok")

        def policy(session="", **_kwargs):
            seen.append(session)
            return mcp_shared.ToolPolicy(frozenset(), "")

        monkeypatch.setattr(mcp_shared, "_resolve_tool_policy", policy)
        try:
            for req_id, method, session in (
                (1, "tools/list", "dashboard:alice"),
                (2, "tools/call", "dashboard:bob"),
                (3, "tools/list", "dashboard:global"),
            ):
                msg = _tools_call(req_id, "echo")
                msg["method"] = method
                msg["params"]["_meta"] = build_caller_meta(
                    CallerContext(session_key=session, from_gateway=True)
                )
                harness.send(msg)
                assert harness.wait_for(lambda: len(harness.responses) >= req_id)
            assert seen == [
                "dashboard:alice",
                "dashboard:bob",
                "dashboard:global",
            ]
        finally:
            harness.close()

    def test_tools_call_hands_the_caller_block_token_to_the_policy_read(self, monkeypatch):
        """The policy read runs on the read loop, before the worker installs the
        caller ContextVar, so ``current_caller()`` is None there. A pooled
        control-plane backend's only copy of the per-session token is the caller
        block gatewayd injected; the dispatch must hand it to the resolver, or the
        gateway answers the read as an unattested caller and refuses every call."""
        from kiro_crew.mcp_caller import CallerContext, build_caller_meta, current_caller

        seen: list[tuple[str, str, object]] = []
        harness = _LoopHarness(monkeypatch, lambda _name, _args: "ok")

        def policy(session="", *, caller_token="", **_kwargs):
            seen.append((session, caller_token, current_caller()))
            return mcp_shared.ToolPolicy(frozenset(), "")

        monkeypatch.setattr(mcp_shared, "_resolve_tool_policy", policy)
        try:
            msg = _tools_call(1, "echo")
            msg["params"]["_meta"] = build_caller_meta(
                CallerContext(
                    session_key="dashboard:carol",
                    from_gateway=True,
                    session_token="tok-frame-carol",
                )
            )
            harness.send(msg)
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert seen == [("dashboard:carol", "tok-frame-carol", None)]
            # And a caller block without a token hands an empty one, never a
            # stale one from a previous frame.
            msg = _tools_call(2, "echo")
            msg["params"]["_meta"] = build_caller_meta(
                CallerContext(session_key="dashboard:dave", from_gateway=True)
            )
            harness.send(msg)
            assert harness.wait_for(lambda: len(harness.responses) >= 2)
            assert seen[-1] == ("dashboard:dave", "", None)
        finally:
            harness.close()

    def test_identity_unattested_refuses_with_the_identity_message(self, monkeypatch):
        """The attestation refusal is fail-closed like the unreadable-spec one,
        but its text names the missing token rather than the agents directory."""
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattested"),
        )
        try:
            harness.send(_tools_call_with_caller(51, "echo", "dashboard:chat-11"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
            body = json.dumps(harness.responses[0][1])
            assert "identity_unattested" in body
            assert "could not prove which session it acts for" in body
            assert "carried no session token" in body
            assert "agents directory" not in body
            assert "Retry shortly" not in body
            assert harness.wait_for(lambda: harness.sel_mock.log_tool_invocation.call_count >= 1)
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "rejected_policy_unresolved"
            assert kw["session_key"] == "dashboard:chat-11"
            assert kw["error"] == "managedToolPolicy.unresolved:identity_unattested"
            # A gateway-stamped caller is a server the gateway spawned; the
            # external-client explanation is not for it, so its text is unchanged.
            assert mcp_shared.external_client_identity_note() not in body
        finally:
            harness.close()

    def test_identity_unattestable_refuses_without_claiming_a_gateway_read(self, monkeypatch):
        """A key resolved from a lenient source with no attestation fails closed.

        The resolver skips the futile dial and returns ``identity_unattestable``, so
        ``tools/call`` refuses (a key resolved, so an operator exclusion may exist)
        but the text does not claim a gateway read that never happened.
        """
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattestable"),
        )
        try:
            harness.send(_tools_call_with_caller(53, "echo", "dashboard:chat-13"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
            body = json.dumps(harness.responses[0][1])
            assert "identity_unattestable" in body
            assert "could not prove which session it acts for" in body
            assert "the gateway refused the tool-policy read" not in body
            assert harness.wait_for(lambda: harness.sel_mock.log_tool_invocation.call_count >= 1)
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "rejected_policy_unresolved"
            assert kw["session_key"] == "dashboard:chat-13"
            assert kw["error"] == "managedToolPolicy.unresolved:identity_unattestable"
        finally:
            harness.close()

    def test_identity_unattested_explains_an_externally_spawned_server(self, monkeypatch):
        """The editor-config report: ``KIROCREW_SESSION_KEY`` copied into an
        editor's own MCP config, no token, no launcher pid, no gateway caller. The
        decision is the same refusal; the text gains the one explanation the
        reader can act on, shared verbatim with the strict-identity refusals."""
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattested"),
        )
        monkeypatch.delenv("KIROCREW_STUB_SESSION_TOKEN", raising=False)
        monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-copied-by-hand")
        try:
            harness.send(_tools_call(54, "echo"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
            body = harness.responses[0][1]["content"][0]["text"]
            assert "identity_unattested" in body
            assert "could not prove which session it acts for" in body
            assert body.endswith(mcp_shared.external_client_identity_note("test-server"))
            assert harness.wait_for(lambda: harness.sel_mock.log_tool_invocation.call_count >= 1)
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "rejected_policy_unresolved"
            assert kw["session_key"] == "dashboard:chat-copied-by-hand"
        finally:
            harness.close()

    def test_identity_unattested_without_a_caller_keeps_its_text_for_a_spawned_server(
        self, monkeypatch
    ):
        """No gateway caller but a token on the element: the non-pooled stdio
        topology the gateway itself spawns. Its failure is the trust root, not an
        editor config, so the note stays off and the wording is what it was."""
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattested"),
        )
        monkeypatch.setenv("KIROCREW_STUB_SESSION_TOKEN", "tok")
        monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:chat-12")
        try:
            harness.send(_tools_call(55, "echo"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = harness.responses[0][1]["content"][0]["text"]
            assert "identity_unattested" in body
            assert body.endswith("operator's exclusion list.")
            assert mcp_shared.external_client_identity_note("test-server") not in body
        finally:
            harness.close()

    def test_identity_unattested_quotes_the_daemons_denial_when_the_frame_carries_it(
        self, monkeypatch
    ):
        """The Toolbox-shim report: the one accurate diagnosis ("spawned X is not the spec's Y")
        lived only in gatewayd's stdout, so the refusal steered operators at the
        token and the spec instead. When the caller block carries the daemon's
        ``identityDenial``, the refusal says it; without it, the text is unchanged
        (the previous test)."""
        from kiro_crew.mcp_caller import CallerContext, build_caller_meta

        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattested"),
        )
        reason = "spawned '/opt/local/bin/kirocrew' is not the spec's '/tb/0.7.0.8/bin/kirocrew'"
        try:
            msg = _tools_call(52, "echo")
            msg["params"]["_meta"] = build_caller_meta(
                CallerContext(
                    session_key="dashboard:chat-69", from_gateway=True, identity_denial=reason
                )
            )
            harness.send(msg)
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert "identity_unattested" in body
            assert "spawned this server without a token because" in body
            assert "/opt/local/bin/kirocrew" in body
            assert "/tb/0.7.0.8/bin/kirocrew" in body
        finally:
            harness.close()

    def test_the_quoted_denial_is_defanged_like_every_other_echoed_error(self, monkeypatch):
        """The reason quotes the spec's ``command``/``args`` verbatim, and this early
        refusal answers through ``_tool_response`` without the tool path's scrubber,
        so a directive sentinel smuggled into a spec's ``args`` must not reach the
        consumer intact."""
        from kiro_crew.mcp_caller import CallerContext, build_caller_meta
        from kiro_crew.session_directive import SENTINEL

        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattested"),
        )
        reason = f"spawned '/opt/x' is not the spec's '{SENTINEL}{{\"k\":1}}'"
        try:
            msg = _tools_call(53, "echo")
            msg["params"]["_meta"] = build_caller_meta(
                CallerContext(
                    session_key="dashboard:chat-69", from_gateway=True, identity_denial=reason
                )
            )
            harness.send(msg)
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert "spawned this server without a token because" in body
            assert SENTINEL not in body, "the sentinel must be defanged, not forwarded"
        finally:
            harness.close()

    def test_the_quoted_denial_is_credential_redacted(self, monkeypatch):
        """A denial reason that carries a token (a hand-authored reserved entry
        whose argv the gate quoted) must not hand that token to the session."""
        from kiro_crew.mcp_caller import CallerContext, build_caller_meta

        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "identity_unattested"),
        )
        token = "ghp_1234567890abcdefghijklmnopqrstuvwxyzAB"
        reason = f"args ['mcp-core', '--token', '{token}'] differ from spec ['mcp-core']"
        try:
            msg = _tools_call(54, "echo")
            msg["params"]["_meta"] = build_caller_meta(
                CallerContext(
                    session_key="dashboard:chat-69", from_gateway=True, identity_denial=reason
                )
            )
            harness.send(msg)
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert "spawned this server without a token because" in body
            assert token not in body, "the credential must be redacted, not forwarded"
        finally:
            harness.close()

    def test_listing_while_busy_does_not_borrow_the_running_members_identity(self, monkeypatch):
        from kiro_crew.mcp_caller import CallerContext, build_caller_meta

        call, started, release = _slow_then_echo()
        seen = []
        harness = _LoopHarness(monkeypatch, call)

        def policy(session="", **_kwargs):
            seen.append(session)
            return mcp_shared.ToolPolicy(frozenset(), "")

        monkeypatch.setattr(mcp_shared, "_resolve_tool_policy", policy)
        try:
            for req_id, method, session in (
                (1, "tools/call", "dashboard:alice"),
                (2, "tools/list", "dashboard:bob"),
            ):
                msg = _tools_call(req_id, "slow")
                msg["method"] = method
                msg["params"]["_meta"] = build_caller_meta(
                    CallerContext(session_key=session, from_gateway=True)
                )
                harness.send(msg)
                if req_id == 1:
                    assert started.wait(timeout=5)
            assert harness.wait_for(lambda: any(row[0] == 2 for row in harness.responses))
            assert seen == [
                "dashboard:alice",
                "dashboard:bob",
            ]
        finally:
            release.set()
            harness.close()

    def test_initialize_advertises_capability_when_opted_in(self, monkeypatch):
        # Without the advertisement gatewayd treats the backend as
        # single-session and never injects the caller block, which would make
        # the whole per-call identity path dead code.
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok", {"advertise_caller_identity": True})
        try:
            harness.send(_initialize(1))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            caps = harness.responses[0][1]["capabilities"]
            assert caps["experimental"] == {"kirocrew.caller-identity": {"schemaVersion": 1}}
        finally:
            harness.close()

    def test_initialize_advertises_the_inflight_report_only_when_opted_in(self, monkeypatch):
        # Exact pin: this dict is what gatewayd reads to decide pooling
        # eligibility (caller identity) and the recycle ceiling (in-flight
        # report), so an added or misspelled key must fail here.
        harness = _LoopHarness(
            monkeypatch,
            lambda n, a: "ok",
            {"advertise_caller_identity": True, "reports_inflight": True},
        )
        try:
            harness.send(_initialize(1))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            caps = harness.responses[0][1]["capabilities"]
            assert caps["experimental"] == {
                "kirocrew.caller-identity": {"schemaVersion": 1},
                **_LOOP_OWN_CAPABILITIES,
            }
        finally:
            harness.close()

    def test_initialize_omits_capability_by_default(self, monkeypatch):
        # kirocrew-cron does NOT consume per-call identity — it must stay
        # single-session (gatewayd refuses to pool non-advertising backends).
        # Nor does it park calls, so it does not opt into the in-flight report.
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        try:
            harness.send(_initialize(1))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert "experimental" not in harness.responses[0][1]["capabilities"]
        finally:
            harness.close()

    def test_tool_sees_current_caller_from_meta(self, monkeypatch):
        # The dispatch loop must install the gateway-injected caller for the
        # duration of the call and clear it afterwards.
        from kiro_crew import mcp_caller

        seen: list = []

        def call_tool(name, args):
            ctx = mcp_caller.current_caller()
            seen.append(ctx.session_key if ctx else None)
            return "ok"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call_with_caller(11, "echo", "dashboard:chat-3"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert seen == ["dashboard:chat-3"]
            assert mcp_caller.current_caller() is None  # cleared after dispatch
        finally:
            harness.close()

    def test_tool_sees_the_tenant_nonce_WITHOUT_an_identity(self, monkeypatch):
        """The separator arrives even when the identity does not.

        This is the frame an unnamed co-tenant of a pooled backend receives. The
        nonce must reach the tool (it is what per-tenant state is keyed on when
        there is nothing else), the caller must stay None (a connection name is not
        an identity), and both must be cleared afterwards so the next dispatch on
        this thread cannot inherit them.
        """
        from kiro_crew import mcp_caller

        seen: list = []

        def call_tool(name, args):
            seen.append((mcp_caller.current_caller(), mcp_caller.current_tenant_nonce()))
            return "ok"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call_with_tenant(12, "echo", "n0nce-a"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert seen == [(None, "n0nce-a")]
            assert mcp_caller.current_tenant_nonce() == ""  # cleared after dispatch
        finally:
            harness.close()

    def test_a_call_with_no_tenant_block_sees_an_empty_nonce(self, monkeypatch):
        """The 1:1 topology, where no gateway injects anything.

        An empty nonce is the signal to keep using the backend's own per-process
        fallback, so it must not be a stale value from a previous call.
        """
        from kiro_crew import mcp_caller

        seen: list = []

        def call_tool(name, args):
            seen.append(mcp_caller.current_tenant_nonce())
            return "ok"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call_with_tenant(13, "echo", "n0nce-a"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            harness.send(_tools_call(14, "echo"))
            assert harness.wait_for(lambda: len(harness.responses) >= 2)
            assert seen == ["n0nce-a", ""]
        finally:
            harness.close()

    def test_excluded_tool_audit_attributes_caller_session(self, monkeypatch):
        # In a shared backend the env var attributes rejection audits to "mcp"
        # or the wrong session, so the parsed caller identity must win when
        # present.
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset({"blocked"}), ""),
        )
        try:
            harness.send(_tools_call_with_caller(21, "blocked", "dashboard:chat-9"))
            assert harness.wait_for(lambda: harness.sel_mock.log_tool_invocation.call_count >= 1)
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "rejected_excluded"
            assert kw["session_key"] == "dashboard:chat-9"
        finally:
            harness.close()

    def test_unresolved_policy_refuses_the_call(self, monkeypatch):
        # The one reason that refuses. It means ONE thing -- the gateway read a
        # spec for this session and could not determine its policy -- so an
        # operator's exclusion may exist and is being withheld, and running the
        # tool would ignore it.
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "policy_unreadable"),
        )
        try:
            harness.send(_tools_call_with_caller(41, "echo", "dashboard:chat-7"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            # The tool never ran.
            assert ran == []
            # The refusal is loud and says why, rather than looking like a
            # missing tool.
            body = json.dumps(harness.responses[0][1])
            assert "tool policy could not be read" in body
            assert "policy_unreadable" in body
            # And it is attributable: the audit names the caller and the path.
            assert harness.wait_for(lambda: harness.sel_mock.log_tool_invocation.call_count >= 1)
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "rejected_policy_unresolved"
            assert kw["session_key"] == "dashboard:chat-7"
            assert kw["error"] == "managedToolPolicy.unresolved:policy_unreadable"
            # Regression pin: with no reason from the gateway the text is the
            # historical wording, so a client on an older gateway reads exactly
            # what it read before.
            assert "fix or remove the unreadable spec in the agents directory" in body
            assert "Gateway reason:" not in body
        finally:
            harness.close()

    def test_the_unreadable_policy_refusal_rules_out_a_connection_problem(self, monkeypatch):
        """``policy_unreadable`` is an answer from the gateway, so its text says so.

        It shares its symptom -- every call refused -- with an unreachable
        gateway, and an operator who reads it as an outage restarts healthy
        processes while the spec stays broken. The text therefore rules out
        reachability, and names every condition the endpoint answers this code
        for (an unreadable or unparseable spec, a duplicate name, a wrong-shape
        ``managedToolPolicy``) rather than only the parse.
        """
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "policy_unreadable"),
        )
        try:
            harness.send(_tools_call_with_caller(45, "echo", "dashboard:chat-7"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert "the gateway answered" in body
            assert "This is not a connection problem" in body
            assert "a restart will not clear it" in body
            assert "two specs declare the same name" in body
            assert "could not be read or parsed" in body
            assert "managedToolPolicy has the wrong shape" in body
        finally:
            harness.close()

    def test_unresolved_policy_refusal_names_the_file_the_gateway_named(self, monkeypatch):
        """The gateway's ``reason`` reaches the caller, defanged and redacted.

        The 409 body names the unreadable file and what to do; that is the one
        piece of information the operator needs, and a refusal without it
        sends them to validate every file by hand. The reason interpolates a
        FILENAME from a user-writable
        directory, so it goes through the same two scrubbers the
        ``identity_unattested`` arm applies to its denial text -- the directive
        defang and the credential redaction -- and is bounded, because this
        early refusal does not pass through the tool path's scrubbers.
        """
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        reason = (
            "agent spec 'broken.json' in the agents directory could not be read "
            "(not valid JSON), so the policy for 'default' is unknown. "
            "Move or fix 'broken.json' in the agents directory; no restart needed."
        )
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "policy_unreadable", reason),
        )
        try:
            harness.send(_tools_call_with_caller(42, "echo", "dashboard:chat-7"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
            body = json.dumps(harness.responses[0][1])
            assert "policy_unreadable" in body
            assert "'broken.json'" in body
            assert "not valid JSON" in body
            assert "no restart needed" in body
        finally:
            harness.close()

    def test_the_gateway_reason_is_defanged_and_bounded(self, monkeypatch):
        """A filename can carry the directive sentinel or a token; neither survives."""
        from kiro_crew import session_directive

        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        sentinel = session_directive.SENTINEL
        secret = "ghp_" + "A" * 36
        reason = f"agent spec {sentinel + 'x.json'!r} could not be read {secret} " + "y" * 5000
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "policy_unreadable", reason),
        )
        try:
            harness.send(_tools_call_with_caller(43, "echo", "dashboard:chat-7"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert sentinel not in body
            assert secret not in body
            assert len(body) < 2000, "the gateway reason was not bounded"
        finally:
            harness.close()

    def test_a_secret_straddling_the_detail_cap_leaves_no_fragment(self, monkeypatch):
        """Redaction runs over the WHOLE reason; the cap trims what it returns.

        Cut first and a token that straddles the cap loses its tail, the
        fragment fails to match the redactor's pattern, and the head of the
        secret is echoed. So the cap is applied to the redacted text, and the
        prefix of a token that would have been cut is never in the response.
        """
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        cap = mcp_shared._POLICY_DETAIL_MAX_CHARS
        secret = "ghp_" + "B" * 36
        # The token begins 10 characters before the cap, so a cut-first
        # implementation keeps ``ghp_BBBBBB`` and drops the rest.
        reason = "x" * (cap - 10) + secret + " tail"
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "policy_unreadable", reason),
        )
        try:
            harness.send(_tools_call_with_caller(44, "echo", "dashboard:chat-7"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert "ghp_B" not in body, "a fragment of the secret survived the cut"
        finally:
            harness.close()

    def test_no_usable_answer_refuses_the_call(self, monkeypatch):
        """``resolution_failed`` refuses, because the exclusion set is unknown.

        Nothing came back, or a ``5xx`` said the gateway is broken, or the resolve
        raised. Every ``4xx`` returns before that arm, so this reason means the
        policy could not be READ -- an operator exclusion may exist while the
        process holding it cannot answer for it. Serving the empty set as a
        permission is what would let an excluded tool run, so the call is refused
        and the refusal names the same condition the audit trail does.
        """
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "resolution_failed"),
        )
        try:
            harness.send(_tools_call_with_caller(53, "echo", "dashboard:chat-13"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
            _body = json.dumps(harness.responses[0][1])
            assert "is unavailable" in _body
            assert "resolution_failed" in _body
            ops = [
                c.kwargs.get("operation") for c in harness.sel_mock.log_api_access.call_args_list
            ]
            assert "tool_policy.unenforced_call" not in ops
        finally:
            harness.close()

    def test_the_unreachable_gateway_refusal_names_the_exception_class(self, monkeypatch):
        """The agent sees WHICH failure blocked it, not just that one happened."""
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(
                frozenset(), "resolution_failed", "ConnectionRefusedError"
            ),
        )
        try:
            harness.send(_tools_call_with_caller(57, "echo", "dashboard:chat-17"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert "(cause: ConnectionRefusedError)" in body
        finally:
            harness.close()

    def test_the_unreachable_gateway_refusal_names_a_retry_not_a_spec_edit(self, monkeypatch):
        """The refusal has to diagnose the condition it actually hit.

        ``policy_unreadable`` means the gateway read a spec and could not use it,
        so its text sends the caller to the agents directory. ``resolution_failed``
        means the gateway was never reached: no spec is implicated, and that same
        text would have the caller edit healthy files to fix an outage, leaving the
        edit as the real defect. The condition clears on its own, so the remedy is
        a retry.
        """
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "resolution_failed"),
        )
        try:
            harness.send(_tools_call_with_caller(56, "echo", "dashboard:chat-16"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            body = json.dumps(harness.responses[0][1])
            assert "could not reach the gateway" in body
            assert "retry" in body
            assert "agents directory" not in body
            assert "could not parse" not in body
        finally:
            harness.close()

    def test_an_excluded_tool_stays_excluded_when_the_gateway_cannot_answer(self, monkeypatch):
        """The defect this guards: a real exclusion going unenforced.

        The named tool is genuinely excluded for this session, and the resolver
        cannot reach the gateway to say so. The exclusion set therefore arrives
        empty with ``resolution_failed``, which is indistinguishable by value
        alone from an operator who excluded nothing. The reason is what keeps them
        apart, so the excluded tool must not execute.
        """
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "resolution_failed"),
        )
        try:
            harness.send(_tools_call_with_caller(55, "blocked", "dashboard:chat-15"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
        finally:
            harness.close()

    def test_a_gateway_that_declines_this_caller_does_not_refuse(self, monkeypatch):
        """``policy_forbidden`` is a boundary the gateway HOLDS, not one it lost.

        The 403 is ``member_session_unverified``: a session claiming a member
        store without a scope the gateway can verify. That is the steady
        state for a whole class of callers rather than a window that closes, so
        refusing would deny them tools permanently. The call proceeds and the
        window is audited.
        """
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "policy_forbidden"),
        )
        try:
            harness.send(_tools_call_with_caller(54, "echo", "dashboard:chat-14"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == ["echo"]
            ops = [
                c.kwargs.get("operation") for c in harness.sel_mock.log_api_access.call_args_list
            ]
            assert "tool_policy.unenforced_call" in ops
        finally:
            harness.close()

    def test_an_identity_reason_lets_the_call_run_and_audits_it(self, monkeypatch):
        # no_session_key / agent_not_resolved mean no AGENT was named, and a
        # managedToolPolicy is a property of an agent -- so there is no operator
        # exclusion for this call to bypass. The gateway returns the same 404
        # both for a session still registering and for a caller it can never
        # name, so refusing here denies the second class forever. The call runs,
        # and the window is audited rather than silent.
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "agent_not_resolved"),
        )
        try:
            harness.send(_tools_call_with_caller(51, "echo", "dashboard:chat-11"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == ["echo"]
            ops = [
                c.kwargs.get("operation") for c in harness.sel_mock.log_api_access.call_args_list
            ]
            assert "tool_policy.unenforced_call" in ops
        finally:
            harness.close()

    def test_an_unreadable_policy_refuses_the_call(self, monkeypatch):
        # The other half: the gateway NAMED an agent and could not read its
        # policy, so an operator exclusion may exist and is being withheld.
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "policy_unreadable"),
        )
        try:
            harness.send(_tools_call_with_caller(52, "echo", "dashboard:chat-12"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == []
            assert "tool policy could not be read" in json.dumps(harness.responses[0][1])
        finally:
            harness.close()

    def test_unresolved_policy_still_lists_every_tool(self, monkeypatch):
        # The anti-brick half. kiro-cli calls tools/list ONCE per
        # session and caches the answer, so hiding tools on a transient policy
        # failure would hide them for the session's whole life. Listing is not
        # the enforcement point; the call path above is.
        tools = [{"name": "echo"}, {"name": "blocked"}]
        harness = _LoopHarness(monkeypatch, lambda n, a: "ok", list_tools_fn=lambda: list(tools))
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), "no_session_key"),
        )
        try:
            harness.send({"jsonrpc": "2.0", "id": 42, "method": "tools/list", "params": {}})
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            listed = [t["name"] for t in harness.responses[0][1]["tools"]]
            assert listed == ["echo", "blocked"]
            # The widened listing window is recorded, so it is attributable.
            ops = [
                c.kwargs.get("operation") for c in harness.sel_mock.log_api_access.call_args_list
            ]
            assert "tool_policy.unfiltered_listing" in ops
        finally:
            harness.close()

    def test_resolved_empty_policy_runs_the_call(self, monkeypatch):
        # The control for the test above it: a session whose policy WAS read
        # and excludes nothing must still be able to call tools. A fix that
        # refused here would brick every session that never had an exclusion.
        ran = []
        harness = _LoopHarness(monkeypatch, lambda n, a: ran.append(n) or "ok")
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), ""),
        )
        try:
            harness.send(_tools_call_with_caller(43, "echo", "dashboard:chat-8"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert ran == ["echo"]
            assert "could not be read" not in json.dumps(harness.responses[0][1])
        finally:
            harness.close()

    def test_failed_tool_audit_attributes_caller_session(self, monkeypatch):
        def boom(name, args):
            raise RuntimeError("kaput")

        harness = _LoopHarness(monkeypatch, boom)
        try:
            harness.send(_tools_call_with_caller(31, "echo", "dashboard:chat-5"))
            assert harness.wait_for(lambda: len(harness.responses) >= 1)
            assert harness.wait_for(lambda: harness.sel_mock.log_tool_invocation.call_count >= 1)
            kw = harness.sel_mock.log_tool_invocation.call_args.kwargs
            assert kw["outcome"] == "failed"
            assert kw["session_key"] == "dashboard:chat-5"
        finally:
            harness.close()


class TestPerSessionToolPolicy:
    """Pooled backends must not bleed one session's policy into another."""

    def _reset(self):
        # Full module-state reset: the negative-cache timestamps are shared
        # process globals — a failure-path test from another file in the
        # same shard leaves them set, short-circuiting this test to
        # fail-open set() (exactly what happened on CI shard 3).
        mcp_shared._excluded_tools_by_session.clear()
        mcp_shared._last_failure_time = 0.0
        mcp_shared._last_startup_race_time = 0.0
        mcp_shared._failure_count = 0

    def setup_method(self):
        self._reset()

    def teardown_method(self):
        self._reset()

    def test_policy_http_forwards_each_session_without_a_memory_capability(self, monkeypatch):
        requests = []

        def policy(req, timeout=0):
            requests.append(dict(req.header_items()))
            return io.BytesIO(b'{"exclude":["blocked"]}')

        monkeypatch.setattr(mcp_shared, "loopback_urlopen", policy)
        monkeypatch.setattr(mcp_shared, "read_local_secret", lambda _port, **_kw: "internal")
        monkeypatch.setattr(mcp_shared, "resolve_client_port_src", lambda port: (5476, "config"))
        assert mcp_shared._resolve_tool_policy("dashboard:alice").excluded == {"blocked"}
        assert mcp_shared._resolve_tool_policy("dashboard:global").excluded == {"blocked"}
        assert requests[0]["X-session-key"] == "dashboard:alice"
        assert "X-member-session-proof" not in requests[0]
        assert "X-member-session-proof" not in requests[1]
        assert mcp_shared._excluded_tools_by_session == {
            "dashboard:alice": {"blocked"},
            "dashboard:global": {"blocked"},
        }

    def test_cache_is_keyed_per_session(self, monkeypatch):
        calls: list = []

        def fake_urlopen(req, timeout=0):
            import io

            calls.append(req.headers.get("X-session-key"))
            body = (
                b'{"exclude": ["tool_a"]}'
                if req.headers.get("X-session-key") == "dashboard:chat-1"
                else b'{"exclude": ["tool_b"]}'
            )

            class _Resp(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return _Resp(body)

        monkeypatch.setattr(mcp_shared, "loopback_urlopen", fake_urlopen)
        monkeypatch.setattr(mcp_shared, "resolve_client_port_src", lambda port: (5476, "config"))
        monkeypatch.setattr(mcp_shared, "_read_internal_secret", lambda: "s3cr3t", raising=False)

        a = mcp_shared._resolve_tool_policy("dashboard:chat-1").excluded
        b = mcp_shared._resolve_tool_policy("dashboard:chat-2").excluded
        assert a == {"tool_a"}
        assert b == {"tool_b"}  # NOT session-1's cached policy
        # Cache hit path: no third HTTP call for a repeat lookup.
        n = len(calls)
        assert mcp_shared._resolve_tool_policy("dashboard:chat-1").excluded == {"tool_a"}
        assert len(calls) == n
