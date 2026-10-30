"""Tests for ACP client: ACP-layer tool audit, PreToolUse/PostToolUse hooks and the zero-tools
refusal.
"""

import json
import time
import types
from unittest.mock import AsyncMock

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

import kiro_crew.acp.client as acp_client
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, ACP_BACKEND_OPENCODE


class TestAcpClientToolAudit:
    """Covers the _maybe_audit_tool_call ACP-layer SEL emit (worker-pool path)."""

    @staticmethod
    def _ev(title="Reading /x/SKILL.md", kind="read"):
        # Minimal stand-in for AcpEvent: the helper only reads .title/.tool_kind.
        return types.SimpleNamespace(title=title, tool_kind=kind)

    @pytest.mark.asyncio
    async def test_emits_when_audit_source_set(self, monkeypatch):
        calls = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(acp_client, "sel", lambda: _Sel())
        client = AcpClient(session_key="sk", audit_source="subagent")
        await client._maybe_audit_tool_call(self._ev())
        assert len(calls) == 1
        assert calls[0]["source"] == "subagent"
        assert calls[0]["session_key"] == "sk"
        assert calls[0]["tool_name"] == "Reading /x/SKILL.md"
        assert calls[0]["tool_kind"] == "read"
        assert calls[0]["outcome"] == "auto_approved"

    @pytest.mark.asyncio
    async def test_none_title_kind_fall_back(self, monkeypatch):
        # tool_event.title/.tool_kind can be None (see the observed-tool-call
        # bookkeeping in the dispatch loop); the audit must still emit meaningful
        # values instead of a None tool_name or a swallowed error.
        calls = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(acp_client, "sel", lambda: _Sel())
        client = AcpClient(session_key="sk", audit_source="subagent")
        await client._maybe_audit_tool_call(self._ev(title=None, kind=None))
        assert len(calls) == 1
        assert calls[0]["tool_name"] == "unknown"
        assert calls[0]["tool_kind"] == ""

    @pytest.mark.asyncio
    async def test_noop_when_audit_source_none(self, monkeypatch):
        calls = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(acp_client, "sel", lambda: _Sel())
        client = AcpClient()  # audit_source defaults to None
        await client._maybe_audit_tool_call(self._ev())
        assert calls == []  # chat / subagent clients must never double-log

    @pytest.mark.asyncio
    async def test_swallows_sel_errors(self, monkeypatch):
        reached = []

        def _boom():
            reached.append(True)
            raise RuntimeError("SEL backend down")

        monkeypatch.setattr(acp_client, "sel", _boom)
        client = AcpClient(session_key="sk", audit_source="subagent")
        # A SEL failure must never break tool dispatch.
        await client._maybe_audit_tool_call(self._ev())
        assert reached == [True]

    @pytest.mark.asyncio
    async def test_hung_backend_times_out_and_is_swallowed(self, monkeypatch):
        # A wedged SEL backend must not stall tool dispatch: the wait_for bound
        # raises TimeoutError (caught) so the coroutine returns promptly.
        class _Sel:
            def log_tool_invocation(self, **kw):
                # flake-ok: the stubbed SEL backend hangs 5 s on purpose so the patched 50 ms wait_for bound returns
                # nosemgrep: kirocrew.test-sleep-as-barrier -- the stubbed SEL backend's own hang, which the wait_for bound cuts short; not a barrier
                time.sleep(5)  # simulate a hung backend, well past the timeout

        monkeypatch.setattr(acp_client, "sel", lambda: _Sel())
        monkeypatch.setattr(acp_client, "_SEL_AUDIT_TIMEOUT_SECONDS", 0.05)
        client = AcpClient(session_key="sk", audit_source="subagent")
        start = time.monotonic()
        await client._maybe_audit_tool_call(self._ev())  # must not raise / hang
        assert time.monotonic() - start < 4  # returned via timeout, not the 5s sleep


class TestAcpClientUnaskedClaudeAutoAudit:
    """A claude ``auto`` call that never asks the host still gets an SEL record.

    Claude's own classifier approves the call, so no ``session/request_permission``
    arrives and no consumer writes a decision row. ``_maybe_audit_unasked_tool_call``
    writes one when the call reaches a terminal status.
    """

    @staticmethod
    def _sel_calls(monkeypatch) -> list[dict]:
        calls: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                calls.append(kw)

        monkeypatch.setattr(acp_client, "sel", lambda: _Sel())
        return calls

    @staticmethod
    def _client(**kw) -> AcpClient:
        kw.setdefault("permission_mode", "auto")
        return AcpClient(session_key="sk", agent="kirocrew", acp_backend=ACP_BACKEND_CLAUDE, **kw)

    @staticmethod
    def _result_ev(call_id="tc1", status="completed"):
        return types.SimpleNamespace(tool_call_id=call_id, tool_status=status)

    @staticmethod
    def _permission_request(call_id="tc1"):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            id=7,
            method="session/request_permission",
            params={
                "sessionId": "s-1",
                "toolCall": {"toolCallId": call_id, "kind": "execute", "title": "Bash"},
                "options": [{"optionId": "allow", "name": "Allow", "kind": "allow_once"}],
            },
        )

    @pytest.mark.asyncio
    async def test_an_unasked_completed_call_is_recorded(self, monkeypatch):
        calls = self._sel_calls(monkeypatch)
        client = self._client()
        client._observed_tool_calls["tc1"] = ("Bash", "execute")
        await client._maybe_audit_unasked_tool_call(self._result_ev())
        assert len(calls) == 1
        row = calls[0]
        assert row["session_key"] == "sk"
        assert row["agent"] == "kirocrew"
        assert row["source"] == "acp"
        assert row["tool_name"] == "Bash"
        assert row["tool_kind"] == "execute"
        assert row["outcome"] == "auto_approved"
        assert row["request_id"] == "tc1"
        assert row["metadata"]["decided_by"] == "claude_classifier"
        assert row["metadata"]["tool_status"] == "completed"

    @pytest.mark.asyncio
    async def test_a_failed_unasked_call_is_recorded_with_its_status(self, monkeypatch):
        calls = self._sel_calls(monkeypatch)
        client = self._client()
        client._observed_tool_calls["tc1"] = ("Bash", "execute")
        await client._maybe_audit_unasked_tool_call(self._result_ev(status="failed"))
        assert [c["outcome"] for c in calls] == ["failed"]

    @pytest.mark.asyncio
    async def test_a_call_that_asked_is_left_to_its_consumer(self, monkeypatch):
        calls = self._sel_calls(monkeypatch)
        client = self._client()
        client._observed_tool_calls["tc1"] = ("Bash", "execute")
        assert client._process_message(self._permission_request("tc1"), 1) == "permission"
        await client._maybe_audit_unasked_tool_call(self._result_ev())
        assert calls == []

    @pytest.mark.asyncio
    async def test_one_record_per_call(self, monkeypatch):
        calls = self._sel_calls(monkeypatch)
        client = self._client()
        client._observed_tool_calls["tc1"] = ("Bash", "execute")
        await client._maybe_audit_unasked_tool_call(self._result_ev())
        await client._maybe_audit_unasked_tool_call(self._result_ev())
        assert len(calls) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", ["in_progress", "pending", None])
    async def test_a_running_call_is_not_recorded_yet(self, monkeypatch, status):
        calls = self._sel_calls(monkeypatch)
        client = self._client()
        await client._maybe_audit_unasked_tool_call(self._result_ev(status=status))
        assert calls == []

    @pytest.mark.asyncio
    async def test_a_session_not_in_auto_mode_writes_nothing(self, monkeypatch):
        calls = self._sel_calls(monkeypatch)
        client = self._client(permission_mode=None)
        client._observed_tool_calls["tc1"] = ("Bash", "execute")
        await client._maybe_audit_unasked_tool_call(self._result_ev())
        assert calls == []

    @pytest.mark.asyncio
    async def test_an_audit_source_client_is_not_double_logged(self, monkeypatch):
        calls = self._sel_calls(monkeypatch)
        client = self._client(audit_source="subagent")
        client._observed_tool_calls["tc1"] = ("Bash", "execute")
        await client._maybe_audit_unasked_tool_call(self._result_ev())
        assert calls == []

    @pytest.mark.asyncio
    async def test_swallows_sel_errors(self, monkeypatch):
        def _boom():
            raise RuntimeError("SEL backend down")

        monkeypatch.setattr(acp_client, "sel", _boom)
        client = self._client()
        # A SEL failure must never break tool dispatch.
        await client._maybe_audit_unasked_tool_call(self._result_ev())

    @pytest.mark.asyncio
    async def test_the_send_message_path_records_an_unasked_call(self, monkeypatch):
        """``_read_prompt_response`` writes the row for a call that never asked."""
        calls = self._sel_calls(monkeypatch)
        client = self._client()
        call_msg = types.SimpleNamespace(
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc9",
                    "title": "Bash",
                    "kind": "execute",
                }
            }
        )
        result_msg = types.SimpleNamespace(
            params={
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc9",
                    "status": "completed",
                    "content": [{"content": {"type": "text", "text": "ok"}}],
                }
            }
        )
        complete_msg = types.SimpleNamespace(result={"stopReason": "end_turn"})

        async def _fake_loop(req_id, timeout):
            yield "update", call_msg
            yield "update", result_msg
            yield "complete", complete_msg

        monkeypatch.setattr(client, "_prompt_loop", _fake_loop)
        await client._read_prompt_response(1, 5.0)
        rows = [c for c in calls if c.get("request_id") == "tc9"]
        assert len(rows) == 1
        assert rows[0]["outcome"] == "auto_approved"

    @pytest.mark.asyncio
    async def test_the_stream_path_records_an_unasked_call(self, monkeypatch):
        """``_dispatch_events`` writes the row for a call that never asked."""
        from kiro_crew.acp.types import JsonRpcMessage

        calls = self._sel_calls(monkeypatch)
        client = self._client()
        frames = [
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc9",
                        "title": "Bash",
                        "kind": "execute",
                    }
                },
            ),
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "tc9",
                        "status": "completed",
                        "content": [{"content": {"type": "text", "text": "ok"}}],
                    }
                },
            ),
        ]

        async def _fake_loop(req_id, timeout):
            for frame in frames:
                yield "update", frame
            yield "complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

        client._prompt_loop = _fake_loop
        client._read_new_tool_results = AsyncMock(return_value=[])
        async for _ in client._dispatch_events(req_id=1, timeout=5.0):
            pass
        rows = [c for c in calls if c.get("request_id") == "tc9"]
        assert len(rows) == 1
        assert rows[0]["outcome"] == "auto_approved"


class TestAcpClientZeroTools:
    """``_deny_zero_tools`` -- the one refusal not scoped to MCP calls.

    Unit-level: sets ``_spec_zero_tools`` directly rather than driving the full
    mirror/projection pipeline (covered separately by
    ``test_acp_session_mcp.py::TestZeroTools`` and the provider-mirror tests),
    since the fact this refusal reacts to is a single boolean on the client.
    """

    @staticmethod
    def _permission_request(request_id: int = 1) -> "acp_client.JsonRpcMessage":
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            id=request_id,
            method="session/request_permission",
            params={
                "sessionId": "s-1",
                "toolCall": {
                    "toolCallId": "c1",
                    "kind": "execute",
                    "status": "pending",
                    "title": "Bash",
                },
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
                ],
            },
        )

    @pytest.mark.asyncio
    async def test_refuses_when_the_spec_declares_zero_tools(self, monkeypatch):
        audited: list[dict] = []

        class _Sel:
            def log_tool_invocation(self, **kw):
                audited.append(kw)

        monkeypatch.setattr(acp_client, "sel_module", types.SimpleNamespace(sel=lambda: _Sel()))
        client = AcpClient()
        client._spec_zero_tools = True
        sent: list[tuple] = []

        async def _send(request_id, payload):
            sent.append((request_id, payload))

        client._send_response = _send  # type: ignore[method-assign]
        event = client._build_permission_event(self._permission_request())
        assert event is not None
        assert await client._deny_zero_tools(event) is True
        assert sent, "no rejection was sent for a zero-tool session"
        assert audited and audited[0]["outcome"] == "denied"
        assert audited[0]["metadata"]["reason"] == "spec_zero_tools"
        assert audited[0]["tool_kind"] == "native"

    @pytest.mark.asyncio
    async def test_a_session_with_tools_is_untouched(self):
        client = AcpClient()
        assert client._spec_zero_tools is False
        event = client._build_permission_event(self._permission_request())
        assert event is not None
        assert await client._deny_zero_tools(event) is False

    def test_zero_tools_forces_the_session_to_judge_requests(self):
        client = AcpClient()
        assert client._judges_permission_requests is False
        client._spec_zero_tools = True
        assert client._judges_permission_requests is True

    @pytest.mark.asyncio
    async def test_opencode_still_refuses_when_the_spec_declares_zero_tools(self, monkeypatch):
        monkeypatch.setattr(
            acp_client,
            "sel_module",
            types.SimpleNamespace(
                sel=lambda: types.SimpleNamespace(log_tool_invocation=lambda **k: None)
            ),
        )
        client = AcpClient(acp_backend=ACP_BACKEND_OPENCODE)
        client._spec_zero_tools = True
        client._send_response = AsyncMock()  # type: ignore[method-assign]
        event = client._build_permission_event(self._permission_request())
        assert event is not None
        assert await client._deny_zero_tools(event) is True

    @pytest.mark.asyncio
    async def test_a_backend_that_does_not_honour_the_ban_is_not_refused(self):
        """claude's routing is not enforced, so the refusal is not applied there and
        a zero-tool spec does not force the session to judge requests."""
        client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE)
        client._spec_zero_tools = True
        event = client._build_permission_event(self._permission_request())
        assert event is not None
        assert await client._deny_zero_tools(event) is False
        assert client._judges_permission_requests is False

    def test_spec_zero_tools_property_mirrors_the_private_flag(self):
        client = AcpClient()
        assert client.spec_zero_tools is False
        client._spec_zero_tools = True
        assert client.spec_zero_tools is True

    @pytest.mark.asyncio
    async def test_the_auto_approve_site_refuses_before_approving(self, monkeypatch):
        """``_handle_permission`` must refuse a zero-tool session's own native
        tool call, not just an MCP one -- the whole gap this refusal closes."""
        monkeypatch.setattr(
            acp_client,
            "sel_module",
            types.SimpleNamespace(
                sel=lambda: types.SimpleNamespace(log_tool_invocation=lambda **k: None)
            ),
        )
        client = AcpClient()
        client._spec_zero_tools = True
        sent: list[tuple] = []

        async def _send(request_id, payload):
            sent.append((request_id, payload))

        client._send_response = _send  # type: ignore[method-assign]
        await client._handle_permission(self._permission_request())
        assert sent, "the auto-approve path approved a call on a zero-tool session"


class TestEffectiveSpecDeclaresZeroTools:
    """``effective_spec_declares_zero_tools`` judges, when a projection exists, both
    the view the harness was launched with and the published alias file it loaded,
    which needs no mirror, so a native-routed backend can be checked; with none, it
    needs the authored spec to declare an empty list both before the spawn and after
    start."""

    _ALIAS = "kirocrew-view-abc"

    @staticmethod
    def _client_with_projection(specs, errors=None) -> AcpClient:
        from kiro_crew.acp.skill_projection import NativeSkillProjection

        client = AcpClient(agent="kirocrew-knowledge")
        client._native_skill_projection = NativeSkillProjection(
            {"kirocrew-knowledge": TestEffectiveSpecDeclaresZeroTools._ALIAS} if specs else {},
            specs,
            errors or {},
        )
        return client

    @classmethod
    def _publish(cls, spec: object) -> None:
        """Write the alias file the harness loads, where the projection publishes it."""
        from kiro_crew.acp import skill_projection

        agents = skill_projection.kiro_agents_dir()
        agents.mkdir(parents=True, exist_ok=True)
        (agents / f"{cls._ALIAS}.json").write_text(json.dumps(spec), encoding="utf-8")

    def test_an_empty_projected_view_is_confirmed(self):
        self._publish({"name": self._ALIAS, "tools": [], "includeMcpJson": False})
        client = self._client_with_projection(
            {"kirocrew-knowledge": {"name": self._ALIAS, "tools": [], "includeMcpJson": False}},
        )
        assert client.effective_spec_declares_zero_tools() is True

    @pytest.mark.parametrize(
        "published",
        [{"tools": ["@another-server"]}, {"name": "x"}, {"tools": "none"}, ["tools"]],
    )
    def test_an_empty_view_whose_published_file_is_not_an_empty_list_is_not_confirmed(
        self, published
    ):
        """The harness loads the published file, so a tool written to it after the
        view was prepared refuses even though the view itself is still empty."""
        self._publish(published)
        client = self._client_with_projection(
            {"kirocrew-knowledge": {"name": self._ALIAS, "tools": [], "includeMcpJson": False}},
        )
        assert client.effective_spec_declares_zero_tools() is False
        assert client.effective_spec_declares_zero_tools(authored_before_spawn=True) is False

    def test_an_empty_view_whose_published_file_is_missing_is_not_confirmed(self):
        client = self._client_with_projection(
            {"kirocrew-knowledge": {"name": self._ALIAS, "tools": [], "includeMcpJson": False}},
        )
        assert client.effective_spec_declares_zero_tools() is False

    def test_an_empty_view_with_no_alias_for_the_agent_is_not_confirmed(self):
        from kiro_crew.acp.skill_projection import NativeSkillProjection

        self._publish({"name": self._ALIAS, "tools": [], "includeMcpJson": False})
        client = AcpClient(agent="kirocrew-knowledge")
        client._native_skill_projection = NativeSkillProjection(
            {}, {"kirocrew-knowledge": {"name": self._ALIAS, "tools": [], "includeMcpJson": False}}
        )
        assert client.effective_spec_declares_zero_tools() is False

    def test_a_projected_view_carrying_a_tool_is_not_confirmed(self):
        client = self._client_with_projection(
            {
                "kirocrew-knowledge": {
                    "name": "kirocrew-view-abc",
                    "tools": ["@kirocrew-core/skill_search"],
                }
            },
        )
        assert client.effective_spec_declares_zero_tools() is False

    def test_a_projected_view_without_a_tools_list_is_not_confirmed(self):
        client = self._client_with_projection({"kirocrew-knowledge": {"name": "kirocrew-view-abc"}})
        assert client.effective_spec_declares_zero_tools() is False

    def test_a_non_list_tools_value_is_not_confirmed(self):
        client = self._client_with_projection(
            {"kirocrew-knowledge": {"name": "kirocrew-view-abc", "tools": "none"}}
        )
        assert client.effective_spec_declares_zero_tools() is False

    def test_a_projection_with_an_error_for_the_agent_is_not_confirmed(self):
        client = self._client_with_projection(
            {}, errors={"kirocrew-knowledge": "skill_search is disabled"}
        )
        assert client.effective_spec_declares_zero_tools() is False

    def test_a_projection_with_no_view_for_the_agent_is_not_confirmed(self):
        client = self._client_with_projection(
            {"another-agent": {"name": "kirocrew-view-def", "tools": [], "includeMcpJson": False}}
        )
        assert client.effective_spec_declares_zero_tools() is False

    def test_no_projection_without_a_pre_spawn_read_is_not_confirmed(self, monkeypatch):
        """No pre-spawn confirmation means the bracket is not met, whatever the
        authored spec says now; the post-start read is not even needed."""
        client = AcpClient(agent="kirocrew-knowledge")
        client._native_skill_projection = None
        monkeypatch.setattr(
            acp_client,
            "agent_spec_snapshot",
            lambda *_a, **_k: {"tools": [], "includeMcpJson": False},
        )
        assert client.effective_spec_declares_zero_tools() is False
        assert client.effective_spec_declares_zero_tools(authored_before_spawn=False) is False

    def test_a_client_that_never_prepared_a_projection_uses_the_bracket(self, monkeypatch):
        client = AcpClient(agent="kirocrew-knowledge")
        assert not hasattr(client, "_native_skill_projection")
        monkeypatch.setattr(
            acp_client,
            "agent_spec_snapshot",
            lambda *_a, **_k: {"tools": [], "includeMcpJson": False},
        )
        assert client.effective_spec_declares_zero_tools() is False
        assert client.effective_spec_declares_zero_tools(authored_before_spawn=True) is True

    def test_no_projection_with_both_reads_empty_is_confirmed(self, monkeypatch):
        client = AcpClient(agent="kirocrew-knowledge")
        client._native_skill_projection = None
        monkeypatch.setattr(
            acp_client,
            "agent_spec_snapshot",
            lambda *_a, **_k: {"tools": [], "includeMcpJson": False},
        )
        assert client.effective_spec_declares_zero_tools(authored_before_spawn=True) is True

    def test_no_projection_with_a_spec_changed_after_the_pre_spawn_read_is_not_confirmed(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        agents = tmp_path / "project" / ".kiro" / "agents"
        agents.mkdir(parents=True)
        spec_path = agents / "kirocrew-knowledge.json"
        spec_path.write_text(
            json.dumps({"name": "kirocrew-knowledge", "tools": [], "includeMcpJson": False})
        )
        client = AcpClient(agent="kirocrew-knowledge", work_dir=tmp_path / "project")
        client._native_skill_projection = None
        before = client.authored_spec_declares_zero_tools()
        assert before is True
        spec_path.write_text(json.dumps({"name": "kirocrew-knowledge", "tools": ["@builtin"]}))
        assert client.effective_spec_declares_zero_tools(authored_before_spawn=before) is False

    @pytest.mark.parametrize(
        "snapshot",
        [None, {}, {"name": "x"}, {"tools": "none"}, {"tools": ["@builtin"]}, ["tools"]],
    )
    def test_no_projection_with_an_unconfirming_authored_spec_is_not_confirmed(
        self, monkeypatch, snapshot
    ):
        client = AcpClient(agent="kirocrew-knowledge")
        client._native_skill_projection = None
        monkeypatch.setattr(acp_client, "agent_spec_snapshot", lambda *_a, **_k: snapshot)
        assert client.effective_spec_declares_zero_tools(authored_before_spawn=True) is False

    def test_a_projected_view_with_tools_ignores_the_authored_spec(self, monkeypatch):
        client = self._client_with_projection(
            {"kirocrew-knowledge": {"name": "v", "tools": ["@builtin"]}}
        )
        monkeypatch.setattr(
            acp_client,
            "agent_spec_snapshot",
            lambda *_a, **_k: {"tools": [], "includeMcpJson": False},
        )
        assert client.effective_spec_declares_zero_tools(authored_before_spawn=True) is False

    def test_a_zero_tool_projected_view_ignores_the_authored_spec(self, monkeypatch):
        self._publish({"name": self._ALIAS, "tools": [], "includeMcpJson": False})
        client = self._client_with_projection(
            {"kirocrew-knowledge": {"name": "v", "tools": [], "includeMcpJson": False}}
        )
        monkeypatch.setattr(
            acp_client, "agent_spec_snapshot", lambda *_a, **_k: {"tools": ["@builtin"]}
        )
        assert client.effective_spec_declares_zero_tools(authored_before_spawn=False) is True
        assert client.effective_spec_declares_zero_tools(authored_before_spawn=True) is True

    def test_the_authored_spec_is_not_read_when_a_projection_exists(self, tmp_path, monkeypatch):
        """With a projection the answer rests on the captured view and the published
        file: a resolver that raises and an authored spec carrying tools on disk
        leave a zero-tool view whose published file is empty confirmed."""
        monkeypatch.setenv("KIROCREW_HOME", str(tmp_path / "home"))
        project_agents = tmp_path / "project" / ".kiro" / "agents"
        project_agents.mkdir(parents=True)
        (project_agents / "kirocrew-knowledge.json").write_text(
            json.dumps({"name": "kirocrew-knowledge", "tools": ["@builtin"]})
        )

        def _boom(*_args, **_kwargs):
            raise AssertionError("the confirmation re-read an authored spec")

        monkeypatch.setattr(acp_client, "agent_spec_snapshot", _boom)
        self._publish({"name": self._ALIAS, "tools": [], "includeMcpJson": False})
        client = self._client_with_projection(
            {"kirocrew-knowledge": {"name": self._ALIAS, "tools": [], "includeMcpJson": False}}
        )
        assert client.effective_spec_declares_zero_tools() is True
        assert client.effective_spec_declares_zero_tools(authored_before_spawn=True) is True


class TestAuthoredSpecDeclaresZeroTools:
    """``authored_spec_declares_zero_tools`` reads the spec the harness resolves
    ``--agent`` to, project checkout first, and fails closed."""

    @staticmethod
    def _client(work_dir=None) -> AcpClient:
        return AcpClient(agent="kirocrew-knowledge", work_dir=work_dir)

    @pytest.mark.parametrize(
        ("snapshot", "expected"),
        [
            ({"tools": [], "includeMcpJson": False}, True),
            ({"tools": ["@builtin"]}, False),
            ({"name": "x"}, False),
            ({"tools": None}, False),
            ({"tools": "none"}, False),
            ({"tools": {}}, False),
            (None, False),
            (["tools"], False),
        ],
    )
    def test_the_snapshot_decides(self, monkeypatch, snapshot, expected):
        monkeypatch.setattr(acp_client, "agent_spec_snapshot", lambda *_a, **_k: snapshot)
        assert self._client().authored_spec_declares_zero_tools() is expected

    def test_a_resolver_failure_is_not_confirmed_and_is_logged(self, monkeypatch, caplog):
        def _boom(*_args, **_kwargs):
            raise OSError("unreadable")

        monkeypatch.setattr(acp_client, "agent_spec_snapshot", _boom)
        with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
            assert self._client().authored_spec_declares_zero_tools() is False
        assert any("unreadable" in r.getMessage() for r in caplog.records)

    def test_the_resolver_receives_the_agent_and_work_dir(self, tmp_path, monkeypatch):
        seen: dict = {}

        def _snap(agent, *, work_dir=None):
            seen.update(agent=agent, work_dir=work_dir)
            return {"tools": [], "includeMcpJson": False}

        monkeypatch.setattr(acp_client, "agent_spec_snapshot", _snap)
        assert self._client(tmp_path).authored_spec_declares_zero_tools() is True
        assert seen == {"agent": "kirocrew-knowledge", "work_dir": tmp_path}

    def test_the_project_spec_wins_over_the_user_level_one(self, tmp_path, monkeypatch):
        """Project-nearest, as the harness resolves ``--agent``: a project spec
        carrying tools is judged even though the user-level one is empty."""
        from kiro_crew import agent as agent_mod
        from kiro_crew.acp import session_mcp

        user_agents = tmp_path / "agents"
        user_agents.mkdir()
        monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", user_agents)
        monkeypatch.setattr(session_mcp, "ensure_agent_materialized", lambda _a: True)
        (user_agents / "kirocrew-knowledge.json").write_text(
            json.dumps({"name": "kirocrew-knowledge", "tools": [], "includeMcpJson": False})
        )
        project = tmp_path / "project"
        project_agents = project / ".kiro" / "agents"
        project_agents.mkdir(parents=True)
        (project_agents / "kirocrew-knowledge.json").write_text(
            json.dumps({"name": "kirocrew-knowledge", "tools": ["@builtin"]})
        )
        assert self._client(project).authored_spec_declares_zero_tools() is False
        (project_agents / "kirocrew-knowledge.json").unlink()
        assert self._client(project).authored_spec_declares_zero_tools() is True


class TestAcpClientToolHooks:
    """Covers the ACP-layer PreToolUse/PostToolUse HOOK ENGINE fire (worker-pool path).

    These fire the script-hook engine (not the SEL audit) so app/worker-pool
    subagents reach hook parity with the main agent and SubagentManager
    subagents — e.g. so the skill-usage 'Reading *SKILL.md*' PostToolUse hook
    fires for their skill loads. Gated on ``audit_source`` so the chat/main
    client (audit_source=None) never fires here (it fires via chat_runner).
    """

    @staticmethod
    def _call_ev(title="Reading /x/SKILL.md", tool_input=None, call_id="tc1"):
        # Stand-in for an EVENT_TOOL_CALL AcpEvent (reads .title/.tool_input).
        return types.SimpleNamespace(title=title, tool_input=tool_input, tool_call_id=call_id)

    @staticmethod
    def _result_ev(output="---\nname: x\n---", call_id="tc1"):
        # Stand-in for an EVENT_TOOL_RESULT AcpEvent (no .title — matches
        # _build_tool_result_event, which only carries tool_call_id/tool_output).
        return types.SimpleNamespace(tool_call_id=call_id, tool_output=output)

    class _Store:
        """Minimal ScriptHookStore stand-in recording fire() calls."""

        def __init__(self):
            self.calls = []

        async def fire(self, event, **kw):
            self.calls.append({"event": event, **kw})
            return []

    @pytest.mark.asyncio
    async def test_pre_and_post_fire_when_audit_source_set(self, monkeypatch):
        store = self._Store()
        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: store)
        client = AcpClient(session_key="sk", agent="code-review-sage", audit_source="subagent")
        # Pre-side: fire_tool_hooks (Pre-only) for the tool_call.
        await client._maybe_fire_pre_tool_hooks(self._call_ev())
        # Post-side: the result event has no title, so the name is recovered
        # from _observed_tool_calls (populated on the tool_call in dispatch).
        client._observed_tool_calls["tc1"] = ("Reading /x/SKILL.md", "read")
        # tool_output embeds a real AWS-key credential pattern inside SKILL.md
        # frontmatter: the Post-fire path MUST redact it (parity with chat_runner's
        # PostToolUse) before handing it to the hook engine — addresses review-bot
        # security-controls.
        raw_output = "---\nname: x\nsecret: AKIAIOSFODNN7EXAMPLE\n---"
        await client._maybe_fire_post_tool_hooks(self._result_ev(output=raw_output))

        pre = [c for c in store.calls if c["event"] == "PreToolUse"]
        post = [c for c in store.calls if c["event"] == "PostToolUse"]
        assert len(pre) == 1
        assert pre[0]["tool_name"] == "Reading /x/SKILL.md"
        assert len(post) == 1
        assert post[0]["tool_name"] == "Reading /x/SKILL.md"
        # Post MUST carry the output on tool_response.output so the skill-usage
        # emit.sh can read the SKILL.md frontmatter — but the credential MUST be
        # redacted (not the raw form).
        fired_output = post[0]["tool_response"]["output"]
        assert fired_output == "---\nname: x\nsecret: [REDACTED: credential]\n---"
        assert "AKIAIOSFODNN7EXAMPLE" not in fired_output  # raw credential scrubbed
        # YAML frontmatter (name:) is untouched by redaction → skill capture intact.
        assert "name: x" in fired_output

    @pytest.mark.asyncio
    async def test_post_fire_truncates_tool_output_to_2000(self, monkeypatch):
        """Post-fire MUST bound tool_output to 2000 chars before firing user hooks.

        Regression guard: the redacted tool_output was passed to
        the hook engine unbounded — chat_runner/subagent.py cap it at [:2000].
        Redact-then-truncate is deliberate (redact the FULL output first so a
        secret past char 2000 is still scrubbed, THEN truncate). The credential
        sits at the START, so after truncation the redaction marker survives and
        proves the value reaching the hook is BOTH truncated AND redacted.
        """
        store = self._Store()
        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: store)
        client = AcpClient(session_key="sk", agent="code-review-sage", audit_source="subagent")
        client._observed_tool_calls["tc1"] = ("Reading /x/SKILL.md", "read")
        # Credential near the front + padding to blow past the 2000-char bound.
        raw_output = "secret: AKIAIOSFODNN7EXAMPLE\n" + ("x" * 3000)
        await client._maybe_fire_post_tool_hooks(self._result_ev(output=raw_output))

        post = [c for c in store.calls if c["event"] == "PostToolUse"]
        assert len(post) == 1
        fired_output = post[0]["tool_response"]["output"]
        assert len(fired_output) == 2000  # bounded to first 2000 chars
        assert "AKIAIOSFODNN7EXAMPLE" not in fired_output  # raw credential scrubbed
        assert "[REDACTED: credential]" in fired_output  # redaction survives truncation

    @pytest.mark.asyncio
    async def test_pre_fire_redacts_credential_in_tool_input(self, monkeypatch):
        """Pre-fire MUST redact tool_input before handing it to the hook engine.

        tool_event.tool_input is LLM-generated. It must be redacted (credentials +
        exfil URLs) before reaching user hook scripts, mirroring the Post path. A
        JSON string is used because fire_tool_hooks json.loads the input; the marker
        keeps it valid JSON so the parsed value reaching store.fire is redacted.
        """
        store = self._Store()
        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: store)
        client = AcpClient(session_key="sk", agent="code-review-sage", audit_source="subagent")
        raw_input = '{"cmd": "aws configure set key AKIAIOSFODNN7EXAMPLE"}'
        await client._maybe_fire_pre_tool_hooks(self._call_ev(tool_input=raw_input))

        pre = [c for c in store.calls if c["event"] == "PreToolUse"]
        assert len(pre) == 1
        fired_input = pre[0]["tool_input"]  # parsed by fire_tool_hooks
        fired_str = json.dumps(fired_input)
        assert "AKIAIOSFODNN7EXAMPLE" not in fired_str  # raw credential scrubbed
        assert "[REDACTED: credential]" in fired_str  # redaction marker present

    @pytest.mark.asyncio
    async def test_pre_fire_redacts_credential_in_dict_tool_input(self, monkeypatch):
        """A DICT tool_input containing a credential MUST also be redacted.

        An isinstance(str) guard would bypass redaction for dict/list inputs, so
        LLM-generated dict inputs could reach user hook scripts RAW. Non-str inputs
        are serialized to JSON, redacted, and passed as a redacted JSON string
        (fire_tool_hooks json.loads it, yielding a redacted parsed value at
        store.fire).
        """
        store = self._Store()
        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: store)
        client = AcpClient(session_key="sk", agent="code-review-sage", audit_source="subagent")
        raw_input = {"cmd": "aws configure set key AKIAIOSFODNN7EXAMPLE"}
        await client._maybe_fire_pre_tool_hooks(self._call_ev(tool_input=raw_input))

        pre = [c for c in store.calls if c["event"] == "PreToolUse"]
        assert len(pre) == 1
        fired_input = pre[0]["tool_input"]  # parsed by fire_tool_hooks
        fired_str = json.dumps(fired_input)
        assert "AKIAIOSFODNN7EXAMPLE" not in fired_str  # raw credential scrubbed
        assert "[REDACTED: credential]" in fired_str  # redaction marker present

    @pytest.mark.asyncio
    async def test_pre_fire_uses_unknown_tool_name_when_title_none(self, monkeypatch):
        """A title-less tool_call MUST fire with tool_name 'unknown' (Post parity).

        tool_event.title may be None. The Pre path applies the same 'unknown'
        fallback the Post path uses, so a hook matcher sees a consistent name
        across Pre and Post.
        """
        store = self._Store()
        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: store)
        client = AcpClient(session_key="sk", agent="code-review-sage", audit_source="subagent")
        await client._maybe_fire_pre_tool_hooks(self._call_ev(title=None))

        pre = [c for c in store.calls if c["event"] == "PreToolUse"]
        assert len(pre) == 1
        assert pre[0]["tool_name"] == "unknown"

    @pytest.mark.asyncio
    async def test_post_fire_uses_unknown_tool_name_when_call_id_unrecorded(self, monkeypatch):
        """A tool_result whose tool_call_id is absent from _observed_tool_calls
        MUST fire Post with tool_name 'unknown' (Pre parity), not '' (empty string).

        A ("", "") default tuple would resolve a missing/unrecorded tool_call to an
        empty tool_name, inconsistent with the Pre path's 'unknown' fallback.
        """
        store = self._Store()
        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: store)
        client = AcpClient(session_key="sk", agent="code-review-sage", audit_source="subagent")
        # Deliberately do NOT populate _observed_tool_calls for this call_id.
        await client._maybe_fire_post_tool_hooks(self._result_ev(call_id="missing"))

        post = [c for c in store.calls if c["event"] == "PostToolUse"]
        assert len(post) == 1
        assert post[0]["tool_name"] == "unknown"

    @pytest.mark.asyncio
    async def test_noop_when_audit_source_none(self, monkeypatch):
        store = self._Store()
        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: store)
        client = AcpClient(session_key="sk")  # audit_source defaults to None
        await client._maybe_fire_pre_tool_hooks(self._call_ev())
        client._observed_tool_calls["tc1"] = ("Reading /x/SKILL.md", "read")
        await client._maybe_fire_post_tool_hooks(self._result_ev())
        # chat / subagent clients must never fire ACP-layer hooks (no double-fire).
        assert store.calls == []

    @pytest.mark.asyncio
    async def test_noop_when_hook_store_uninitialized(self, monkeypatch):
        # get_global_hook_store() returns None before the dashboard registers it.
        looked_up = []
        monkeypatch.setattr(
            acp_client, "get_global_hook_store", lambda: looked_up.append(True) or None
        )
        client = AcpClient(session_key="sk", audit_source="subagent")
        # Must not raise despite no store.
        await client._maybe_fire_pre_tool_hooks(self._call_ev())
        await client._maybe_fire_post_tool_hooks(self._result_ev())
        assert looked_up == [True, True]

    @pytest.mark.asyncio
    async def test_swallows_hook_engine_errors(self, monkeypatch):
        fired = []

        class _BoomStore:
            async def fire(self, event, **kw):
                fired.append(event)
                raise RuntimeError("hook engine down")

        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: _BoomStore())
        # fire_tool_hooks awaits store.fire internally; force both paths to raise.
        client = AcpClient(session_key="sk", audit_source="subagent")
        client._observed_tool_calls["tc1"] = ("Reading /x/SKILL.md", "read")
        # A hook-engine failure must never break tool dispatch.
        await client._maybe_fire_pre_tool_hooks(self._call_ev())
        await client._maybe_fire_post_tool_hooks(self._result_ev())
        assert fired == ["PreToolUse", "PostToolUse"]

    @pytest.mark.asyncio
    async def test_read_prompt_response_update_fires_hooks(self, monkeypatch):
        """send_message -> _read_prompt_response (worker-pool path) fires hooks.

        Worker-pool subagents (knowledge/llm_pool) drive tools through
        send_message -> _read_prompt_response, not the stream _dispatch_events. Its
        update branch must fire Pre+Post hooks so those clients reach hook parity:
        otherwise an llm_pool worker's auto-approved @builder-mcp/ReadInternalWebsites
        call fires NO hook.
        """
        store = self._Store()
        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: store)
        client = AcpClient(session_key="sk", agent="knowledge-worker", audit_source="subagent")
        # Isolate hook behavior from the SEL audit sink (covered separately).
        client._maybe_audit_tool_call = AsyncMock()

        # Synthetic ACP session updates: a tool_call then its tool_call_update
        # result (only .params is read by the update branch; .result by complete).
        call_msg = types.SimpleNamespace(
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc9",
                    "title": "@builder-mcp/ReadInternalWebsites",
                    "kind": "read",
                    "rawInput": {"inputs": ["https://wiki.example.com/x"]},
                }
            }
        )
        result_msg = types.SimpleNamespace(
            params={
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc9",
                    "status": "completed",
                    "content": [{"content": {"type": "text", "text": "ok"}}],
                }
            }
        )
        complete_msg = types.SimpleNamespace(result={"stopReason": "end_turn"})

        async def _fake_loop(req_id, timeout):
            yield "update", call_msg
            yield "update", result_msg
            yield "complete", complete_msg

        monkeypatch.setattr(client, "_prompt_loop", _fake_loop)

        out = await client._read_prompt_response(1, 5.0)
        assert out == ""  # no text chunks, only tool updates

        pre = [c for c in store.calls if c["event"] == "PreToolUse"]
        post = [c for c in store.calls if c["event"] == "PostToolUse"]
        assert len(pre) == 1
        assert pre[0]["tool_name"] == "@builder-mcp/ReadInternalWebsites"
        # CALL branch populated _observed_tool_calls; Post recovers tool_name from it.
        assert client._observed_tool_calls["tc9"][0] == "@builder-mcp/ReadInternalWebsites"
        assert len(post) == 1
        assert post[0]["tool_name"] == "@builder-mcp/ReadInternalWebsites"
        # SEL audit invoked once on the CALL branch (mirrors _dispatch_events).
        assert client._maybe_audit_tool_call.await_count == 1

    @pytest.mark.asyncio
    async def test_read_prompt_response_update_noop_when_audit_source_none(self, monkeypatch):
        """Main-chat send_message callers (audit_source=None) stay no-op: the
        _read_prompt_response update branch must NOT double-fire hooks."""
        store = self._Store()
        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: store)
        client = AcpClient(session_key="sk")  # audit_source defaults to None

        call_msg = types.SimpleNamespace(
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc9",
                    "title": "SomeTool",
                    "kind": "read",
                }
            }
        )
        result_msg = types.SimpleNamespace(
            params={
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc9",
                    "status": "completed",
                    "content": [{"content": {"type": "text", "text": "ok"}}],
                }
            }
        )
        complete_msg = types.SimpleNamespace(result={"stopReason": "end_turn"})

        async def _fake_loop(req_id, timeout):
            yield "update", call_msg
            yield "update", result_msg
            yield "complete", complete_msg

        monkeypatch.setattr(client, "_prompt_loop", _fake_loop)
        await client._read_prompt_response(1, 5.0)
        assert store.calls == []  # gated off in the _maybe_* methods


class TestSpecIsToolless:
    """``spec_is_toolless`` needs all five keys to mount nothing; the knowledge,
    guest and lite checks all judge through it."""

    _EMPTY = {"tools": [], "mcpServers": {}, "includeMcpJson": False}

    @pytest.mark.parametrize(
        "extra",
        [
            {},
            {"allowedTools": []},
            {"hooks": {}},
            {"hooks": []},
            {"includePowers": False},
            {"permissions": {}},
            {"toolsSettings": {}},
            {"resources": ["skill://x"]},
        ],
    )
    def test_an_empty_spec_is_toolless(self, extra):
        from kiro_crew.acp.client_mcp_array import spec_is_toolless

        assert spec_is_toolless({**self._EMPTY, **extra}) is True

    @pytest.mark.parametrize(
        "change",
        [
            {"tools": ["@creds-agent"]},
            {"allowedTools": ["@creds-agent"]},
            {"allowedTools": "*"},
            {"mcpServers": {"creds-agent": {"command": "creds"}}},
            {"mcpServers": []},
            {"hooks": {"agentSpawn": [{"command": "echo"}]}},
            {"hooks": "echo"},
            {"includeMcpJson": True},
            {"includePowers": True},
            {"includePowers": None},
            {"permissions": {"allow": ["@creds-agent"]}},
            {"toolsSettings": {"shell": {"allowedCommands": [".*"]}}},
            {"includeMcpJson": None},
            {"includeMcpJson": 0},
        ],
    )
    def test_anything_mounted_is_not_toolless(self, change):
        from kiro_crew.acp.client_mcp_array import spec_is_toolless

        assert spec_is_toolless({**self._EMPTY, **change}) is False

    def test_a_missing_include_mcp_json_is_not_toolless(self):
        from kiro_crew.acp.client_mcp_array import spec_is_toolless

        assert spec_is_toolless({"tools": [], "mcpServers": {}}) is False

    @pytest.mark.parametrize("spec", [None, [], "tools", {}])
    def test_a_non_spec_is_not_toolless(self, spec):
        from kiro_crew.acp.client_mcp_array import spec_is_toolless

        assert spec_is_toolless(spec) is False
