"""One-shot prompt surface: permission events are answered fail-closed.

The reviewer session runs unattended with no approval surface. A permission
request left unanswered stalls the review to its timeout; auto-approving
would grant an invisible escalation. The contract is REJECT + audit log.
"""

import os
from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
)
from kiro_crew.agent_sdk.oneshot import prompt_for_reply

_REJECT = lambda ev: "unattended_no_approval_surface"  # noqa: E731 - the reject-everything gate


class _FakeHandle:
    def __init__(self, events):
        self._events = events
        self.rejected: list = []
        self.approved: list = []
        self.destroyed = False

    def prompt(self, _prompt):
        async def _gen():
            for ev in self._events:
                yield ev

        return _gen()

    async def reject_tool(self, request_id):
        self.rejected.append(request_id)

    async def approve_tool(self, request_id, option_id=None):
        self.approved.append(request_id)

    async def destroy(self):
        self.destroyed = True


class _FakeRuntime:
    def __init__(self, handle):
        self._handle = handle

    async def create_session(self, *, cwd, agent):
        return self._handle


class TestPermissionEventsFailClosed:
    @pytest.mark.asyncio
    async def test_permission_request_is_rejected_and_stream_continues(self, caplog):
        events = [
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="part1 "),
            SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="req-7", tool_kind="fs_read"),
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="part2"),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        handle = _FakeHandle(events)
        with caplog.at_level("WARNING"):
            reply = await prompt_for_reply(
                _FakeRuntime(handle), cwd=None, prompt="review this", permission_gate=_REJECT
            )
        # Fail-closed: rejected, never approved, and the stream still
        # completes so the reviewer replies with what it has.
        assert handle.rejected == ["req-7"]
        assert handle.approved == []
        assert reply.text == "part1 part2"
        assert reply.terminal is events[-1]
        assert any("permission" in r.message.lower() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_reject_failure_does_not_break_the_review(self):
        class _BrokenRejectHandle(_FakeHandle):
            async def reject_tool(self, request_id):
                raise RuntimeError("transport gone")

        events = [
            SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="req-8"),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        handle = _BrokenRejectHandle(events)
        reply = await prompt_for_reply(
            _FakeRuntime(handle), cwd=None, prompt="p", permission_gate=_REJECT
        )
        assert reply.terminal is events[-1]


class TestServedModelSurfaced:
    """The reply carries the session's SERVED model so usage attribution is
    correct even when the configured reviewer model is empty (= inherit)."""

    @pytest.mark.asyncio
    async def test_reply_carries_served_model(self):
        events = [
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="ok"),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        handle = _FakeHandle(events)
        handle.served_model = "resolved-model-7"
        reply = await prompt_for_reply(
            _FakeRuntime(handle), cwd=None, prompt="p", permission_gate=_REJECT
        )
        assert reply.model == "resolved-model-7"

    @pytest.mark.asyncio
    async def test_absent_served_model_degrades_to_empty(self):
        events = [SimpleNamespace(kind=EVENT_COMPLETE)]
        reply = await prompt_for_reply(
            _FakeRuntime(_FakeHandle(events)), cwd=None, prompt="p", permission_gate=_REJECT
        )
        assert reply.model == ""


class TestAutoApprovedToolCallsAreAudited:
    """kiro-cli auto-approves its builtin reads (fs_read, grep) and raises no
    permission request for them, so the permission loop alone leaves those
    calls off the SEL trail. Every observed tool call is recorded as
    ``auto_approved`` unless a permission decision already covered it."""

    @pytest.mark.asyncio
    async def test_tool_call_without_permission_request_is_audited(self, monkeypatch):
        records = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                records.append(kw)

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.sel", lambda: _Sel())
        events = [
            SimpleNamespace(
                kind=EVENT_TOOL_CALL, title="fs_read", tool_kind="read", request_id="tc-1"
            ),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        await prompt_for_reply(
            _FakeRuntime(_FakeHandle(events)), cwd=None, prompt="p", permission_gate=_REJECT
        )
        assert [r["outcome"] for r in records] == ["auto_approved"]
        assert records[0]["tool_name"] == "fs_read" and records[0]["tool_kind"] == "read"

    @pytest.mark.asyncio
    async def test_tool_call_after_a_decision_is_not_double_counted(self, monkeypatch):
        records = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                records.append(kw)

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.sel", lambda: _Sel())
        events = [
            SimpleNamespace(
                kind=EVENT_PERMISSION_REQUEST, request_id="req-1", tool_kind="execute_bash"
            ),
            SimpleNamespace(
                kind=EVENT_TOOL_CALL, title="execute_bash", tool_kind="execute", request_id="req-1"
            ),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        await prompt_for_reply(
            _FakeRuntime(_FakeHandle(events)), cwd=None, prompt="p", permission_gate=_REJECT
        )
        assert [r["outcome"] for r in records] == ["denied"]


class TestPermissionRejectIsAudited:
    """Round-13: a permission DECISION must leave the same SEL record every
    other rejection path emits -- silence hides a security-relevant event."""

    @pytest.mark.asyncio
    async def test_reject_emits_tool_invocation_record(self, monkeypatch):
        # Round-25: a permission decision on a TOOL is a tool-audit event --
        # it carries the standard tool fields, not a generic api_access row.
        records = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                records.append(kw)

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.sel", lambda: _Sel())
        events = [
            SimpleNamespace(
                kind=EVENT_PERMISSION_REQUEST, request_id="req-9", tool_kind="execute_bash"
            ),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        await prompt_for_reply(
            _FakeRuntime(_FakeHandle(events)), cwd=None, prompt="p", permission_gate=_REJECT
        )
        assert len(records) == 1
        rec = records[0]
        assert rec["tool_name"] == "execute_bash"
        assert rec["outcome"] == "denied"
        assert rec["request_id"] == "req-9"
        assert rec["source"] == "oneshot_permission_gate"
        assert rec["session_key"]


class TestDenialAuditPrecedesWireIO:
    """Round-34: the SEL denial record must be written BEFORE the reject
    write to the backend pipe -- a stall/cancellation there must not leave
    the permission decision unaudited."""

    @pytest.mark.asyncio
    async def test_sel_record_emitted_before_reject_tool(self, monkeypatch):
        order = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                order.append("sel")

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.sel", lambda: _Sel())
        events = [
            SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="r1", tool_kind="x"),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        handle = _FakeHandle(events)
        orig = handle.reject_tool

        async def reject(rid):
            order.append("reject")
            return await orig(rid)

        handle.reject_tool = reject
        await prompt_for_reply(_FakeRuntime(handle), cwd=None, prompt="p", permission_gate=_REJECT)
        assert order == ["sel", "reject"]


class TestPermissionGate:
    """Round-41: the one-shot surface routes each permission request through a
    caller-supplied gate. Empty string = approve once; a reason = reject.
    Without a gate the request is rejected (unattended fail-closed)."""

    @pytest.mark.asyncio
    async def test_gate_approval_approves_once_and_audits_ok(self, monkeypatch):
        records = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                records.append(kw)

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.sel", lambda: _Sel())
        events = [
            SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="r-ok", tool_kind="fs_read"),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        handle = _FakeHandle(events)
        approved = []

        async def approve(rid, option_id=None, *, always=False):
            approved.append((rid, always))

        handle.approve_tool = approve
        await prompt_for_reply(
            _FakeRuntime(handle), cwd=None, prompt="p", permission_gate=lambda ev: ""
        )
        assert approved == [("r-ok", False)]
        assert records[-1]["outcome"] == "approved"

    @pytest.mark.asyncio
    async def test_gate_reason_rejects_with_reason_in_audit(self, monkeypatch):
        records = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                records.append(kw)

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.sel", lambda: _Sel())
        events = [
            SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="r-no", tool_kind="fs_read"),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        handle = _FakeHandle(events)
        rejected = []
        orig = handle.reject_tool

        async def reject(rid):
            rejected.append(rid)
            return await orig(rid)

        handle.reject_tool = reject
        await prompt_for_reply(
            _FakeRuntime(handle),
            cwd=None,
            prompt="p",
            permission_gate=lambda ev: "Blocked: access to sensitive path",
        )
        assert rejected == ["r-no"]
        assert records[-1]["outcome"] == "denied"
        assert "sensitive path" in records[-1]["resources"]

    @pytest.mark.asyncio
    async def test_gate_exception_fails_closed(self, monkeypatch):
        monkeypatch.setattr(
            "kiro_crew.agent_sdk.oneshot.sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: None),
        )
        events = [
            SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="r-x", tool_kind="fs_read"),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        handle = _FakeHandle(events)
        approved = []

        async def approve(rid, option_id=None, *, always=False):
            approved.append(rid)

        handle.approve_tool = approve

        def boom(ev):
            raise RuntimeError("gate broken")

        await prompt_for_reply(_FakeRuntime(handle), cwd=None, prompt="p", permission_gate=boom)
        assert approved == []


class TestApprovalIsAuditOrDeny:
    """Round-42: an APPROVAL must not proceed unless its SEL record is
    durably written -- the record is the only trace an unattended tool ran.
    The critical audit runs off the event loop; a failure rejects."""

    @pytest.mark.asyncio
    async def test_audit_failure_rejects_instead_of_approving(self, monkeypatch):
        class _Sel:
            def log_tool_invocation(self, **kw):
                if kw.get("outcome") == "approved":
                    raise OSError("audit sink unwritable")

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.sel", lambda: _Sel())
        events = [
            SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="r-a", tool_kind="fs_read"),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        handle = _FakeHandle(events)
        approved, rejected = [], []

        async def approve(rid, option_id=None, *, always=False):
            approved.append(rid)

        orig = handle.reject_tool

        async def reject(rid):
            rejected.append(rid)
            return await orig(rid)

        handle.approve_tool = approve
        handle.reject_tool = reject
        await prompt_for_reply(
            _FakeRuntime(handle), cwd=None, prompt="p", permission_gate=lambda ev: ""
        )
        assert approved == [] and rejected == ["r-a"]

    @pytest.mark.asyncio
    async def test_approval_audit_is_critical_and_off_loop(self, monkeypatch):
        seen = {}
        import threading

        loop_thread = threading.get_ident()

        class _Sel:
            def log_tool_invocation(self, **kw):
                if kw.get("outcome") == "approved":
                    seen["critical"] = kw.get("critical")
                    seen["off_loop"] = threading.get_ident() != loop_thread

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.sel", lambda: _Sel())
        events = [
            SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="r-b", tool_kind="glob"),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        handle = _FakeHandle(events)

        async def approve(rid, option_id=None, *, always=False):
            pass

        handle.approve_tool = approve
        await prompt_for_reply(
            _FakeRuntime(handle), cwd=None, prompt="p", permission_gate=lambda ev: ""
        )
        assert seen == {"critical": True, "off_loop": True}

    def test_module_docstring_describes_mediation(self):
        from kiro_crew.agent_sdk import oneshot

        doc = oneshot.__doc__ or ""
        assert "no tool-call" not in doc
        assert "permission_gate" in doc


class TestPermissionGateRunsOffLoop:
    """Round-43: the gate does config/policy IO -- it must run in a worker
    thread, never on the event loop."""

    @pytest.mark.asyncio
    async def test_gate_invoked_off_loop(self, monkeypatch):
        import threading

        monkeypatch.setattr(
            "kiro_crew.agent_sdk.oneshot.sel",
            lambda: SimpleNamespace(log_tool_invocation=lambda **kw: None),
        )
        loop_thread = threading.get_ident()
        seen = {}

        def gate(ev):
            seen["off_loop"] = threading.get_ident() != loop_thread
            return ""

        events = [
            SimpleNamespace(kind=EVENT_PERMISSION_REQUEST, request_id="r", tool_kind="fs_read"),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        handle = _FakeHandle(events)

        async def approve(rid, option_id=None, *, always=False):
            pass

        handle.approve_tool = approve
        await prompt_for_reply(_FakeRuntime(handle), cwd=None, prompt="p", permission_gate=gate)
        assert seen == {"off_loop": True}


class TestReviewerRuntimeSandbox:
    def test_create_agent_runtime_pins_the_strict_sandbox(self, monkeypatch):
        """kiro-cli auto-approves its builtin reads (fs_read, grep) without a
        permission request, so the OS sandbox -- not the permission gate -- is
        what keeps the reviewer's reads away from credentials. The reviewer
        runtime therefore runs under the strict tier, never the primary's
        default."""
        from kiro_crew.agent_sdk import oneshot

        seen = {}

        class FakeRuntime:
            def __init__(self, **kw):
                seen.update(kw)

        monkeypatch.setattr(oneshot, "AcpRuntime", FakeRuntime)
        oneshot.create_agent_runtime(agent="kirocrew-advisor", work_dir="/tmp/x", model=None)
        assert seen["sandbox_mode"] == "strict"


class TestProceedIsRecheckedAfterTheSessionOpens:
    """``create_session`` is an await (ACP session/new); an authorization that
    was true when the caller checked can be revoked while it runs. The prompt
    must not leave the process once ``proceed`` answers False: the fresh
    session is destroyed and the caller learns it was cancelled, never a reply."""

    @pytest.mark.asyncio
    async def test_revoked_during_session_open_sends_nothing(self):
        handle = _FakeHandle([SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="leak")])
        prompted: list = []
        _orig = handle.prompt
        handle.prompt = lambda p: (prompted.append(p), _orig(p))[1]
        gate: dict = {"ok": True}

        class Runtime(_FakeRuntime):
            async def create_session(self, *, cwd, agent):
                gate["ok"] = False  # an opt-out lands while the session opens
                return self._handle

        from kiro_crew.agent_sdk.oneshot import OneShotCancelled

        with pytest.raises(OneShotCancelled):
            await prompt_for_reply(
                Runtime(handle),
                cwd=None,
                prompt="p",
                permission_gate=_REJECT,
                proceed=lambda: gate["ok"],
            )
        assert prompted == []
        assert handle.destroyed is True

    @pytest.mark.asyncio
    async def test_proceed_true_prompts_normally(self):
        handle = _FakeHandle(
            [
                SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="ok"),
                SimpleNamespace(kind=EVENT_COMPLETE),
            ]
        )
        reply = await prompt_for_reply(
            _FakeRuntime(handle),
            cwd=None,
            prompt="p",
            permission_gate=_REJECT,
            proceed=lambda: True,
        )
        assert reply.text == "ok"


class TestReviewerRuntimeHidesTheCrewHomeSelLeaves:
    """The crew home leaves that stay READ-WRITE under every tier (the SEL
    trust root and its key, the security-event log, the MCP shared secret)
    exist for the in-sandbox MCP servers a primary session runs. The reviewer
    runs no MCP server, and the builtin reads auto-approve, so those leaves
    are hidden from its child explicitly: an injected checkpoint must not be
    able to read ``trust/sel_hmac.key`` into the reviewer model."""

    def test_create_agent_runtime_hides_the_mcp_only_leaves(self, monkeypatch):
        from kiro_crew.agent_sdk import oneshot

        seen = {}

        class FakeRuntime:
            def __init__(self, **kw):
                seen.update(kw)

        monkeypatch.setattr(oneshot, "AcpRuntime", FakeRuntime)
        oneshot.create_agent_runtime(agent="kirocrew-advisor", work_dir="/tmp/x", model=None)
        leaves = {os.path.basename(p) for p in seen["extra_hidden_dirs"]}
        assert {
            "trust",
            "sel_hmac.key",
            "security_events.jsonl",
            "security_events.d",
            ".local_secret",
        } <= leaves
        assert "run" not in leaves, "the launcher's own leaf must stay readable"
        assert all(os.path.isabs(p) for p in seen["extra_hidden_dirs"])


class TestAuditFailureAbortsTheReview:
    """A builtin read has already executed when its tool_call event arrives, so
    the audit cannot deny it. What it CAN do is refuse to let an unaudited read
    contribute: an audit-sink failure aborts the prompt, destroys the session,
    and the caller gets no reply."""

    @pytest.mark.asyncio
    async def test_unwritable_sink_aborts_and_destroys(self, monkeypatch):
        from kiro_crew.agent_sdk.oneshot import OneShotAuditFailed

        class _Sel:
            def log_tool_invocation(self, **kw):
                raise OSError("read-only sink")

        monkeypatch.setattr("kiro_crew.agent_sdk.oneshot.sel", lambda: _Sel())
        events = [
            SimpleNamespace(
                kind=EVENT_TOOL_CALL, request_id="c1", title="Reading /x", tool_kind="read"
            ),
            SimpleNamespace(kind=EVENT_TEXT_CHUNK, text="leak"),
            SimpleNamespace(kind=EVENT_COMPLETE),
        ]
        handle = _FakeHandle(events)
        with pytest.raises(OneShotAuditFailed):
            await prompt_for_reply(
                _FakeRuntime(handle), cwd=None, prompt="p", permission_gate=_REJECT
            )
        assert handle.destroyed is True
