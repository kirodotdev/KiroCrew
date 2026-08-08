"""Tests for ACP client."""

import asyncio
import json
import os
import signal
import sys
import time
import types
from collections import deque
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from spawn_test_helpers import strip_spawn_shim

import kiro_crew.acp.client as acp_client
from kiro_crew.acp.client import (
    _CLAUDE_ACP_PKG_ENTRY,
    _DRAIN_DURATION,
    _DRAIN_IDLE_EXIT,
    AcpClient,
    AcpError,
    AcpProcessDied,
    _format_acp_error,
    _is_model_substitution_advisory,
    _make_unified_diff,
    _resolve_vendored_claude_acp,
    _substitute_model_from_advisory,
    _vendored_claude_acp_roots,
)
from kiro_crew.acp.liveness import (
    VERDICT_DEAD,
    VERDICT_UNKNOWN,
    VERDICT_WORKING,
    LivenessOracle,
)
from kiro_crew.acp.types import ACP_BACKEND_CLAUDE, AcpPromptStats

# Windows lacks os.killpg and POSIX process-tree APIs (ps, /proc).
# Tests that exercise these paths are skipped on Windows.
_POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX process tree APIs only")

# Separate from _POSIX_ONLY because the reason differs: these tests assert POSIX
# EXECUTABLE-RESOLUTION semantics, not process-tree APIs. They build fixtures that
# have no Windows equivalent — extensionless binaries made runnable with
# chmod(0o755), which `shutil.which` cannot find on Windows because it resolves
# candidates through PATHEXT, and `/`-rooted paths, which os.path.realpath()
# anchors to the current drive (`/home/u/x` -> `D:\home\u\x`). The production
# resolvers are correct on Windows; only these fixtures are POSIX-shaped.
_POSIX_EXEC_PATHS_ONLY = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX executable-resolution semantics only"
)


class TestVendoredClaudeAcp:
    """Resolve the vendored claude-agent-acp adapter (no npm/network)."""

    def _make_vendored(self, root: Path, *, with_deps: bool = True) -> Path:
        """Create a fake vendored adapter under *root*.

        With *with_deps* (default) also creates the hoisted dependency marker
        ``@agentclientprotocol/sdk`` so the completeness guard accepts it.
        """
        from kiro_crew.acp.client import _CLAUDE_ACP_DEP_MARKER

        entry = root / _CLAUDE_ACP_PKG_ENTRY
        entry.parent.mkdir(parents=True, exist_ok=True)
        entry.write_text("// fake adapter\n", encoding="utf-8")
        if with_deps:
            (root / _CLAUDE_ACP_DEP_MARKER).mkdir(parents=True, exist_ok=True)
        return entry

    def test_finds_vendored_in_pkg_vendor_dir(self, tmp_path, monkeypatch):
        # Toolbox/pip layout: <pkg_dir>/_vendor/node_modules/<pkg>/dist/index.js.
        # Inject a fake pkg_dir under an isolated home so the real workspace
        # (sibling-website detection / KIROCREW_PROJECT_DIR) is not consulted.
        monkeypatch.delenv("KIROCREW_PROJECT_DIR", raising=False)
        pkg_dir = tmp_path / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        entry = self._make_vendored(pkg_dir / "_vendor" / "node_modules")
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) == str(entry)

    def test_finds_vendored_under_project_dir(self, tmp_path, monkeypatch):
        # KIROCREW_PROJECT_DIR/node_modules holds the adapter; pkg_dir has none.
        pkg_dir = tmp_path / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        entry = self._make_vendored(tmp_path / "proj" / "node_modules")
        (tmp_path / "proj").mkdir(exist_ok=True)
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "proj"))
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) == str(entry)

    def test_returns_none_when_absent(self, tmp_path, monkeypatch):
        # Isolated pkg_dir with no _vendor and a project dir with no adapter.
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "empty"))
        (tmp_path / "empty").mkdir()
        pkg_dir = tmp_path / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) is None

    def test_skips_incomplete_copy_missing_deps(self, tmp_path, monkeypatch):
        # Regression: an entry script with no hoisted deps must be rejected
        # (it would crash with ERR_MODULE_NOT_FOUND @agentclientprotocol/sdk),
        # falling through to a complete copy under KIROCREW_PROJECT_DIR.
        pkg_dir = tmp_path / "site-packages" / "kiro_crew"
        pkg_dir.mkdir(parents=True)
        # Incomplete copy in _vendor (entry only, no deps) — must be skipped.
        self._make_vendored(pkg_dir / "_vendor" / "node_modules", with_deps=False)
        # Complete copy in the project dir — must win.
        (tmp_path / "proj").mkdir()
        good = self._make_vendored(tmp_path / "proj" / "node_modules")
        monkeypatch.setenv("KIROCREW_PROJECT_DIR", str(tmp_path / "proj"))
        assert _resolve_vendored_claude_acp(pkg_dir=pkg_dir) == str(good)

    def test_roots_include_pkg_vendor_dir(self):
        # The toolbox-bundle vendor location must always be the first candidate.
        roots = _vendored_claude_acp_roots()
        assert roots[0].name == "node_modules" and roots[0].parent.name == "_vendor"


class TestAcpClientInit:
    def test_defaults(self):
        client = AcpClient()
        assert not client.is_ready
        assert client._session_id is None

    def test_custom_work_dir(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        assert client._work_dir == tmp_path

    def test_audit_source_defaults_none(self):
        # Chat/subagent clients already audit via chat_runner / SubagentManager;
        # the flag must stay None by default so they never double-log.
        assert AcpClient()._audit_source is None

    def test_audit_source_opt_in(self):
        # App/worker-pool clients (code-review-sage, knowledge llm_pool) have no
        # external audit loop, so they opt in to ACP-layer SEL tool auditing.
        assert AcpClient(audit_source="subagent")._audit_source == "subagent"


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
        def _boom():
            raise RuntimeError("SEL backend down")

        monkeypatch.setattr(acp_client, "sel", _boom)
        client = AcpClient(session_key="sk", audit_source="subagent")
        # A SEL failure must never break tool dispatch.
        await client._maybe_audit_tool_call(self._ev())

    @pytest.mark.asyncio
    async def test_hung_backend_times_out_and_is_swallowed(self, monkeypatch):
        # A wedged SEL backend must not stall tool dispatch: the wait_for bound
        # raises TimeoutError (caught) so the coroutine returns promptly.
        class _Sel:
            def log_tool_invocation(self, **kw):
                time.sleep(5)  # simulate a hung backend, well past the timeout

        monkeypatch.setattr(acp_client, "sel", lambda: _Sel())
        monkeypatch.setattr(acp_client, "_SEL_AUDIT_TIMEOUT_SECONDS", 0.05)
        client = AcpClient(session_key="sk", audit_source="subagent")
        start = time.monotonic()
        await client._maybe_audit_tool_call(self._ev())  # must not raise / hang
        assert time.monotonic() - start < 4  # returned via timeout, not the 5s sleep


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

        Regression for review-bot security-controls: tool_event.tool_input
        is LLM-generated and was passed to user hook scripts RAW. It must be
        redacted (credentials + exfil URLs) first, mirroring the Post path. A JSON
        string is used because fire_tool_hooks json.loads the input; the marker
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

        Regression for review-bot security-controls: the isinstance(str)
        guard bypassed redaction for dict/list inputs, so LLM-generated dict inputs
        reached user hook scripts RAW. Non-str inputs are now serialized to JSON,
        redacted, and passed as a redacted JSON string (fire_tool_hooks json.loads
        it, yielding a redacted parsed value at store.fire).
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

        Regression for the Pre path passed tool_event.title (which may
        be None) while the Post path recovers a name and falls back — the Pre path
        now applies the same 'unknown' fallback so a hook matcher sees a consistent
        name across Pre and Post.
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

        Regression for the Post path previously used a ("", "")
        default tuple, so a missing/unrecorded tool_call resolved to an empty
        tool_name — inconsistent with the Pre path's 'unknown' fallback.
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
        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: None)
        client = AcpClient(session_key="sk", audit_source="subagent")
        # Must not raise despite no store.
        await client._maybe_fire_pre_tool_hooks(self._call_ev())
        await client._maybe_fire_post_tool_hooks(self._result_ev())

    @pytest.mark.asyncio
    async def test_swallows_hook_engine_errors(self, monkeypatch):
        class _BoomStore:
            async def fire(self, event, **kw):
                raise RuntimeError("hook engine down")

        monkeypatch.setattr(acp_client, "get_global_hook_store", lambda: _BoomStore())
        # fire_tool_hooks awaits store.fire internally; force both paths to raise.
        client = AcpClient(session_key="sk", audit_source="subagent")
        client._observed_tool_calls["tc1"] = ("Reading /x/SKILL.md", "read")
        # A hook-engine failure must never break tool dispatch.
        await client._maybe_fire_pre_tool_hooks(self._call_ev())
        await client._maybe_fire_post_tool_hooks(self._result_ev())

    @pytest.mark.asyncio
    async def test_read_prompt_response_update_fires_hooks(self, monkeypatch):
        """send_message -> _read_prompt_response (worker-pool path) fires hooks.

        Regression for worker-pool subagents (knowledge/llm_pool)
        drive tools through send_message -> _read_prompt_response, not the stream
        _dispatch_events. Its update branch must fire Pre+Post hooks so those
        clients reach hook parity — the live gateway test caught an llm_pool
        worker's auto-approved @builder-mcp/ReadInternalWebsites call firing NO
        hook because this path was uninstrumented.
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
                    "rawInput": {"inputs": ["https://w.amazon.com/x"]},
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


class TestAcpClientSessionKey:
    def test_stores_session_key(self):
        client = AcpClient(session_key="test-key")
        assert client._session_key == "test-key"

    @pytest.mark.asyncio
    async def test_spawn_sets_env_with_session_key(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, session_key="test-key")
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert env["KIROCREW_SESSION_KEY"] == "test-key"

    @pytest.mark.asyncio
    async def test_spawn_sets_env_with_channel_id(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, session_key="k", channel_id="C0ABC123")
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert env["KIROCREW_CHANNEL_ID"] == "C0ABC123"
            assert env["KIROCREW_SESSION_KEY"] == "k"

    @pytest.mark.asyncio
    async def test_spawn_forwards_claude_config_dir_from_extra_env(self, tmp_path):
        # The loader factory injects CLAUDE_CONFIG_DIR into cc_env (→ extra_env);
        # _spawn must forward it verbatim to the subprocess so the adapter's
        # SettingsManager reads the isolated dir (creds kept, plugins stripped).
        iso = str(tmp_path / "cc-config")
        client = AcpClient(
            work_dir=tmp_path,
            acp_backend=ACP_BACKEND_CLAUDE,
            extra_env={"CLAUDE_CONFIG_DIR": iso, "CLAUDE_CODE_USE_BEDROCK": "1"},
        )
        with (
            patch(
                "kiro_crew.acp.client._resolve_claude_acp_bin",
                return_value=["/usr/bin/node", "/x/acp.js"],
            ),
            patch(
                "kiro_crew.acp.client.wrap_argv",
                return_value=(["/usr/bin/node", "/x/acp.js"], None),
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert env["CLAUDE_CONFIG_DIR"] == iso
            # Bedrock flag must ride alongside (regression guard).
            assert env["CLAUDE_CODE_USE_BEDROCK"] == "1"

    @pytest.mark.asyncio
    async def test_spawn_no_channel_id_env_absent(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, session_key="k", channel_id=None)
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert "KIROCREW_CHANNEL_ID" not in env

    @pytest.mark.asyncio
    async def test_spawn_channel_id_only_no_session_key(self, tmp_path):
        clean_env = {k: v for k, v in os.environ.items() if k != "KIROCREW_SESSION_KEY"}
        client = AcpClient(work_dir=tmp_path, session_key=None, channel_id="C0ABC123")
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
            patch.dict(os.environ, clean_env, clear=True),
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None
            assert env["KIROCREW_CHANNEL_ID"] == "C0ABC123"
            assert "KIROCREW_SESSION_KEY" not in env

    @pytest.mark.asyncio
    async def test_spawn_no_session_key_env_none(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, session_key=None)
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv", return_value=(["/usr/bin/kiro-cli", "acp"], None)
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            call_kwargs = mock_exec.call_args
            env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
            assert env is not None, "env should be a dict (SSH_AUTH_SOCK resolution)"
            assert "KIROCREW_SESSION_KEY" not in env


class TestAcpClientBackendSelection:
    """Verify the right backend binary is launched for kiro vs claude."""

    @pytest.fixture(autouse=True)
    def _reset_claude_cache(self):
        import kiro_crew.acp.client as _mod

        _mod._claude_acp_argv_cache = _mod._UNRESOLVED
        yield
        _mod._claude_acp_argv_cache = _mod._UNRESOLVED

    @pytest.fixture(autouse=True)
    def _no_cgroup_scope(self):
        # These tests assert the exact/leading spawn argv to verify BACKEND
        # SELECTION (which binary), not cgroup wrapping. Our branch wires
        # cgroup_scope_argv() into _spawn(), which prepends a `systemd-run
        # --user --scope ... --` prefix on Linux hosts WITH cgroup-v2 systemd
        # delegation (XDG_RUNTIME_DIR set). Neutralize the probe here so the
        # argv is host-independent — no prefix regardless of the runner. Scoped
        # to this class only; test_sandbox_argv.py (which tests the wrapping
        # itself) is a separate file and is unaffected.
        with patch(
            "kiro_crew.sandbox._probe_cgroup_scope",
            return_value=(False, "disabled-in-test"),
        ):
            yield

    @pytest.mark.asyncio
    async def test_spawn_claude_backend_uses_claude_acp_bin(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        with (
            patch(
                "kiro_crew.acp.client._resolve_claude_acp_bin",
                return_value=["/usr/local/bin/node", "/usr/local/lib/claude-agent-acp/index.js"],
            ),
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv",
                side_effect=lambda argv, mode, **kwargs: (argv, None),
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            argv = list(strip_spawn_shim(mock_exec.call_args.args))
            assert argv == [
                "/usr/local/bin/node",
                "/usr/local/lib/claude-agent-acp/index.js",
            ], "claude backend must spawn node + script explicitly"

    @pytest.mark.asyncio
    async def test_spawn_claude_backend_missing_bin_raises(self, tmp_path):
        client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        with (
            patch("kiro_crew.acp.client._resolve_claude_acp_bin", return_value=None),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock),
        ):
            with pytest.raises(AcpError, match="claude-agent-acp not found"):
                await client._spawn()

    @pytest.mark.asyncio
    async def test_spawn_kiro_backend_unchanged(self, tmp_path):
        """Default (non-claude) backend still spawns `kiro-cli acp --agent <name>`."""
        client = AcpClient(work_dir=tmp_path)
        with (
            patch("kiro_crew.acp.client._resolve_kiro_bin", return_value="/usr/bin/kiro-cli"),
            patch(
                "kiro_crew.acp.client.wrap_argv",
                side_effect=lambda argv, mode, **kwargs: (argv, None),
            ),
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec,
            patch("kiro_crew.session._track_pid"),
            patch("kiro_crew.session._track_session_pid"),
        ):
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.returncode = None
            mock_exec.return_value = mock_proc

            await client._spawn()

            argv = list(strip_spawn_shim(mock_exec.call_args.args))
            assert argv[0] == "/usr/bin/kiro-cli"
            assert argv[1] == "acp"
            assert "--agent" in argv

    @pytest.mark.asyncio
    async def test_initialize_protocol_version_per_backend(self, tmp_path):
        """kiro expects a date string; claude-agent-acp expects an integer."""
        from kiro_crew.acp.client import (
            PROTOCOL_VERSION,
            PROTOCOL_VERSION_CLAUDE,
        )

        for backend, expected in (
            ("", PROTOCOL_VERSION),
            (ACP_BACKEND_CLAUDE, PROTOCOL_VERSION_CLAUDE),
        ):
            client = AcpClient(work_dir=tmp_path, acp_backend=backend)
            client._session_id = "sess-1"  # short-circuit past the new-session call
            sent_params: dict = {}

            async def fake_send_request(method, params, _sent=sent_params):
                if method == "initialize":
                    _sent.update(params)
                return 1

            async def fake_wait(_req_id, timeout=0):
                return {"protocolVersion": expected, "agentCapabilities": {}}

            client._send_request = fake_send_request  # type: ignore[assignment]
            client._wait_for_response = fake_wait  # type: ignore[assignment]
            client._drain_notifications = AsyncMock()  # type: ignore[assignment]

            # Stop after step 1 (initialize) — we only care about the first request.
            try:
                await client._initialize_session()
            except Exception:
                pass
            assert sent_params.get("protocolVersion") == expected, (
                f"backend={backend!r} expected protocolVersion={expected!r}, "
                f"got {sent_params.get('protocolVersion')!r}"
            )


class TestResolveClaudeAcpBin:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _resolve_claude_acp_bin

        bin_path = tmp_path / "claude-agent-acp"
        bin_path.write_text("#!/bin/sh\nexit 0\n")
        bin_path.chmod(0o755)
        monkeypatch.setenv("CLAUDE_AGENT_ACP_BIN", str(bin_path))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        result = _resolve_claude_acp_bin()
        assert result is not None
        assert str(bin_path) in result

    @_POSIX_EXEC_PATHS_ONLY
    def test_path_lookup(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        bin_path = tmp_path / "claude-agent-acp"
        bin_path.write_text("#!/bin/sh\nexit 0\n")
        bin_path.chmod(0o755)
        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: str(bin_path) if name == "claude-agent-acp" else None,
        )
        result = client_mod._resolve_claude_acp_bin()
        assert result is not None
        assert str(bin_path) in result

    @_POSIX_EXEC_PATHS_ONLY
    def test_mise_which_preferred(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        script = tmp_path / "bin" / "claude-agent-acp"
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env node\nconsole.log('hi')\n")
        script.chmod(0o755)
        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: str(script))
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: None,
        )
        result = client_mod._resolve_claude_acp_bin()
        assert result == [str(script)]

    @_POSIX_EXEC_PATHS_ONLY
    def test_mise_installed_script_resolves_node(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _resolve_claude_acp_bin

        mise_node = tmp_path / ".local" / "share" / "mise" / "installs" / "node" / "20.18.0"
        node_bin = mise_node / "bin" / "node"
        node_bin.parent.mkdir(parents=True)
        node_bin.write_text("#!/bin/sh\nexit 0\n")
        node_bin.chmod(0o755)
        script = (
            mise_node
            / "lib"
            / "node_modules"
            / "@agentclientprotocol"
            / "claude-agent-acp"
            / "dist"
            / "index.js"
        )
        script.parent.mkdir(parents=True)
        script.write_text("#!/usr/bin/env node\nconsole.log('hi')\n")
        script.chmod(0o755)
        monkeypatch.setenv("CLAUDE_AGENT_ACP_BIN", str(script))
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        result = _resolve_claude_acp_bin()
        assert result == [str(node_bin), str(script.resolve())]

    def test_non_executable_script_falls_back_to_path_node(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)

        script = tmp_path / "node_modules" / "claude-agent-acp" / "dist" / "index.js"
        script.parent.mkdir(parents=True)
        script.write_text("console.log('hi')\n")
        script.chmod(0o644)  # NOT executable

        node_bin = tmp_path / "bin" / "node"
        node_bin.parent.mkdir(parents=True)
        node_bin.write_text("#!/bin/sh\nexit 0\n")
        node_bin.chmod(0o755)

        monkeypatch.setenv("CLAUDE_AGENT_ACP_BIN", str(script))
        monkeypatch.delenv("PATH", raising=False)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: str(node_bin) if name == "node" else None,
        )
        result = client_mod._resolve_claude_acp_bin()
        assert result == [str(node_bin), str(script.resolve())]

    @_POSIX_EXEC_PATHS_ONLY
    def test_mise_glob_fallback(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        mise_node = tmp_path / ".local" / "share" / "mise" / "installs" / "node" / "22.1.0"
        bin_dir = mise_node / "bin"
        bin_dir.mkdir(parents=True)
        acp_script = bin_dir / "claude-agent-acp"
        acp_script.write_text("#!/usr/bin/env node\nconsole.log('hi')\n")
        acp_script.chmod(0o755)
        node_bin = bin_dir / "node"
        node_bin.write_text("#!/bin/sh\nexit 0\n")
        node_bin.chmod(0o755)

        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: None,
        )
        result = client_mod._resolve_claude_acp_bin()
        assert result == [str(node_bin), str(acp_script.resolve())]

    def test_returns_none_when_not_found(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        fake_home = tmp_path / "fakehome"
        fake_home.mkdir()
        monkeypatch.delenv("CLAUDE_AGENT_ACP_BIN", raising=False)
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod, "_resolve_vendored_claude_acp", lambda: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: None,
        )
        result = client_mod._resolve_claude_acp_bin()
        assert result is None


class TestResolveClaudeCodeExecutable:
    def test_env_override_wins(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        exe = tmp_path / "claude"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        monkeypatch.setenv("CLAUDE_CODE_EXECUTABLE", str(exe))
        # mise/PATH must NOT be consulted when the override is a real file.
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: "/should/not/win")
        assert client_mod._resolve_claude_code_executable() == str(exe)

    def test_env_override_ignored_when_missing(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.setenv("CLAUDE_CODE_EXECUTABLE", str(tmp_path / "nope"))
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod.shutil, "which", lambda name, path=None: None)
        assert client_mod._resolve_claude_code_executable() is None

    def test_mise_preferred_over_path(self, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: "/mise/bin/claude")
        monkeypatch.setattr(client_mod.shutil, "which", lambda name, path=None: "/usr/bin/claude")
        assert client_mod._resolve_claude_code_executable() == "/mise/bin/claude"

    @_POSIX_EXEC_PATHS_ONLY
    def test_path_lookup(self, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name, path=None: "/home/u/.toolbox/bin/claude" if name == "claude" else None,
        )
        assert client_mod._resolve_claude_code_executable() == "/home/u/.toolbox/bin/claude"

    def test_none_when_absent(self, monkeypatch):
        from kiro_crew.acp import client as client_mod

        monkeypatch.delenv("CLAUDE_CODE_EXECUTABLE", raising=False)
        monkeypatch.setattr(client_mod, "_mise_which", lambda tool: None)
        monkeypatch.setattr(client_mod.shutil, "which", lambda name, path=None: None)
        assert client_mod._resolve_claude_code_executable() is None


class TestMiseWhich:
    def test_returns_path_on_success(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _mise_which

        script = tmp_path / "claude-agent-acp"
        script.write_text("#!/usr/bin/env node\n")
        script.chmod(0o755)

        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name: str(tmp_path / "mise") if name == "mise" else None,
        )
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout=str(script) + "\n"))
        monkeypatch.setattr(client_mod, "subprocess_mod", MagicMock(run=mock_run))
        assert _mise_which("claude-agent-acp") == str(script)

    def test_returns_none_when_mise_not_installed(self, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _mise_which

        monkeypatch.setattr(client_mod.shutil, "which", lambda name: None)
        assert _mise_which("claude-agent-acp") is None

    def test_returns_none_on_nonzero_exit(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _mise_which

        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name: str(tmp_path / "mise") if name == "mise" else None,
        )
        mock_run = MagicMock(return_value=MagicMock(returncode=1, stdout=""))
        monkeypatch.setattr(client_mod, "subprocess_mod", MagicMock(run=mock_run))
        assert _mise_which("claude-agent-acp") is None

    def test_returns_none_on_timeout(self, tmp_path, monkeypatch):
        import subprocess

        from kiro_crew.acp import client as client_mod
        from kiro_crew.acp.client import _mise_which

        monkeypatch.setattr(
            client_mod.shutil,
            "which",
            lambda name: str(tmp_path / "mise") if name == "mise" else None,
        )
        mock_sub = MagicMock()
        mock_sub.run = MagicMock(side_effect=subprocess.TimeoutExpired("mise", 5))
        mock_sub.TimeoutExpired = subprocess.TimeoutExpired
        monkeypatch.setattr(client_mod, "subprocess_mod", mock_sub)
        assert _mise_which("claude-agent-acp") is None


class TestAcpClientStaleTurn:
    """Regression for the stale-turn false-positive on the thinking path.

    The staleness check in ``_prompt_loop`` must fold in ``_last_activity``
    (refreshed by the stderr drain when the agent streams ``thinking_tokens``)
    and not rely solely on the stdout clock. Otherwise a turn that streams its
    final text and then thinks silently on stdout — while still emitting
    thinking events on stderr — is falsely declared stale, burning
    ~_STALE_TURN_TIMEOUT seconds per turn and reaping long subagent runs.
    """

    def _client_with_silent_stdout(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        # stdout is silent: _read_message returns None (timeout/no data).
        client._read_message = AsyncMock(return_value=None)
        # process stays alive so the consecutive-empty death path is not taken.
        client._is_process_alive = MagicMock(return_value=True)
        # caller streamed text earlier this turn, so staleness is eligible.
        client._stale_eligible = True
        return client

    @pytest.mark.asyncio
    async def test_recent_stderr_activity_prevents_stale_turn(self, tmp_path, caplog):
        """thinking_tokens on stderr (recent _last_activity) keeps the turn alive.

        With a tiny stale timeout, a turn whose stdout is silent but whose
        _last_activity keeps refreshing (as the stderr drain does during
        thinking) must NOT be declared stale.
        """
        client = self._client_with_silent_stdout(tmp_path)

        # Emulate the stderr drain refreshing _last_activity on every poll, so
        # the turn is continuously "active" even though stdout yields nothing.
        # Sleep well under the stale timeout each poll so liveness never lapses
        # (mirrors thinking_tokens streaming faster than _STALE_TURN_TIMEOUT).
        async def read_and_touch(*args, **kwargs):
            await asyncio.sleep(0.05)
            client._last_activity = time.monotonic()
            return None

        client._read_message = AsyncMock(side_effect=read_and_touch)

        # The overall loop timeout (1.0s) must exceed _STALE_TURN_TIMEOUT (0.2s)
        # so the staleness check gets multiple chances to fire during the loop —
        # otherwise the loop exits on `remaining <= 0` before the check runs and
        # the test would pass trivially, even without the fix. Each 0.05s poll
        # refreshes _last_activity, staying well under the 0.2s stale window.
        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.2):
            with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                actions = []
                async for action, _msg in client._prompt_loop(req_id=1, timeout=1.0):
                    actions.append(action)

        assert actions == []  # stdout silent → nothing yielded
        assert "Stale turn detected" not in caplog.text  # fix: not falsely stale

    @pytest.mark.asyncio
    async def test_genuine_silence_still_triggers_stale_turn(self, tmp_path, caplog):
        """Silence on BOTH stdout and stderr still trips the stale-turn guard."""
        client = self._client_with_silent_stdout(tmp_path)
        # _last_activity never refreshes → genuinely silent on both clocks.
        client._last_activity = time.monotonic()

        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.05):
            with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                actions = []
                async for action, _msg in client._prompt_loop(req_id=7, timeout=5.0):
                    actions.append(action)

        assert actions == []  # returned via stale-turn early return
        assert "Stale turn detected" in caplog.text  # real protection preserved


class TestAcpClientStaleTurnOracleGate:
    """The 90s stale-turn cutoff is oracle-gated.

    A turn whose stdout AND stderr are silent but whose backend process
    subtree is provably WORKING (CPU/IO movement) must NOT be declared stale
    at ``_STALE_TURN_TIMEOUT`` — the kiro shared-runtime path already defers on
    a WORKING liveness verdict; the ``AcpClient`` capture path must converge
    onto the same contract instead of a blunt wall-clock. A non-WORKING verdict
    preserves today's behavior exactly (the turn ends).
    """

    def _silent_client(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._read_message = AsyncMock(return_value=None)
        client._is_process_alive = MagicMock(return_value=True)
        client._stale_eligible = True
        client._last_activity = time.monotonic()
        return client

    @pytest.mark.asyncio
    async def test_working_verdict_defers_stale_turn(self, tmp_path, caplog):
        """WORKING backend movement keeps a silent turn alive past the cutoff."""
        client = self._silent_client(tmp_path)
        client._consult_liveness_model_wait = AsyncMock(
            return_value=(VERDICT_WORKING, "backend activity (io)")
        )

        # Loop timeout must exceed the stale window so the check fires repeatedly;
        # without the fix the first stale hit returns and the turn ends.
        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.1):
            with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                actions = []
                async for action, _msg in client._prompt_loop(req_id=11, timeout=0.6):
                    actions.append(action)

        assert actions == []
        # The turn was NOT declared stale — it ran until the loop deadline.
        assert "Stale turn detected" not in caplog.text
        # Proves priming: the oracle is consulted on silent reads BEFORE the
        # cutoff too, not once at the 90s mark — a single consult would read
        # UNKNOWN (no movement baseline) and reap. Expect several consults over
        # the 0.6s loop at the ~_READ_TIMEOUT cadence (patched small in the loop).
        assert client._consult_liveness_model_wait.await_count >= 2

    @pytest.mark.asyncio
    async def test_real_oracle_movement_defers_via_fake_proc(self, tmp_path, caplog):
        """End-to-end with the REAL LivenessOracle over a fake /proc tree.

        Guards the priming bug the mock hides: the oracle needs a prior sample
        to compute a movement delta, so the loop must consult it repeatedly to
        build a baseline before the cutoff. Here the backend subtree's IO
        counter grows between reads → real WORKING verdict → deferral.

        Builds a minimal fake ``/proc`` inline (only the files the model-wait
        oracle reads: ``<pid>/stat``, ``<pid>/task/<tid>/children``,
        ``<pid>/io``) rather than importing a helper from another test module —
        a cross-``test``-package import fails in the pipeline runner where
        ``test`` is not an importable package (``ModuleNotFoundError``).
        """
        proc = tmp_path / "proc"

        def _write_pid(pid: int, *, children: list[int], io_bytes: int) -> None:
            d = proc / str(pid)
            (d / "task" / str(pid)).mkdir(parents=True, exist_ok=True)
            (d / "task" / str(pid) / "children").write_text(" ".join(str(c) for c in children))
            # stat: pid (comm) state ... starttime(field 22); huge starttime →
            # negative age so the oracle's start-time guard accepts the pid.
            fields = ["0"] * 50
            fields[0] = "S"
            fields[19] = "10000000"
            (d / "stat").write_text(f"{pid} (fake) {' '.join(fields)}\n")
            (d / "io").write_text(f"rchar: {io_bytes}\nwchar: 0\n")

        def _set_io(pid: int, io_bytes: int) -> None:
            (proc / str(pid) / "io").write_text(f"rchar: {io_bytes}\nwchar: 0\n")

        _write_pid(4242, children=[4243], io_bytes=0)
        _write_pid(4243, children=[], io_bytes=1000)

        client = AcpClient(work_dir=tmp_path)
        client._is_process_alive = MagicMock(return_value=True)
        client._stale_eligible = True
        client._last_activity = time.monotonic()
        client._pid = 4242
        # Real oracle, tiny sample window so movement is detectable within the
        # fast test loop; point it at the fake /proc tree.
        client._liveness_oracle = LivenessOracle(str(proc), sample_min_secs=0.0)

        # Grow the subtree's IO on every silent read → movement between samples.
        _io = {"n": 1000}

        async def read_and_move(*args, **kwargs):
            await asyncio.sleep(0.02)
            _io["n"] += 5000
            _set_io(4243, _io["n"])
            return None

        client._read_message = AsyncMock(side_effect=read_and_move)

        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.05):
            with patch("kiro_crew.acp.client._READ_TIMEOUT", 0.02):
                with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                    actions = []
                    async for action, _msg in client._prompt_loop(req_id=42, timeout=0.5):
                        actions.append(action)

        assert actions == []
        assert "Stale turn detected" not in caplog.text  # real movement → deferred

    @pytest.mark.asyncio
    async def test_dead_verdict_still_ends_stale_turn(self, tmp_path, caplog):
        """A non-WORKING verdict preserves today's end-the-turn behavior."""
        client = self._silent_client(tmp_path)
        client._consult_liveness_model_wait = AsyncMock(
            return_value=(VERDICT_DEAD, "no established backend socket")
        )

        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.05):
            with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                actions = []
                async for action, _msg in client._prompt_loop(req_id=12, timeout=5.0):
                    actions.append(action)

        assert actions == []
        assert "Stale turn detected" in caplog.text

    @pytest.mark.asyncio
    async def test_unknown_verdict_still_ends_stale_turn(self, tmp_path, caplog):
        """UNKNOWN keeps the conservative 90s cutoff — the gate never weakens
        hang recovery; only a provably-WORKING turn is extended."""
        client = self._silent_client(tmp_path)
        client._consult_liveness_model_wait = AsyncMock(
            return_value=(VERDICT_UNKNOWN, "established-but-flat")
        )

        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.05):
            with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
                actions = []
                async for action, _msg in client._prompt_loop(req_id=13, timeout=5.0):
                    actions.append(action)

        assert actions == []
        assert "Stale turn detected" in caplog.text


class TestAcpClientReadMessage:
    @pytest.mark.asyncio
    async def test_read_valid_json(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        msg_data = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}
        line = json.dumps(msg_data) + "\n"

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=line.encode())
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        msg = await client._read_message(timeout=1.0)
        assert msg is not None
        assert msg.is_response_for(1)
        assert msg.result == {"ok": True}

    @pytest.mark.asyncio
    async def test_read_non_json_skipped(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"not json\n")
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        msg = await client._read_message(timeout=1.0)
        assert msg is None

    @pytest.mark.asyncio
    async def test_read_empty_line(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"\n")
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        msg = await client._read_message(timeout=1.0)
        assert msg is None

    @pytest.mark.asyncio
    async def test_read_eof(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"")
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        msg = await client._read_message(timeout=1.0)
        assert msg is None

    @pytest.mark.asyncio
    async def test_read_buffer_overrun_drops_frame_and_keeps_reading(self, tmp_path):
        """A line exceeding the stdout buffer costs that ONE frame, not the turn.

        The stream is NOT corrupted afterwards, contrary to what this call site
        used to assume: readline() removes the oversize line through its
        terminating newline (or clears the buffer when the newline has not
        arrived yet) and resumes the transport before raising ValueError. So the
        overrun joins the blank-line and non-JSON paths in returning None, and
        the caller's next read gets the following frame. Raising AcpProcessDied
        here killed a healthy live turn over one unreadably large frame.

        Driven through a REAL StreamReader so the recovery claim is asserted
        against asyncio's actual behaviour rather than a mock's side_effect.
        """
        client = AcpClient(work_dir=tmp_path)

        reader = asyncio.StreamReader(limit=256)
        mock_process = MagicMock()
        mock_process.stdout = reader
        mock_process.returncode = None
        client._process = mock_process

        reader.feed_data(b"X" * 1024 + b"\n")  # oversize frame
        reader.feed_data(b'{"jsonrpc":"2.0","method":"session/update","params":{}}\n')

        assert await client._read_message(timeout=1.0) is None  # frame dropped
        msg = await client._read_message(timeout=1.0)
        assert msg is not None and msg.method == "session/update"

    @pytest.mark.asyncio
    async def test_repeated_buffer_overruns_never_kill_the_process(self, tmp_path):
        """Oversize frames must not accumulate into a kill here.

        This reader carries no drain budget on purpose (see the asymmetry note in
        `_read_message`): every call is bounded by the caller's timeout, so a run
        of oversize frames costs only those frames. A frame-count cap would
        reintroduce exactly the defect this PR removes — death from a replay of
        properly-terminated but oversize frames."""
        client = AcpClient(work_dir=tmp_path)

        reader = asyncio.StreamReader(limit=256)
        mock_process = MagicMock()
        mock_process.stdout = reader
        mock_process.returncode = None
        client._process = mock_process

        for _ in range(40):
            reader.feed_data(b"X" * 1024 + b"\n")  # oversize, terminated
            assert await client._read_message(timeout=1.0) is None

        reader.feed_data(b'{"jsonrpc":"2.0","method":"session/update","params":{}}\n')
        msg = await client._read_message(timeout=1.0)
        assert msg is not None and msg.method == "session/update"


class TestAcpClientExtractChunk:
    def test_extract_text_chunk(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": "hello"},
                }
            },
        )
        assert client._extract_text_chunk(msg) == ("hello", False)

    def test_extract_thinking_chunk(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "thinking", "text": "let me think"},
                }
            },
        )
        assert client._extract_text_chunk(msg) == ("let me think", True)

    def test_extract_non_text_chunk(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "tool_call", "title": "exec"}},
        )
        assert client._extract_text_chunk(msg) == (None, False)

    @pytest.mark.parametrize("bad_update", [None, "chunk", [1], 7])
    def test_non_dict_update_returns_none(self, tmp_path, bad_update):
        """The update value comes straight from the agent process; a non-dict
        raised AttributeError on update.get() inside the prompt-turn dispatch
        path, tearing down the whole turn."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(method="session/update", params={"update": bad_update})
        assert client._extract_text_chunk(msg) == (None, False)  # must not raise


class TestAcpClientTrackToolCall:
    def test_tracks_tool_call(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "execute_bash",
                    "kind": "tool_use",
                }
            },
        )
        client._track_tool_call(msg)
        assert ("tool_use", "execute_bash") in client.last_prompt_stats.tool_calls

    @pytest.mark.parametrize("bad_update", [None, "call", [1], 7])
    def test_non_dict_update_ignored(self, tmp_path, bad_update):
        """Same boundary as _extract_text_chunk: non-dict update must be a
        no-op, not an AttributeError in the dispatch path."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(method="session/update", params={"update": bad_update})
        client._track_tool_call(msg)  # must not raise
        assert client.last_prompt_stats.tool_calls == []


class TestAcpClientTrackMetadata:
    def test_tracks_context_pct(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"contextUsagePercentage": 42.5},
        )
        client._track_metadata(msg)
        assert client.last_prompt_stats.context_pct == 42.5

    def test_backfill_clamps_malformed_pct(self, tmp_path, monkeypatch):
        """A degenerate metadata percentage (huge finite / inf / NaN) must not
        overflow round() and abort the turn; derived used stays in [0, window]."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200000)
        for bad in (1e308, float("inf"), float("nan")):
            client = AcpClient(work_dir=tmp_path)
            client._model = "some-model"
            # Must not raise OverflowError/ValueError.
            client._track_metadata(
                JsonRpcMessage(
                    method="_kiro.dev/metadata",
                    params={"contextUsagePercentage": bad},
                )
            )
            used = client.last_prompt_stats.context_used_tokens
            assert 0 <= used <= 200000
            # context_pct is sanitized at the source, never left non-finite.
            pct = client.last_prompt_stats.context_pct
            assert 0.0 <= pct <= 100.0


class TestAcpClientTrackUsageUpdate:
    """claude-agent-acp usage_update {used, size}: derives context_pct and
    records the raw token counts for the dashboard token text."""

    def _usage_msg(self, used, size):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "usage_update", "used": used, "size": size}},
        )

    def test_populates_pct_and_tokens(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(50000, 200000))
        stats = client.last_prompt_stats
        assert stats.context_pct == 25.0  # 50000 / 200000 * 100
        assert stats.context_used_tokens == 50000
        assert stats.context_window_tokens == 200000
        # A real usage_update marks the counts authoritative.
        assert stats.context_tokens_from_usage is True

    def test_metadata_pct_does_not_clobber_authoritative_tokens(self, tmp_path):
        """Regression: a real usage_update (408K/1000K → 40.8%) followed by a
        kiro metadata contextUsagePercentage=73 must NOT leave the headline pct
        at 73 while the token text still reads 408K/1000K (the desync the user
        saw in the context-window popover). usage_update wins for BOTH."""
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(408_000, 1_000_000))
        assert client.last_prompt_stats.context_pct == 40.8

        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={"contextUsagePercentage": 73},
            )
        )
        stats = client.last_prompt_stats
        assert stats.context_pct == 40.8  # NOT clobbered to 73
        assert stats.context_used_tokens == 408_000
        assert stats.context_window_tokens == 1_000_000

    def test_metadata_credits_still_captured_when_tokens_authoritative(self, tmp_path):
        """The pct guard must not drop kiro billing credits — those accumulate
        regardless of whether token counts are authoritative."""
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(408_000, 1_000_000))
        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={
                    "contextUsagePercentage": 73,
                    "meteringUsage": [{"unit": "credit", "value": 1.5}],
                },
            )
        )
        assert client.last_prompt_stats.credits == 1.5
        assert client.last_prompt_stats.context_pct == 40.8

    def test_missing_fields_leave_tokens_zero(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(None, None))
        stats = client.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_window_tokens == 0

    @pytest.mark.parametrize(
        "used,size",
        [
            ("50000", "200000"),  # numeric strings: `size > 0` raises TypeError
            (50000, "200000"),
            ([50000], 200000),
            (50000, {"n": 1}),
            (True, True),  # bools are ints but nonsense as token counts
            # json parses NaN/Infinity literals to non-finite floats, which
            # pass an isinstance check but crash int() (ValueError/OverflowError).
            (float("nan"), 200000),
            (50000, float("inf")),
            (float("inf"), float("nan")),
            # An arbitrary-precision int beyond float range makes
            # math.isfinite itself raise OverflowError.
            (10**400, 200000),
            (50000, 10**400),
        ],
    )
    def test_non_numeric_used_size_treated_as_absent(self, tmp_path, used, size):
        """used/size come straight from the agent process; a non-numeric value
        raised TypeError on `size > 0` / `used / size` inside the prompt-turn
        dispatch path, tearing down the whole turn. Malformed values must be
        treated like absent ones."""
        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(used, size))  # must not raise
        stats = client.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_window_tokens == 0

    def test_float_used_size_still_tracked(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(50000.0, 200000.0))
        stats = client.last_prompt_stats
        assert stats.context_pct == 25.0
        assert stats.context_used_tokens == 50000
        assert stats.context_window_tokens == 200000

    def test_tokens_carry_forward_across_prompt_reset(self, tmp_path):
        # The per-prompt reset preserves the last known pct + token counts so
        # the dashboard ring/text doesn't flicker to 0 at the start of a turn.
        client = AcpClient(work_dir=tmp_path)
        client._track_usage_update(self._usage_msg(88000, 200000))
        prev_pct = client.last_prompt_stats.context_pct
        prev_used = client.last_prompt_stats.context_used_tokens
        prev_window = client.last_prompt_stats.context_window_tokens
        prev_from_usage = client.last_prompt_stats.context_tokens_from_usage
        from kiro_crew.acp.types import AcpPromptStats

        # Mirror the reset sites (send_message_stream / _dispatch_events / etc.)
        client.last_prompt_stats = AcpPromptStats(
            context_pct=prev_pct,
            context_used_tokens=prev_used,
            context_window_tokens=prev_window,
            context_tokens_from_usage=prev_from_usage,
        )
        assert client.last_prompt_stats.context_used_tokens == 88000
        assert client.last_prompt_stats.context_window_tokens == 200000
        assert client.last_prompt_stats.context_pct == 44.0
        # The authoritative flag must survive the reset, else the next turn's
        # early metadata pct could clobber the carried token-derived pct.
        assert client.last_prompt_stats.context_tokens_from_usage is True


class TestAcpClientNoProcess:
    @pytest.mark.asyncio
    async def test_send_request_no_process(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        with pytest.raises(AcpError, match="not running"):
            await client._send_request("test", {})

    @pytest.mark.asyncio
    async def test_read_message_no_process(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        with pytest.raises(AcpError, match="not running"):
            await client._read_message()


# ── Process tree cleanup tests ──


class TestGetChildPids:
    def test_nonexistent_pid(self):
        from kiro_crew.acp.client import _get_child_pids

        assert _get_child_pids(999999) == []

    def test_none_pid(self):
        from kiro_crew.acp.client import _get_child_pids

        assert _get_child_pids(None) == []

    def test_own_pid_returns_list(self):
        import os

        from kiro_crew.acp.client import _get_child_pids

        # May or may not have children, but should not raise
        result = _get_child_pids(os.getpid())
        assert isinstance(result, list)

    def test_recursive_children(self, monkeypatch):
        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import _get_child_pids

        # _direct_children tries /proc first, falls back to pgrep.
        # Mock _direct_children directly to avoid platform-specific /proc behavior.
        tree = {1000: [2000, 3000], 2000: [4000], 3000: [5000]}
        monkeypatch.setattr(client_mod, "_direct_children", lambda pid: tree.get(pid, []))
        # Depth-first: 2000 → 4000, then 3000 → 5000
        assert _get_child_pids(1000) == [2000, 4000, 3000, 5000]


@_POSIX_ONLY
class TestIsOurChild:
    def test_nonexistent_pid(self):
        from kiro_crew.acp.client import _is_our_child

        assert _is_our_child(999999) is False

    def test_own_pid_matches_with_basename(self, monkeypatch):
        import sys

        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import _is_our_child

        monkeypatch.setattr(client_mod, "_get_start_time", lambda pid: 42)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            client_mod.subprocess_mod,
            "check_output",
            lambda cmd, **kw: b"python3",
        )
        # With correct start_time and basename, should match
        assert _is_our_child(999, expected_start=42, expected_basename=b"python3") is True

    def test_basename_match_returns_true(self, monkeypatch):
        import sys

        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import _is_our_child

        monkeypatch.setattr(client_mod, "_get_start_time", lambda pid: 42)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            client_mod.subprocess_mod,
            "check_output",
            lambda cmd, **kw: b"node",
        )
        assert _is_our_child(999, expected_start=42, expected_basename=b"node") is True

    def test_basename_mismatch_returns_false(self, monkeypatch):
        import sys

        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import _is_our_child

        monkeypatch.setattr(client_mod, "_get_start_time", lambda pid: 42)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            client_mod.subprocess_mod,
            "check_output",
            lambda cmd, **kw: b"firefox",
        )
        # Recorded as node, but live reads as firefox → recycled
        assert _is_our_child(999, expected_start=42, expected_basename=b"node") is False

    def test_no_start_time_rejects(self):
        import os

        from kiro_crew.acp.client import _is_our_child

        # No expected_start → fail-closed
        assert _is_our_child(os.getpid()) is False

    def test_start_time_mismatch_rejects(self):
        import os

        from kiro_crew.acp.client import _is_our_child

        # Our own PID with a wrong start time → should reject (recycled)
        assert _is_our_child(os.getpid(), expected_start=-999) is False

    def test_novel_binary_tracked(self, monkeypatch):
        """Any binary recorded at spawn is supervised — no allowlist needed."""
        import sys

        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import _is_our_child

        monkeypatch.setattr(client_mod, "_get_start_time", lambda pid: 42)
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            client_mod.subprocess_mod,
            "check_output",
            lambda cmd, **kw: b"some-future-mcp-server",
        )
        # Novel binary name that would have failed the old allowlist
        assert (
            _is_our_child(999, expected_start=42, expected_basename=b"some-future-mcp-server")
            is True
        )

    def test_none_basename_denied_fail_closed(self, monkeypatch):
        """When no basename was recorded, deny (fail-closed)."""
        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import _is_our_child

        monkeypatch.setattr(client_mod, "_get_start_time", lambda pid: 42)
        # No expected_basename → deny-by-default even with matching start_time
        assert _is_our_child(999, expected_start=42, expected_basename=None) is False


@_POSIX_ONLY
class TestKillEscapedChildren:
    def test_empty_dict(self):
        from kiro_crew.acp.client import _kill_escaped_children

        _kill_escaped_children({})

    def test_dead_pids_skipped(self):
        from kiro_crew.acp.client import _kill_escaped_children

        _kill_escaped_children({999998: None, 999999: None})

    def test_reverse_order_and_allowlist(self, monkeypatch):
        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import _kill_escaped_children

        killed: list[int] = []

        def fake_kill(pid, sig):
            if sig == 0:
                return  # alive check
            killed.append(pid)

        def fake_is_our(pid, expected_start=None, expected_basename=None):
            return pid != 200  # 200 is "recycled"

        monkeypatch.setattr(client_mod.os, "kill", fake_kill)
        monkeypatch.setattr(client_mod, "_is_our_child", fake_is_our)

        _kill_escaped_children({100: None, 200: None, 300: None})
        # 200 skipped (not ours), killed in reverse: 300, 100
        assert killed == [300, 100]


class TestChildPidsField:
    def test_default_empty(self):
        client = AcpClient()
        assert client._child_pids == {}

    def test_cleared_on_reset(self):
        client = AcpClient()
        client._child_pids = {123: (None, b"node"), 456: (None, b"python")}
        client._reset_state()
        assert client._child_pids == {}


@_POSIX_ONLY
class TestReadBasename:
    def test_reads_basename_from_ps(self, monkeypatch):
        import sys

        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import _read_basename

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            client_mod.subprocess_mod,
            "check_output",
            lambda cmd, **kw: b"/usr/local/bin/node\n",
        )
        assert _read_basename(123) == b"node"

    def test_returns_none_on_error(self, monkeypatch):
        import subprocess
        import sys

        import kiro_crew.acp.client as client_mod
        from kiro_crew.acp.client import _read_basename

        def raise_error(cmd, **kw):
            raise subprocess.CalledProcessError(1, cmd)

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(client_mod.subprocess_mod, "check_output", raise_error)
        assert _read_basename(999999) is None


class TestProcessMessage:
    """Tests for _process_message action classification."""

    def _make_client(self):
        return AcpClient()

    def test_compaction_status_classified(self):
        client = self._make_client()
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS, params={"status": {"type": "completed"}}
        )
        assert client._process_message(msg, req_id=99) == "compaction"

    def test_clear_status_classified(self):
        client = self._make_client()
        from kiro_crew.acp.types import METHOD_CLEAR_STATUS, JsonRpcMessage

        msg = JsonRpcMessage(method=METHOD_CLEAR_STATUS, params={"sessionId": "s1"})
        assert client._process_message(msg, req_id=99) == "clear"

    def test_agent_switched_classified(self):
        client = self._make_client()
        from kiro_crew.acp.types import METHOD_AGENT_SWITCHED, JsonRpcMessage

        msg = JsonRpcMessage(method=METHOD_AGENT_SWITCHED, params={"agentName": "planner"})
        assert client._process_message(msg, req_id=99) == "agent_switched"

    def test_subagent_list_update_classified(self):
        client = self._make_client()
        from kiro_crew.acp.types import METHOD_SUBAGENT_LIST_UPDATE, JsonRpcMessage

        msg = JsonRpcMessage(method=METHOD_SUBAGENT_LIST_UPDATE, params={"subagents": []})
        assert client._process_message(msg, req_id=99) == "subagent_list"

    def test_kiro_session_update_classified(self):
        client = self._make_client()
        from kiro_crew.acp.types import METHOD_KIRO_SESSION_UPDATE, JsonRpcMessage

        msg = JsonRpcMessage(method=METHOD_KIRO_SESSION_UPDATE, params={"sessionId": "s1"})
        assert client._process_message(msg, req_id=99) == "subagent_activity"

    def test_unknown_method_skipped(self):
        client = self._make_client()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(method="_kiro.dev/unknown/thing")
        assert client._process_message(msg, req_id=99) == "skip"

    def test_mcp_oauth_request_classified(self):
        client = self._make_client()
        from kiro_crew.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        msg = JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"serverName": "linear", "oauthUrl": "https://mcp.linear.app/authorize?..."},
        )
        assert client._process_message(msg, req_id=99) == "mcp_oauth_request"

    def test_permission_request_with_colliding_id_not_complete(self):
        # Regression: the agent's server→client request_permission id space is
        # independent of our prompt req_id space, so they collide on small
        # integers.  A permission request whose id == the in-flight prompt's
        # req_id must classify as "permission", NOT "complete" — otherwise the
        # turn ends early and the tool blocks forever (stuck Claude Code turn).
        client = self._make_client()
        from kiro_crew.acp.types import METHOD_REQUEST_PERMISSION, JsonRpcMessage

        msg = JsonRpcMessage(
            id=4,
            method=METHOD_REQUEST_PERMISSION,
            params={"toolCall": {"title": "ls"}, "options": []},
        )
        assert client._process_message(msg, req_id=4) == "permission"

    def test_real_response_with_matching_id_completes(self):
        # The genuine prompt response (id + result, no method) still completes.
        client = self._make_client()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(id=4, result={"stopReason": "end_turn"})
        assert client._process_message(msg, req_id=4) == "complete"


class TestStreamEventsExtension:
    """End-to-end tests for stream_events() yielding extension events."""

    @pytest.mark.asyncio
    async def test_agent_switched_event_fields(self):
        """stream_events extracts agentName from params and yields correct AcpEvent."""
        from kiro_crew.acp.types import (
            EVENT_AGENT_SWITCHED,
            EVENT_COMPLETE,
            METHOD_AGENT_SWITCHED,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        # Build the two messages the prompt loop would yield
        switch_msg = JsonRpcMessage(method=METHOD_AGENT_SWITCHED, params={"agentName": "planner"})
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "agent_switched", switch_msg
            yield "complete", complete_msg

        # Patch internals so stream_events doesn't need a real process
        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 2
        assert events[0].kind == EVENT_AGENT_SWITCHED
        assert events[0].text == "planner"
        assert events[1].kind == EVENT_COMPLETE

    @pytest.mark.asyncio
    async def test_compaction_event_fields(self):
        """stream_events extracts status.type and summary from compaction params."""
        from kiro_crew.acp.types import (
            EVENT_COMPACTION_STATUS,
            METHOD_COMPACTION_STATUS,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        compact_msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "completed"}, "summary": "3k tokens saved"},
        )
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "compaction", compact_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 2
        assert events[0].kind == EVENT_COMPACTION_STATUS
        assert events[0].text == "completed"
        assert events[0].title == "3k tokens saved"

    @pytest.mark.asyncio
    async def test_clear_event_fields(self):
        """stream_events yields EVENT_CLEAR_STATUS with no extra fields."""
        from kiro_crew.acp.types import (
            EVENT_CLEAR_STATUS,
            METHOD_CLEAR_STATUS,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        clear_msg = JsonRpcMessage(method=METHOD_CLEAR_STATUS, params={"sessionId": "s1"})
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "clear", clear_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 2
        assert events[0].kind == EVENT_CLEAR_STATUS
        assert events[0].text == ""

    @pytest.mark.asyncio
    async def test_mcp_oauth_request_event_fields(self):
        """stream_events extracts serverName + oauthUrl and yields EVENT_MCP_OAUTH_REQUEST."""
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_MCP_OAUTH_REQUEST,
            METHOD_MCP_OAUTH_REQUEST,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        url = "https://mcp.linear.app/authorize?response_type=code&client_id=abc&state=xyz"
        oauth_msg = JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"sessionId": "s1", "serverName": "linear", "oauthUrl": url},
        )
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "mcp_oauth_request", oauth_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 2
        assert events[0].kind == EVENT_MCP_OAUTH_REQUEST
        assert events[0].server_name == "linear"
        assert events[0].oauth_url == url
        assert events[1].kind == EVENT_COMPLETE

    @pytest.mark.asyncio
    async def test_mcp_oauth_request_missing_url_skipped(self):
        """Notifications without oauthUrl are dropped (no event yielded)."""
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_MCP_OAUTH_REQUEST,
            METHOD_MCP_OAUTH_REQUEST,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        bad_msg = JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"serverName": "broken-server"},  # no oauthUrl
        )
        complete_msg = JsonRpcMessage(id=1, result={"status": "complete"})

        async def fake_prompt_loop(req_id, timeout):
            yield "mcp_oauth_request", bad_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        # Only the complete event — the malformed oauth notification was dropped.
        assert len(events) == 1
        assert events[0].kind == EVENT_COMPLETE
        assert not any(e.kind == EVENT_MCP_OAUTH_REQUEST for e in events)

    @pytest.mark.asyncio
    async def test_tool_interrupted_marker_synthesizes_complete(self):
        """When kiro-cli cancels tools, stream_events completes instead of hanging.

        Regression: kiro-cli's built-in security filter cancels tool uses and emits a
        text chunk "Tool uses were interrupted, waiting for the next user prompt", but
        never sends a session/prompt response. Detect the marker and synthesize a
        complete event so the caller exits cleanly.
        """
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            UPDATE_AGENT_MESSAGE_CHUNK,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        interrupt_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": "Tool uses were interrupted, waiting for the next user prompt",
                    },
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", interrupt_msg
            # No "complete" — simulates kiro-cli leaving the prompt hanging.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []
        client._emit_tool_interrupted_sel = MagicMock()

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        # Text chunk yielded to caller, then synthesized EVENT_COMPLETE.
        assert [e.kind for e in events] == [EVENT_TEXT_CHUNK, EVENT_COMPLETE]
        assert "Tool uses were interrupted" in events[0].text
        client._emit_tool_interrupted_sel.assert_called_once_with("_dispatch_events")

    @pytest.mark.asyncio
    async def test_tool_interrupted_marker_send_message_stream_returns(self):
        """send_message_stream returns cleanly (no hang) when kiro-cli cancels tools."""
        from kiro_crew.acp.types import UPDATE_AGENT_MESSAGE_CHUNK, JsonRpcMessage

        client = AcpClient()
        interrupt_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": "Tool uses were interrupted, waiting for the next user prompt",
                    },
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", interrupt_msg
            # No "complete" — without the fix, the generator would never exit.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._emit_tool_interrupted_sel = MagicMock()

        chunks: list[str] = []
        async for c in client.send_message_stream("test"):
            chunks.append(c)

        assert chunks == ["Tool uses were interrupted, waiting for the next user prompt"]
        client._emit_tool_interrupted_sel.assert_called_once_with("send_message_stream")

    @pytest.mark.asyncio
    async def test_tool_interrupted_marker_send_message_returns(self):
        """send_message returns accumulated text instead of raising AcpTimeoutError."""
        from kiro_crew.acp.types import UPDATE_AGENT_MESSAGE_CHUNK, JsonRpcMessage

        client = AcpClient()
        interrupt_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": "Tool uses were interrupted, waiting for the next user prompt",
                    },
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", interrupt_msg
            # No "complete" — would otherwise raise AcpTimeoutError on loop exit.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._emit_tool_interrupted_sel = MagicMock()

        result = await client.send_message("test", timeout=5.0)
        assert "Tool uses were interrupted" in result
        client._emit_tool_interrupted_sel.assert_called_once_with("_read_prompt_response")

    @pytest.mark.asyncio
    async def test_tool_interrupted_marker_requires_exact_match(self):
        """Substring-but-not-exact match must NOT trigger early completion.

        Protects against false positives when the model quotes the marker text.
        """
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            UPDATE_AGENT_MESSAGE_CHUNK,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        quoted_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": (
                            "The message 'Tool uses were interrupted, waiting for the next "
                            "user prompt' means kiro-cli blocked the tool."
                        ),
                    },
                }
            },
        )
        complete_msg = JsonRpcMessage(method="session/prompt", id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "update", quoted_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []
        client._emit_tool_interrupted_sel = MagicMock()

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        # Normal flow: text chunk + real complete event (not synthesized early).
        assert [e.kind for e in events] == [EVENT_TEXT_CHUNK, EVENT_COMPLETE]
        # Precondition + guard: marker IS a substring, but NOT an exact match.
        marker = "Tool uses were interrupted, waiting for the next user prompt"
        assert marker in events[0].text
        assert events[0].text.strip() != marker
        # Exact-match guard held: SEL must NOT be emitted for a quoted marker.
        client._emit_tool_interrupted_sel.assert_not_called()

    @pytest.mark.asyncio
    async def test_tool_interrupted_marker_ignored_in_thinking_chunk(self):
        """Marker arriving as a thinking chunk must NOT trigger early completion.

        The `_dispatch_events` path guards marker detection with `not is_thinking`.
        If a reasoning/thinking chunk happens to contain the exact marker text,
        it must not be treated as kiro-cli's interrupt signal — only top-level
        agent_message_chunk with non-thinking content type is the real signal.
        """
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_THINKING_CHUNK,
            UPDATE_AGENT_MESSAGE_CHUNK,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        thinking_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "thinking",
                        "text": "Tool uses were interrupted, waiting for the next user prompt",
                    },
                }
            },
        )
        complete_msg = JsonRpcMessage(method="session/prompt", id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "update", thinking_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []
        client._emit_tool_interrupted_sel = MagicMock()

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        # Thinking chunk yielded as EVENT_THINKING_CHUNK, real complete follows.
        assert [e.kind for e in events] == [EVENT_THINKING_CHUNK, EVENT_COMPLETE]
        # Guard held: SEL must NOT be emitted for a thinking-type chunk.
        client._emit_tool_interrupted_sel.assert_not_called()

    @pytest.mark.asyncio
    async def test_tool_interrupted_sel_contract(self):
        """SEL audit fields are pinned: tool_name, outcome, kind, site.

        The other marker tests mock _emit_tool_interrupted_sel itself, which only
        asserts it's invoked — not that the audit event carries the right fields.
        This test patches kiro_crew.sel.sel so the real helper runs, protecting
        against silent regressions in the security-audit contract.
        """
        from kiro_crew.acp.types import (
            UPDATE_AGENT_MESSAGE_CHUNK,
            JsonRpcMessage,
        )

        client = AcpClient(session_key="sess-xyz")
        interrupt_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": "Tool uses were interrupted, waiting for the next user prompt",
                    },
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", interrupt_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        with patch("kiro_crew.sel.sel") as mock_sel:
            async for _ in client.stream_events("test"):
                pass

        mock_sel.return_value.log_tool_invocation.assert_called_once()
        kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert kwargs["tool_name"] == "kiro_cli_security_filter"
        assert kwargs["tool_kind"] == "client_built_in"
        assert kwargs["outcome"] == "denied"
        assert kwargs["source"] == "acp"
        assert kwargs["session_key"] == "sess-xyz"
        assert kwargs["metadata"]["site"] == "_dispatch_events"
        assert kwargs["metadata"]["reason"] == "tool_interrupted_marker"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "entry_point, expected_site",
        [
            ("stream_events", "_dispatch_events"),
            ("send_message_stream", "send_message_stream"),
            ("send_message", "_read_prompt_response"),
        ],
    )
    async def test_tool_interrupted_all_paths_log_and_return_promptly(
        self, entry_point, expected_site, caplog
    ):
        """Across all three entry points: returns in bounded time, logs WARNING
        with the correct site tag, records one SEL audit.

        Pins three properties the other tests don't cover:
        1. Timing — without the fix the call hangs until the 2h prompt timeout.
           An explicit <2s bound documents the "no hang" contract.
        2. Single WARNING per call, tagged with the originating call site.
           On-call greps ``kiro-cli cancelled tool use`` — a silent regression at
           any site would mask real filter firings in production.
        3. SEL audit fires with metadata.site matching the call site, across all
           three sites (not just _dispatch_events).
        """
        import logging
        import time

        from kiro_crew.acp.types import (
            UPDATE_AGENT_MESSAGE_CHUNK,
            JsonRpcMessage,
        )

        client = AcpClient(session_key="sess-abc")
        client._session_id = "session-pin-1234"

        interrupt_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {
                        "type": "text",
                        "text": "Tool uses were interrupted, " "waiting for the next user prompt",
                    },
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", interrupt_msg
            # No "complete" — simulates kiro-cli leaving the prompt hanging.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        caplog.set_level(logging.WARNING, logger="kiro_crew.acp.client")

        t0 = time.monotonic()
        with patch("kiro_crew.sel.sel") as mock_sel:
            if entry_point == "stream_events":
                async for _ in client.stream_events("test"):
                    pass
            elif entry_point == "send_message_stream":
                async for _ in client.send_message_stream("test"):
                    pass
            elif entry_point == "send_message":
                await client.send_message("test")
        elapsed = time.monotonic() - t0

        # (1) Timing — no hang.  Generous bound; in practice this is <50ms.
        assert elapsed < 2.0, f"entry_point={entry_point} took {elapsed:.3f}s"

        # (2) Exactly one WARNING, tagged with the originating call site.
        warns = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "kiro-cli cancelled tool use" in r.getMessage()
        ]
        assert len(warns) == 1, (
            f"entry_point={entry_point} expected 1 WARNING, got {len(warns)}: "
            f"{[r.getMessage() for r in warns]}"
        )
        assert f"site={expected_site}" in warns[0].getMessage()

        # (3) SEL audit — one call, metadata.site matches the call site.
        mock_sel.return_value.log_tool_invocation.assert_called_once()
        kwargs = mock_sel.return_value.log_tool_invocation.call_args.kwargs
        assert kwargs["metadata"]["site"] == expected_site


class TestDrainStderrRedaction:
    """Test 3.1: _drain_stderr logs redacted text but stores raw."""

    @pytest.mark.asyncio
    async def test_raw_stored_redacted_logged(self):
        client = AcpClient()
        raw = "Error: key=AKIAIOSFODNN7EXAMPLE connect failed"

        reader = AsyncMock(spec=["readline"])
        reader.readline = AsyncMock(side_effect=[raw.encode() + b"\n", b""])

        with patch("kiro_crew.acp.client.logger") as mock_logger:
            await client._drain_stderr(reader)

        # Raw stored
        assert list(client._stderr_lines) == [raw]
        # Logged call used redacted text (credential replaced)
        logged_text = mock_logger.warning.call_args[0][2]
        assert "AKIAIOSFODNN7EXAMPLE" not in logged_text


class TestDrainStderrSuppression:
    """_drain_stderr drops high-frequency content-free adapter
    diagnostics (thinking_tokens "Unexpected case" lines) without warning or
    polluting the diagnostic ring buffer, while still proving liveness."""

    def _reader(self, lines):
        """A mock StreamReader that yields *lines* (str) then EOF."""
        reader = AsyncMock(spec=["readline"])
        reader.readline = AsyncMock(side_effect=[line.encode() + b"\n" for line in lines] + [b""])
        return reader

    @pytest.mark.asyncio
    async def test_marker_lines_suppressed_not_logged_or_buffered(self):
        client = AcpClient()
        marker = (
            'Unexpected case: {"type":"system","subtype":"thinking_tokens",'
            '"estimated_tokens":1234,"session_id":"abc-123"}'
        )
        reader = self._reader([marker] * 5)

        with patch("kiro_crew.acp.client.logger") as mock_logger:
            await client._drain_stderr(reader)

        # No per-line WARNING for suppressed markers.
        mock_logger.warning.assert_not_called()
        # Not appended to the bounded diagnostic ring buffer.
        assert list(client._stderr_lines) == []

    @pytest.mark.asyncio
    async def test_suppressed_lines_still_advance_liveness(self):
        client = AcpClient()
        client._last_activity = 0.0  # force a detectable advance
        reader = self._reader(["estimated thinking_tokens delta"])

        with patch("kiro_crew.acp.client.logger"):
            await client._drain_stderr(reader)

        # Liveness must advance for suppressed lines so the idle watchdog
        # (is_responsive) does not kill an actively-thinking turn.
        assert client._last_activity > 0.0

    @pytest.mark.asyncio
    async def test_real_errors_pass_through(self):
        client = AcpClient()
        raw = "Error: adapter crashed in handler"
        reader = self._reader([raw])

        with patch("kiro_crew.acp.client.logger") as mock_logger:
            await client._drain_stderr(reader)

        assert list(client._stderr_lines) == [raw]
        mock_logger.warning.assert_called_once()
        assert mock_logger.warning.call_args[0][2] == raw

    @pytest.mark.asyncio
    async def test_mixed_stream_keeps_real_error_after_burst(self):
        client = AcpClient()
        real_before = "Error: first real failure"
        real_after = "Error: second real failure"
        burst = ["system subtype thinking_tokens delta"] * 50
        reader = self._reader([real_before] + burst + [real_after])

        with patch("kiro_crew.acp.client.logger") as mock_logger:
            await client._drain_stderr(reader)

        # Only the two real lines are logged and buffered; the 50-line burst
        # neither logs nor evicts the earlier real error from the ring buffer.
        assert list(client._stderr_lines) == [real_before, real_after]
        assert mock_logger.warning.call_count == 2


class TestReadMessageStderrRedaction:
    """Test 3.2: _read_message redacts stderr in AcpError."""

    @pytest.mark.asyncio
    async def test_acperror_contains_redacted_stderr(self):
        client = AcpClient()
        client._cancelled = False
        client._buffer = MagicMock()
        client._buffer.__bool__ = lambda s: False
        client._process = MagicMock()
        client._process.returncode = 1
        client._process.stdout = MagicMock()
        client._process.stdout.readline = AsyncMock(return_value=b"")
        client._stderr_lines = deque(["secret key=AKIAIOSFODNN7EXAMPLE"])
        client._stderr_task = MagicMock()
        client._stderr_task.done.return_value = True

        with pytest.raises(AcpError, match="ACP process exited") as exc_info:
            await client._read_message(timeout=1.0)

        assert "AKIAIOSFODNN7EXAMPLE" not in str(exc_info.value)


class TestEnsureReadyRetryOnAcpError:
    """Test 2: ensure_ready retries once on AcpError."""

    @pytest.mark.asyncio
    async def test_retries_on_acp_error(self):
        client = AcpClient()
        client._process = None
        client._session_id = None

        call_count = 0

        async def fake_spawn():
            nonlocal call_count
            call_count += 1
            client._process = MagicMock()
            client._process.returncode = None
            client._process.pid = 100 + call_count
            client._process.stderr = None

        async def fake_init():
            if call_count == 1:
                raise AcpError("MCP server crashed")
            client._session_id = "sess-ok"

        def fake_reset():
            client._process = None
            client._session_id = None
            client._pid = None

        client._spawn = fake_spawn
        client._initialize_session = fake_init
        client._kill_process = AsyncMock()
        client._reset_state = fake_reset
        client._snapshot_process_tree = AsyncMock()

        await client.ensure_ready()

        assert call_count == 2
        assert client._session_id == "sess-ok"
        client._kill_process.assert_called_once_with(force=True)


class TestEnsureReadyRecreatesWorkDir:
    @pytest.mark.asyncio
    async def test_recreates_missing_work_dir(self, tmp_path):
        work_dir = tmp_path / "ws"
        client = AcpClient(work_dir=work_dir)
        client._process = MagicMock()
        client._process.returncode = None
        client._session_id = "sess-1"
        client._spawn = AsyncMock()

        assert not work_dir.exists()
        await client.ensure_ready()
        assert work_dir.is_dir()
        client._spawn.assert_not_called()


class TestMakeUnifiedDiff:
    """Tests for _make_unified_diff helper."""

    def test_both_empty_returns_empty(self):
        assert _make_unified_diff("", "", "file.py") == ""

    def test_addition(self):
        result = _make_unified_diff("", "new line\n", "file.py")
        assert "+new line" in result
        assert "--- file.py" in result

    def test_deletion(self):
        result = _make_unified_diff("old line\n", "", "file.py")
        assert "-old line" in result

    def test_modification(self):
        result = _make_unified_diff("old\n", "new\n", "file.py")
        assert "-old" in result
        assert "+new" in result
        assert "@@" in result

    def test_identical_returns_empty(self):
        assert _make_unified_diff("same\n", "same\n", "file.py") == ""

    def test_truncation(self):
        result = _make_unified_diff("", "x\n" * 5000, "file.py", max_len=100)
        assert len(result) <= 100

    def test_no_trailing_newline(self):
        result = _make_unified_diff("old", "new", "file.py")
        assert "-old" in result
        assert "+new" in result


# ── Phase 1: stop_reason and turn_done tests ──


class TestStopReasonPopulated:
    """Tests for stop_reason extraction from prompt response."""

    @pytest.mark.asyncio
    async def test_stop_reason_populated_on_complete(self):
        """Prompt response with stopReason='cancelled' populates event.stop_reason."""
        from kiro_crew.acp.types import EVENT_COMPLETE, JsonRpcMessage

        client = AcpClient()
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "cancelled"})

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 1
        assert events[0].kind == EVENT_COMPLETE
        assert events[0].stop_reason == "cancelled"

    @pytest.mark.asyncio
    async def test_stop_reason_populated_end_turn(self):
        """Prompt response with stopReason='end_turn' populates event.stop_reason."""
        from kiro_crew.acp.types import EVENT_COMPLETE, JsonRpcMessage

        client = AcpClient()
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 1
        assert events[0].kind == EVENT_COMPLETE
        assert events[0].stop_reason == "end_turn"

    @pytest.mark.asyncio
    async def test_stop_reason_empty_when_absent(self):
        """Prompt response without stopReason key yields empty stop_reason."""
        from kiro_crew.acp.types import EVENT_COMPLETE, JsonRpcMessage

        client = AcpClient()
        complete_msg = JsonRpcMessage(id=1, result={"status": "ok"})

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        events = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert len(events) == 1
        assert events[0].kind == EVENT_COMPLETE
        assert events[0].stop_reason == ""


class TestWaitTurnDone:
    """Tests for wait_turn_done and has_active_turn."""

    @pytest.mark.asyncio
    async def test_wait_turn_done_returns_reason(self):
        """wait_turn_done returns the stop_reason after turn completes."""
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "cancelled"})

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        # Consume stream_events to trigger turn_done
        async for _ in client.stream_events("test"):
            pass

        reason = await client.wait_turn_done(timeout=1.0)
        assert reason == "cancelled"

    @pytest.mark.asyncio
    async def test_wait_turn_done_times_out(self):
        """wait_turn_done raises TimeoutError when no complete fires."""
        client = AcpClient()
        client._turn_done.clear()

        with pytest.raises(asyncio.TimeoutError):
            await client.wait_turn_done(timeout=0.05)


class TestHasActiveTurn:
    """Tests for has_active_turn() across its three conditions."""

    def test_has_active_turn_states(self):
        client = AcpClient()
        # Set up happy state: not cancelled, turn not done, process alive
        client._cancelled = False
        client._turn_done.clear()
        client._process = MagicMock()
        client._process.returncode = None
        assert client.has_active_turn() is True

        # Condition 1: process dies
        client._process.returncode = 1
        assert client.has_active_turn() is False
        client._process.returncode = None  # reset

        # Condition 2: cancelled flag set
        client._cancelled = True
        assert client.has_active_turn() is False
        client._cancelled = False  # reset

        # Condition 3: turn_done is set
        client._turn_done.set()
        assert client.has_active_turn() is False
        client._turn_done.clear()  # reset

        # Confirm happy state restored
        assert client.has_active_turn() is True


class TestCancelledGraceWindow:
    """Tests for the _cancelled grace window in _read_message."""

    @pytest.mark.asyncio
    async def test_cancelled_flag_does_not_short_circuit_reads(self):
        """_read_message reads a queued message within the grace window."""
        client = AcpClient()
        msg_data = {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=json.dumps(msg_data).encode() + b"\n")
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        # Set cancelled with recent timestamp (within grace window)
        client._cancelled = True
        client._cancel_ts = time.monotonic()

        msg = await client._read_message(timeout=1.0)
        assert msg is not None
        assert msg.is_response_for(1)
        assert client._cancelled is True

    @pytest.mark.asyncio
    async def test_cancelled_flag_enforces_grace_window(self):
        """_read_message raises AcpError when grace window is exceeded."""
        client = AcpClient()

        mock_process = MagicMock()
        mock_stdout = AsyncMock()
        mock_stdout.readline = AsyncMock(return_value=b"")
        mock_process.stdout = mock_stdout
        mock_process.returncode = None
        client._process = mock_process

        # Set cancelled with timestamp > 10s ago
        client._cancelled = True
        client._cancel_ts = time.monotonic() - 11.0

        with pytest.raises(AcpError, match="grace window exceeded"):
            await client._read_message(timeout=1.0)


class TestSendPipeErrors:
    """Verify that broken pipe errors on stdin are raised as AcpProcessDied."""

    def _make_client_with_mock_process(self):
        client = AcpClient()
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.returncode = None
        client._process = proc
        client._next_req_id = MagicMock(return_value=1)
        return client, proc

    @pytest.mark.asyncio
    async def test_send_request_connection_reset(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.drain.side_effect = ConnectionResetError("Connection lost")

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_request("test/method", {})

    @pytest.mark.asyncio
    async def test_send_request_broken_pipe(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.drain.side_effect = BrokenPipeError("Broken pipe")

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_request("test/method", {})

    @pytest.mark.asyncio
    async def test_send_response_connection_reset(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.drain.side_effect = ConnectionResetError("Connection lost")

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_response(1, {"result": "ok"})

    @pytest.mark.asyncio
    async def test_send_response_broken_pipe(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.drain.side_effect = BrokenPipeError("Broken pipe")

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_response(1, {"result": "ok"})

    @pytest.mark.asyncio
    async def test_send_request_success_unaffected(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.drain.return_value = None

        req_id = await client._send_request("test/method", {"key": "val"})
        assert req_id == 1
        proc.stdin.write.assert_called_once()
        proc.stdin.drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_request_write_broken_pipe(self):
        client, proc = self._make_client_with_mock_process()
        proc.stdin.write.side_effect = BrokenPipeError("Broken pipe")

        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client._send_request("test/method", {})

    # ── Staleness timeout tests ──────────────────────────────────────────
    @pytest.mark.asyncio
    async def test_stale_turn_synthesizes_complete_after_text(self):
        """When text is streamed but no complete arrives, synthesize EVENT_COMPLETE."""
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            STOP_REASON_END_TURN,
            UPDATE_AGENT_MESSAGE_CHUNK,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        text_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "Hello world"},
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text_msg
            # No "complete" — simulates kiro-cli going silent after text.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        assert [e.kind for e in events] == [EVENT_TEXT_CHUNK, EVENT_COMPLETE]
        assert events[1].stop_reason == STOP_REASON_END_TURN

    @pytest.mark.asyncio
    async def test_stale_eligible_cleared_by_tool_call(self):
        """Tool call after text clears _stale_eligible — no synthetic complete."""
        from kiro_crew.acp.client import AcpTimeoutError
        from kiro_crew.acp.types import (
            UPDATE_AGENT_MESSAGE_CHUNK,
            UPDATE_TOOL_CALL,
            JsonRpcMessage,
        )

        client = AcpClient()

        text_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "Let me check..."},
                }
            },
        )
        tool_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_TOOL_CALL,
                    "toolUseId": "tool_1",
                    "name": "Read",
                    "input": "{}",
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text_msg
            yield "update", tool_msg
            # No "complete" — tool is running, loop ends (simulates timeout).

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        # Should raise AcpTimeoutError because _stale_eligible was cleared by tool_call
        with pytest.raises(AcpTimeoutError):
            async for _ in client.stream_events("test"):
                pass

    @pytest.mark.asyncio
    async def test_stale_eligible_cleared_by_permission(self):
        """Permission request after text clears _stale_eligible."""
        from kiro_crew.acp.client import AcpTimeoutError
        from kiro_crew.acp.types import (
            UPDATE_AGENT_MESSAGE_CHUNK,
            JsonRpcMessage,
        )

        client = AcpClient()

        text_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "I need to run..."},
                }
            },
        )
        perm_msg = JsonRpcMessage(
            id=99,
            method="session/requestPermission",
            params={
                "toolName": "shell",
                "toolInput": "rm -rf /tmp/test",
                "options": [{"id": "allow_once", "label": "Allow"}],
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text_msg
            yield "permission", perm_msg
            # No "complete" — waiting for user approval, loop ends.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        # Should raise AcpTimeoutError because _stale_eligible was cleared by permission
        with pytest.raises(AcpTimeoutError):
            async for _ in client.stream_events("test"):
                pass

    @pytest.mark.asyncio
    async def test_stale_eligible_re_enabled_after_tool_then_text(self):
        """Text after tool re-enables _stale_eligible — synthetic complete fires."""
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            EVENT_TOOL_CALL,
            STOP_REASON_END_TURN,
            UPDATE_AGENT_MESSAGE_CHUNK,
            UPDATE_TOOL_CALL,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        text1 = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "Checking..."},
                }
            },
        )
        tool_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_TOOL_CALL,
                    "toolUseId": "tool_1",
                    "name": "Read",
                    "input": "{}",
                }
            },
        )
        text2 = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "Done."},
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text1
            yield "update", tool_msg
            yield "update", text2
            # No "complete" — text after tool, stale eligible again.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert EVENT_TEXT_CHUNK in kinds
        assert EVENT_TOOL_CALL in kinds
        assert kinds[-1] == EVENT_COMPLETE
        assert events[-1].stop_reason == STOP_REASON_END_TURN

    @pytest.mark.asyncio
    async def test_passive_update_does_not_clear_stale_eligible(self):
        """Passive updates (usage_update, available_commands) after text must NOT
        reset _stale_eligible — stale detection should still fire.

        Regression: kiro-cli sends a non-text update after the final text chunk
        but never sends complete. The blanket _stale_eligible=False on every event
        disabled the 90s timeout, causing the session to hang until the 2h deadline.
        """
        from kiro_crew.acp.types import (
            EVENT_COMPLETE,
            EVENT_TEXT_CHUNK,
            STOP_REASON_END_TURN,
            UPDATE_AGENT_MESSAGE_CHUNK,
            UPDATE_USAGE,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()

        text_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_AGENT_MESSAGE_CHUNK,
                    "content": {"type": "text", "text": "BUILD SUCCEEDED"},
                }
            },
        )
        # Passive update after final text — must NOT reset stale
        usage_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": UPDATE_USAGE,
                    "used": 50000,
                    "size": 200000,
                }
            },
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text_msg
            yield "update", usage_msg
            # No "complete" — simulates kiro-cli going silent.

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        events: list[AcpEvent] = []
        async for ev in client.stream_events("test"):
            events.append(ev)

        # Stale detection should still fire despite the passive update
        kinds = [e.kind for e in events]
        assert kinds == [
            EVENT_TEXT_CHUNK,
            EVENT_COMPLETE,
        ], f"Expected stale detection to synthesize complete after passive update, got {kinds}"
        assert events[-1].stop_reason == STOP_REASON_END_TURN


# ── Coverage push: process lifecycle ──


@_POSIX_ONLY
class TestKillProcess:
    """Tests for _kill_process covering SIGTERM, SIGKILL, and edge cases."""

    @pytest.mark.asyncio
    async def test_noop_when_no_process(self):
        client = AcpClient()
        client._process = None
        await client._kill_process()  # should not raise

    @pytest.mark.asyncio
    async def test_noop_when_already_exited(self):
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = 0
        client._process = proc
        await client._kill_process()  # should not raise

    @pytest.mark.asyncio
    async def test_sigterm_success(self):
        """Normal path: SIGTERM → process exits within timeout."""
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        client._process = proc
        client._pid = 12345
        client._child_pids = {}

        with (
            patch("os.killpg") as mock_killpg,
            patch("os.getpgid", return_value=12345),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children") as mock_esc,
        ):
            await client._kill_process()
            mock_killpg.assert_called_once_with(12345, signal.SIGTERM)
            mock_esc.assert_called_once()

    @pytest.mark.asyncio
    async def test_sigterm_timeout_then_sigkill(self):
        """SIGTERM times out → falls through to SIGKILL."""
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = None
        # First wait times out, second succeeds
        proc.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), None])
        client._process = proc
        client._pid = 99
        client._child_pids = {}

        killpg_calls = []

        def fake_killpg(pgid, sig):
            killpg_calls.append(sig)

        with (
            patch("os.killpg", side_effect=fake_killpg),
            patch("os.getpgid", return_value=99),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            await client._kill_process()

        assert signal.SIGTERM in killpg_calls
        assert signal.SIGKILL in killpg_calls

    @pytest.mark.asyncio
    async def test_force_skips_sigterm(self):
        """force=True goes straight to SIGKILL."""
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        proc.kill = MagicMock()
        client._process = proc
        client._pid = 55
        client._child_pids = {}

        killpg_sigs = []

        def fake_killpg(pgid, sig):
            killpg_sigs.append(sig)

        with (
            patch("os.killpg", side_effect=fake_killpg),
            patch("os.getpgid", return_value=55),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            await client._kill_process(force=True)

        assert signal.SIGTERM not in killpg_sigs
        assert signal.SIGKILL in killpg_sigs

    @pytest.mark.asyncio
    async def test_killpg_process_lookup_error_falls_to_proc_kill(self):
        """When killpg raises ProcessLookupError, falls back to proc.kill()."""
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdout = MagicMock()
        proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        proc.kill = MagicMock()
        client._process = proc
        client._pid = 77
        client._child_pids = {}

        with (
            patch("os.killpg", side_effect=ProcessLookupError()),
            patch("os.getpgid", return_value=77),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            await client._kill_process(force=True)

        proc.kill.assert_called_once()

    @pytest.mark.asyncio
    async def test_kill_process_awaits_async_variant_not_sync(self):
        """Mesh-2801: _kill_process MUST await platform_compat.kill_process_tree_async
        (the offloading variant) — never fall back to the sync
        kill_process_tree, whose whole reason for existing is the Windows
        event-loop offload. A test that patches only kill_process_tree would
        silently pass if someone regresses the await back to sync, so pin
        both symbols and hard-fail if the sync one is ever called."""
        from kiro_crew import platform_compat

        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.pid = 42
        proc.stdin = proc.stdout = proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        client._process = proc
        client._pid = 42
        client._child_pids = {}

        def _sync_forbidden(*_a, **_kw):
            raise AssertionError("sync variant must NOT be called from _kill_process")

        # SIGTERM path
        with (
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                new_callable=AsyncMock,
            ) as mock_async,
            patch(
                "kiro_crew.platform_compat.kill_process_tree",
                side_effect=_sync_forbidden,
            ),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            await client._kill_process(force=False)

        assert mock_async.await_count == 1
        assert mock_async.await_args.args == (42, platform_compat.SIGTERM)

        # Reset process state for the SIGKILL path (force=True)
        proc.returncode = None
        proc.wait = AsyncMock(return_value=0)
        client._process = proc
        client._pid = 42
        client._child_pids = {}

        with (
            patch(
                "kiro_crew.platform_compat.kill_process_tree_async",
                new_callable=AsyncMock,
            ) as mock_async_kill,
            patch(
                "kiro_crew.platform_compat.kill_process_tree",
                side_effect=_sync_forbidden,
            ),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            await client._kill_process(force=True)

        assert mock_async_kill.await_count == 1
        assert mock_async_kill.await_args.args == (42, platform_compat.SIGKILL)


class TestResetStateExtended:
    """Extended _reset_state tests covering sandbox cleanup and PID untracking."""

    def test_sandbox_cleanup_removes_file(self, tmp_path):
        client = AcpClient()
        sb_file = tmp_path / "sandbox.sb"
        sb_file.write_text("sandbox profile")
        client._sandbox_cleanup = str(sb_file)
        client._process = None
        client._child_pids = {}
        client._pid = None

        client._reset_state()

        assert not sb_file.exists()
        assert client._sandbox_cleanup is None

    def test_sandbox_cleanup_missing_file_no_error(self):
        client = AcpClient()
        client._sandbox_cleanup = "/nonexistent/path.sb"
        client._process = None
        client._child_pids = {}
        client._pid = None

        client._reset_state()  # should not raise
        assert client._sandbox_cleanup is None

    def test_untracks_pids(self):
        client = AcpClient()
        client._process = None
        client._pid = 1234
        client._child_pids = {5678: None, 9012: None}

        with (
            patch("kiro_crew.session_pid._pid_gone_or_unmanaged", return_value=True),
            patch("kiro_crew.session._untrack_child_pids") as mock_uc,
            patch("kiro_crew.session._untrack_pid") as mock_up,
            patch("kiro_crew.session._untrack_session_pid") as mock_usp,
        ):
            client._reset_state()

        mock_uc.assert_called_once_with({5678: None, 9012: None})
        mock_up.assert_called_once_with(1234)
        mock_usp.assert_called_once_with(1234)
        assert client._child_pids == {}
        assert client._pid is None

    def test_retains_live_root_pid_survivor(self):
        """A root PID still alive after teardown MUST keep its tracking entry so
        the orphan sweep can reap it — untracking a live survivor is the
        permanent-orphan leak this guards against. In-memory state still clears.
        """
        client = AcpClient()
        client._process = None
        client._pid = 4242
        client._child_pids = {}

        with (
            patch("kiro_crew.session_pid._pid_gone_or_unmanaged", return_value=False),
            patch("kiro_crew.session._untrack_pid") as mock_up,
            patch("kiro_crew.session._untrack_session_pid") as mock_usp,
        ):
            client._reset_state()

        mock_up.assert_not_called()
        mock_usp.assert_not_called()
        assert client._pid is None  # in-memory state is still cleared

    def test_untracks_dead_children_retains_live(self):
        """Dead children are untracked; a live child is retained (partial-reap:
        killpg missed a child in another process group)."""
        client = AcpClient()
        client._process = None
        client._pid = None
        client._child_pids = {5678: None, 9012: None}

        def _gone(pid):
            return pid == 5678  # 5678 dead -> untrack; 9012 alive -> retain

        with (
            patch("kiro_crew.session_pid._pid_gone_or_unmanaged", side_effect=_gone),
            patch("kiro_crew.session._untrack_child_pids") as mock_uc,
        ):
            client._reset_state()

        mock_uc.assert_called_once_with({5678: None})

    def test_cancels_stderr_task(self):
        client = AcpClient()
        client._process = None
        client._pid = None
        client._child_pids = {}
        mock_task = MagicMock()
        mock_task.done.return_value = False
        client._stderr_task = mock_task

        client._reset_state()

        mock_task.cancel.assert_called_once()
        assert client._stderr_task is None


# ── Coverage push: session lifecycle ──


class TestInitializeSession:
    """Tests for _initialize_session covering new session, resume, and model set."""

    @pytest.fixture(autouse=True)
    def _isolate_home(self, tmp_path, monkeypatch):
        """Redirect Path.home to tmp_path so session-file writes are isolated.

        Without this, tests pollute the real ~/.kiro/sessions/cli/ on the
        build host and collide with parallel runs (hardcoded session names).
        """
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    def _make_client(self, tmp_path, **kwargs):
        client = AcpClient(work_dir=tmp_path, **kwargs)
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        client._process = proc
        client._next_req_id = MagicMock(side_effect=range(1, 100))
        return client

    @pytest.mark.asyncio
    async def test_new_session_basic(self, tmp_path):
        """Happy path: initialize → session/new → set_mode → drain."""
        client = self._make_client(tmp_path)
        responses = {
            1: {"protocolVersion": "2025-08-22", "agentCapabilities": {}},
            2: {"sessionId": "sess-abc"},
        }

        async def fake_wait(req_id, timeout=50.0):
            return responses.get(req_id, {})

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        await client._initialize_session()

        assert client._session_id == "sess-abc"
        assert client._resumed is False
        client._drain_notifications.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_session_resume_success(self, tmp_path):
        """session/load succeeds when file exists and kiro-cli supports it."""
        client = self._make_client(tmp_path)
        client._resume_session_id = "old-sess"

        # Create the session file
        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "old-sess.json"
        session_file.write_text("{}")

        responses = {
            1: {"protocolVersion": "2025-08-22", "agentCapabilities": {"loadSession": True}},
            2: {"modes": ["chat"]},  # load success
        }

        async def fake_wait(req_id, timeout=50.0):
            return responses.get(req_id, {})

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        try:
            await client._initialize_session()
            assert client._session_id == "old-sess"
            assert client._resumed is True
        finally:
            session_file.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_session_resume_fallback_to_new(self, tmp_path):
        """session/load fails → falls back to session/new."""
        from kiro_crew.acp.client import AcpError

        client = self._make_client(tmp_path)
        client._resume_session_id = "bad-sess"

        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        session_file = session_dir / "bad-sess.json"
        session_file.write_text("{}")

        call_idx = [0]

        async def fake_wait(req_id, timeout=50.0):
            call_idx[0] += 1
            if call_idx[0] == 1:
                return {"protocolVersion": "2025-08-22", "agentCapabilities": {"loadSession": True}}
            if call_idx[0] == 2:
                raise AcpError("session not found")
            if call_idx[0] == 3:
                return {"sessionId": "new-sess"}
            return {}

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        try:
            await client._initialize_session()
            assert client._session_id == "new-sess"
            assert client._resumed is False
        finally:
            session_file.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_cc_resume_skips_load_when_transcript_missing(self, tmp_path):
        """claude backend: a stale persisted sid with NO transcript on disk
        must fall back to session/new (a fresh start), not replay via
        session/load. Guards against the ~38%-on-'hi' base-context bloat."""
        from kiro_crew.acp.types import ACP_BACKEND_CLAUDE

        client = self._make_client(tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
        client._resume_session_id = "ghost-sess"  # no transcript exists for it

        call_idx = [0]

        async def fake_wait(req_id, timeout=50.0):
            call_idx[0] += 1
            if call_idx[0] == 1:
                return {"protocolVersion": "2025-08-22", "agentCapabilities": {"loadSession": True}}
            # session/load must NOT be called; the next request is session/new.
            return {"sessionId": "fresh-sess"}

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        await client._initialize_session()
        assert client._session_id == "fresh-sess"
        assert client._resumed is False

    @pytest.mark.asyncio
    async def test_set_model_when_non_default(self, tmp_path):
        """Non-default model triggers set_model request."""
        client = self._make_client(tmp_path)
        client._model = "claude-sonnet"

        send_calls = []

        async def fake_wait(req_id, timeout=50.0):
            if req_id == 1:
                return {"protocolVersion": "2025-08-22", "agentCapabilities": {}}
            if req_id == 2:
                return {"sessionId": "s1"}
            return {}

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        # Track all send_request calls
        original_send_request = client._send_request

        async def tracking_send(method, params):
            send_calls.append(method)
            return await original_send_request(method, params)

        client._send_request = tracking_send

        await client._initialize_session()

        assert "session/set_model" in send_calls

    @pytest.mark.asyncio
    async def test_no_set_model_when_auto(self, tmp_path):
        """Default model 'auto' skips set_model request."""
        client = self._make_client(tmp_path)
        client._model = "auto"

        send_calls = []

        async def fake_wait(req_id, timeout=50.0):
            if req_id == 1:
                return {"protocolVersion": "2025-08-22", "agentCapabilities": {}}
            if req_id == 2:
                return {"sessionId": "s1"}
            return {}

        client._wait_for_response = AsyncMock(side_effect=fake_wait)
        client._drain_notifications = AsyncMock()

        original_send_request = client._send_request

        async def tracking_send(method, params):
            send_calls.append(method)
            return await original_send_request(method, params)

        client._send_request = tracking_send

        await client._initialize_session()

        assert "session/set_model" not in send_calls


# ── Coverage push: JSON-RPC plumbing ──


class TestWaitForResponse:
    """Tests for _wait_for_response covering matching, buffering, and errors."""

    @pytest.mark.asyncio
    async def test_matching_response_returned(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(id=5, result={"data": "ok"})
        client._read_message = AsyncMock(return_value=msg)

        result = await client._wait_for_response(5, timeout=5.0)
        assert result == {"data": "ok"}

    @pytest.mark.asyncio
    async def test_error_response_raises(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(id=5, error={"code": -1, "message": "fail"})
        client._read_message = AsyncMock(return_value=msg)

        with pytest.raises(AcpError, match="JSON-RPC error"):
            await client._wait_for_response(5, timeout=5.0)

    @pytest.mark.asyncio
    async def test_notification_buffered(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        notif = JsonRpcMessage(method="mcp/serverReady", params={"name": "builder"})
        response = JsonRpcMessage(id=3, result={"ok": True})
        client._read_message = AsyncMock(side_effect=[notif, response])

        result = await client._wait_for_response(3, timeout=5.0)
        assert result == {"ok": True}
        assert len(client._mcp_notifications) == 1

    @pytest.mark.asyncio
    async def test_non_matching_response_buffered(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        other = JsonRpcMessage(id=99, result={"other": True})
        target = JsonRpcMessage(id=3, result={"target": True})
        client._read_message = AsyncMock(side_effect=[other, target])

        result = await client._wait_for_response(3, timeout=5.0)
        assert result == {"target": True}
        assert len(client._buffer) == 1

    @pytest.mark.asyncio
    async def test_timeout_raises(self, tmp_path):
        from kiro_crew.acp.client import AcpTimeoutError

        client = AcpClient(work_dir=tmp_path)
        client._read_message = AsyncMock(return_value=None)

        with pytest.raises(AcpTimeoutError):
            await client._wait_for_response(1, timeout=0.1)

    @pytest.mark.asyncio
    async def test_shutdown_event_raises(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._read_message = AsyncMock(return_value=None)

        with patch("kiro_crew.shutdown_event") as mock_ev:
            mock_ev.is_set.return_value = True
            with pytest.raises(AcpError, match="Shutdown"):
                await client._wait_for_response(1, timeout=5.0)

    @pytest.mark.asyncio
    async def test_server_request_warned_and_dropped(self, tmp_path):
        """Unexpected server request (method + id) is warned and dropped."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        server_req = JsonRpcMessage(id=50, method="unexpected/request", params={})
        response = JsonRpcMessage(id=3, result={"ok": True})
        client._read_message = AsyncMock(side_effect=[server_req, response])

        result = await client._wait_for_response(3, timeout=5.0)
        assert result == {"ok": True}


class TestDrainNotifications:
    """Tests for _drain_notifications covering buffered and live messages."""

    @pytest.mark.asyncio
    async def test_drains_buffered_notifications(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        client._mcp_notifications = [
            JsonRpcMessage(method="mcp/serverReady", params={"name": "builder-mcp"}),
            JsonRpcMessage(method="mcp/serverReady", params={"name": "slack-mcp"}),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        assert len(client._mcp_notifications) == 0

    @pytest.mark.asyncio
    async def test_drains_live_messages(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        live_msg = JsonRpcMessage(method="mcp/serverReady", params={"name": "core"})
        client._mcp_notifications = []
        call_count = [0]

        async def fake_read(timeout=2.0):
            call_count[0] += 1
            if call_count[0] == 1:
                return live_msg
            return None

        client._read_message = fake_read

        await client._drain_notifications(duration=0.2)
        # Should have processed the live message without error

    @pytest.mark.asyncio
    async def test_handles_acp_error_during_drain(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = []
        client._read_message = AsyncMock(side_effect=AcpError("process died"))

        await client._drain_notifications(duration=0.1)  # should not raise

    @pytest.mark.asyncio
    async def test_captures_buffered_mcp_oauth_request(self, tmp_path):
        """OAuth notifications buffered during init are captured into pending list."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        url = "https://mcp.linear.app/authorize?response_type=code&client_id=abc"
        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"sessionId": "s1", "serverName": "linear", "oauthUrl": url},
            ),
            JsonRpcMessage(method="mcp/serverReady", params={"name": "builder-mcp"}),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        pending = client.pop_pending_oauth_requests()
        assert len(pending) == 1
        assert pending[0]["serverName"] == "linear"
        assert pending[0]["oauthUrl"] == url
        # Drained — second pop returns empty.
        assert client.pop_pending_oauth_requests() == []

    @pytest.mark.asyncio
    async def test_captures_live_mcp_oauth_request(self, tmp_path):
        """OAuth notifications arriving live during drain are also captured."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        url = "https://auth.smithery.ai/neon/authorize?client_id=xyz"
        oauth_msg = JsonRpcMessage(
            method=METHOD_MCP_OAUTH_REQUEST,
            params={"serverName": "neon", "oauthUrl": url},
        )
        client._mcp_notifications = []
        call_count = [0]

        async def fake_read(timeout=2.0):
            call_count[0] += 1
            if call_count[0] == 1:
                return oauth_msg
            return None

        client._read_message = fake_read

        await client._drain_notifications(duration=0.5)

        pending = client.pop_pending_oauth_requests()
        assert len(pending) == 1
        assert pending[0]["serverName"] == "neon"
        assert pending[0]["oauthUrl"] == url

    @pytest.mark.asyncio
    async def test_oauth_request_without_url_not_captured(self, tmp_path):
        """Malformed oauth notifications (no oauthUrl) are ignored."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"serverName": "broken"},  # no oauthUrl key
            ),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        assert client.pop_pending_oauth_requests() == []

    @pytest.mark.asyncio
    async def test_oauth_request_dedupes_per_server(self, tmp_path):
        """kiro-cli may emit oauth_request multiple times per server probe — dedupe."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        url = "https://mcp.linear.app/authorize?client_id=abc"
        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"serverName": "linear", "oauthUrl": url},
            ),
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"serverName": "linear", "oauthUrl": url},
            ),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        pending = client.pop_pending_oauth_requests()
        assert len(pending) == 1, "duplicate oauth_request for same server should be collapsed"

    @pytest.mark.asyncio
    async def test_unsafe_url_does_not_consume_dedupe_slot(self, tmp_path):
        """Unsafe-scheme URL must be rejected *before* the dedupe key is recorded,
        so a later safe retry for the same server still surfaces."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"serverName": "linear", "oauthUrl": "javascript:alert(1)"},
            ),
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={
                    "serverName": "linear",
                    "oauthUrl": "https://mcp.linear.app/authorize",
                },
            ),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        pending = client.pop_pending_oauth_requests()
        assert len(pending) == 1
        assert pending[0]["oauthUrl"] == "https://mcp.linear.app/authorize"

    @pytest.mark.asyncio
    async def test_oauth_request_with_empty_server_name_dropped(self, tmp_path):
        """server_initialized/server_init_failure discard by server_name only —
        recording a banner with empty server_name would create a permanently-
        stuck dedupe entry.  Drop instead."""
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_MCP_OAUTH_REQUEST, JsonRpcMessage

        client._mcp_notifications = [
            JsonRpcMessage(
                method=METHOD_MCP_OAUTH_REQUEST,
                params={"serverName": "", "oauthUrl": "https://example.com/auth"},
            ),
        ]
        client._read_message = AsyncMock(return_value=None)

        await client._drain_notifications(duration=0.1)

        assert client.pop_pending_oauth_requests() == []
        assert client._oauth_emitted_servers == set()


# ── Coverage push: prompt loop ──


class TestPromptLoop:
    """Tests for _prompt_loop covering normal flow, process death, and staleness."""

    @pytest.mark.asyncio
    async def test_yields_actions(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msgs = [
            JsonRpcMessage(
                method="session/update",
                params={
                    "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}}
                },
            ),
            JsonRpcMessage(id=1, result={"status": "ok"}),
        ]
        idx = [0]

        async def fake_read(timeout=20.0):
            if idx[0] < len(msgs):
                m = msgs[idx[0]]
                idx[0] += 1
                return m
            return None

        client._read_message = fake_read

        actions = []
        async for action, msg in client._prompt_loop(req_id=1, timeout=5.0):
            actions.append(action)
            if action == "complete":
                break

        assert "update" in actions
        assert "complete" in actions

    @pytest.mark.asyncio
    async def test_process_death_raises(self, tmp_path):
        from kiro_crew.acp.client import AcpProcessDied

        client = AcpClient(work_dir=tmp_path)
        proc = MagicMock()
        proc.returncode = 1
        client._process = proc
        client._read_message = AsyncMock(return_value=None)

        with pytest.raises(AcpProcessDied):
            async for _ in client._prompt_loop(req_id=1, timeout=5.0):
                pass

    @pytest.mark.asyncio
    async def test_stale_turn_exits_early(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._stale_eligible = True
        proc = MagicMock()
        proc.returncode = None
        client._process = proc
        client._read_message = AsyncMock(return_value=None)

        # With stale_eligible=True and no data, should exit after _STALE_TURN_TIMEOUT
        # We patch the timeout to be very short
        with patch("kiro_crew.acp.client._STALE_TURN_TIMEOUT", 0.05):
            actions = []
            async for action, msg in client._prompt_loop(req_id=1, timeout=2.0):
                actions.append(action)

        # Should exit cleanly (return, not raise)
        assert actions == []


class TestDispatchEventsExtended:
    """Extended tests for _dispatch_events covering permission, tool events, compaction."""

    @pytest.mark.asyncio
    async def test_permission_event_yielded(self):
        from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_PERMISSION_REQUEST, JsonRpcMessage

        client = AcpClient()
        perm_msg = JsonRpcMessage(
            id=99,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell", "toolCallId": "tc1"},
                "options": [{"id": "allow_once", "label": "Allow"}],
            },
        )
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

        async def fake_prompt_loop(req_id, timeout):
            yield "permission", perm_msg
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop
        client.last_prompt_stats = AcpPromptStats()
        client._tool_call_inputs = {}
        client._stale_eligible = False
        client._turn_done = asyncio.Event()
        client._last_stop_reason = ""

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=5.0):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert EVENT_PERMISSION_REQUEST in kinds
        assert EVENT_COMPLETE in kinds

    @pytest.mark.asyncio
    async def test_tool_event_yielded(self):
        from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TOOL_CALL, JsonRpcMessage

        client = AcpClient()
        tool_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "Read",
                    "kind": "tool_use",
                    "toolCallId": "tc1",
                    "input": {"path": "/tmp/x"},
                }
            },
        )
        complete_msg = JsonRpcMessage(id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "update", tool_msg
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop
        client.last_prompt_stats = AcpPromptStats()
        client._tool_call_inputs = {}
        client._stale_eligible = False
        client._turn_done = asyncio.Event()
        client._last_stop_reason = ""
        client._read_new_tool_results_sync = lambda: []

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=5.0):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert EVENT_TOOL_CALL in kinds
        assert EVENT_COMPLETE in kinds

    @pytest.mark.asyncio
    async def test_extract_agent_from_result(self):
        """extract_agent_from_result=True yields agent_switched from result data."""
        from kiro_crew.acp.types import EVENT_AGENT_SWITCHED, EVENT_COMPLETE, JsonRpcMessage

        client = AcpClient()
        complete_msg = JsonRpcMessage(
            id=1, result={"data": {"agent": {"name": "planner"}}, "message": ""}
        )

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop
        client.last_prompt_stats = AcpPromptStats()
        client._tool_call_inputs = {}
        client._stale_eligible = False
        client._turn_done = asyncio.Event()
        client._last_stop_reason = ""
        client._read_new_tool_results_sync = lambda: []

        events = []
        async for ev in client._dispatch_events(
            req_id=1, timeout=5.0, extract_agent_from_result=True
        ):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert EVENT_AGENT_SWITCHED in kinds
        assert EVENT_COMPLETE in kinds

    @pytest.mark.asyncio
    async def test_error_action_raises(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        error_msg = JsonRpcMessage(id=1, error={"code": -1, "message": "boom"})

        async def fake_prompt_loop(req_id, timeout):
            yield "error", error_msg

        client._prompt_loop = fake_prompt_loop
        client.last_prompt_stats = AcpPromptStats()
        client._tool_call_inputs = {}
        client._stale_eligible = False
        client._turn_done = asyncio.Event()
        client._last_stop_reason = ""

        with pytest.raises(AcpError, match="boom"):
            async for _ in client._dispatch_events(req_id=1, timeout=5.0):
                pass


# ── Coverage push: command wrappers ──


class TestSendCommand:
    """Tests for send_command."""

    @pytest.mark.asyncio
    async def test_send_command_returns_text(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        client._process = MagicMock()
        client._process.returncode = None
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=10)
        client._wait_for_response = AsyncMock(return_value={"text": "usage: 42%"})

        result = await client.send_command("/usage")
        assert "42%" in result

    @pytest.mark.asyncio
    async def test_send_command_timeout_returns_empty(self, tmp_path):
        from kiro_crew.acp.client import AcpTimeoutError

        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        client._process = MagicMock()
        client._process.returncode = None
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=10)
        client._wait_for_response = AsyncMock(side_effect=AcpTimeoutError())

        result = await client.send_command("/compact")
        assert result == ""

    @pytest.mark.asyncio
    async def test_send_command_redacts_urls(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        client._process = MagicMock()
        client._process.returncode = None
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=10)
        client._wait_for_response = AsyncMock(
            return_value={"text": "visit https://evil.com/exfil?data=secret"}
        )

        # send_command MUST run raw text through the redactor before returning.
        # The redactor itself is unit-tested separately in test_security.py;
        # here we verify the call site wires it up at all (a vacuous isinstance
        # check would not catch a missed redact step).
        with patch(
            "kiro_crew.acp.client.redact_exfiltration_urls",
            return_value=("[redacted]", ["url"]),
        ) as mock_redact:
            result = await client.send_command("/test")

        mock_redact.assert_called_once_with("visit https://evil.com/exfil?data=secret")
        assert result == "[redacted]"


class TestCancelSession:
    """Tests for cancel_session."""

    @pytest.mark.asyncio
    async def test_cancel_sends_notification(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "sess-1"
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.returncode = None
        client._process = proc

        await client.cancel_session()

        assert client._cancelled is True
        proc.stdin.write.assert_called_once()
        written = proc.stdin.write.call_args[0][0]
        data = json.loads(written.decode())
        assert data["method"] == "session/cancel"
        assert data["params"]["sessionId"] == "sess-1"

    @pytest.mark.asyncio
    async def test_cancel_no_session_id_skips(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = None
        await client.cancel_session()  # should not raise

    @pytest.mark.asyncio
    async def test_cancel_no_process_skips(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        client._process = None
        await client.cancel_session()
        assert client._cancelled is True

    @pytest.mark.asyncio
    async def test_cancel_write_exception_handled(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock(side_effect=BrokenPipeError())
        proc.stdin.drain = AsyncMock()
        proc.returncode = None
        client._process = proc

        await client.cancel_session()  # should not raise
        assert client._cancelled is True

    @pytest.mark.asyncio
    async def test_cancel_raises_grace_window_to_budget(self, tmp_path):
        """A budget above the 10s floor must extend the read-grace window so
        the read loop does not abort the turn early and force a hard kill."""
        from kiro_crew.acp.client import _CANCEL_GRACE_SECS

        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        proc = MagicMock()
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.returncode = None
        client._process = proc

        await client.cancel_session(grace_secs=30.0)
        assert client._cancel_grace_secs == 30.0

        # A budget below the floor never shrinks the window.
        client._cancel_grace_secs = _CANCEL_GRACE_SECS
        await client.cancel_session(grace_secs=2.0)
        assert client._cancel_grace_secs == _CANCEL_GRACE_SECS


class TestWaitForCompaction:
    """Tests for wait_for_compaction."""

    @pytest.mark.asyncio
    async def test_returns_completed(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "completed"}, "summary": "saved 3k"},
        )
        client._read_message = AsyncMock(side_effect=[None, msg])

        result = await client.wait_for_compaction(timeout=5.0)
        assert result == {"type": "completed", "summary": "saved 3k"}

    @pytest.mark.asyncio
    async def test_returns_failed(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "failed"}, "summary": "error"},
        )
        client._read_message = AsyncMock(return_value=msg)

        result = await client.wait_for_compaction(timeout=5.0)
        assert result == {"type": "failed", "summary": "error"}

    @pytest.mark.asyncio
    async def test_timeout_returns_timeout_dict(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._read_message = AsyncMock(return_value=None)

        result = await client.wait_for_compaction(timeout=0.1)
        assert result == {"type": "timeout"}

    @pytest.mark.asyncio
    async def test_buffers_non_compaction_notifications(self, tmp_path, monkeypatch):
        import kiro_crew.acp.client as c

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200_000)
        client = AcpClient(work_dir=tmp_path)
        client._model = "some-model"
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, METHOD_METADATA, JsonRpcMessage

        meta_msg = JsonRpcMessage(method=METHOD_METADATA, params={"contextUsagePercentage": 55.0})
        other_notif = JsonRpcMessage(method="mcp/something", params={"x": 1})
        compact_msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "completed"}, "summary": "ok"},
        )
        client._read_message = AsyncMock(side_effect=[meta_msg, other_notif, compact_msg])

        result = await client.wait_for_compaction(timeout=5.0)
        assert result["type"] == "completed"
        # The metadata WAS consumed (window backfill proves _track_metadata
        # ran), but its pct described the PRE-compaction transcript — the
        # completed status drops it (reset_after_compaction) so the meter
        # doesn't freeze at a stale value.
        assert client.last_prompt_stats.context_window_tokens == 200_000
        assert client.last_prompt_stats.context_pct == 0.0
        assert len(client._mcp_notifications) == 1


# ── Coverage push: tool tracking ──


class TestExtractToolEvent:
    """Tests for _extract_tool_event covering various tool_call shapes."""

    @pytest.mark.parametrize("bad_update", [None, "call", [1], 7])
    def test_non_dict_update_returns_none(self, bad_update):
        """Non-dict update raised AttributeError on update.get() in the
        prompt-turn dispatch path — must be a clean None instead."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(method="session/update", params={"update": bad_update})
        assert client._extract_tool_event(msg) is None  # must not raise

    def test_basic_tool_call(self):
        client = AcpClient()
        from kiro_crew.acp.types import EVENT_TOOL_CALL, JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "Read",
                    "kind": "tool_use",
                    "toolCallId": "tc-1",
                    "input": {"path": "/tmp/file.txt"},
                }
            },
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert event.kind == EVENT_TOOL_CALL
        assert event.title == "Read"

    def test_tool_call_with_diff_content(self):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "write",
                    "kind": "tool_use",
                    "toolCallId": "tc-2",
                    "input": {},
                    "content": [
                        {"type": "diff", "oldText": "old\n", "newText": "new\n", "path": "f.py"}
                    ],
                }
            },
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert "-old" in event.tool_input or "+new" in event.tool_input

    def test_tool_call_str_replace_fallback(self):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "write",
                    "kind": "tool_use",
                    "toolCallId": "tc-3",
                    "input": {
                        "command": "strReplace",
                        "oldStr": "a",
                        "newStr": "b",
                        "path": "x.py",
                    },
                }
            },
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert "-a" in event.tool_input or "+b" in event.tool_input

    def test_non_tool_call_returns_none(self):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}}},
        )
        event = client._extract_tool_event(msg)
        assert event is None

    def test_tool_purpose_extracted(self):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "shell",
                    "kind": "tool_use",
                    "toolCallId": "tc-4",
                    "input": {"__tool_use_purpose": "run tests", "command": "pytest"},
                }
            },
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert event.tool_purpose == "run tests"

    def test_tool_purpose_extracted_camel_case_key(self):
        """kiro-cli echoes the reserved purpose arg back camelCased on some tool
        calls. Reading only the snake_case spelling dropped the purpose, which
        made the dashboard's concise tool pill fall back to the raw command."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "Running: node kc-shot.mjs",
                    "kind": "execute",
                    "toolCallId": "tc-5",
                    "input": {
                        "__toolUsePurpose": "check harness render errors",
                        "command": "node kc-shot.mjs",
                    },
                }
            },
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert event.tool_purpose == "check harness render errors"


class TestBuildPermissionEvent:
    """Tests for _build_permission_event."""

    def test_basic_permission(self):
        client = AcpClient()
        from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, JsonRpcMessage

        msg = JsonRpcMessage(
            id=42,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell", "toolCallId": "tc-5"},
                "options": [
                    {"id": "allow_once", "label": "Allow once"},
                    {"id": "allow_always", "label": "Allow always"},
                ],
            },
        )
        event = client._build_permission_event(msg)
        assert event.kind == EVENT_PERMISSION_REQUEST
        assert event.request_id == 42
        assert event.title == "shell"
        assert len(event.options) == 2

    def test_tool_kind_carried_from_toolcall(self):
        # The ACP toolCall carries kind="execute" for Bash; carrying it onto the
        # event lets downstream validation apply the execute-tool exemptions
        # (e.g. the display-name length cap). Regression for the empty-kind bug
        # where long bash commands aborted as "User refused permission".
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=43,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "ls", "kind": "execute", "toolCallId": "tc-k"},
                "options": [{"id": "allow_once", "label": "Allow"}],
            },
        )
        event = client._build_permission_event(msg)
        assert event.tool_kind == "execute"

    def test_tool_kind_defaults_empty_when_absent(self):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=44,
            method="session/requestPermission",
            params={"toolCall": {"title": "ls"}, "options": []},
        )
        event = client._build_permission_event(msg)
        assert event.tool_kind == ""

    def test_default_options_when_empty(self):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=10,
            method="session/requestPermission",
            params={"toolCall": {"title": "rm"}, "options": []},
        )
        event = client._build_permission_event(msg)
        assert len(event.options) == 2
        assert event.options[0]["id"] == "allow_once"

    @pytest.mark.parametrize("bad_options", [None, "allow", 42, {"id": "allow_once"}])
    def test_non_list_options_degrade_to_defaults(self, bad_options):
        """The permission payload comes straight from the agent process; a
        non-list options value made the for-loop raise TypeError (or iterate
        dict keys), tearing down the prompt-turn event generator instead of
        degrading to the default allow options."""
        client = AcpClient()
        from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, JsonRpcMessage

        msg = JsonRpcMessage(
            id=12,
            method="session/requestPermission",
            params={"toolCall": {"title": "rm"}, "options": bad_options},
        )
        event = client._build_permission_event(msg)  # must not raise
        assert event.kind == EVENT_PERMISSION_REQUEST
        assert len(event.options) == 2
        assert event.options[0]["id"] == "allow_once"

    @pytest.mark.parametrize("bad_toolcall", [None, "shell", ["x"], 7])
    def test_non_dict_toolcall_degrades_to_unknown(self, bad_toolcall):
        """A non-dict toolCall made tool_call.get(...) raise AttributeError."""
        client = AcpClient()
        from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, JsonRpcMessage

        msg = JsonRpcMessage(
            id=13,
            method="session/requestPermission",
            params={"toolCall": bad_toolcall, "options": []},
        )
        event = client._build_permission_event(msg)  # must not raise
        assert event.kind == EVENT_PERMISSION_REQUEST
        assert event.title == "unknown"

    def test_non_dict_and_non_string_option_entries_skipped(self):
        """Non-dict entries raised AttributeError on o.get(); an int optionId
        crashed opt_id.lower() in the legacy-kind synthesis. Both must be
        skipped while valid entries still parse."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=14,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [
                    "allow",  # non-dict
                    None,  # non-dict
                    {"id": 42, "label": "int id"},  # non-string id
                    {"id": "allow_once", "label": "Allow once"},
                ],
            },
        )
        event = client._build_permission_event(msg)  # must not raise
        assert event.options == [{"id": "allow_once", "label": "Allow once"}]

    def test_cached_tool_input_used(self):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        client._tool_call_inputs["tc-6"] = '{"cmd": "rm -rf /"}'
        msg = JsonRpcMessage(
            id=11,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell", "toolCallId": "tc-6"},
                "options": [{"id": "allow_once", "label": "Allow"}],
            },
        )
        event = client._build_permission_event(msg)
        assert "rm -rf" in event.tool_input
        # Cache consumed
        assert "tc-6" not in client._tool_call_inputs

    def test_fallback_input_from_tool_call(self):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=12,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "write", "toolCallId": "tc-7", "input": {"path": "/x"}},
                "options": [{"id": "allow_once", "label": "Allow"}],
            },
        )
        event = client._build_permission_event(msg)
        assert "/x" in event.tool_input

    def test_acp_spec_shape_records_optionids(self):
        """ACP-spec shape (optionId/name/kind) populates _permission_options."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=20,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "allow_always", "name": "Always", "kind": "allow_always"},
                ],
            },
        )
        client._build_permission_event(msg)
        assert client._permission_options[20] == {"once": "allow", "always": "allow_always"}

    def test_legacy_kiro_shape_records_optionids(self):
        """Legacy kiro shape (id/label, no kind) is classified by literal id."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=21,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [
                    {"id": "allow_once", "label": "Allow once"},
                    {"id": "allow_always", "label": "Allow always"},
                ],
            },
        )
        client._build_permission_event(msg)
        assert client._permission_options[21] == {
            "once": "allow_once",
            "always": "allow_always",
        }

    def test_reject_option_recorded_even_without_allow(self):
        """A reject option must be recorded so reject_tool can send a clean
        ``selected`` reject (behavior:"deny") instead of ``cancelled`` (which
        the claude-agent-acp adapter turns into "Tool use aborted").
        """
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=22,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [
                    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                ],
            },
        )
        client._build_permission_event(msg)
        assert client._permission_options[22].get("reject") == "reject_once"

    def test_unknown_legacy_id_not_classified(self):
        """Unknown legacy ids do not get a synthesized kind."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=23,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [{"id": "weird_custom_id", "label": "?"}],
            },
        )
        client._build_permission_event(msg)
        assert 23 not in client._permission_options


class TestApproveTool:
    """Tests for approve_tool always= and recorded-option dispatch."""

    @pytest.mark.asyncio
    async def test_always_uses_recorded_optionid(self, tmp_path):
        from kiro_crew.acp.types import OUTCOME_SELECTED

        client = AcpClient(work_dir=tmp_path)
        client._permission_options[42] = {"once": "allow", "always": "allow_always"}
        client._send_response = AsyncMock()
        await client.approve_tool(42, always=True)
        client._send_response.assert_awaited_once_with(
            42,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": "allow_always"}},
        )
        assert 42 not in client._permission_options

    @pytest.mark.asyncio
    async def test_once_uses_recorded_optionid(self, tmp_path):
        from kiro_crew.acp.types import OUTCOME_SELECTED

        client = AcpClient(work_dir=tmp_path)
        client._permission_options[43] = {"once": "allow", "always": "allow_always"}
        client._send_response = AsyncMock()
        await client.approve_tool(43)
        client._send_response.assert_awaited_once_with(
            43,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": "allow"}},
        )

    @pytest.mark.asyncio
    async def test_no_recorded_falls_back_to_literal(self, tmp_path):
        from kiro_crew.acp.types import (
            OPTION_ALLOW_ALWAYS,
            OPTION_ALLOW_ONCE,
            OUTCOME_SELECTED,
        )

        client = AcpClient(work_dir=tmp_path)
        client._send_response = AsyncMock()
        await client.approve_tool(44)
        client._send_response.assert_awaited_with(
            44,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_ALLOW_ONCE}},
        )
        await client.approve_tool(45, always=True)
        client._send_response.assert_awaited_with(
            45,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_ALLOW_ALWAYS}},
        )

    @pytest.mark.asyncio
    async def test_explicit_option_id_skips_recorded_pop(self, tmp_path):
        """Explicit option_id bypasses the recorded entry — defensive retries
        with a recorded entry left intact still send the explicit id."""
        from kiro_crew.acp.types import OUTCOME_SELECTED

        client = AcpClient(work_dir=tmp_path)
        client._permission_options[46] = {"once": "allow", "always": "allow_always"}
        client._send_response = AsyncMock()
        await client.approve_tool(46, option_id="custom_id")
        client._send_response.assert_awaited_with(
            46,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": "custom_id"}},
        )
        assert client._permission_options[46] == {"once": "allow", "always": "allow_always"}

    @pytest.mark.asyncio
    async def test_reject_only_recorded_falls_back_to_literal_on_approve(self, tmp_path):
        """A request that advertised only a reject option records {"reject": ...}
        with no "once"/"always" keys. Approving it must fall back to the canonical
        allow id rather than KeyError-ing on the missing key."""
        from kiro_crew.acp.types import (
            OPTION_ALLOW_ALWAYS,
            OPTION_ALLOW_ONCE,
            OUTCOME_SELECTED,
        )

        client = AcpClient(work_dir=tmp_path)
        client._permission_options[47] = {"reject": "reject_once"}
        client._send_response = AsyncMock()
        await client.approve_tool(47)
        client._send_response.assert_awaited_with(
            47,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_ALLOW_ONCE}},
        )
        assert 47 not in client._permission_options

        client._permission_options[48] = {"reject": "reject_once"}
        await client.approve_tool(48, always=True)
        client._send_response.assert_awaited_with(
            48,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_ALLOW_ALWAYS}},
        )


class TestRejectTool:
    """Tests for reject_tool clean-reject vs cancelled dispatch."""

    @pytest.mark.asyncio
    async def test_uses_recorded_reject_optionid(self, tmp_path):
        """When a reject option was advertised, reject_tool sends a clean
        ``selected`` reject so the adapter returns behavior:"deny" rather than
        throwing "Tool use aborted" on a cancelled outcome."""
        from kiro_crew.acp.types import OUTCOME_SELECTED

        client = AcpClient(work_dir=tmp_path)
        # claude-agent-acp advertises optionId "reject" with kind "reject_once"
        client._permission_options[60] = {"once": "allow", "reject": "reject"}
        client._send_response = AsyncMock()
        await client.reject_tool(60)
        client._send_response.assert_awaited_once_with(
            60,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": "reject"}},
        )
        assert 60 not in client._permission_options

    @pytest.mark.asyncio
    async def test_falls_back_to_cancelled_when_no_reject_option(self, tmp_path):
        """When no reject option was advertised (kiro-cli), reject_tool falls
        back to the cancelled outcome — kiro handles it as a clean rejection."""
        from kiro_crew.acp.types import OUTCOME_CANCELLED

        client = AcpClient(work_dir=tmp_path)
        client._permission_options[61] = {"once": "allow_once", "always": "allow_always"}
        client._send_response = AsyncMock()
        await client.reject_tool(61)
        client._send_response.assert_awaited_once_with(
            61,
            {"outcome": {"outcome": OUTCOME_CANCELLED}},
        )
        assert 61 not in client._permission_options

    @pytest.mark.asyncio
    async def test_falls_back_to_cancelled_when_nothing_recorded(self, tmp_path):
        """No recorded options at all → cancelled fallback (safe for kiro)."""
        from kiro_crew.acp.types import OUTCOME_CANCELLED

        client = AcpClient(work_dir=tmp_path)
        client._send_response = AsyncMock()
        await client.reject_tool(62)
        client._send_response.assert_awaited_once_with(
            62,
            {"outcome": {"outcome": OUTCOME_CANCELLED}},
        )


class TestHandlePermission:
    """Tests for _handle_permission (auto-approve)."""

    @pytest.mark.asyncio
    async def test_auto_approves(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=55,
            method="session/requestPermission",
            params={"toolCall": {"title": "bash"}, "options": []},
        )
        client.approve_tool = AsyncMock()

        await client._handle_permission(msg)
        client.approve_tool.assert_awaited_once_with(55)


class TestReadNewToolResultsSync:
    """Tests for _read_new_tool_results_sync."""

    @pytest.fixture(autouse=True)
    def _isolate_home(self, tmp_path, monkeypatch):
        """Redirect Path.home to tmp_path so JSONL writes are isolated."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    def test_no_session_returns_empty(self):
        client = AcpClient()
        client._session_id = None
        assert client._read_new_tool_results_sync() == []

    def test_missing_file_returns_empty(self):
        client = AcpClient()
        client._session_id = "nonexistent-session-xyz"
        assert client._read_new_tool_results_sync() == []

    def test_reads_tool_results(self, tmp_path):
        client = AcpClient()
        client._session_id = "test-sess"
        client._jsonl_pos = 0

        # Create fake JSONL
        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = session_dir / "test-sess.jsonl"

        entry = {
            "kind": "ToolResults",
            "data": {
                "content": [
                    {
                        "kind": "toolResult",
                        "data": {
                            "toolUseId": "tu-1",
                            "content": [{"kind": "text", "data": "output here"}],
                        },
                    }
                ]
            },
        }
        jsonl_path.write_text(json.dumps(entry) + "\n")

        try:
            results = client._read_new_tool_results_sync()
            assert len(results) == 1
            assert results[0].tool_call_id == "tu-1"
            assert "output here" in results[0].tool_output
        finally:
            jsonl_path.unlink(missing_ok=True)

    def test_reads_json_kind_with_stdout(self, tmp_path):
        client = AcpClient()
        client._session_id = "test-sess-2"
        client._jsonl_pos = 0

        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = session_dir / "test-sess-2.jsonl"

        entry = {
            "kind": "ToolResults",
            "data": {
                "content": [
                    {
                        "kind": "toolResult",
                        "data": {
                            "toolUseId": "tu-2",
                            "content": [{"kind": "json", "data": {"stdout": "hello world"}}],
                        },
                    }
                ]
            },
        }
        jsonl_path.write_text(json.dumps(entry) + "\n")

        try:
            results = client._read_new_tool_results_sync()
            assert len(results) == 1
            assert "hello world" in results[0].tool_output
        finally:
            jsonl_path.unlink(missing_ok=True)

    def test_skips_non_tool_results(self, tmp_path):
        client = AcpClient()
        client._session_id = "test-sess-3"
        client._jsonl_pos = 0

        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = session_dir / "test-sess-3.jsonl"

        lines = [
            json.dumps({"kind": "Message", "data": {"text": "hi"}}) + "\n",
            json.dumps(
                {
                    "kind": "ToolResults",
                    "data": {
                        "content": [
                            {
                                "kind": "toolResult",
                                "data": {
                                    "toolUseId": "tu-3",
                                    "content": [{"kind": "text", "data": "result"}],
                                },
                            }
                        ]
                    },
                }
            )
            + "\n",
        ]
        jsonl_path.write_text("".join(lines))

        try:
            results = client._read_new_tool_results_sync()
            assert len(results) == 1
            assert results[0].tool_call_id == "tu-3"
        finally:
            jsonl_path.unlink(missing_ok=True)

    def test_partial_line_not_consumed(self, tmp_path):
        client = AcpClient()
        client._session_id = "test-sess-4"
        client._jsonl_pos = 0

        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = session_dir / "test-sess-4.jsonl"

        # Write a complete line + partial line (no trailing newline)
        complete = (
            json.dumps(
                {
                    "kind": "ToolResults",
                    "data": {
                        "content": [
                            {
                                "kind": "toolResult",
                                "data": {
                                    "toolUseId": "tu-ok",
                                    "content": [{"kind": "text", "data": "done"}],
                                },
                            }
                        ]
                    },
                }
            )
            + "\n"
        )
        partial = '{"kind": "ToolResults", "data": {"content": [{"kind": "toolRes'
        jsonl_path.write_text(complete + partial)

        try:
            results = client._read_new_tool_results_sync()
            assert len(results) == 1
            assert results[0].tool_call_id == "tu-ok"
        finally:
            jsonl_path.unlink(missing_ok=True)


# ── Coverage push: additional coverage ──


class TestFormatCommandResult:
    """Tests for _format_command_result."""

    def test_structured_data_with_message(self):
        result = AcpClient._format_command_result({"data": {"key": "value"}, "message": "Done"})
        assert "Done" in result
        assert "```json" in result
        assert '"key"' in result

    def test_structured_data_without_message(self):
        result = AcpClient._format_command_result({"data": {"key": "val"}, "message": ""})
        assert "```json" in result
        assert '"key"' in result

    def test_agent_model_filtered(self):
        result = AcpClient._format_command_result(
            {"data": {"agent": "x", "model": "y"}, "message": ""}
        )
        # Only agent/model → display is empty → falls through to message
        assert result == ""

    def test_message_only(self):
        result = AcpClient._format_command_result({"message": "hello"})
        assert result == "hello"

    def test_empty_result(self):
        result = AcpClient._format_command_result({})
        assert result == ""


class TestParseSlashCommand:
    """Tests for _parse_slash_command."""

    def test_simple_command(self):
        name, args = AcpClient._parse_slash_command("/compact")
        assert name == "compact"
        assert args == {}

    def test_command_with_value(self):
        name, args = AcpClient._parse_slash_command("/agent planner")
        assert name == "agent"
        assert args == {"value": "planner"}

    def test_command_with_multi_word_value(self):
        name, args = AcpClient._parse_slash_command("/usage detailed view")
        assert name == "usage"
        assert args == {"value": "detailed view"}


class TestStreamCommand:
    """Tests for stream_command."""

    @pytest.mark.asyncio
    async def test_stream_command_yields_events(self):
        from kiro_crew.acp.types import EVENT_COMPLETE, JsonRpcMessage

        client = AcpClient()
        client._session_id = "s1"
        client.ensure_ready = AsyncMock()
        client._send_request = AsyncMock(return_value=5)

        complete_msg = JsonRpcMessage(id=5, result={"message": "compacted", "data": {}})

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop
        client._read_new_tool_results_sync = lambda: []

        events = []
        async for ev in client.stream_command("/compact"):
            events.append(ev)

        kinds = [e.kind for e in events]
        assert EVENT_COMPLETE in kinds


class TestReadPromptResponse:
    """Tests for _read_prompt_response covering text accumulation and timeout."""

    @pytest.mark.asyncio
    async def test_accumulates_text(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        text_msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hello "}}
            },
        )
        text_msg2 = JsonRpcMessage(
            method="session/update",
            params={
                "update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "world"}}
            },
        )
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

        async def fake_prompt_loop(req_id, timeout):
            yield "update", text_msg
            yield "update", text_msg2
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop

        result = await client._read_prompt_response(req_id=1, timeout=5.0)
        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        from kiro_crew.acp.client import AcpTimeoutError

        client = AcpClient()

        async def fake_prompt_loop(req_id, timeout):
            # yields nothing — simulates timeout
            return
            yield  # make it an async generator

        client._prompt_loop = fake_prompt_loop

        with pytest.raises(AcpTimeoutError):
            await client._read_prompt_response(req_id=1, timeout=5.0)

    @pytest.mark.asyncio
    async def test_error_raises(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        error_msg = JsonRpcMessage(id=1, error={"code": -1, "message": "fail"})

        async def fake_prompt_loop(req_id, timeout):
            yield "error", error_msg

        client._prompt_loop = fake_prompt_loop

        with pytest.raises(AcpError, match="fail"):
            await client._read_prompt_response(req_id=1, timeout=5.0)


class TestPromptLoopReleasesTurnDone:
    """The core loop must release _turn_done on every exit so a cooperative
    cancel waiter (wait_turn_done) is not left blocking the full budget."""

    @pytest.mark.asyncio
    async def test_process_death_releases_turn_done(self, tmp_path):
        # An exception raised INSIDE the loop (process death) must still set
        # _turn_done via the finally, so a concurrent wait_turn_done returns
        # promptly instead of blocking its whole timeout.
        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._read_message = AsyncMock(side_effect=AcpProcessDied("boom"))
        client._is_process_alive = lambda: False

        with pytest.raises(AcpProcessDied):
            async for _ in client._prompt_loop(req_id=1, timeout=5.0):
                pass

        assert client._turn_done.is_set()
        # wait_turn_done resolves immediately, not after the budget.
        reason = await client.wait_turn_done(timeout=5.0)
        assert reason == ""  # no clean stop reason → caller escalates correctly

    @pytest.mark.asyncio
    async def test_cancel_grace_exceeded_releases_turn_done(self, tmp_path):
        # The grace-window AcpError (cancel ack never arrived) must also release
        # the waiter rather than bypassing it.
        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._cancelled = True
        client._cancel_ts = time.monotonic() - 1000.0  # well past any grace
        proc = MagicMock()
        proc.returncode = None
        proc.stdout = MagicMock()
        client._process = proc

        with pytest.raises(AcpError, match="grace window"):
            async for _ in client._prompt_loop(req_id=1, timeout=5.0):
                pass

        assert client._turn_done.is_set()

    @pytest.mark.asyncio
    async def test_permission_auto_approved(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        perm_msg = JsonRpcMessage(
            id=99,
            method="session/requestPermission",
            params={"toolCall": {"title": "shell"}, "options": []},
        )
        complete_msg = JsonRpcMessage(id=1, result={})
        client.approve_tool = AsyncMock()

        async def fake_prompt_loop(req_id, timeout):
            yield "permission", perm_msg
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop

        result = await client._read_prompt_response(req_id=1, timeout=5.0)
        client.approve_tool.assert_awaited_once()
        assert result == ""


class TestToolStallWatchdog:
    """A tool dispatched that never returns must abort the turn via the
    _TOOL_STALL_TIMEOUT watchdog, not hang to the full prompt timeout."""

    @pytest.mark.asyncio
    async def test_dispatched_tool_with_no_data_exits(self, tmp_path, monkeypatch):
        from kiro_crew.acp import client as acp_client

        # Shrink the windows so the watchdog trips quickly under test.
        monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 0.2)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        # Simulate "a tool was dispatched this turn".
        client._tool_dispatched = True
        client._stale_eligible = False  # the stale check must NOT be what saves us
        client._last_activity = time.monotonic() - 10.0  # stderr/keepalive also silent
        # Process is alive but silent: _read_message always returns None.
        client._read_message = AsyncMock(return_value=None)
        client._is_process_alive = lambda: True
        # Recovery kills the wedged child; stub it so the test doesn't touch a
        # real process (there is none — AcpClient was not spawned).
        client._kill_process = AsyncMock()

        actions = []
        # Generous outer timeout; the watchdog must end the loop well before it.
        t0 = time.monotonic()
        with pytest.raises(AcpProcessDied, match="tool stalled"):
            async for action, _ in client._prompt_loop(req_id=1, timeout=30.0):
                actions.append(action)
        elapsed = time.monotonic() - t0

        # No spurious actions, the wedged child was killed, and the turn-done
        # waiter was released via the finally so no cooperative-stop hangs.
        assert actions == []
        client._kill_process.assert_awaited_once()
        assert client._turn_done.is_set()
        # Prove it was the watchdog (stall window 0.2s) that ended the loop,
        # not the outer 30s deadline — guards against the watchdog branch being
        # removed and the test still passing on the outer timeout.
        assert elapsed < 5.0, f"loop ran too long ({elapsed:.2f}s) — watchdog may not have fired"

    @pytest.mark.asyncio
    async def test_no_dispatch_does_not_trip_watchdog(self, tmp_path, monkeypatch):
        # Without a dispatched tool, the stall watchdog must NOT fire; the loop
        # simply runs to its own deadline (proving the guard is gated on the flag).
        from kiro_crew.acp import client as acp_client

        monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 0.2)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._tool_dispatched = False
        client._stale_eligible = False
        client._read_message = AsyncMock(return_value=None)
        client._is_process_alive = lambda: True

        actions = []
        # Short outer timeout: the loop should exhaust it (no early watchdog exit).
        t0 = time.monotonic()
        async for action, _ in client._prompt_loop(req_id=1, timeout=0.5):
            actions.append(action)
        elapsed = time.monotonic() - t0

        assert actions == []
        assert client._turn_done.is_set()
        # The guard is gated on _tool_dispatched, so with the flag clear the
        # watchdog (0.2s stall window) must NOT fire — the loop runs to its own
        # 0.5s deadline. If the `self._tool_dispatched and ...` guard were
        # removed, the loop would exit ~0.2s early and this assertion would fail.
        assert (
            elapsed >= 0.4
        ), f"loop exited too early ({elapsed:.2f}s) — watchdog gate may be broken"

    @pytest.mark.asyncio
    async def test_progress_frame_then_stall_still_trips(self, tmp_path, monkeypatch):
        # Regression: a dispatched tool that emits ONE inbound frame and then
        # goes silent must still trip the watchdog.  _prompt_loop must not clear
        # _tool_dispatched on inbound frames — only an actual tool result or
        # turn completion (handled in _dispatch_events) clears it.  The
        # last_data_ts reset alone prevents false positives for tools that keep
        # streaming.
        from kiro_crew.acp import client as acp_client
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 0.2)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._tool_dispatched = True  # a tool was dispatched this turn
        client._stale_eligible = False
        client._last_activity = time.monotonic() - 10.0  # stderr/keepalive silent
        client._kill_process = AsyncMock()

        # One progress frame, then silence forever.
        frames = [JsonRpcMessage(method="session/update", params={})]

        async def fake_read(*_args, **_kwargs):
            return frames.pop(0) if frames else None

        client._read_message = fake_read  # type: ignore[assignment]
        client._is_process_alive = lambda: True

        actions = []
        # The single frame is yielded as an action; then the watchdog must end
        # the loop well before this generous outer timeout.
        with pytest.raises(AcpProcessDied, match="tool stalled"):
            async for action, _ in client._prompt_loop(req_id=1, timeout=30.0):
                actions.append(action)

        # The flag was NOT cleared by the inbound frame (that's _dispatch_events'
        # job), so the watchdog still fired, killed the child, and raised.
        assert client._tool_dispatched is True
        client._kill_process.assert_awaited_once()
        assert client._turn_done.is_set()

    @pytest.mark.asyncio
    async def test_complete_clears_tool_dispatched(self, monkeypatch):
        # Covers the EVENT_COMPLETE clear path in _dispatch_events: a turn that
        # ends while a tool is still marked dispatched must disarm the flag so a
        # stale True can't leak into the next turn's watchdog.
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        # No tool results to flush on the complete path.
        monkeypatch.setattr(client, "_read_new_tool_results_sync", lambda: [])
        complete_msg = JsonRpcMessage(id=1, result={"stopReason": "end_turn"})

        async def fake_prompt_loop(req_id, timeout):
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop  # type: ignore[assignment]
        client._tool_dispatched = True  # a tool was in flight when the turn ended

        async for _ in client._dispatch_events(req_id=1, timeout=5.0):
            pass

        assert client._tool_dispatched is False

    @pytest.mark.asyncio
    async def test_tool_result_clears_tool_dispatched(self, monkeypatch):
        # Covers the tool_call_update clear path in _dispatch_events: when the
        # dispatched tool produces a result, the watchdog is disarmed.
        from kiro_crew.acp.types import (
            EVENT_TOOL_CALL_UPDATE,
            METHOD_SESSION_UPDATE,
            AcpEvent,
            JsonRpcMessage,
        )

        client = AcpClient()
        monkeypatch.setattr(client, "_read_new_tool_results_sync", lambda: [])
        # An update frame that carries a tool result (no text chunk, no new
        # tool_call) — drive only the tool_call_update branch.
        monkeypatch.setattr(client, "_extract_text_chunk", lambda msg: ("", False))
        monkeypatch.setattr(client, "_extract_tool_event", lambda msg: None)
        monkeypatch.setattr(
            client,
            "_extract_tool_call_update",
            lambda msg: AcpEvent(kind=EVENT_TOOL_CALL_UPDATE, tool_call_id="t1"),
        )
        monkeypatch.setattr(client, "_extract_tool_call_refinement", lambda msg: None)
        update_msg = JsonRpcMessage(method=METHOD_SESSION_UPDATE, params={})
        complete_msg = JsonRpcMessage(id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "update", update_msg
            yield "complete", complete_msg

        client._prompt_loop = fake_prompt_loop  # type: ignore[assignment]
        client._tool_dispatched = True

        saw_result = False
        async for event in client._dispatch_events(req_id=1, timeout=5.0):
            if event.kind == EVENT_TOOL_CALL_UPDATE:
                # Cleared the instant the result is dispatched, before complete.
                assert client._tool_dispatched is False
                saw_result = True

        assert saw_result
        assert client._tool_dispatched is False


class TestToolStallKeepalive:
    """Keepalive pings (touch_activity) must prevent false tool-stall timeouts
    for MCP tools that legitimately block (wait, spawn_sub_agents)."""

    @pytest.mark.asyncio
    async def test_keepalive_prevents_tool_stall(self, tmp_path, monkeypatch):
        """When _last_activity is refreshed by keepalive pings, the tool stall
        watchdog must NOT fire even if no JSON-RPC stdout data arrives."""
        from kiro_crew.acp import client as acp_client

        monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 0.5)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.05)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._tool_dispatched = True
        client._stale_eligible = False
        client._is_process_alive = lambda: True

        # _read_message must actually yield to the event loop (simulating the
        # real readline timeout) so the keepalive pinger gets scheduled.
        async def slow_read(*_a, **_kw):
            await asyncio.sleep(0.05)
            return None

        client._read_message = slow_read  # type: ignore[assignment]

        # Simulate keepalive pings refreshing _last_activity every 0.1s
        # while no stdout data arrives.
        stop = asyncio.Event()

        async def keepalive_pinger():
            while not stop.is_set():
                client.touch_activity()
                await asyncio.sleep(0.1)

        pinger = asyncio.create_task(keepalive_pinger())

        actions: list[object] = []
        # Outer timeout 1.2s > stall window 0.5s.  Without the fix, the
        # watchdog would fire at ~0.5s.  With the fix, keepalive keeps it
        # alive and the loop runs to the outer deadline.
        t0 = time.monotonic()
        async for action, _ in client._prompt_loop(req_id=1, timeout=1.2):
            actions.append(action)
        elapsed = time.monotonic() - t0

        stop.set()
        await pinger

        # Loop ran to outer timeout (1.2s), NOT cut short by stall watchdog (0.5s).
        assert elapsed >= 1.0, f"loop exited at {elapsed:.2f}s — watchdog fired despite keepalive"
        assert actions == []

    @pytest.mark.asyncio
    async def test_genuine_stall_still_fires_without_keepalive(self, tmp_path, monkeypatch):
        """Without keepalive pings, the tool stall watchdog must still fire
        when a dispatched tool goes completely silent."""
        from kiro_crew.acp import client as acp_client

        monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 0.2)
        monkeypatch.setattr(acp_client, "_READ_TIMEOUT", 0.02)

        client = AcpClient(work_dir=tmp_path)
        client._turn_done.clear()
        client._tool_dispatched = True
        client._stale_eligible = False
        client._read_message = AsyncMock(return_value=None)
        client._is_process_alive = lambda: True
        client._kill_process = AsyncMock()
        # No keepalive pings — _last_activity stays at construction time.

        actions: list[object] = []
        t0 = time.monotonic()
        with pytest.raises(AcpProcessDied, match="tool stalled"):
            async for action, _ in client._prompt_loop(req_id=1, timeout=30.0):
                actions.append(action)
        elapsed = time.monotonic() - t0

        # Watchdog fired quickly (at ~0.2s stall window), NOT the outer 30s,
        # killed the wedged child, and raised.
        assert elapsed < 5.0, f"watchdog didn't fire ({elapsed:.2f}s)"
        assert actions == []
        client._kill_process.assert_awaited_once()
        assert client._turn_done.is_set()


class TestSendMessageStreamBranches:
    """Tests for send_message_stream covering metadata and compaction branches."""

    @pytest.mark.asyncio
    async def test_metadata_tracked(self):
        from kiro_crew.acp.types import METHOD_METADATA, JsonRpcMessage

        client = AcpClient()
        meta_msg = JsonRpcMessage(method=METHOD_METADATA, params={"contextUsagePercentage": 75.0})
        complete_msg = JsonRpcMessage(id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "metadata", meta_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        chunks = []
        async for c in client.send_message_stream("test"):
            chunks.append(c)

        assert client.last_prompt_stats.context_pct == 75.0

    @pytest.mark.asyncio
    async def test_compaction_logged(self):
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        client = AcpClient()
        compact_msg = JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": "in_progress"}},
        )
        complete_msg = JsonRpcMessage(id=1, result={})

        async def fake_prompt_loop(req_id, timeout):
            yield "compaction", compact_msg
            yield "complete", complete_msg

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        chunks = []
        async for c in client.send_message_stream("test"):
            chunks.append(c)

        # No text chunks expected, just verifying no crash
        assert chunks == []

    @pytest.mark.asyncio
    async def test_timeout_sets_turn_done(self):
        """When prompt loop ends without complete, turn_done is set."""

        client = AcpClient()

        async def fake_prompt_loop(req_id, timeout):
            # Empty generator — simulates timeout
            return
            yield

        client.ensure_ready = AsyncMock()
        client._send_prompt = AsyncMock(return_value=1)
        client._prompt_loop = fake_prompt_loop

        chunks = []
        async for c in client.send_message_stream("test"):
            chunks.append(c)

        assert client._turn_done.is_set()


@_POSIX_ONLY
class TestKillProcessPipeClose:
    """Test pipe closing in _kill_process."""

    @pytest.mark.asyncio
    async def test_pipes_closed_before_kill(self):
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        stdin_mock = MagicMock()
        stdout_mock = MagicMock()
        stderr_mock = MagicMock()
        proc.stdin = stdin_mock
        proc.stdout = stdout_mock
        proc.stderr = stderr_mock
        proc.wait = AsyncMock(return_value=0)
        client._process = proc
        client._pid = 100
        client._child_pids = {}

        with (
            patch("os.killpg"),
            patch("os.getpgid", return_value=100),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            await client._kill_process()

        stdin_mock.close.assert_called_once()
        stdout_mock.close.assert_called_once()
        stderr_mock.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_pipe_close_exception_ignored(self):
        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.close.side_effect = OSError("already closed")
        proc.stdout = MagicMock()
        proc.stderr = None
        proc.wait = AsyncMock(return_value=0)
        client._process = proc
        client._pid = 101
        client._child_pids = {}

        with (
            patch("os.killpg"),
            patch("os.getpgid", return_value=101),
            patch("kiro_crew.acp.client._get_child_pids", return_value=[]),
            patch("kiro_crew.acp.client._kill_escaped_children"),
        ):
            await client._kill_process()  # should not raise


# ── _extract_tool_call_update tests ──


class TestExtractToolCallUpdate:
    """Tests for real-time tool result extraction from session updates."""

    def _make_msg(self, update):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(params={"update": update})

    def _client(self):
        return AcpClient()

    def test_ignores_non_tool_call_update(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = self._client()
        msg = JsonRpcMessage(params={"update": {"sessionUpdate": "other"}})
        assert client._extract_tool_call_update(msg) is None

    def test_ignores_missing_tool_call_id(self):
        client = self._client()
        msg = self._make_msg({"sessionUpdate": "tool_call_update", "toolCallId": ""})
        assert client._extract_tool_call_update(msg) is None

    def test_content_blocks_path(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-1",
                "content": [
                    {"content": {"type": "text", "text": "hello world"}},
                ],
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert event.kind == "tool_result"
        assert event.tool_call_id == "tc-1"
        assert "hello world" in event.tool_output

    def test_raw_output_stdout_path(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2",
                "rawOutput": {
                    "items": [{"Json": {"stdout": "ls output here"}}],
                },
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert "ls output here" in event.tool_output

    def test_raw_output_json_fallback(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-3",
                "rawOutput": {
                    "items": [{"Json": {"key": "value"}}],
                },
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert "key" in event.tool_output

    def test_raw_output_text_path(self):
        # fs_read / shell-style tools land their output in items[].Text —
        # this is the common case for native sub-agent read/summary tools.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-text",
                "status": "completed",
                "rawOutput": {
                    "items": [{"Text": "file contents line 1\nline 2"}],
                },
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert event.tool_call_id == "tc-text"
        assert "file contents line 1" in event.tool_output
        assert event.tool_final is True

    def test_raw_output_text_priority_over_json(self):
        # When an item carries both Text and Json, Text wins (it's the
        # human-readable rendering kiro-cli already produced).
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-both",
                "rawOutput": {
                    "items": [{"Text": "rendered text", "Json": {"stdout": "raw json"}}],
                },
            }
        )
        event = client._extract_tool_call_update(msg)
        assert event is not None
        assert "rendered text" in event.tool_output
        assert "raw json" not in event.tool_output

    def test_raw_output_skips_empty_text(self):
        # Empty Text must not short-circuit to a useless empty result; an
        # item with empty Text and no Json yields no output.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-empty",
                "rawOutput": {"items": [{"Text": ""}]},
            }
        )
        assert client._extract_tool_call_update(msg) is None

    def test_content_takes_priority_over_raw(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-4",
                "content": [{"content": {"type": "text", "text": "from content"}}],
                "rawOutput": {"items": [{"Json": {"stdout": "from raw"}}]},
            }
        )
        event = client._extract_tool_call_update(msg)
        assert "from content" in event.tool_output
        assert "from raw" not in event.tool_output

    def test_empty_content_falls_through_to_raw(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-5",
                "content": [],
                "rawOutput": {"items": [{"Json": {"stdout": "fallback"}}]},
            }
        )
        event = client._extract_tool_call_update(msg)
        assert "fallback" in event.tool_output

    def test_no_output_returns_none(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-6",
                "content": [],
                "rawOutput": {"items": []},
            }
        )
        assert client._extract_tool_call_update(msg) is None

    def test_output_truncated_to_8000(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-7",
                "content": [
                    {"content": {"type": "text", "text": "x" * 5000}},
                    {"content": {"type": "text", "text": "y" * 5000}},
                ],
            }
        )
        event = client._extract_tool_call_update(msg)
        assert len(event.tool_output) <= 8000

    def test_redaction_applied(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-8",
                "content": [
                    {"content": {"type": "text", "text": "key=AKIAIOSFODNN7EXAMPLE secret"}},
                ],
            }
        )
        event = client._extract_tool_call_update(msg)
        assert "AKIAIOSFODNN7EXAMPLE" not in event.tool_output

    def test_ignores_non_dict_content_blocks(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-9",
                "content": ["not a dict", None, 42],
            }
        )
        assert client._extract_tool_call_update(msg) is None

    def test_ignores_non_text_content_type(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-10",
                "content": [{"content": {"type": "image", "url": "http://x"}}],
            }
        )
        assert client._extract_tool_call_update(msg) is None

    def test_none_params(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = self._client()
        msg = JsonRpcMessage(params=None)
        assert client._extract_tool_call_update(msg) is None


# ── _extract_tool_call_refinement tests ──


class TestExtractToolCallRefinement:
    """claude-agent-acp emits a follow-up tool_call_update once the streamed
    tool input is complete; the refinement carries title / kind / rawInput."""

    def _make_msg(self, update):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(params={"update": update})

    def _client(self):
        return AcpClient()

    def test_ignores_non_tool_call_update(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = self._client()
        msg = JsonRpcMessage(params={"update": {"sessionUpdate": "other"}})
        assert client._extract_tool_call_refinement(msg) is None

    def test_ignores_missing_tool_call_id(self):
        client = self._client()
        msg = self._make_msg({"sessionUpdate": "tool_call_update", "toolCallId": ""})
        assert client._extract_tool_call_refinement(msg) is None

    def test_pure_output_update_returns_none(self):
        # tool_call_update carrying ONLY content (no title/kind/rawInput) is
        # the result-only path handled by _extract_tool_call_update.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-1",
                "content": [{"content": {"type": "text", "text": "out"}}],
            }
        )
        assert client._extract_tool_call_refinement(msg) is None

    def test_refines_title_and_kind(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2",
                "title": "ls /tmp",
                "kind": "execute",
                "rawInput": {"command": "ls /tmp"},
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.kind == "tool_call_update"
        assert event.tool_call_id == "tc-2"
        assert event.title == "ls /tmp"
        assert event.tool_kind == "execute"
        assert "ls /tmp" in event.tool_input

    def test_prefers_rawinput_description_over_title(self):
        # Bash tool emits both `command` and `description` — the description
        # is the human-readable purpose ("List KiroCrew dashboard module
        # files"), and that's what we surface on the pill.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2b",
                "title": "ls /workplace/.../dashboard/",
                "kind": "execute",
                "rawInput": {
                    "command": "ls /workplace/.../dashboard/",
                    "description": "List KiroCrew dashboard module files",
                },
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.title == "List KiroCrew dashboard module files"

    def test_blank_description_falls_back_to_title(self):
        # Whitespace-only description shouldn't override a useful title.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2c",
                "title": "ls /tmp",
                "rawInput": {"command": "ls /tmp", "description": "   "},
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.title == "ls /tmp"

    def test_caches_input_for_permission_lookup(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-3",
                "title": "grep foo",
                "rawInput": {"pattern": "foo"},
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        # The refined input is also cached so a later permission request
        # for the same tool_call_id can pick it up.
        assert client._tool_call_inputs.get("tc-3") == event.tool_input

    def test_diff_content_block_replaces_raw_input(self):
        # Edit-style tools send the diff in content; the refinement should
        # surface the unified diff instead of the raw input dict.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-4",
                "title": "Edit foo.py",
                "kind": "edit",
                "rawInput": {"old_string": "old", "new_string": "new", "file_path": "foo.py"},
                "content": [
                    {
                        "type": "diff",
                        "path": "foo.py",
                        "oldText": "old",
                        "newText": "new",
                    },
                ],
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        # _make_unified_diff prefixes file headers
        assert "foo.py" in event.tool_input

    def test_redacts_credentials_in_input(self):
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-5",
                "title": "Bash",
                "rawInput": {"command": "echo AKIAIOSFODNN7EXAMPLE"},
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert "AKIAIOSFODNN7EXAMPLE" not in event.tool_input

    def test_no_refinement_fields_returns_none(self):
        # Empty update with just tool_call_id should not emit a refinement.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-6",
            }
        )
        assert client._extract_tool_call_refinement(msg) is None

    def test_kind_only_emits_refinement(self):
        # Even a lone `kind` update is worth surfacing — avoids losing the
        # Bash/Edit distinction if the upstream renders it without a title.
        client = self._client()
        msg = self._make_msg(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-7",
                "kind": "search",
            }
        )
        event = client._extract_tool_call_refinement(msg)
        assert event is not None
        assert event.tool_kind == "search"


class TestCaptureAvailableModels:
    """Capturing the backend-advertised model list from session responses."""

    def _client(self):
        return AcpClient(acp_backend=ACP_BACKEND_CLAUDE)

    def test_captures_versioned_models(self):
        c = self._client()
        c._capture_available_models(
            {
                "sessionId": "s",
                "models": {
                    "currentModelId": "claude-opus-4-8-1m",
                    "availableModels": [
                        {"modelId": "claude-opus-4-8-1m", "name": "Opus 4.8", "description": "new"},
                        {"modelId": "claude-sonnet-4-6", "name": "Sonnet 4.6"},
                    ],
                },
            }
        )
        am = c.available_models()
        assert [m["modelId"] for m in am] == ["claude-opus-4-8-1m", "claude-sonnet-4-6"]
        assert am[1]["description"] == ""  # missing description -> empty string
        assert c._resolved_model_id == "claude-opus-4-8-1m"

    def test_current_model_id_defaults_to_none(self):
        c = self._client()
        assert c._resolved_model_id is None

    def test_current_model_id_not_overwritten_when_absent(self):
        """A later session/new (or session/load) without currentModelId (e.g.
        a minimal/degenerate response) must not clobber a previously-resolved
        model id — _track_metadata's window resolution should keep working."""
        c = self._client()
        c._capture_available_models(
            {"models": {"currentModelId": "claude-opus-4.6", "availableModels": []}}
        )
        assert c._resolved_model_id == "claude-opus-4.6"
        c._capture_available_models({"models": {"availableModels": []}})
        assert c._resolved_model_id == "claude-opus-4.6"

    def test_no_models_key_leaves_empty(self):
        c = self._client()
        c._capture_available_models({"sessionId": "s"})
        assert c.available_models() == []

    def test_entries_without_modelid_skipped(self):
        c = self._client()
        c._capture_available_models(
            {"models": {"availableModels": [{"name": "x"}, {"modelId": "ok", "name": "OK"}]}}
        )
        assert [m["modelId"] for m in c.available_models()] == ["ok"]

    def test_value_field_accepted_as_model_id(self):
        # ACP config-option shape uses "value" rather than "modelId".
        c = self._client()
        c._capture_available_models(
            {"models": {"availableModels": [{"value": "m1", "name": "M1"}]}}
        )
        assert c.available_models()[0]["modelId"] == "m1"


def _scripted_process(lines, *, returncode=None):
    """Build a mock subprocess whose stdout.readline yields *lines* in order.

    Each entry of *lines* is a dict (serialized to a JSON-RPC line) or a raw
    bytes value. After the list is exhausted, readline blocks-then-returns an
    empty timeout-friendly value so _read_message's wait_for sees nothing new.
    """
    queue = deque(lines)

    async def _readline():
        if queue:
            item = queue.popleft()
            if isinstance(item, (bytes, bytearray)):
                return bytes(item)
            return (json.dumps(item) + "\n").encode()
        # Nothing left — emulate a quiet stream (no more frames this read).
        await asyncio.sleep(0)
        return b""

    proc = MagicMock()
    stdout = AsyncMock()
    stdout.readline = AsyncMock(side_effect=_readline)
    proc.stdout = stdout
    proc.returncode = returncode
    # stdin used by _send_error / _send_response.
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock()
    proc.stdin = stdin
    return proc


class TestWaitForResponseDeferral:
    """F1: _wait_for_response must not spin on inbound server requests or
    foreign-id responses, must not drop them, and must re-inject them."""

    @pytest.mark.asyncio
    async def test_inbound_permission_request_deferred_not_spun(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        # Awaiting response for req_id=7. A server->client permission request
        # arrives first carrying id=7 (colliding namespace), THEN the real
        # response for req 7 arrives.
        perm = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {"sessionId": "s", "options": [], "toolCall": {"title": "x"}},
        }
        resp = {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}
        client._process = _scripted_process([perm, resp])

        result = await asyncio.wait_for(client._wait_for_response(7, timeout=5.0), timeout=10.0)
        assert result == {"ok": True}
        # Permission request must be re-injected (not dropped, not spun).
        assert len(client._buffer) == 1
        buffered = client._buffer.popleft()
        assert buffered.method == "session/request_permission"
        assert buffered.id == 7

    @pytest.mark.asyncio
    async def test_foreign_id_response_preserved(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        foreign = {"jsonrpc": "2.0", "id": 99, "result": {"stale": True}}
        resp = {"jsonrpc": "2.0", "id": 3, "result": {"ok": True}}
        client._process = _scripted_process([foreign, resp])

        result = await asyncio.wait_for(client._wait_for_response(3, timeout=5.0), timeout=10.0)
        assert result == {"ok": True}
        assert len(client._buffer) == 1
        assert client._buffer.popleft().id == 99

    @pytest.mark.asyncio
    async def test_notification_goes_to_mcp_notifications(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        notif = {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {}}}
        resp = {"jsonrpc": "2.0", "id": 5, "result": {"ok": True}}
        client._process = _scripted_process([notif, resp])

        result = await asyncio.wait_for(client._wait_for_response(5, timeout=5.0), timeout=10.0)
        assert result == {"ok": True}
        # Notification buffered for drain, NOT re-injected into _buffer.
        assert len(client._buffer) == 0
        assert len(client._mcp_notifications) == 1
        assert client._mcp_notifications[0].method == "session/update"

    @pytest.mark.asyncio
    async def test_deferred_reinjected_in_order_on_timeout(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        first = {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/request_permission",
            "params": {},
        }
        second = {"jsonrpc": "2.0", "id": 88, "result": {"other": True}}
        # No matching response ever arrives -> timeout. Deferred frames must
        # still be re-injected in arrival order.
        client._process = _scripted_process([first, second])

        with pytest.raises(AcpError):
            await asyncio.wait_for(client._wait_for_response(7, timeout=1.0), timeout=10.0)
        assert len(client._buffer) == 2
        m0 = client._buffer.popleft()
        m1 = client._buffer.popleft()
        assert m0.method == "session/request_permission"
        assert m1.id == 88


class TestWaitForResponseActivityDeadline:
    """Low-A: a steady stream of notifications keeps _wait_for_response alive
    past the base timeout until the real response arrives."""

    @pytest.mark.asyncio
    async def test_streaming_notifications_extend_deadline(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path)

        # Virtual clock so the test is fast and deterministic. Each readline
        # advances time by 0.05s; base timeout is 0.1s. Without activity-based
        # extension the call would die after ~2 reads; with it, the stream of
        # notifications keeps pushing the deadline out until the response lands.
        clock = {"t": 1000.0}

        def fake_monotonic():
            return clock["t"]

        notif = {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {}}}
        frames = [notif] * 20 + [{"jsonrpc": "2.0", "id": 4, "result": {"loaded": True}}]
        queue = deque(frames)

        async def _readline():
            if queue:
                clock["t"] += 0.05  # each frame advances the virtual clock
                return (json.dumps(queue.popleft()) + "\n").encode()
            return b""

        proc = MagicMock()
        stdout = AsyncMock()
        stdout.readline = AsyncMock(side_effect=_readline)
        proc.stdout = stdout
        proc.returncode = None
        client._process = proc

        monkeypatch.setattr("kiro_crew.acp.client.time.monotonic", fake_monotonic)

        result = await asyncio.wait_for(client._wait_for_response(4, timeout=0.1), timeout=10.0)
        assert result == {"loaded": True}
        # All 20 notifications were drained while the deadline kept extending.
        assert len(client._mcp_notifications) == 20

    @pytest.mark.asyncio
    async def test_hard_cap_eventually_times_out(self, tmp_path, monkeypatch):
        client = AcpClient(work_dir=tmp_path)
        clock = {"t": 5000.0}

        def fake_monotonic():
            return clock["t"]

        notif = {"jsonrpc": "2.0", "method": "session/update", "params": {"update": {}}}

        async def _readline():
            # Endless notifications, large time jumps — never a matching
            # response. The absolute hard cap must eventually fire.
            clock["t"] += 30.0
            return (json.dumps(notif) + "\n").encode()

        proc = MagicMock()
        stdout = AsyncMock()
        stdout.readline = AsyncMock(side_effect=_readline)
        proc.stdout = stdout
        proc.returncode = None
        client._process = proc

        monkeypatch.setattr("kiro_crew.acp.client.time.monotonic", fake_monotonic)

        with pytest.raises(AcpError):
            await asyncio.wait_for(client._wait_for_response(1, timeout=0.1), timeout=10.0)


class TestProcessMessageUnknownServerRequest:
    """F5: unknown server->client requests are classified for a -32601 reply,
    not silently skipped."""

    def test_unknown_server_request_classified(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(id=12, method="fs/read_text_file", params={"path": "/x"})
        assert client._process_message(msg, req_id=1) == "server_request_unknown"

    def test_terminal_create_classified(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(id=3, method="terminal/create", params={})
        assert client._process_message(msg, req_id=1) == "server_request_unknown"

    def test_notification_still_skipped(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        # Unknown notification (method, NO id) is not a request -> skip.
        msg = JsonRpcMessage(method="some/unknown_notification", params={})
        assert client._process_message(msg, req_id=1) == "skip"

    def test_known_permission_request_not_unknown(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(id=2, method="session/request_permission", params={})
        assert client._process_message(msg, req_id=1) == "permission"

    @pytest.mark.asyncio
    async def test_reject_sends_method_not_found_error(self, tmp_path):
        from kiro_crew.acp.client import _JSONRPC_METHOD_NOT_FOUND
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._process = _scripted_process([])
        msg = JsonRpcMessage(id=42, method="terminal/create", params={})

        await client._reject_unknown_server_request(msg)

        client._process.stdin.write.assert_called_once()
        written = client._process.stdin.write.call_args[0][0].decode()
        payload = json.loads(written)
        assert payload["id"] == 42
        assert payload["error"]["code"] == _JSONRPC_METHOD_NOT_FOUND
        assert "terminal/create" in payload["error"]["message"]

    @pytest.mark.asyncio
    async def test_reject_noop_when_no_id(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._process = _scripted_process([])
        msg = JsonRpcMessage(id=None, method="terminal/create", params={})

        await client._reject_unknown_server_request(msg)
        client._process.stdin.write.assert_not_called()


class TestFormatAcpError:
    """Tests for _format_acp_error — Bedrock-aware error rewriting.

    Covers the bug filed at task 86089e43: ACP backend errors used
    to be surfaced as the raw JSON-RPC dict (`Prompt error: {'code': -32603,
    ...}`), which dead-ends users when the picker can't expose a valid
    alternative. The helper rewrites known Bedrock failures into actionable
    text while preserving the request_id for support correlation, and scrubs
    embedded credentials / exfiltration URLs as defense-in-depth.
    """

    def test_non_dict_falls_back(self):
        assert _format_acp_error(None) == "Prompt error: None"
        assert _format_acp_error("boom") == "Prompt error: boom"

    def test_unknown_dict_preserves_raw(self):
        """An unrecognised shape must lose no information.

        The fallback now leads with the provider's own text rather than a repr
        of the JSON-RPC dict, but both fields still have to survive: the
        provider detail AND the non-boilerplate JSON-RPC message, which for
        codes other than -32603 is the only summary the error carries.
        """
        err = {"code": -32000, "message": "Something else", "data": "weird"}
        out = _format_acp_error(err)
        assert "Something else" in out
        assert "weird" in out
        # No dict repr: punctuation soup is not a user-facing error message.
        assert "Prompt error: {" not in out

    def test_model_not_available_rewrite(self):
        # With NO advertised list the caller cannot know whether this is an
        # entitlement or a capacity failure, so it keeps the capacity wording.
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: The model 'opus' "
                "is not available. Please use '/model' to select a different "
                "model and try again. (request_id: 3ce0318a-24d6-4b1a-a4a7-ee81f1a3991e)"
            ),
        }
        out = _format_acp_error(err)
        assert "unavailable on the backend" in out
        assert "'opus'" in out
        assert "model picker" in out
        # Must NOT point at ~/.claude/settings.json — KiroCrew never reads it;
        # the model lives in config.json / the agent spec.
        assert "settings.json" not in out
        assert "config.json" in out
        # Nor echo the provider's own "use '/model'" advice: that is a kiro-cli
        # TUI command, inert in the dashboard, Slack, and cron.
        assert "/model" not in out
        # Request id is preserved for support correlation.
        assert "3ce0318a-24d6-4b1a-a4a7-ee81f1a3991e" in out
        # Should not leak the raw dict prefix when we have a real rewrite.
        assert "Prompt error: {" not in out

    def test_unentitled_model_names_what_the_account_can_use(self):
        """The free-tier case: say it is an access problem and list the options.

        Upstream sends the SAME string for entitlement and capacity failures,
        so the advertised list is the only thing that distinguishes them.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "The model 'opus-4.8-1m' is not available. (request_id: abc-123)",
        }
        out = _format_acp_error(err, ["claude-sonnet-4-6", "claude-haiku-4-5"])
        assert "does not have access" in out
        assert "'opus-4.8-1m'" in out
        # The actionable part: what they CAN pick.
        assert "claude-sonnet-4-6" in out
        assert "claude-haiku-4-5" in out
        # Must NOT blame capacity or tell them to wait — no retry will help.
        assert "capacity" not in out
        assert "wait a minute" not in out
        assert "abc-123" in out

    def test_advertised_model_still_reads_as_capacity(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # The model IS on offer, so a rejection really is a transient blip and
        # must keep the capacity wording (and stay retryable).
        err = {"code": -32603, "message": "x", "data": "The model 'opus' is not available."}
        out = _format_acp_error(err, ["opus", "sonnet"])
        assert "unavailable on the backend" in out
        assert "does not have access" not in out
        assert _is_transient_raw_error(err, ["opus", "sonnet"]) is True

    def test_unentitled_model_is_terminal_not_retried(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # The retry verdict must move with the wording -- retrying a model the
        # account was never offered just reproduces the same rejection.
        err = {"code": -32603, "message": "x", "data": "The model 'opus' is not available."}
        assert _is_transient_raw_error(err, ["sonnet", "haiku"]) is False
        # Unknown entitlement -> unchanged (transient), never a false accusation.
        assert _is_transient_raw_error(err, None) is True
        assert _is_transient_raw_error(err, []) is True

    def test_unentitled_match_is_case_insensitive(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # An entitled-but-differently-cased model must not be called unentitled.
        err = {"code": -32603, "message": "x", "data": "The model 'Opus' is not available."}
        assert _is_transient_raw_error(err, ["opus"]) is True
        assert "does not have access" not in _format_acp_error(err, ["opus"])

    def test_long_available_list_is_capped(self):
        err = {"code": -32603, "message": "x", "data": "The model 'nope' is not available."}
        out = _format_acp_error(err, [f"model-{i}" for i in range(12)])
        assert "+4 more" in out
        assert "model-7" in out
        assert "model-8" not in out

    def test_throttling_exception_rewrite(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "ThrottlingException: Too many requests "
                "(request_id: aaaa1111-bbbb-2222-cccc-333344445555)"
            ),
        }
        out = _format_acp_error(err)
        assert "throttling" in out.lower()
        assert "wait" in out.lower()
        assert "aaaa1111-bbbb-2222-cccc-333344445555" in out

    def test_too_many_requests_rewrite(self):
        err = {"code": -32603, "message": "x", "data": "TooManyRequestsException: rate limited"}
        out = _format_acp_error(err)
        assert "throttling" in out.lower()

    def test_access_denied_rewrite(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "AccessDeniedException: not authorized to invoke",
        }
        out = _format_acp_error(err)
        assert "authentication failed" in out.lower()
        assert "aws sso login" in out.lower()

    def test_expired_token_rewrite(self):
        err = {"code": -32603, "message": "x", "data": "ExpiredToken: signature expired"}
        out = _format_acp_error(err)
        assert "authentication failed" in out.lower()

    def test_missing_request_id_omits_suffix(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "The model 'sonnet' is not available.",
        }
        out = _format_acp_error(err)
        assert "request_id" not in out
        assert "'sonnet'" in out

    def test_throttle_keyword_in_message(self):
        # Some backends put the trigger word in `message` rather than `data`.
        err = {"code": -32603, "message": "Rate limit exceeded", "data": ""}
        out = _format_acp_error(err)
        assert "throttling" in out.lower()

    def test_credentials_in_data_are_redacted(self):
        """AWS access keys embedded in upstream errors must not leak to the UI.

        Recognized error patterns (auth/throttle/model-unavailable) already drop
        the `data` field when constructing the rewritten message, so the secret
        is gone simply by virtue of the rewrite. The redaction layer is the
        defense-in-depth fallback for the unknown-shape path; this test pins
        the absence guarantee on the recognized-pattern path.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "AccessDenied: AKIAIOSFODNN7EXAMPLE not authorized",
        }
        out = _format_acp_error(err)
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_credentials_in_unknown_dict_fallback_are_redacted(self):
        """The fallback path echoes raw dict — must still scrub secrets."""
        err = {
            "code": -32000,
            "message": "weird upstream",
            "data": "leak: AKIAIOSFODNN7EXAMPLE",
        }
        out = _format_acp_error(err)
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        # The scrubbed placeholder proves redaction ran on this path.
        assert "REDACTED" in out
        # The surrounding provider text still reaches the user.
        assert "leak:" in out

    def test_exfiltration_url_in_unknown_dict_fallback_is_redacted(self):
        """The fallback path echoes raw dict — exfil URLs must also be scrubbed.

        Pairs with `test_credentials_in_unknown_dict_fallback_are_redacted` to
        cover the second redaction layer (`redact_exfiltration_urls`).
        """
        # Use a URL whose query carries a base64-blob — matches _EXFIL_PATTERNS
        # and is what real provider error payloads tend to look like when they
        # echo signed callback URLs.
        leaked_blob = "QUtJQUlPU0ZPRE5ON0VYQU1QTEVTRUNSRVRBQ0NFU1NLRVk" + "A" * 30
        err = {
            "code": -32000,
            "message": "weird upstream",
            "data": f"callback to https://attacker.example.com/exfil?token={leaked_blob}",
        }
        out = _format_acp_error(err)
        assert leaked_blob not in out, "leaked credential blob must be redacted"
        assert "REDACTED" in out

    def test_sensitive_content_emits_log_warning(self, caplog):
        """When the redaction layer scrubs anything, a warning MUST be logged.

        Silent scrubbing hides upstream-leak signals from security review;
        the warning lets operators notice that a provider echoed sensitive
        content back. The warning intentionally includes only counts — never
        the redacted values.
        """
        import logging

        err = {
            "code": -32000,
            "message": "weird upstream",
            "data": "leak: AKIAIOSFODNN7EXAMPLE",
        }
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.client"):
            _format_acp_error(err)

        warnings = [r for r in caplog.records if "sensitive content" in r.getMessage()]
        assert warnings, "expected a redaction warning to be logged"
        assert "AKIAIOSFODNN7EXAMPLE" not in warnings[0].getMessage()

    def test_internal_server_error_rewrite(self):
        """The real transient 5xx repro (live 2026-06-14) must

        classify as a momentary backend error rather than dumping the raw
        -32603 JSON-RPC dict at the user.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: "
                "CodewhispererChatResponseStream(ServiceError(ServiceError "
                '{ name: "InternalServerError", ... please try again ... })) '
                "(request_id: 91c99864-7d2e-4f0a-9b1c-2a3b4c5d6e7f)"
            ),
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "retry in a moment" in out.lower()
        assert "5xx" in out
        # Request id preserved for support correlation.
        assert "91c99864-7d2e-4f0a-9b1c-2a3b4c5d6e7f" in out
        # Must not dead-end the user with the raw dict.
        assert "Prompt error: {" not in out

    def test_please_try_again_phrasing_rewrite(self):
        """A bare 'please try again' transient hint (no explicit 5xx token)

        still classifies as transient.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "Service hiccup, please try again.",
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "Prompt error: {" not in out

    def test_dispatch_failure_rewrite(self):
        """DispatchFailure / connection-reset shapes are transient too."""
        err = {
            "code": -32603,
            "message": "x",
            "data": "DispatchFailure: ConnectionReset (request_id: bbbb2222-cccc-3333-dddd-444455556666)",
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "bbbb2222-cccc-3333-dddd-444455556666" in out

    def test_transient_5xx_missing_request_id_omits_suffix(self):
        err = {"code": -32603, "message": "Internal error", "data": "InternalServerError occurred"}
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "request_id" not in out

    def test_transient_branch_does_not_leak_credentials(self):
        """Defense-in-depth on the new transient path: an AWS key embedded in

        the upstream `data` must never reach the UI. The rewrite drops the
        `data` field (so the secret is gone by construction), and the function
        still routes the result through redact_credentials/redact_exfiltration_urls
        at the end — the new branch adds no early return that bypasses it.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "InternalServerError: leaked AKIAIOSFODNN7EXAMPLE please try again",
        }
        out = _format_acp_error(err)
        assert "AKIAIOSFODNN7EXAMPLE" not in out
        assert "transient error" in out.lower()

    def test_throttle_precedence_over_transient(self):
        """A throttle that also says 'please try again' stays a throttle —

        throttling precedence must survive the new transient branch.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "ThrottlingException: slow down, please try again",
        }
        out = _format_acp_error(err)
        assert "throttling" in out.lower()
        assert "transient error" not in out.lower()

    def test_non_transient_minus_32603_falls_through_to_fallback(self):
        """A non-transient -32603 whose only 'internal'-ish signal is the

        canonical JSON-RPC message 'Internal error' must NOT be misclassified
        as transient. The transient branch keys off the provider `data` field
        only (never the generic `message`), so a deterministic failure like a
        ValidationException surfaces its real cause instead.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "ValidationException: input contains an unsupported field 'foo'",
        }
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        # The real cause is shown verbatim — stronger than the old assertion,
        # which only required a dict repr containing it somewhere.
        assert "ValidationException: input contains an unsupported field 'foo'" in out
        # The -32603 boilerplate message is dropped as noise next to that.
        assert "Internal error" not in out

    def test_bare_500_token_is_not_treated_as_5xx(self):
        """A standalone numeric (e.g. a token-limit value) must not match the

        5xx branch — the numeric match is anchored to an HTTP/status context,
        not a bare 500/502/503 token.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "max_tokens 500 exceeds the model limit of 200000",
        }
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        assert "max_tokens 500 exceeds the model limit of 200000" in out

    def test_http_500_status_context_still_classifies_transient(self):
        """An HTTP/status-anchored 50x token IS a genuine transient signal."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "upstream returned HTTP 503 (request_id: aaaa1111-bbbb-2222-cccc-333344445555)",
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "aaaa1111-bbbb-2222-cccc-333344445555" in out

    def test_kiro_generic_generation_failure_formats_as_transient(self):
        """kiro-cli's pre-stream generation failure wrapper gets the friendly
        retry guidance instead of the raw-dict fallback: data carried no
        request_id and no error class, only the generic wrapper string.
        """
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "Kiro failed to generate a response",
        }
        out = _format_acp_error(err)
        assert "transient error" in out.lower()
        assert "Prompt error: {" not in out

    def test_generation_failure_phrase_in_message_only_is_not_transient(self):
        """The generation-failure pattern is scoped to `data` — the phrase
        appearing only in the JSON-RPC `message` field must not trigger the
        friendly transient branch (mirrors the model-unavailable scoping)."""
        err = {
            "code": -32603,
            "message": "Kiro failed to generate a response",
            "data": "ValidationException: input contains an unsupported field 'foo'",
        }
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        assert "ValidationException: input contains an unsupported field 'foo'" in out

    def test_session_expired_rewrite(self):
        """An expired session gets actionable sign-in guidance rather than the
        misleading transient-5xx retry advice."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "DispatchFailure: session expired",
        }
        out = _format_acp_error(err)
        assert "session has expired" in out.lower() or "session expired" in out.lower()
        assert "kiro-cli login" in out.lower()
        assert "retry" in out.lower() and "will not help" in out.lower()
        # Must NOT show the misleading 5xx message.
        assert "transient error" not in out.lower()
        assert "retry in a moment" not in out.lower()

    def test_session_expired_by_http_status(self):
        """A bare 401/403 is the shape an expired session actually arrives in:
        the rejection carries no explanatory wording, so status alone must
        drive the classification."""
        for status in ("HTTP 401", "HTTP 403", "status code 401", "status 403"):
            err = {"code": -32603, "message": "Internal error", "data": status}
            out = _format_acp_error(err)
            assert "kiro-cli login" in out.lower(), f"No sign-in guidance for: {status!r}"
            assert "transient error" not in out.lower(), f"Misclassified: {status!r}"

    def test_session_expired_401_with_transport_error(self):
        """The reported failure mode: an aborted request leaves a transport
        error alongside the 401, and the 5xx family used to win and tell the
        user to retry."""
        err = {
            "code": -32603,
            "message": "Encountered an error in the response stream",
            "data": "DispatchFailure ConnectionResetError: HTTP 401",
        }
        out = _format_acp_error(err)
        assert "kiro-cli login" in out.lower()
        assert "transient error" not in out.lower()
        assert "retry in a moment" not in out.lower()

    def test_genuine_5xx_still_transient_with_auth_absent(self):
        """The new auth-status branch must not swallow real 5xx errors."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "ServiceUnavailableException: HTTP 503",
        }
        out = _format_acp_error(err)
        assert "transient" in out.lower()
        assert "kiro-cli login" not in out.lower()

    def test_session_expired_variants(self):
        """Various kiro-cli session-expiry error shapes are all classified."""
        variants = [
            "not logged in",
            "session has expired",
            "login expired",
            "authentication required",
            "session timed out",
            "not authenticated",
            "login required",
        ]
        for text in variants:
            err = {"code": -32603, "message": "Internal error", "data": text}
            out = _format_acp_error(err)
            assert "transient error" not in out.lower(), f"Failed for: {text!r}"
            assert "kiro-cli login" in out.lower(), f"No login guidance for: {text!r}"

    def test_session_expired_with_5xx_token_wins(self):
        """Session expiry checked before 5xx: a DispatchFailure wrapping a
        session-expired message must surface as auth, not transient."""
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "DispatchFailure ConnectionResetError: session expired",
        }
        out = _format_acp_error(err)
        assert "transient error" not in out.lower()
        assert "kiro-cli login" in out.lower()


class TestIsTransientRawError:
    """_is_transient_raw_error classifies retryability from the RAW JSON-RPC
    error so the verdict is independent of the formatted message."""

    def test_internal_server_error_is_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        err = {
            "code": -32603,
            "message": "Internal error",
            "data": (
                "Encountered an error in the response stream: ... "
                "CodewhispererChatResponseStream(ServiceError(ServiceError "
                "{ source: InternalServerError(...) please try again ...)))"
            ),
        }
        assert _is_transient_raw_error(err) is True

    def test_status_50x_and_hint_are_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        assert _is_transient_raw_error({"data": "HTTP 503 Service Unavailable"}) is True
        assert _is_transient_raw_error({"data": "HTTP 504 Gateway Timeout"}) is True
        assert _is_transient_raw_error({"data": "status code 529 overloaded"}) is True
        assert _is_transient_raw_error({"data": "ServiceUnavailableException"}) is True
        assert _is_transient_raw_error({"data": "DispatchFailure ConnectionReset"}) is True
        assert _is_transient_raw_error({"data": "please try again"}) is True

    def test_5xx_token_in_message_field_is_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # 5xx signal carried in `message` (not `data`) must still be caught:
        # the classifier scans the combined haystack, so this no longer fails
        # fast the way a data-only scan would (SHOULD-FIX). Auth is
        # still checked first, so an auth error with a stray 50x stays terminal.
        assert _is_transient_raw_error({"message": "InternalServerError", "data": ""}) is True
        assert _is_transient_raw_error({"message": "HTTP 503", "data": "Internal error"}) is True
        assert (
            _is_transient_raw_error(
                {"message": "please try again", "data": "AccessDeniedException"}
            )
            is False
        )

    def test_throttle_and_model_unavailable_are_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        assert _is_transient_raw_error({"data": "ThrottlingException: slow down"}) is True
        assert _is_transient_raw_error({"message": "Rate limit exceeded", "data": ""}) is True
        assert _is_transient_raw_error({"data": "The model 'opus' is not available."}) is True

    def test_auth_and_unknown_are_not_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # Auth is terminal — a retry can't fix an expired/denied credential.
        assert _is_transient_raw_error({"data": "AccessDeniedException: nope"}) is False
        assert _is_transient_raw_error({"data": "ExpiredTokenException"}) is False
        # Unknown shapes and non-dicts are terminal (fail-fast, not retry).
        assert _is_transient_raw_error({"data": "max_tokens 500 exceeded"}) is False
        # 501 Not Implemented is terminal — only 500/502/503/504 + 529 retry.
        assert _is_transient_raw_error({"data": "HTTP 501 Not Implemented"}) is False
        assert (
            _is_transient_raw_error({"code": -32603, "message": "Internal error", "data": ""})
            is False
        )
        assert _is_transient_raw_error(None) is False
        assert _is_transient_raw_error("boom") is False

    def test_session_expired_is_not_transient(self):
        """Regression test: kiro-cli session expiry must be terminal.

        These error shapes previously fell through to the 5xx branch (when they
        also carried DispatchFailure/ConnectionResetError), telling the user to
        retry when re-authentication was required.
        """
        from kiro_crew.acp.client import _is_transient_raw_error

        # Direct session-expired wording from kiro-cli.
        assert _is_transient_raw_error({"data": "session expired"}) is False
        assert _is_transient_raw_error({"data": "session has expired"}) is False
        assert _is_transient_raw_error({"data": "not logged in"}) is False
        assert _is_transient_raw_error({"data": "not authenticated"}) is False
        assert _is_transient_raw_error({"data": "login required"}) is False
        assert _is_transient_raw_error({"data": "authentication required"}) is False
        assert _is_transient_raw_error({"data": "re-authenticate"}) is False
        assert _is_transient_raw_error({"message": "session timed out", "data": ""}) is False
        # Session expiry with a co-occurring 5xx token: the session-expiry
        # branch must win (checked first).
        assert (
            _is_transient_raw_error(
                {"data": "DispatchFailure: session expired", "message": ""}
            )
            is False
        )
        assert (
            _is_transient_raw_error(
                {"data": "ConnectionResetError: not logged in", "message": ""}
            )
            is False
        )
        # A bare 401/403 — the shape an expired session actually arrives in.
        assert _is_transient_raw_error({"data": "HTTP 401"}) is False
        assert _is_transient_raw_error({"data": "HTTP 403"}) is False
        assert _is_transient_raw_error({"data": "status code 401"}) is False
        # 401 alongside the transport error left by the aborted request: the
        # 5xx family must not reclaim it and re-arm the retry ladder.
        assert (
            _is_transient_raw_error(
                {"data": "DispatchFailure ConnectionResetError: HTTP 401", "message": ""}
            )
            is False
        )
        # Real 5xx stays retryable — the auth-status branch must not overreach.
        assert _is_transient_raw_error({"data": "ServiceUnavailableException"}) is True
        assert _is_transient_raw_error({"data": "HTTP 503"}) is True

    def test_kiro_generic_generation_failure_is_transient(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # kiro-cli's pre-stream generation failure wrapper: no request_id, no
        # error class, none of the 5xx/throttle tokens. Retryable — this exact
        # shape was surfaced to the user with no retry during a model-capacity
        # blip.
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "Kiro failed to generate a response",
        }
        assert _is_transient_raw_error(err) is True

    def test_generation_failure_scoped_to_data_and_loses_to_auth(self):
        from kiro_crew.acp.client import _is_transient_raw_error

        # Phrase only in `message` (data carries a deterministic failure):
        # scoped match must not fire — stays terminal.
        assert (
            _is_transient_raw_error(
                {
                    "message": "Kiro failed to generate a response",
                    "data": "ValidationException: unsupported field",
                }
            )
            is False
        )
        # Auth is checked first and stays terminal even when the generation-
        # failure phrase co-occurs in data.
        assert (
            _is_transient_raw_error(
                {"data": "AccessDeniedException: Kiro failed to generate a response"}
            )
            is False
        )

    def test_raise_acp_error_carries_transient_flag_and_formatted_message(self):
        import pytest

        from kiro_crew.acp.client import AcpError, _raise_acp_error

        transient_err = {
            "code": -32603,
            "message": "Internal error",
            "data": "InternalServerError: please try again (request_id: abc-123)",
        }
        with pytest.raises(AcpError) as ei:
            _raise_acp_error(transient_err)
        exc = ei.value
        assert exc.transient is True
        # Message is the formatted, user-facing string (not the raw dict).
        assert "transient error (HTTP 5xx)" in str(exc)
        assert "abc-123" in str(exc)

        auth_err = {"code": -32603, "message": "Internal error", "data": "AccessDeniedException"}
        with pytest.raises(AcpError) as auth_ei:
            _raise_acp_error(auth_err)
        auth_exc = auth_ei.value
        assert auth_exc.transient is False
        assert "authentication failed" in str(auth_exc).lower()

    def test_acp_error_default_transient_is_none(self):
        from kiro_crew.acp.client import AcpError

        assert AcpError("plain").transient is None
        assert AcpError("flagged", transient=True).transient is True


class TestAcpClientDrainEarlyExit:
    """_drain_notifications exits early once MCP servers go quiet, bounded by
    the hard cap. Guards the TTFT optimization (idle early-exit) against
    regressing into either a full-cap wait or a premature exit while active."""

    def test_idle_exit_constant_below_duration_constant(self):
        # The idle early-exit only short-circuits the drain if it can fire
        # before the hard cap. If a future edit bumps _DRAIN_IDLE_EXIT to or
        # above _DRAIN_DURATION, the cap always fires first, the idle path
        # becomes dead code, and cold-start TTFT silently regresses to the full
        # cap. The default-bound drain tests pass explicit duration/idle_exit
        # args, so only this assertion guards the module constants themselves.
        assert _DRAIN_IDLE_EXIT < _DRAIN_DURATION, (
            "_DRAIN_IDLE_EXIT must stay strictly below _DRAIN_DURATION "
            "to keep the idle early-exit reachable"
        )

    @pytest.mark.asyncio
    async def test_drain_exits_early_when_quiet(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        # Pre-buffered notification (arrived during _wait_for_response) — exercises
        # the buffered-drain path in addition to the live-read path (CR feedback).
        client._mcp_notifications = [
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"name": "buffered-mcp"}
            ),
        ]

        # Two live MCP notifications, then quiet (None) forever.
        msgs = [
            JsonRpcMessage(
                method="_kiro.dev/mcp/server_initialized", params={"name": "builder-mcp"}
            ),
            JsonRpcMessage(method="_kiro.dev/mcp/server_initialized", params={"name": "node"}),
        ]
        calls = {"n": 0}

        async def fake_read(timeout):
            if calls["n"] < len(msgs):
                m = msgs[calls["n"]]
                calls["n"] += 1
                return m
            await asyncio.sleep(timeout)  # honor the poll timeout, return nothing
            return None

        client._read_message = fake_read  # type: ignore[assignment]

        start = time.monotonic()
        # Hard cap 10s; idle window 0.2s for a fast test.
        await client._drain_notifications(duration=10.0, idle_exit=0.2)
        elapsed = time.monotonic() - start

        # Tight window proves it was the IDLE exit that fired (~0.2s after the last
        # live message), not the 10s cap NOR an instant exit from a swallowed
        # exception / always-None read (CR feedback). Floor guards against the
        # idle logic being deleted; ceiling guards against the full-cap wait.
        assert 0.15 < elapsed < 1.0, f"expected ~0.2s idle exit, got {elapsed:.2f}s"
        assert calls["n"] == 2  # both LIVE notifications were drained
        # The buffered notification is consumed + the buffer cleared by the drain.
        assert client._mcp_notifications == []

    @pytest.mark.asyncio
    async def test_drain_respects_hard_cap_when_chatty(self, tmp_path):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        client._mcp_notifications = []

        # A server that never goes quiet — the cap must bound the wait.
        reads = {"n": 0}

        async def fake_read(timeout):
            reads["n"] += 1
            return JsonRpcMessage(method="_kiro.dev/mcp/progress", params={"name": "slow"})

        client._read_message = fake_read  # type: ignore[assignment]

        start = time.monotonic()
        await client._drain_notifications(duration=0.5, idle_exit=5.0)
        elapsed = time.monotonic() - start

        # Idle never triggers (always chatty), so the 0.5s cap bounds it.
        assert 0.4 < elapsed < 2.0, f"hard cap not respected (took {elapsed:.2f}s)"
        # Confirm the drain loop actually iterated (processed reads), not just
        # that the timer expired (CR feedback).
        assert reads["n"] > 0, "drain loop never read a message during the cap window"


class TestResolveKiroBinEnvOverride:
    """_resolve_kiro_bin honors the KIROCREW_KIRO_BIN override for environments
    (e.g. AgentSpaces/DevSpaces) where the toolbox shim is broken."""

    def test_env_override_used_when_valid(self, tmp_path):
        from kiro_crew.acp.client import _resolve_kiro_bin
        from kiro_crew.kiro_prerequisite import TrustedAcpExecutableSnapshot

        fake = tmp_path / "kiro-cli"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o755)
        with (
            patch(
                "kiro_crew.acp.client.resolve_kiro_cli",
                return_value=str(fake),
            ),
            patch(
                "kiro_crew.kiro_prerequisite.snapshot_trusted_acp_executable",
                return_value=TrustedAcpExecutableSnapshot("/immutable/kiro-cli"),
            ),
        ):
            assert _resolve_kiro_bin() == "/immutable/kiro-cli"

    def test_resolver_returns_installed_path_without_copying(self, tmp_path):
        # The resolver hands back the user's installed binary path itself. It must
        # never substitute a private copy: Kiro CLI 2.15+ exec's a sibling
        # subcommand binary resolved relative to its own path, so a copy into a
        # flat dir breaks every spawn with ENOENT.
        from kiro_crew.acp import client as client_module

        macos_dir = tmp_path / "Kiro CLI.app" / "Contents" / "MacOS"
        macos_dir.mkdir(parents=True)
        source = macos_dir / "kiro-cli"
        source.write_bytes(b"source")
        source.chmod(0o700)
        (macos_dir / "kiro-cli-chat").write_bytes(b"sibling")

        with patch.object(client_module, "resolve_kiro_cli", return_value=str(source)):
            launch_path = client_module._resolve_kiro_bin()

        assert launch_path == str(source)
        # The sibling the CLI dispatches to is still beside the launch path.
        assert (Path(launch_path).parent / "kiro-cli-chat").exists()

    def test_windows_resolver_accepts_runnable_candidate_anywhere(self, tmp_path):
        from kiro_crew.acp import client as client_module

        # Trust is "it runs": a Windows CLI outside Program Files (winget/scoop,
        # a venv Scripts dir) resolves for ACP launch rather than being rejected.
        candidate = tmp_path / "venv" / "Scripts" / "kiro-cli.exe"
        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"runnable")
        program_files = tmp_path / "Program Files"

        with (
            patch.object(client_module.platform_compat, "IS_WINDOWS", True),
            patch.object(
                client_module,
                "resolve_kiro_cli",
                return_value=str(candidate),
            ),
            patch.dict(
                "os.environ",
                {"ProgramFiles": str(program_files)},
                clear=True,
            ),
        ):
            assert client_module._resolve_kiro_bin() == os.path.realpath(str(candidate))

    @pytest.mark.asyncio
    async def test_spawn_launches_self_updated_override(self, tmp_path):
        from kiro_crew import sandbox as sandbox_module
        from kiro_crew.acp import client as client_module
        from kiro_crew.kiro_prerequisite import KiroPrerequisiteService

        fake = tmp_path / "kiro-cli"
        fake.write_bytes(b"#!/bin/sh\n# original\n")
        fake.chmod(0o755)
        mock_exec = AsyncMock()
        with (
            patch.dict("os.environ", {"KIROCREW_KIRO_BIN": str(fake)}),
            # This test asserts WHICH bytes get launched, not that the spawn is
            # sandboxed (covered by test_sandbox_*.py). A CI runner with
            # kernel.apparmor_restrict_unprivileged_userns=1 genuinely cannot
            # build a namespace sandbox and wrap_argv fail-closes by design, so
            # pin the decision instead of inheriting the host's capability.
            patch.object(sandbox_module, "_allow_unsandboxed_exec", return_value=True),
            patch.object(
                client_module,
                "resolve_kiro_cli",
                return_value=str(fake),
            ),
            patch(
                "asyncio.create_subprocess_exec",
                mock_exec,
            ),
        ):
            # A Kiro self-update legitimately rewrites the binary after gateway
            # start. Trust is "it runs", so the updated bytes still launch — the
            # snapshot just pins whatever bytes are resolved at spawn time.
            KiroPrerequisiteService(home=tmp_path, data_home=tmp_path / "data")
            fake.write_bytes(b"#!/bin/sh\n# self-updated\n")
            fake.chmod(0o755)

            client = AcpClient(work_dir=tmp_path / "workspace")
            await client._spawn()
            mock_exec.assert_awaited()

    @pytest.mark.asyncio
    async def test_spawn_passes_installed_path_through_exact_wrappers(self, tmp_path):
        from kiro_crew.acp import client as client_module

        fake = tmp_path / "kiro-cli"
        fake.write_bytes(b"#!/bin/sh\n")
        fake.chmod(0o755)
        launch_path = str(fake)
        mock_exec = AsyncMock(side_effect=RuntimeError("spawn failed"))
        wrapped: dict[str, object] = {}

        def capture_wrap(argv, mode, **kwargs):
            wrapped.update(argv=list(argv), mode=mode, kwargs=kwargs)
            return ["/usr/bin/sandbox-wrapper", *argv], None

        with (
            patch.object(
                client_module,
                "_resolve_kiro_bin",
                return_value=launch_path,
            ),
            patch.object(client_module, "wrap_argv", side_effect=capture_wrap),
            patch.object(
                client_module,
                "cgroup_scope_argv",
                side_effect=lambda argv: ["/usr/bin/cgroup-wrapper", *argv],
            ),
            patch(
                "asyncio.create_subprocess_exec",
                mock_exec,
            ),
        ):
            client = AcpClient(work_dir=tmp_path / "workspace")

            with pytest.raises(RuntimeError, match="spawn failed"):
                await client._spawn()

        launch_argv = wrapped["argv"]
        assert isinstance(launch_argv, list)
        assert launch_argv == [launch_path, "acp", "--agent", client._agent]
        assert wrapped["mode"] == "auto"
        assert wrapped["kwargs"] == {
            "strip_python_env": True,
            "is_kiro_cli": True,
        }
        spawn_call = mock_exec.await_args
        assert strip_spawn_shim(spawn_call.args) == (
            "/usr/bin/cgroup-wrapper",
            "/usr/bin/sandbox-wrapper",
            launch_path,
            "acp",
            "--agent",
            client._agent,
        )
        # No inherited snapshot descriptor: the installed binary is exec'd in
        # place, so there is nothing to hand down to the wrapper chain.
        assert "pass_fds" not in spawn_call.kwargs

    def test_env_override_ignored_when_missing_file(self, tmp_path):
        # A configured-but-nonexistent path must not be returned; resolution
        # falls through to the normal candidates / PATH lookup.
        from kiro_crew.acp.client import _resolve_kiro_bin

        missing = str(tmp_path / "does-not-exist")
        with (
            patch.dict("os.environ", {"KIROCREW_KIRO_BIN": missing}),
            patch(
                "kiro_crew.acp.client.resolve_kiro_cli",
                return_value=None,
            ),
        ):
            assert _resolve_kiro_bin() is None

    def test_env_override_ignored_when_not_executable(self, tmp_path):
        from kiro_crew.acp.client import _resolve_kiro_bin

        nonexec = tmp_path / "kiro-cli"
        nonexec.write_text("#!/bin/sh\n")
        nonexec.chmod(0o644)  # not executable
        with (
            patch.dict("os.environ", {"KIROCREW_KIRO_BIN": str(nonexec)}),
            patch(
                "kiro_crew.acp.client.resolve_kiro_cli",
                return_value=None,
            ),
        ):
            assert _resolve_kiro_bin() is None

    def test_no_env_falls_through(self):
        # With no override set, resolution uses the normal candidate/PATH path
        # and never returns the override sentinel.
        from kiro_crew.acp.client import _resolve_kiro_bin

        env = {k: v for k, v in os.environ.items() if k != "KIROCREW_KIRO_BIN"}
        with (
            patch.dict("os.environ", env, clear=True),
            patch(
                "kiro_crew.acp.client.resolve_kiro_cli",
                return_value=None,
            ),
        ):
            # Should not raise; returns either a real path or None.
            result = _resolve_kiro_bin()
            assert result is None or isinstance(result, str)


class TestDispatchSubagentEvents:
    """_dispatch_events must yield EVENT_SUBAGENT_LIST and EVENT_SUBAGENT_ACTIVITY
    for kiro-cli's _kiro.dev/subagent/list_update and _kiro.dev/session/update
    notifications."""

    @pytest.mark.asyncio
    async def test_dispatch_yields_subagent_list_and_activity(self):
        from kiro_crew.acp.types import (
            EVENT_SUBAGENT_ACTIVITY,
            EVENT_SUBAGENT_LIST,
            JsonRpcMessage,
        )

        client = AcpClient()

        subs = [{"sessionId": "s1", "role": "explorer", "status": {"type": "working"}}]
        frames = [
            ("subagent_list", JsonRpcMessage(params={"subagents": subs})),
            # tool_call_chunk → toolCallId attribution
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "tool_call_chunk",
                            "toolCallId": "tc-1",
                            "title": "read",
                        },
                    }
                ),
            ),
            # agent_message_chunk → sub-agent text
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "text": "hello from sub",
                        },
                    }
                ),
            ),
            # non-dict subagents payload must be ignored (no yield)
            ("subagent_list", JsonRpcMessage(params={"subagents": "nope"})),
            ("complete", JsonRpcMessage(result={"stopReason": "end_turn"})),
        ]

        async def _fake_loop(req_id, timeout):
            for f in frames:
                yield f

        client._prompt_loop = _fake_loop  # type: ignore[assignment]

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=1.0):
            events.append(ev)

        lists = [e for e in events if e.kind == EVENT_SUBAGENT_LIST]
        acts = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        assert len(lists) == 1
        assert lists[0].subagents == subs
        # one tool-call activity (with toolCallId) + one text activity
        tc = [a for a in acts if a.tool_call_id == "tc-1"]
        txt = [a for a in acts if a.text == "hello from sub"]
        assert len(tc) == 1 and tc[0].sub_session_id == "s1" and tc[0].title == "read"
        assert len(txt) == 1 and txt[0].sub_session_id == "s1"

    @pytest.mark.asyncio
    async def test_dispatch_redacts_and_extracts_subagent_output(self):
        """Sub-agent titles/text are LLM-influenced, so credentials + exfil URLs
        must be scrubbed before they surface as EVENT_SUBAGENT_ACTIVITY. Also
        the streamed text must be read from the nested ``content.text`` shape
        kiro-cli 2.10.0 emits, not only the flat top-level ``text`` field."""
        from kiro_crew.acp.types import (
            EVENT_SUBAGENT_ACTIVITY,
            JsonRpcMessage,
        )

        client = AcpClient()

        frames = [
            # tool_call_chunk with a credential embedded in the title
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "tool_call_chunk",
                            "toolCallId": "tc-1",
                            "title": "leak AKIAIOSFODNN7EXAMPLE now",
                        },
                    }
                ),
            ),
            # agent_message_chunk with the text under nested content.text (2.10.0)
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"text": "secret AKIAIOSFODNN7EXAMPLE here"},
                        },
                    }
                ),
            ),
            ("complete", JsonRpcMessage(result={"stopReason": "end_turn"})),
        ]

        async def _fake_loop(req_id, timeout):
            for f in frames:
                yield f

        client._prompt_loop = _fake_loop  # type: ignore[assignment]

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=1.0):
            events.append(ev)

        acts = [e for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY]
        tc = [a for a in acts if a.tool_call_id == "tc-1"]
        txt = [a for a in acts if a.text]
        # Title redacted (credential scrubbed, no raw AKIA leaks through).
        assert len(tc) == 1
        assert "AKIAIOSFODNN7EXAMPLE" not in tc[0].title
        assert "[REDACTED" in tc[0].title
        # Nested content.text was extracted (robustness) AND redacted (security).
        assert len(txt) == 1 and txt[0].sub_session_id == "s1"
        assert "AKIAIOSFODNN7EXAMPLE" not in txt[0].text
        assert "[REDACTED" in txt[0].text

    @pytest.mark.asyncio
    async def test_dispatch_subagent_text_edge_cases(self):
        """Regression guards for the nested-vs-flat content.text extraction:
        (1) an EMPTY nested content.text must not shadow a populated flat
        top-level ``text`` (older payloads); (2) a reasoning/thinking content
        block must NOT surface as user-visible sub-agent output; (3) a non-dict
        ``content`` must not raise (the flat pre-port read tolerated it)."""
        from kiro_crew.acp.types import (
            EVENT_SUBAGENT_ACTIVITY,
            JsonRpcMessage,
        )

        client = AcpClient()

        frames = [
            # (1) empty nested content.text + populated flat text -> flat wins
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"text": ""},
                            "text": "flat fallback output",
                        },
                    }
                ),
            ),
            # (2) reasoning/thinking content block -> suppressed (no user text)
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": {"type": "thinking", "text": "internal reasoning"},
                        },
                    }
                ),
            ),
            # (3) non-dict content -> must not crash; flat text used if present
            (
                "subagent_activity",
                JsonRpcMessage(
                    params={
                        "sessionId": "s1",
                        "update": {
                            "sessionUpdate": "agent_message_chunk",
                            "content": ["a", "list", "block"],
                            "text": "flat when content is a list",
                        },
                    }
                ),
            ),
            ("complete", JsonRpcMessage(result={"stopReason": "end_turn"})),
        ]

        async def _fake_loop(req_id, timeout):
            for f in frames:
                yield f

        client._prompt_loop = _fake_loop  # type: ignore[assignment]

        events = []
        async for ev in client._dispatch_events(req_id=1, timeout=1.0):
            events.append(ev)

        txt = [e.text for e in events if e.kind == EVENT_SUBAGENT_ACTIVITY and e.text]
        # (1) flat fallback surfaced when nested content.text is empty.
        assert "flat fallback output" in txt
        # (2) the reasoning block was never surfaced as sub-agent output.
        assert "internal reasoning" not in txt
        # (3) non-dict content did not crash and the flat text still surfaced.
        assert "flat when content is a list" in txt


class TestTrackMetadataCredits:
    """_track_metadata extracts per-turn credits from kiro meteringUsage."""

    @staticmethod
    def _metadata_msg(credit_values, *, pct=17.1):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={
                "contextUsagePercentage": pct,
                "meteringUsage": [
                    {"value": v, "unit": "credit", "unitPlural": "credits"} for v in credit_values
                ],
            },
        )

    def test_accumulates_credits_from_metering_usage(self):
        client = AcpClient()
        client._track_metadata(self._metadata_msg([0.63]))
        assert client.last_prompt_stats.credits == pytest.approx(0.63)
        assert client.last_prompt_stats.context_pct == pytest.approx(17.1)

    def test_sums_credits_across_notifications(self):
        client = AcpClient()
        client._track_metadata(self._metadata_msg([0.5]))
        client._track_metadata(self._metadata_msg([0.25, 0.25]))
        assert client.last_prompt_stats.credits == pytest.approx(1.0)

    def test_ignores_non_credit_units_and_bad_values(self):
        client = AcpClient()
        msg = self._metadata_msg([])
        msg.params["meteringUsage"] = [
            {"value": 5, "unit": "token"},
            {"value": "x", "unit": "credit"},
            {"value": 0.4, "unit": "credit"},
        ]
        client._track_metadata(msg)
        assert client.last_prompt_stats.credits == pytest.approx(0.4)

    def test_metadata_without_metering_usage_leaves_credits_zero(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={"contextUsagePercentage": 12.5},
            )
        )
        assert client.last_prompt_stats.credits == 0.0
        assert client.last_prompt_stats.context_pct == pytest.approx(12.5)


class TestTrackMetadataWindowResolution:
    """_track_metadata derives context_window_tokens from the resolved model
    when no usage_update has set it (kiro-cli 2.10+ no longer sends one)."""

    @staticmethod
    def _metadata_msg(pct):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"contextUsagePercentage": pct},
        )

    def test_resolves_1m_window_from_resolved_model_id(self):
        client = AcpClient()
        assert client._model == "auto"
        client._resolved_model_id = "claude-opus-4.6"
        client._track_metadata(self._metadata_msg(25.0))
        assert client.last_prompt_stats.context_window_tokens == 1_000_000
        assert client.last_prompt_stats.context_used_tokens == 250_000
        assert client.last_prompt_stats.context_pct == pytest.approx(25.0)

    def test_resolves_200k_window_for_200k_model(self):
        client = AcpClient()
        client._resolved_model_id = "claude-opus-4.5"
        client._track_metadata(self._metadata_msg(50.0))
        assert client.last_prompt_stats.context_window_tokens == 200_000
        assert client.last_prompt_stats.context_used_tokens == 100_000

    def test_falls_back_to_self_model_when_resolved_model_id_unset(self):
        client = AcpClient()
        client._model = "claude-sonnet-4.6"
        client._track_metadata(self._metadata_msg(10.0))
        assert client.last_prompt_stats.context_window_tokens == 1_000_000

    def test_does_not_overwrite_window_already_set_by_usage_update(self):
        client = AcpClient()
        client._resolved_model_id = "claude-opus-4.5"  # would resolve to 200K
        # Simulate a real usage_update having established authoritative counts
        # (1M window, 300K used -> 30%). context_tokens_from_usage marks them
        # authoritative — the shape a real usage_update leaves behind.
        client.last_prompt_stats.context_window_tokens = 1_000_000
        client.last_prompt_stats.context_used_tokens = 300_000
        client.last_prompt_stats.context_pct = 30.0
        client.last_prompt_stats.context_tokens_from_usage = True
        client._track_metadata(self._metadata_msg(50.0))
        # Metadata must neither re-resolve the window (no backfill once
        # authoritative) nor clobber the usage-derived pct to its own 50%.
        assert client.last_prompt_stats.context_window_tokens == 1_000_000
        assert client.last_prompt_stats.context_pct == pytest.approx(30.0)

    def test_no_model_resolvable_leaves_window_zero(self):
        client = AcpClient()
        client._model = ""
        client._resolved_model_id = None
        client._track_metadata(self._metadata_msg(5.0))
        assert client.last_prompt_stats.context_window_tokens == 0

    def test_non_registry_uncached_model_leaves_window_zero_not_200k(self):
        # REGRESSION (gpt-5.6-terra showed 200K): a model that is NEITHER in the
        # registry NOR the kiro-list cache must NOT be backfilled to a guessed
        # 200k — that would override the frontend's authoritative per-model
        # window. Leave it 0 so the frontend cache drives the meter; kiro's real
        # usage_update.size still sets the correct window when it comes. Use a
        # clearly-synthetic id so a real kiro-list cache (which legitimately
        # carries GPT/DeepSeek windows) can't make this model "known".
        client = AcpClient()
        client._resolved_model_id = "totally-unknown-model-xyz"
        client._track_metadata(self._metadata_msg(10.0))
        assert client.last_prompt_stats.context_window_tokens == 0
        # pct is still recorded so the frontend can derive used from its window.
        assert client.last_prompt_stats.context_pct == pytest.approx(10.0)

    def test_kiro_cached_non_registry_model_backfills_real_window(self):
        # The flip side of the centralization: once the kiro-list cache is seeded
        # with a non-Anthropic model's real window, the backfill SHOULD report it
        # (it is now a "known" window), instead of the old no-op. This is what
        # lets GPT/DeepSeek sessions show their real window before a usage_update.
        import kiro_crew.model_registry as mr

        saved = dict(mr._KIRO_WINDOWS)
        try:
            mr._KIRO_WINDOWS["deepseek-3.2"] = 164000
            client = AcpClient()
            client._resolved_model_id = "deepseek-3.2"
            client._track_metadata(self._metadata_msg(50.0))
            assert client.last_prompt_stats.context_window_tokens == 164000
            assert client.last_prompt_stats.context_used_tokens == 82000
        finally:
            mr._KIRO_WINDOWS.clear()
            mr._KIRO_WINDOWS.update(saved)

    def test_malformed_pct_does_not_raise_or_update_stats(self):
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient()
        client._resolved_model_id = "claude-opus-4.6"
        client.last_prompt_stats.context_pct = 42.0
        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata", params={"contextUsagePercentage": "not-a-number"}
            )
        )
        assert client.last_prompt_stats.context_pct == 42.0
        assert client.last_prompt_stats.context_window_tokens == 0


class TestSubstitutionAdvisory:
    """The model-substitution advisory (-32603 'Using X instead') is non-fatal.

    claude-agent-acp returns a JSON-RPC -32603 error frame on session/new (and
    related calls) when admin-tier / headless-tier policy substitutes the
    requested model for another. The substitute is already live, so KiroCrew
    must treat the advisory as a warning and keep the session, NOT raise.
    """

    def test_advisory_detected(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": {
                "details": (
                    'Model "opus" is restricted by your organization\'s '
                    "settings. Using global.anthropic.claude-sonnet-4-6[1m] "
                    "instead."
                )
            },
        }
        assert _is_model_substitution_advisory(err) is True

    def test_advisory_detected_data_is_plain_string(self):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": 'Model "x" is restricted ... Using y instead.',
        }
        assert _is_model_substitution_advisory(err) is True

    def test_real_internal_error_still_raises(self):
        # A genuine -32603 with no substitution wording must NOT be swallowed.
        err = {"code": -32603, "message": "Internal error", "data": "request_id: abc-123"}
        assert _is_model_substitution_advisory(err) is False

    def test_throttle_not_treated_as_advisory(self):
        err = {
            "code": -32603,
            "message": "ThrottlingException: rate exceeded",
            "data": "rate limit",
        }
        assert _is_model_substitution_advisory(err) is False

    def test_wrong_code_not_advisory(self):
        # Right wording, wrong code -> still raise (only -32603 qualifies).
        err = {
            "code": -32602,
            "message": "Invalid params",
            "data": {"details": 'Model "x" is restricted ... Using y instead'},
        }
        assert _is_model_substitution_advisory(err) is False

    def test_non_dict_not_advisory(self):
        assert _is_model_substitution_advisory("plain string") is False
        assert _is_model_substitution_advisory(None) is False

    def test_empty_or_missing_data_not_advisory(self):
        assert (
            _is_model_substitution_advisory({"code": -32603, "message": "Internal error"}) is False
        )
        assert _is_model_substitution_advisory({"code": -32603, "data": ""}) is False

    def test_partial_wording_not_advisory(self):
        # "is restricted" without the "Using X instead" half must not match.
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": {"details": 'Model "opus" is restricted by your settings.'},
        }
        assert _is_model_substitution_advisory(err) is False

    @pytest.mark.asyncio
    async def test_wait_for_response_returns_result_on_advisory(self, tmp_path):
        """_wait_for_response returns the result (no raise) on a substitution advisory."""
        client = AcpClient(work_dir=tmp_path)
        advisory = {
            "code": -32603,
            "message": "Internal error",
            "data": {
                "details": (
                    'Model "opus" is restricted by your organization\'s '
                    "settings. Using global.anthropic.claude-sonnet-4-6[1m] instead."
                )
            },
        }
        msg = MagicMock()
        msg.is_response_for.return_value = True
        msg.error = advisory
        msg.result = {"sessionId": "abc-123"}
        msg.method = None
        msg.id = 7

        async def _one_message(timeout=0.0):
            return msg

        client._read_message = _one_message  # type: ignore[assignment]
        out = await client._wait_for_response(7, timeout=1.0)
        assert out == {"sessionId": "abc-123"}

    @pytest.mark.asyncio
    async def test_wait_for_response_raises_on_real_error(self, tmp_path):
        """A genuine -32603 (no substitution wording) still raises AcpError."""
        client = AcpClient(work_dir=tmp_path)
        real_err = {"code": -32603, "message": "Internal error", "data": "request_id: zzz"}
        msg = MagicMock()
        msg.is_response_for.return_value = True
        msg.error = real_err
        msg.result = None
        msg.method = None
        msg.id = 9

        async def _one_message(timeout=0.0):
            return msg

        client._read_message = _one_message  # type: ignore[assignment]
        with pytest.raises(AcpError):
            await client._wait_for_response(9, timeout=1.0)

    @pytest.mark.asyncio
    async def test_wait_for_response_records_substitute_on_error_only_advisory(self, tmp_path):
        """Spec-real advisory frame: error set, result is None.

        A spec-compliant JSON-RPC response carries either result or error,
        not both. On the realistic error-only advisory (no co-located result):
          1. ``_wait_for_response`` must NOT raise; it returns ``{}``.
          2. It MUST record the gateway-named substitute model on
             ``self._last_substitution_model`` so the session/new caller can
             adopt it and re-issue creation. Without this, ``_session_id``
             would silently become None and the next call would crash on the
             ``Cannot set config option before session is initialized`` guard.
        """
        client = AcpClient(work_dir=tmp_path)
        advisory = {
            "code": -32603,
            "message": "Internal error",
            "data": {
                "details": (
                    'Model "opus" is restricted by your organization\'s '
                    "settings. Using global.anthropic.claude-sonnet-4-6[1m] instead."
                )
            },
        }
        msg = MagicMock()
        msg.is_response_for.return_value = True
        msg.error = advisory
        msg.result = None  # spec-real: error-only frame, no co-located result
        msg.method = None
        msg.id = 11

        async def _one_message(timeout=0.0):
            return msg

        client._read_message = _one_message  # type: ignore[assignment]
        out = await client._wait_for_response(11, timeout=1.0)

        assert out == {}, "advisory must surface as empty dict, not raise"
        assert (
            client._last_substitution_model == "global.anthropic.claude-sonnet-4-6[1m]"
        ), "substitute model must be recorded for the session/new caller to adopt"


class TestSubstituteModelExtractor:
    """_substitute_model_from_advisory pulls the gateway-served model id out of
    the -32603 advisory so the session/new caller can adopt it."""

    def test_extracts_versioned_substitute(self):
        err = {
            "code": -32603,
            "data": {
                "details": (
                    'Model "opus" is restricted by your organization\'s '
                    "settings. Using global.anthropic.claude-sonnet-4-6[1m] instead."
                )
            },
        }
        assert _substitute_model_from_advisory(err) == "global.anthropic.claude-sonnet-4-6[1m]"

    def test_extracts_with_trailing_period_and_quotes(self):
        err = {
            "code": -32603,
            "data": {"details": "Model X is restricted. Using claude-opus-4-6[1m] instead."},
        }
        assert _substitute_model_from_advisory(err) == "claude-opus-4-6[1m]"

    def test_returns_none_for_real_internal_error(self):
        err = {"code": -32603, "data": "request_id: abc-123"}
        assert _substitute_model_from_advisory(err) is None

    def test_returns_none_for_wrong_code(self):
        err = {"code": -32602, "data": {"details": "is restricted ... Using y instead"}}
        assert _substitute_model_from_advisory(err) is None

    def test_returns_none_for_non_dict(self):
        assert _substitute_model_from_advisory(None) is None
        assert _substitute_model_from_advisory("plain") is None


class TestSubstitutionFollow:
    """session/new returning a substitution advisory carries NO sessionId.
    _new_session_following_substitution must adopt the substitute model and
    re-issue session/new so a real session is actually created -- otherwise the
    next step (set_config_option) crashes on the uninitialized-session guard.
    This is the no-sessionId path the earlier suite did not cover.
    """

    def _advisory_resp(self):
        # Mirrors what _wait_for_response returns on the advisory: empty dict,
        # plus it sets _last_substitution_model as a side effect (simulated here).
        return {}

    @pytest.mark.asyncio
    async def test_adopts_substitute_and_reissues(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._model = "global.anthropic.claude-opus-4-8[1m]"
        # Avoid touching the real filesystem seeding.
        client._write_claude_local_settings = MagicMock()  # type: ignore[assignment]

        sent = []

        async def _send(method, params):
            sent.append(method)
            return len(sent)

        # First wait: advisory (no sessionId) + record substitute, as the real
        # _wait_for_response does. Second wait: real session on the substitute.
        calls = {"n": 0}

        async def _wait(req_id, timeout=0.0):
            calls["n"] += 1
            if calls["n"] == 1:
                client._last_substitution_model = "global.anthropic.claude-sonnet-4-6[1m]"
                return {}  # advisory frame -> no sessionId
            return {"sessionId": "real-sess-1"}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        resp = await client._new_session_following_substitution()

        assert resp.get("sessionId") == "real-sess-1"
        # Adopted the gateway-served model and re-seeded settings before retry.
        assert client._model == "global.anthropic.claude-sonnet-4-6[1m]"
        client._write_claude_local_settings.assert_called_once()
        # Exactly two session/new issues: the original + one retry (bounded).
        assert sent.count("session/new") == 2

    @pytest.mark.asyncio
    async def test_happy_path_no_retry(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._model = "global.anthropic.claude-sonnet-4-6[1m]"
        client._write_claude_local_settings = MagicMock()  # type: ignore[assignment]

        sent = []

        async def _send(method, params):
            sent.append(method)
            return len(sent)

        async def _wait(req_id, timeout=0.0):
            return {"sessionId": "sess-ok"}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        resp = await client._new_session_following_substitution()

        assert resp.get("sessionId") == "sess-ok"
        # No substitution -> no re-seed, single session/new.
        client._write_claude_local_settings.assert_not_called()
        assert sent.count("session/new") == 1

    @pytest.mark.asyncio
    async def test_no_infinite_retry_when_substitute_unparseable(self, tmp_path):
        # Advisory fired but substitute could not be parsed (None) -> we do NOT
        # retry; caller gets an empty resp and raises a clear error upstream.
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._model = "global.anthropic.claude-opus-4-8[1m]"
        client._write_claude_local_settings = MagicMock()  # type: ignore[assignment]

        sent = []

        async def _send(method, params):
            sent.append(method)
            return len(sent)

        async def _wait(req_id, timeout=0.0):
            client._last_substitution_model = None  # unparseable
            return {}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        resp = await client._new_session_following_substitution()

        assert resp.get("sessionId") is None
        client._write_claude_local_settings.assert_not_called()
        assert sent.count("session/new") == 1  # no retry

    @pytest.mark.asyncio
    async def test_no_retry_when_substitute_same_as_current(self, tmp_path):
        # Defensive: if the gateway "substitutes" to the model we already asked
        # for, don't loop -- treat as terminal (no retry).
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._model = "global.anthropic.claude-sonnet-4-6[1m]"
        client._write_claude_local_settings = MagicMock()  # type: ignore[assignment]

        sent = []

        async def _send(method, params):
            sent.append(method)
            return len(sent)

        async def _wait(req_id, timeout=0.0):
            client._last_substitution_model = "global.anthropic.claude-sonnet-4-6[1m]"
            return {}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        resp = await client._new_session_following_substitution()

        assert resp.get("sessionId") is None
        client._write_claude_local_settings.assert_not_called()
        assert sent.count("session/new") == 1


class TestSubstitutionWrappersAndRedaction:
    """Pin the wrapper raise paths and the dual-redaction discipline applied to
    backend-derived strings before they hit logger sinks (which fan out to the
    dashboard activity feed and Slack).
    """

    @pytest.mark.asyncio
    async def test_initialize_session_raises_on_no_sessionid(self, tmp_path):
        """If the helper still returns no sessionId after the substitution-follow
        retry, _initialize_session raises AcpError loudly instead of letting the
        next handshake step crash on the opaque "Cannot set config option before
        session is initialized" guard.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._process = MagicMock()
        client._process.returncode = None

        async def _send(method, params):
            return 1

        async def _wait(req_id, timeout=0.0):
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        async def _no_session(*args, **kwargs):
            return {}

        client._new_session_following_substitution = _no_session  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._initialize_session()
        assert "session/new returned no sessionId" in str(excinfo.value)
        assert "the backend did not create a session" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_initialize_session_message_omits_substitute_when_unchanged(self, tmp_path):
        """When the helper returns no sessionId and self._model is unchanged
        (no substitution happened), the AcpError text MUST NOT include the
        "even after adopting substitute model" clause.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._process = MagicMock()
        client._process.returncode = None
        client._model = "global.anthropic.claude-opus-4-8[1m]"

        async def _send(method, params):
            return 1

        async def _wait(req_id, timeout=0.0):
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        async def _no_session_no_substitution(*args, **kwargs):
            # Helper returned without changing self._model -> no substitution.
            return {}

        client._new_session_following_substitution = _no_session_no_substitution  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._initialize_session()
        assert "even after adopting substitute model" not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_initialize_session_message_includes_substitute_when_changed(self, tmp_path):
        """When the helper adopted a substitute (self._model != model_before),
        the AcpError text MUST include the "even after adopting substitute
        model {...}" clause.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._process = MagicMock()
        client._process.returncode = None
        client._model = "global.anthropic.claude-opus-4-8[1m]"

        async def _send(method, params):
            return 1

        async def _wait(req_id, timeout=0.0):
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        async def _no_session_with_substitution(*args, **kwargs):
            # Helper adopted the gateway-served substitute before giving up.
            client._model = "global.anthropic.claude-sonnet-4-6[1m]"
            return {}

        client._new_session_following_substitution = _no_session_with_substitution  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._initialize_session()
        assert "even after adopting substitute model" in str(excinfo.value)
        assert "claude-sonnet-4-6" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_new_session_following_substitution_redacts_log_payload(self, tmp_path):
        """The substitute model logged in _new_session_following_substitution
        MUST flow through redact_exfiltration_urls + redact_credentials before
        reaching logger.warning. Drives the helper with a substitute that
        contains a URL-shaped token and asserts the emitted log payload was
        redacted.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._model = "global.anthropic.claude-opus-4-8[1m]"
        client._write_claude_local_settings = MagicMock()  # type: ignore[assignment]

        # Inject a substitute that embeds an AWS access-key-shaped substring
        # so the dual redactor heuristic actually fires (redact_exfiltration_urls
        # matches the URL because the query carries an AKIA token, and
        # redact_credentials matches the standalone AKIA pattern). A bare
        # arbitrary URL is too tame for the heuristic and would leave the test
        # asserting on a no-op pass-through.
        url_shaped = "http://attacker.example.com/exfil?token=AKIAIOSFODNN7EXAMPLE"

        sent: list[str] = []

        async def _send(method, params):
            sent.append(method)
            return len(sent)

        wait_calls = {"n": 0}

        async def _wait(req_id, timeout=0.0):
            wait_calls["n"] += 1
            if wait_calls["n"] == 1:
                client._last_substitution_model = url_shaped
                return {}  # no sessionId on first call
            return {"sessionId": "real-sess"}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        with patch("kiro_crew.acp.client.logger") as mock_logger:
            await client._new_session_following_substitution()

        # Find the warning call that mentions the substitution adoption.
        adopt_calls = [
            c
            for c in mock_logger.warning.call_args_list
            if c.args and "adopting gateway-served model" in c.args[0]
        ]
        assert adopt_calls, "expected an 'adopting gateway-served model' warning"
        # The redacted payload is the second positional argument.
        emitted = adopt_calls[0].args[1]
        # The credential MUST be gone. The URL MUST be replaced with the
        # redactor diagnostic marker. The redactor preserves the host name
        # inside "[REDACTED: suspicious URL to <host>]" so operators can
        # investigate the leak source -- that is BY DESIGN, not a bypass.
        assert (
            "AKIAIOSFODNN7EXAMPLE" not in emitted
        ), "AWS access-key substring MUST be redacted before logging"
        assert (
            "REDACTED" in emitted
        ), "the redactor marker MUST appear, proving the URL was rewritten"
        assert (
            "http://attacker.example.com/exfil?token=" not in emitted
        ), "the bare exfil URL MUST NOT be emitted verbatim"

    @pytest.mark.asyncio
    async def test_initialize_session_redacts_model_in_info_log(self, tmp_path):
        """The "ACP session created: ... (model=...)" info log MUST flow
        self._model through dual-redaction. Drive _initialize_session through
        the happy path with a URL-shaped self._model and confirm the emitted
        info log does not leak it.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._process = MagicMock()
        client._process.returncode = None
        # AWS access-key-shaped substring guarantees both redactors fire (URL
        # query matches _EXFIL_PATTERNS; standalone key matches _CREDENTIAL_PATTERNS).
        client._model = "http://attacker.example.com/m?token=AKIAIOSFODNN7EXAMPLE"

        async def _send(method, params):
            return 1

        async def _wait(req_id, timeout=0.0):
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        async def _ok_session(*args, **kwargs):
            return {"sessionId": "sess-ok"}

        client._new_session_following_substitution = _ok_session  # type: ignore[assignment]
        client._capture_available_models = MagicMock()  # type: ignore[assignment]
        client._store_session_config = MagicMock()  # type: ignore[assignment]

        async def _drain():
            return None

        client._drain_notifications = _drain  # type: ignore[assignment]

        with patch("kiro_crew.acp.client.logger") as mock_logger:
            await client._initialize_session()

        info_calls = [
            c
            for c in mock_logger.info.call_args_list
            if c.args and "ACP session created" in c.args[0]
        ]
        assert info_calls, "expected an 'ACP session created' info log"
        # Format: logger.info("ACP session created: %s (model=%s)", sid, model_log)
        # The third positional arg is the redacted model.
        emitted_model = info_calls[0].args[2]
        # See sibling test for the design note: redactor keeps the host as a
        # diagnostic label inside "[REDACTED: suspicious URL to <host>]".
        assert (
            "AKIAIOSFODNN7EXAMPLE" not in emitted_model
        ), "AWS access-key substring MUST be redacted before logging"
        assert (
            "REDACTED" in emitted_model
        ), "the redactor marker MUST appear, proving the URL was rewritten"
        assert (
            "http://attacker.example.com/m?token=" not in emitted_model
        ), "the bare exfil URL MUST NOT be emitted verbatim"

    @pytest.mark.asyncio
    async def test_initialize_session_acperror_redacts_substitute_model(self, tmp_path):
        """The AcpError raised on no-sessionId-after-substitution MUST flow
        self._model through dual-redaction before interpolating it into the
        message. AcpError propagates to the dashboard activity feed and Slack
        via the same path as the logger sinks, so the redaction discipline
        applies to exception messages too -- not just logger.* calls.
        """
        client = AcpClient(work_dir=tmp_path)
        client._acp_backend = ACP_BACKEND_CLAUDE
        client._process = MagicMock()
        client._process.returncode = None
        client._model = "global.anthropic.claude-opus-4-8[1m]"

        async def _send(method, params):
            return 1

        async def _wait(req_id, timeout=0.0):
            return {"protocolVersion": 1, "agentCapabilities": {"loadSession": False}}

        client._send_request = _send  # type: ignore[assignment]
        client._wait_for_response = _wait  # type: ignore[assignment]

        async def _no_session_with_credshaped(*args, **kwargs):
            # Helper adopted a credential-shaped substitute then gave up without
            # producing a sessionId. Drives the "even after adopting substitute
            # model X" branch where X is credential-shaped.
            client._model = "AKIAIOSFODNN7EXAMPLE"
            return {}

        client._new_session_following_substitution = _no_session_with_credshaped  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._initialize_session()

        msg = str(excinfo.value)
        assert (
            "AKIAIOSFODNN7EXAMPLE" not in msg
        ), "AWS access-key-shaped substitute MUST be redacted before reaching the AcpError message"
        assert "even after adopting substitute model" in msg

    @pytest.mark.asyncio
    async def test_wait_for_response_catchall_raise_redacts_msg_error(self, tmp_path):
        """The catch-all `raise AcpError(f"JSON-RPC error: {msg.error}")` MUST
        flow msg.error through dual-redaction before f-string-interpolating it
        into the exception message.
        """
        client = AcpClient(work_dir=tmp_path)
        real_err = {
            "code": -32603,
            "message": "Internal error",
            "data": "request_id: AKIAIOSFODNN7EXAMPLE backend trace failed",
        }
        msg = MagicMock()
        msg.is_response_for.return_value = True
        msg.error = real_err
        msg.result = None
        msg.method = None
        msg.id = 13

        async def _one_message(timeout=0.0):
            return msg

        client._read_message = _one_message  # type: ignore[assignment]

        with pytest.raises(AcpError) as excinfo:
            await client._wait_for_response(13, timeout=1.0)

        emitted = str(excinfo.value)
        assert "AKIAIOSFODNN7EXAMPLE" not in emitted, (
            "AWS access-key-shaped substring in msg.error MUST be redacted "
            "before reaching the catch-all AcpError message"
        )
        assert "JSON-RPC error" in emitted, "the catch-all AcpError prefix MUST still be present"


class TestAcpClientIsShellSignal:
    """The canonical is_shell flow: the ACP shell kind ("execute") must be
    captured at the tool_call boundary and inherited by the later
    permission_request event (which carries no reliable kind), so the dashboard
    exempts long shell command titles from the 256-char cap. Regression for the
    empty-kind permission path (long shell commands rejected as
    "Tool name exceeds max length 256").
    """

    def _tool_call_msg(self, kind, tool_call_id="tc-1", command="echo hi"):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": tool_call_id,
                    "title": "Running a very long command",
                    "kind": kind,
                    "rawInput": {"command": command},
                }
            },
        )

    def _permission_msg(self, tool_call_id="tc-1", kind=None):
        from kiro_crew.acp.types import JsonRpcMessage

        tool_call = {"toolCallId": tool_call_id, "title": "x" * 400}
        # By default no "kind" on the permission payload — mirrors live
        # behaviour where the request_permission toolCall carries an empty
        # kind. Pass kind=... to exercise the payload-kind fallback branch.
        if kind is not None:
            tool_call["kind"] = kind
        return JsonRpcMessage(
            id=42,
            method="session/request_permission",
            params={
                "toolCall": tool_call,
                "options": [{"optionId": "allow_once", "name": "Allow once"}],
            },
        )

    def _refinement_msg(self, kind, tool_call_id="tc-1", command="echo hi"):
        from kiro_crew.acp.types import JsonRpcMessage

        update = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": tool_call_id,
            "title": "refined title",
            "rawInput": {"command": command},
        }
        # kind is optional on a refinement; only include it when provided so
        # tests can exercise the no-clobber path (kind absent).
        if kind is not None:
            update["kind"] = kind
        return JsonRpcMessage(method="session/update", params={"update": update})

    def test_execute_kind_marks_event_is_shell(self, tmp_path):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        ev = client._extract_tool_event(self._tool_call_msg("execute"))
        assert ev is not None
        assert ev.is_shell is True
        assert client._tool_call_is_shell.get("tc-1") is True

    def test_non_shell_kind_not_is_shell(self, tmp_path):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        ev = client._extract_tool_event(self._tool_call_msg("edit"))
        assert ev is not None
        assert ev.is_shell is False

    def test_permission_event_inherits_is_shell_from_tool_call(self, tmp_path):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        # 1. tool_call notification arrives first (kind="execute")
        client._extract_tool_event(self._tool_call_msg("execute"))
        # 2. permission request for the SAME toolCallId carries no kind
        perm = client._build_permission_event(self._permission_msg())
        assert perm.is_shell is True  # inherited via the toolCallId cache

    def test_permission_event_non_shell_stays_false(self, tmp_path):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        client._extract_tool_event(self._tool_call_msg("edit"))
        perm = client._build_permission_event(self._permission_msg())
        assert perm.is_shell is False

    def test_permission_event_inherits_trusted_tool_identity(self, tmp_path):
        # The client caches the trusted _meta.kiro identity (mcpServerName +
        # toolName) from the tool_call so the later permission event (which
        # carries no _meta) can rebuild mcp__<server>__<tool> for the
        # app-own-server auto-approve's per-tool governance. Without the
        # tool_name cache the permission event's tool_name is always "" and the
        # auto-approve fails closed (never fires).
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import JsonRpcMessage

        client = AcpClient(work_dir=tmp_path)
        tc = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-1",
                    "title": "Doing app work",  # prose, not the canonical mcp__ form
                    "kind": "other",
                    "rawInput": {},
                    "_meta": {
                        "kiro": {
                            "toolName": "list_intakes",
                            "mcpServerName": "beehive:beehive-mcp",
                        }
                    },
                }
            },
        )
        client._extract_tool_event(tc)
        perm = client._build_permission_event(self._permission_msg())
        assert perm.tool_name == "list_intakes"
        assert perm.mcp_server_name == "beehive:beehive-mcp"

    def test_end_to_end_long_shell_title_passes_validation(self, tmp_path):
        """The full regression: a 400-char shell title validates only because
        is_shell propagated from tool_call → permission → _validate_tool_name."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.dashboard.chat_utils import _validate_tool_name

        client = AcpClient(work_dir=tmp_path)
        client._extract_tool_event(self._tool_call_msg("execute"))
        perm = client._build_permission_event(self._permission_msg())
        # Mirrors the chat_runner call site.
        assert _validate_tool_name(perm.title, is_shell=perm.is_shell) == perm.title

    def test_refinement_kindless_does_not_clobber_cached_shell(self, tmp_path):
        """A refinement update that omits `kind` must NOT overwrite the
        is_shell=True cached by the initial tool_call notification (kind is
        optional on updates). Regression for the no-clobber invariant."""
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        # 1. initial tool_call carries kind="execute" → caches True
        client._extract_tool_event(self._tool_call_msg("execute"))
        # 2. refinement arrives WITHOUT a kind — must preserve the cached True
        ev = client._extract_tool_call_refinement(self._refinement_msg(None))
        assert ev is not None
        assert ev.is_shell is True
        assert client._tool_call_is_shell.get("tc-1") is True

    def test_refinement_with_kind_refreshes_shell_signal(self, tmp_path):
        """A refinement that DOES carry a kind refreshes the cached signal."""
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        # initial tool_call had no shell kind (edit) → cached False
        client._extract_tool_event(self._tool_call_msg("edit"))
        # refinement now reports kind="execute" → must flip cache to True
        ev = client._extract_tool_call_refinement(self._refinement_msg("execute"))
        assert ev is not None
        assert ev.is_shell is True
        assert client._tool_call_is_shell.get("tc-1") is True

    def test_permission_payload_kind_is_not_trusted_on_cache_miss(self, tmp_path):
        """SECURITY (deny-by-default): a cache miss must NOT be rescued by the
        permission payload's own kind. The payload is agent/LLM-influenced, so
        trusting kind="execute" there would let a malicious agent waive the
        length cap on the very name being validated. With no preceding
        tool_call to populate the cache, is_shell must stay False even when the
        payload claims kind="execute"."""
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        # No _extract_tool_event call → cache miss; payload tries kind="execute".
        perm = client._build_permission_event(self._permission_msg(kind="execute"))
        assert perm.is_shell is False

    def test_permission_event_does_not_pop_shell_cache(self, tmp_path):
        """The permission event must read the cached signal with .get(), not
        .pop(): a later tool_call_update refinement for the same toolCallId
        reads the same cache, so popping would make it wrongly see is_shell=
        False. Regression for the review-bot .pop()-consumes-the-entry bug."""
        from kiro_crew.acp.client import AcpClient

        client = AcpClient(work_dir=tmp_path)
        client._extract_tool_event(self._tool_call_msg("execute"))
        perm = client._build_permission_event(self._permission_msg())
        assert perm.is_shell is True
        # Entry survives → a post-permission refinement still resolves True.
        assert client._tool_call_is_shell.get("tc-1") is True
        ev = client._extract_tool_call_refinement(self._refinement_msg(None))
        assert ev is not None and ev.is_shell is True

    def test_is_shell_kind_is_none_safe(self):
        """_is_shell_kind tolerates a non-str/None kind without raising — the
        ACP payload can carry "kind": null (JSON-legal), and equality keeps the
        classifier crash-free where a `.lower()`-based predicate would not."""
        from kiro_crew.acp.client import _is_shell_kind

        assert _is_shell_kind(None) is False
        assert _is_shell_kind("") is False
        assert _is_shell_kind("edit") is False
        assert _is_shell_kind("execute") is True

    def test_permission_event_null_kind_does_not_crash(self, tmp_path):
        """A request_permission payload carrying "kind": null (JSON-legal, and
        emitted by real ACP/MCP servers) must not crash event construction, and
        the resulting title must still validate. Guards the chat-runner event
        loop against a null-kind AttributeError on the length-cap path."""
        from kiro_crew.acp.client import AcpClient
        from kiro_crew.acp.types import JsonRpcMessage
        from kiro_crew.dashboard.chat_utils import _validate_tool_name

        client = AcpClient(work_dir=tmp_path)
        msg = JsonRpcMessage(
            id=7,
            method="session/request_permission",
            params={
                "toolCall": {"toolCallId": "tc-null", "title": "ReadFile", "kind": None},
                "options": [{"optionId": "allow_once", "name": "Allow once"}],
            },
        )
        perm = client._build_permission_event(msg)
        # No cached tool_call → deny-by-default; short non-shell name validates.
        assert perm.is_shell is False
        assert _validate_tool_name(perm.title, is_shell=perm.is_shell) == "ReadFile"

    def test_is_shell_propagates_through_to_llm_event(self):
        """AcpProvider._to_llm_event re-wraps an event field-by-field; the
        is_shell flag must survive that copy or the dashboard (which consumes
        the re-wrapped LLMEvent) would never see it on the live ACP path."""
        from kiro_crew.acp.types import AcpEvent
        from kiro_crew.providers.acp import AcpProvider
        from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST

        src = AcpEvent(kind=EVENT_PERMISSION_REQUEST, title="x" * 400, is_shell=True)
        wrapped = AcpProvider._to_llm_event(src)
        assert wrapped.is_shell is True
        # And the default carries through as False when not a shell tool.
        not_shell = AcpProvider._to_llm_event(AcpEvent(kind=EVENT_PERMISSION_REQUEST))
        assert not_shell.is_shell is False


class TestSpawnEnvChannelCredentialScrub:
    """The default auto/standard ACP spawn path scrubs gateway channel creds.

    Guards the exact production path Codex flagged: ``AcpClient._spawn`` copies a
    raw ``os.environ`` and calls ``wrap_argv`` directly (not
    ``sandboxed_spawn_argv``), and the default tier's launcher does NOT strip
    ``_AGENT_DENIED_ENV_KEYS`` — so the parent-level ``scrub_agent_denied_env``
    must remove them before the child inherits the environment.
    """

    @pytest.mark.asyncio
    async def test_client_spawn_scrubs_channel_creds_on_default_auto(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "0000:FAKE-telegram")
        monkeypatch.setenv("WECOM_BOT_ID", "FAKE-wecom-bot")
        monkeypatch.setenv("WECOM_SECRET", "FAKE-wecom-secret")
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-FAKE")
        monkeypatch.setenv("KIROCREW_OWNER_ID", "U_FAKE_OWNER")
        # A credential the standard sandbox intentionally exposes + a benign key
        # must both survive the parent scrub.
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "FAKE-akid")
        monkeypatch.setenv("KIROCREW_UNRELATED_KEEPME", "keep-this-value")

        captured: dict[str, object] = {}

        class _StopSpawn(Exception):
            pass

        async def _fake_exec(*_args, **kwargs):
            captured["env"] = kwargs.get("env")
            raise _StopSpawn()

        monkeypatch.setattr(acp_client, "_resolve_kiro_bin", lambda: "/fake/kiro")
        monkeypatch.setattr(
            acp_client,
            "wrap_argv",
            lambda argv, mode, strip_python_env=False, is_kiro_cli=None: (argv, None),
        )
        monkeypatch.setattr(acp_client, "cgroup_scope_argv", lambda argv: argv)
        monkeypatch.setattr(acp_client, "augmented_path", lambda p: p)
        monkeypatch.setattr(acp_client, "resolve_krb5_ccname", lambda env: None)
        monkeypatch.setattr(acp_client, "_resolve_ssh_auth_sock", lambda env: None)
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

        client = AcpClient(sandbox_mode="auto")  # default tier
        with pytest.raises(_StopSpawn):
            await client._spawn()

        env = captured["env"]
        assert isinstance(env, dict)
        for key in (
            "TELEGRAM_BOT_TOKEN",
            "WECOM_BOT_ID",
            "WECOM_SECRET",
            "SLACK_BOT_TOKEN",
            "KIROCREW_OWNER_ID",
        ):
            assert key not in env, f"{key} leaked into default-auto ACP child env"
        assert env.get("KIROCREW_UNRELATED_KEEPME") == "keep-this-value"
        assert env.get("AWS_ACCESS_KEY_ID") == "FAKE-akid"


class TestSetModelRebasesContextStats:
    """A mid-session set_model must re-anchor the context-meter stats.

    Regression for the stale context meter: set_model used to leave
    last_prompt_stats untouched, so the old model's window (and its
    authoritative context_tokens_from_usage flag) survived the switch. When
    the new model streams only contextUsagePercentage metadata (kiro 2.10+),
    the stale True gated _backfill_context_window forever — pct updated but
    the token text stayed scaled to the OLD model's window.
    """

    def _client(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        client._send_request = AsyncMock()
        return client

    def _usage_msg(self, used, size):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "usage_update", "used": used, "size": size}},
        )

    @pytest.mark.asyncio
    async def test_rebases_window_and_clears_usage_flag(self, tmp_path, monkeypatch):
        import kiro_crew.acp.client as c

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 272_000)
        client = self._client(tmp_path)
        # Old model reported authoritative counts: 100K / 1M = 10%.
        client._track_usage_update(self._usage_msg(100_000, 1_000_000))
        assert client.last_prompt_stats.context_tokens_from_usage is True

        await client.set_model("gpt-5.6-sol")

        stats = client.last_prompt_stats
        assert stats.context_window_tokens == 272_000
        assert stats.context_used_tokens == 100_000  # transcript unchanged
        assert stats.context_pct == round(100_000 / 272_000 * 100, 1)
        # The old model's usage_update no longer describes the session.
        assert stats.context_tokens_from_usage is False

    @pytest.mark.asyncio
    async def test_post_switch_metadata_backfills_new_window(self, tmp_path, monkeypatch):
        """Load-bearing: with the rebase reverted, context_tokens_from_usage
        stays True and this metadata pct would be ignored, leaving the window
        at the OLD model's 1M forever."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 272_000)
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(100_000, 1_000_000))

        await client.set_model("gpt-5.6-sol")
        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={"contextUsagePercentage": 50.0},
            )
        )

        stats = client.last_prompt_stats
        assert stats.context_window_tokens == 272_000
        assert stats.context_used_tokens == 136_000  # 50% of the NEW window
        assert stats.context_pct == 50.0

    @pytest.mark.asyncio
    async def test_unknown_window_zeroes_out(self, tmp_path, monkeypatch):
        """A registry miss must not keep the old window: zero it so downstream
        consumers fall back to their own model-derived value."""
        import kiro_crew.acp.client as c

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: False)
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(100_000, 1_000_000))

        await client.set_model("mystery-model")

        stats = client.last_prompt_stats
        assert stats.context_window_tokens == 0
        # The old model's pct must not survive either — it would ship in the
        # model-switch reset broadcast as a claim about the unknown window.
        assert stats.context_pct == 0.0
        assert stats.context_tokens_from_usage is False

    def test_rebase_clamps_pct_when_used_exceeds_new_window(self):
        """Shrinking the window below used must clamp pct to 100, not overflow."""
        stats = AcpPromptStats(
            context_used_tokens=500_000,
            context_window_tokens=1_000_000,
            context_pct=50.0,
            context_tokens_from_usage=True,
        )
        stats.rebase_to_window(272_000)
        assert stats.context_pct == 100.0
        assert stats.context_window_tokens == 272_000
        assert stats.context_tokens_from_usage is False


class TestCompactionResetsContextStats:
    """A completed compaction must drop the stale context-usage counts.

    Regression for the frozen context meter after /compact: the pre-compaction
    counts carried an authoritative ``context_tokens_from_usage=True`` flag, so
    ``_track_metadata`` refused to apply any fresh percentage and every
    ``context_usage`` broadcast re-sent the old numbers — the dashboard bar
    never moved after a compact.
    """

    def _client(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        client._session_id = "s1"
        return client

    def _usage_msg(self, used, size):
        from kiro_crew.acp.types import JsonRpcMessage

        return JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "usage_update", "used": used, "size": size}},
        )

    def _compaction_msg(self, status_type):
        from kiro_crew.acp.types import METHOD_COMPACTION_STATUS, JsonRpcMessage

        return JsonRpcMessage(
            method=METHOD_COMPACTION_STATUS,
            params={"status": {"type": status_type}, "summary": ""},
        )

    def test_completed_resets_counts_and_keeps_window(self, tmp_path):
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(150_000, 200_000))
        assert client.last_prompt_stats.context_pct == 75.0

        client._handle_compaction_status(self._compaction_msg("completed"))

        stats = client.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_tokens_from_usage is False
        # The model did not change — the served window still holds.
        assert stats.context_window_tokens == 200_000

    def test_failed_keeps_counts(self, tmp_path):
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(150_000, 200_000))

        client._handle_compaction_status(self._compaction_msg("failed"))

        stats = client.last_prompt_stats
        assert stats.context_pct == 75.0
        assert stats.context_used_tokens == 150_000
        assert stats.context_tokens_from_usage is True

    def test_post_compaction_metadata_reapplies(self, tmp_path, monkeypatch):
        """Load-bearing: with the reset reverted, context_tokens_from_usage
        stays True and this fresh metadata percentage would be ignored,
        freezing the meter at the pre-compaction value forever."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200_000)
        client = self._client(tmp_path)
        client._model = "some-model"
        client._track_usage_update(self._usage_msg(150_000, 200_000))

        client._handle_compaction_status(self._compaction_msg("completed"))
        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={"contextUsagePercentage": 12.0},
            )
        )

        assert client.last_prompt_stats.context_pct == 12.0

    def test_backfill_prefers_kept_served_window_over_registry(self, tmp_path, monkeypatch):
        """After the compaction reset the SERVED window survives (model
        unchanged) and can differ from the registry's static entry (opus
        served at 1M vs a 200K registry row). A metadata pct must derive
        against the kept served window, not clobber it with the registry."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200_000)
        client = self._client(tmp_path)
        client._model = "some-model"
        client._track_usage_update(self._usage_msg(900_000, 1_000_000))  # served 1M
        client._handle_compaction_status(self._compaction_msg("completed"))

        client._track_metadata(
            JsonRpcMessage(
                method="_kiro.dev/metadata",
                params={"contextUsagePercentage": 5.0},
            )
        )

        stats = client.last_prompt_stats
        assert stats.context_window_tokens == 1_000_000  # served window kept
        assert stats.context_used_tokens == 50_000  # derived against it
        assert stats.context_pct == 5.0

    @pytest.mark.asyncio
    async def test_wait_for_compaction_grace_drains_post_metadata(self, tmp_path, monkeypatch):
        """Load-bearing: kiro emits the real post-compaction pct in a
        metadata frame ~1s AFTER the completed status (live-probe confirmed).
        wait_for_compaction must capture it so the dashboard broadcast
        reports accurate numbers instead of the unknown fallback."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200_000)
        client = self._client(tmp_path)
        client._model = "some-model"
        client._track_usage_update(self._usage_msg(900_000, 1_000_000))

        post_meta = JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"contextUsagePercentage": 5.0},
        )
        client._read_message = AsyncMock(side_effect=[self._compaction_msg("completed"), post_meta])

        result = await client.wait_for_compaction(timeout=5.0)

        assert result["type"] == "completed"
        stats = client.last_prompt_stats
        assert stats.context_pct == 5.0
        assert stats.context_window_tokens == 1_000_000
        assert stats.context_used_tokens == 50_000

    @pytest.mark.asyncio
    async def test_grace_drain_skips_metadata_without_percentage(self, tmp_path, monkeypatch):
        """A credits-only metadata frame (no contextUsagePercentage) must not
        end the grace drain — the real usage frame behind it would be
        stranded and the meter would fall back to the reset state."""
        import kiro_crew.acp.client as c
        from kiro_crew.acp.types import JsonRpcMessage

        monkeypatch.setattr(c.model_registry, "has_known_window", lambda mid: True)
        monkeypatch.setattr(c.model_registry, "model_window", lambda mid, **kw: 200_000)
        client = self._client(tmp_path)
        client._model = "some-model"
        client._track_usage_update(self._usage_msg(900_000, 1_000_000))

        credits_only = JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"meteringUsage": [{"unit": "credit", "amount": 0.1}]},
        )
        usage_meta = JsonRpcMessage(
            method="_kiro.dev/metadata",
            params={"contextUsagePercentage": 5.0},
        )
        client._read_message = AsyncMock(
            side_effect=[self._compaction_msg("completed"), credits_only, usage_meta]
        )

        result = await client.wait_for_compaction(timeout=5.0)

        assert result["type"] == "completed"
        assert client.last_prompt_stats.context_pct == 5.0
        assert client.last_prompt_stats.context_used_tokens == 50_000

    @pytest.mark.asyncio
    async def test_wait_for_compaction_without_post_metadata_leaves_reset(self, tmp_path):
        """No metadata within the grace window: the reset state stands (the
        meter shows unknown and self-corrects on the next turn)."""
        client = self._client(tmp_path)
        client._track_usage_update(self._usage_msg(150_000, 200_000))
        # side_effect exhaustion after the completed status makes the grace
        # drain bail immediately instead of sleeping out the window.
        client._read_message = AsyncMock(side_effect=[self._compaction_msg("completed")])

        result = await client.wait_for_compaction(timeout=5.0)

        assert result["type"] == "completed"
        stats = client.last_prompt_stats
        assert stats.context_pct == 0.0
        assert stats.context_used_tokens == 0
        assert stats.context_window_tokens == 200_000


class TestModelEntitlementPreflight:
    """An unusable model is stopped BEFORE the wire, not explained afterwards.

    PR #1550 made a post-hoc rejection readable and terminal. These cover the
    turn never failing in the first place: the advertised set is known at
    session/new, so a model the account cannot run has no business being sent.
    """

    @staticmethod
    def _client(advertised, model, is_claude=False):
        from kiro_crew.acp.client import ACP_BACKEND_CLAUDE, AcpClient

        client = AcpClient()
        client._session_id = "sess-1"
        client._model = model
        # _is_claude is derived from the backend seam, not settable directly.
        client._acp_backend = ACP_BACKEND_CLAUDE if is_claude else ""
        client._available_models = [{"modelId": m, "name": m} for m in advertised]
        return client

    def test_unadvertised_model_is_unusable(self):
        client = self._client(["claude-sonnet-4.6"], "claude-opus-4.8")
        assert client._model_is_unusable("claude-opus-4.8") is True

    def test_advertised_model_is_usable(self):
        client = self._client(["claude-sonnet-4.6", "claude-opus-4.8"], "claude-opus-4.8")
        assert client._model_is_unusable("claude-opus-4.8") is False

    def test_unknown_entitlement_allows_the_send(self):
        """Empty advertised set means "unknowable", never "nothing is allowed".

        A backend that omits ``models`` must not have every model withheld.
        """
        client = self._client([], "claude-opus-4.8")
        assert client._model_is_unusable("claude-opus-4.8") is False

    def test_match_is_case_and_space_insensitive(self):
        client = self._client(["Claude-Opus-4.8"], "claude-opus-4.8")
        assert client._model_is_unusable("  claude-opus-4.8 ") is False

    @pytest.mark.asyncio
    async def test_startup_withholds_unusable_model(self):
        """The screenshot case: a stale default the account cannot run.

        Nothing goes on the wire and the session is recorded as running the
        backend default, so the turn proceeds instead of dying three retries in.
        """
        from kiro_crew.acp.client import DEFAULT_MODEL

        client = self._client(["claude-sonnet-4.6"], "claude-opus-4.8")
        client._resolved_model_id = "claude-sonnet-4.6"
        sent = []
        client._send_request = _record(sent)

        await client._apply_startup_model()

        assert sent == []
        # Not left holding the id we declined -- the warm-pool re-apply path
        # reads this and would re-offer it on every claim.
        assert client._model == DEFAULT_MODEL

    @pytest.mark.asyncio
    async def test_startup_still_applies_a_usable_model(self):
        client = self._client(["claude-sonnet-4.6", "claude-opus-4.8"], "claude-opus-4.8")
        sent = []
        client._send_request = _record(sent)

        await client._apply_startup_model()

        assert len(sent) == 1
        assert sent[0][1]["modelId"] == "claude-opus-4.8"
        assert client._model == "claude-opus-4.8"

    @pytest.mark.asyncio
    async def test_startup_leaves_claude_backend_alone(self):
        """The claude backend advertises BARE ids while _model is prefixed.

        Comparing the two namespaces would call every legitimate model unusable,
        so that backend keeps its own session/new substitution advisory.
        """
        client = self._client(
            ["claude-opus-4-8[1m]"],
            "global.anthropic.claude-opus-4-8[1m]",
            is_claude=True,
        )
        applied = []

        async def _set_config_option(config_id, value):
            applied.append((config_id, value))

        client.set_config_option = _set_config_option

        await client._apply_startup_model()

        assert applied == [("model", "global.anthropic.claude-opus-4-8[1m]")]

    @pytest.mark.asyncio
    async def test_explicit_switch_is_refused_not_downgraded(self):
        """An explicit pick gets an error, because silence would misreport it."""
        from kiro_crew.acp.client import AcpModelUnavailable

        client = self._client(["claude-sonnet-4.6", "claude-haiku-4.5"], "claude-sonnet-4.6")
        sent = []
        client._send_request = _record(sent)

        with pytest.raises(AcpModelUnavailable) as excinfo:
            await client.set_model("claude-opus-4.8")

        assert sent == []
        msg = str(excinfo.value)
        assert "claude-opus-4.8" in msg
        assert "not available on your account" in msg
        # The actionable part: what they CAN pick.
        assert "claude-sonnet-4.6" in msg
        assert "claude-haiku-4.5" in msg
        # The identity hint stays CONDITIONAL and read-only. This error also
        # reaches users who really are on a free tier and for whom picking an
        # advertised model is the whole fix, so it must not read as an
        # instruction to re-authenticate: `whoami` only reports which tier is
        # signed in, and no destructive step is ever named.
        assert "if you expected" in msg.lower()
        assert "kiro-cli whoami" in msg
        assert "logout" not in msg.lower()
        assert "kiro-cli login" not in msg
        # Terminal, and EXPLICITLY so -- None would send the retry layer back to
        # string-matching, which is what produced the retry rows.
        assert excinfo.value.transient is False

    @pytest.mark.asyncio
    async def test_explicit_switch_to_advertised_model_works(self):
        client = self._client(["claude-sonnet-4.6", "claude-opus-4.8"], "claude-sonnet-4.6")
        sent = []
        client._send_request = _record(sent)

        await client.set_model("claude-opus-4.8")

        assert len(sent) == 1
        assert client._model == "claude-opus-4.8"


def _record(sink):
    """An async _send_request stub that records calls and returns a request id."""

    async def _send_request(method, params=None):
        sink.append((method, params or {}))
        return 1

    return _send_request
