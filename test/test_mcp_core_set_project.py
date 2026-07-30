"""Tests for the ``set_project`` MCP tool and its strict identity resolver.

Two layers are under test:

1. ``_resolve_session_key_strict`` — refuses PID-walked identities so a
   subagent cannot silently mutate its parent slot's project. This resolver
   still exists (other call sites use it) and its guarantees are unchanged.
2. The stateless ``set_project`` path (#755). ``_call_tool_inner`` no longer
   resolves session identity or POSTs to the gateway: it VALIDATES its input
   and returns a session directive (see ``kiro_crew.session_directive``). The
   session-aware consumer applies it via
   ``kiro_crew.dashboard.session_directive_apply.apply_session_directive``,
   which is exercised directly here against a fake slot + state.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from kiro_crew import mcp_core, session_directive
from kiro_crew.dashboard.session_directive_apply import apply_session_directive

# ───────────────────────────── _resolve_session_key_strict ─────────────────


class TestResolveSessionKeyStrict:
    """Strict resolver: the ``KIROCREW_SESSION_KEY`` env var, or the direct
    ``KIROCREW_HOST_PID`` -> ``session_pid_<pid>.txt`` lookup — the latter
    ONLY when the gateway-written HMAC sidecar verifies. The /proc ancestor
    WALK the lenient resolver uses is dropped, and an unsigned or forged
    file is refused."""

    def _signed_env(self, monkeypatch, tmp_path, pid: str, session_key: str):
        """Simulate the sandbox: env key stripped, HOST_PID set, and a
        gateway-published (signed) mapping on disk."""
        from kiro_crew import session_pid_sig

        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", pid)
        (tmp_path / "sel_hmac.key").write_bytes(b"k" * 32)
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            session_pid_sig.publish_session_pid(int(pid), session_key)

    def test_env_var_used(self, monkeypatch):
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:slot-B")
        assert mcp_core._resolve_session_key_strict() == "dashboard:slot-B"

    def test_returns_empty_when_only_pid_walk_would_match(self, monkeypatch):
        """Lenient resolver would walk /proc and find a session_pid_*.txt;
        strict returns "" so the caller can refuse."""
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.delenv("KIROCREW_HOST_PID", raising=False)
        assert mcp_core._resolve_session_key_strict() == ""

    def test_env_var_wins_over_host_pid(self, monkeypatch, tmp_path):
        """When both identities are present the env var is authoritative."""
        from kiro_crew import session_pid_sig

        self._signed_env(monkeypatch, tmp_path, "4242", "dashboard:file-slot")
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "dashboard:env-slot")
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            assert mcp_core._resolve_session_key_strict() == "dashboard:env-slot"

    def test_signed_host_pid_mapping_accepted(self, monkeypatch, tmp_path):
        """Sandboxed session: env key stripped, launcher-declared HOST_PID
        maps to a gateway-published signed mapping — accepted."""
        from kiro_crew import session_pid_sig

        self._signed_env(
            monkeypatch, tmp_path, "4242", "dashboard:chat-32-1784855955"
        )
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            assert (
                mcp_core._resolve_session_key_strict()
                == "dashboard:chat-32-1784855955"
            )

    def test_unsigned_host_pid_file_refused(self, monkeypatch, tmp_path):
        """FORGERY: an agent writes a bare session_pid_<pid>.txt pointing at
        another slot's key. Without the HMAC sidecar (which requires the
        agent-unreadable SEL key) the strict resolver must refuse."""
        from kiro_crew import session_pid_sig

        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", "4242")
        (tmp_path / "sel_hmac.key").write_bytes(b"k" * 32)
        (tmp_path / "session_pid_4242.txt").write_text(
            "dashboard:victim-slot", encoding="utf-8"
        )
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            assert mcp_core._resolve_session_key_strict() == ""

    def test_replayed_sidecar_for_other_pid_refused(self, monkeypatch, tmp_path):
        """REPLAY: a subagent copies the parent's .txt/.sig pair under its own
        pid. The pid is bound into the MAC, so verification must fail."""
        from kiro_crew import session_pid_sig

        (tmp_path / "sel_hmac.key").write_bytes(b"k" * 32)
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            # Gateway legitimately publishes the PARENT's mapping (pid 1000).
            session_pid_sig.publish_session_pid(1000, "dashboard:parent-slot")
        # Subagent (host pid 2000) replays the parent's pair under its own pid.
        for ext in ("txt", "sig"):
            (tmp_path / f"session_pid_2000.{ext}").write_text(
                (tmp_path / f"session_pid_1000.{ext}").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", "2000")
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            assert mcp_core._resolve_session_key_strict() == ""

    def test_host_pid_without_file_returns_empty(self, monkeypatch, tmp_path):
        """A subagent sandbox exports its own HOST_PID, but the gateway never
        writes a session_pid file for it — strict must refuse, not walk."""
        from kiro_crew import session_pid_sig

        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", "5555")
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            assert mcp_core._resolve_session_key_strict() == ""

    def test_non_numeric_host_pid_ignored(self, monkeypatch, tmp_path):
        """Malformed HOST_PID (path traversal, garbage) never reaches the
        filesystem lookup."""
        from kiro_crew import session_pid_sig

        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", "../../etc/passwd")
        with patch.object(session_pid_sig, "config_dir", return_value=tmp_path), \
             patch.object(
                 session_pid_sig,
                 "sel_hmac_key_path",
                 return_value=tmp_path / "sel_hmac.key",
             ):
            assert mcp_core._resolve_session_key_strict() == ""

    def test_verifier_failure_returns_empty(self, monkeypatch):
        """Any error inside verification fails closed to ''."""
        from kiro_crew import session_pid_sig

        monkeypatch.delenv("KIROCREW_SESSION_KEY", raising=False)
        monkeypatch.setenv("KIROCREW_HOST_PID", "4242")
        with patch.object(
            session_pid_sig, "verify_session_pid", side_effect=OSError("boom")
        ):
            assert mcp_core._resolve_session_key_strict() == ""


# ───────────────────────────── set_project tool ─────────────────────────────


class TestSetProjectTool:
    """The stateless ``set_project`` dispatch branch in ``_call_tool_inner``.

    The tool validates its input and returns a session DIRECTIVE — it no longer
    resolves session identity, no longer refuses non-dashboard sessions, and no
    longer POSTs to the gateway. Validation still runs at the boundary, so
    malformed input is rejected before a directive is ever produced."""

    def test_returns_directive_with_validated_payload(self):
        result = mcp_core._call_tool_inner("set_project", {"path": "/tmp/foo"})
        assert session_directive.decode(result, "set_project") == {
            "project": "/tmp/foo",
            "clear": False,
        }

    def test_clear_flag_encoded_in_directive(self):
        result = mcp_core._call_tool_inner("set_project", {"path": "", "clear": True})
        assert session_directive.decode(result, "set_project") == {
            "project": "",
            "clear": True,
        }

    def test_empty_path_without_clear_rejected(self):
        from kiro_crew.validation import ValidationError

        with pytest.raises(ValidationError, match="required.*clear=true"):
            mcp_core._call_tool_inner("set_project", {"path": ""})

    def test_non_string_path_raises_validation_error(self):
        from kiro_crew.validation import ValidationError

        with pytest.raises(ValidationError):
            mcp_core._call_tool_inner("set_project", {"path": 123})

    def test_set_project_listed_in_tools(self):
        names = [t["name"] for t in mcp_core._list_tools()]
        assert "set_project" in names
        descriptor = next(t for t in mcp_core._list_tools() if t["name"] == "set_project")
        schema = descriptor["inputSchema"]
        assert schema["type"] == "object"
        assert "path" in schema["properties"]
        assert schema["properties"]["path"]["type"] == "string"
        assert schema["required"] == ["path"]


# ─────────────────────────── set_project applier ────────────────────────────


class _FakeSlot:
    """Minimal slot: the applier only touches ``key``, ``project`` and
    ``_pending_reset_history_key``."""

    def __init__(self, key: str = "dashboard:test-slot", project: str = ""):
        self.key = key
        self.project = project
        self._pending_reset_history_key = None


class _FakeState:
    """Minimal state: the applier only calls ``push_slots_update``."""

    def __init__(self):
        self.pushes = 0

    def push_slots_update(self) -> None:
        self.pushes += 1


class TestSetProjectApplier:
    """The session-aware consumer applies the directive to ITS OWN slot via
    ``apply_session_directive`` — no HTTP, no identity resolution."""

    @pytest.mark.asyncio
    async def test_sets_project_realpath_and_flags_history_reset(self, tmp_path):
        slot = _FakeSlot(project="")
        state = _FakeState()
        target = str(tmp_path)
        result = await apply_session_directive(
            state, slot, slot.key, "set_project", {"project": target, "clear": False}
        )
        assert slot.project == os.path.realpath(target)
        assert slot._pending_reset_history_key is not None
        assert state.pushes == 1
        assert "Project set to" in result

    @pytest.mark.asyncio
    async def test_sensitive_path_denied_without_mutating_slot(self, tmp_path, monkeypatch):
        slot = _FakeSlot(project="/existing/project")
        state = _FakeState()
        # _set_project imports is_sensitive_path lazily from kiro_crew.security,
        # so patch it on the source module.
        monkeypatch.setattr(
            "kiro_crew.security.is_sensitive_path", lambda *a, **k: True
        )
        result = await apply_session_directive(
            state, slot, slot.key, "set_project", {"project": str(tmp_path), "clear": False}
        )
        assert "access denied" in result.lower()
        # Load-bearing: the slot was NOT repointed to the sensitive path.
        assert slot.project == "/existing/project"

    @pytest.mark.asyncio
    async def test_clear_empties_project(self, tmp_path):
        slot = _FakeSlot(project=str(tmp_path))
        state = _FakeState()
        result = await apply_session_directive(
            state, slot, slot.key, "set_project", {"project": "", "clear": True}
        )
        assert slot.project == ""
        assert "cleared" in result.lower()


# ────────────────── applier SEL audit + fail-soft (#755) ─────────────────────


class _SelSpy:
    """Captures every ``log_tool_invocation`` the applier's ``_audit`` emits."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def log_tool_invocation(self, **kw: object) -> None:
        self.calls.append(kw)


@pytest.fixture()
def sel_spy(monkeypatch):
    """Replace the SEL singleton so directive audits are captured, not written.

    ``_audit`` does ``from kiro_crew.sel import sel; sel().log_tool_invocation``,
    so patching the factory on the source module intercepts every path."""
    spy = _SelSpy()
    monkeypatch.setattr("kiro_crew.sel.sel", lambda: spy)
    return spy


class TestApplierAuditAndFailSoft:
    """Every ``apply_session_directive`` path emits exactly one SEL event and
    NEVER raises into the turn loop (#755 backend-security-controls)."""

    @pytest.mark.asyncio
    async def test_success_emits_one_mcp_directive_event(self, tmp_path, monkeypatch, sel_spy):
        """A valid set_project audits source='mcp-directive', the tool name, and
        outcome='success' — and the recent-projects offload actually fires."""
        monkeypatch.setattr("kiro_crew.security.is_sensitive_path", lambda *a, **k: False)
        saved: list[str] = []
        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_handlers._save_recent_project",
            lambda rp: saved.append(rp),
        )
        slot = _FakeSlot(project="")
        state = _FakeState()
        result = await apply_session_directive(
            state, slot, "dashboard:chat-1", "set_project",
            {"project": str(tmp_path), "clear": False},
        )
        assert "Project set to" in result
        assert len(sel_spy.calls) == 1
        call = sel_spy.calls[0]
        assert call["session_key"] == "dashboard:chat-1"
        assert call["source"] == "mcp-directive"
        assert call["tool_name"] == "set_project"
        assert call["outcome"] == "success"
        # The to_thread offload of the recent-projects write happened.
        assert saved == [os.path.realpath(str(tmp_path))]

    @pytest.mark.asyncio
    async def test_denied_path_audits_denied_and_returns_error(self, tmp_path, monkeypatch, sel_spy):
        """A sensitive-path block raises ``_DirectiveDenied`` internally; the
        wrapper audits outcome='denied' and returns the fixed error string."""
        monkeypatch.setattr("kiro_crew.security.is_sensitive_path", lambda *a, **k: True)
        slot = _FakeSlot(project="/existing")
        state = _FakeState()
        result = await apply_session_directive(
            state, slot, "dashboard:chat-1", "set_project",
            {"project": str(tmp_path), "clear": False},
        )
        assert result == "Error: access denied (sensitive path)."
        assert [c["outcome"] for c in sel_spy.calls] == ["denied"]
        assert sel_spy.calls[0]["tool_name"] == "set_project"
        # The slot was not repointed to the sensitive path.
        assert slot.project == "/existing"

    @pytest.mark.asyncio
    async def test_unknown_kind_audits_error(self, sel_spy):
        """An unrecognized directive kind returns a readable error and audits
        outcome='error' (never raises)."""
        slot = _FakeSlot()
        state = _FakeState()
        result = await apply_session_directive(
            state, slot, "dashboard:chat-1", "not_a_real_directive", {}
        )
        assert "unknown session directive" in result
        assert [c["outcome"] for c in sel_spy.calls] == ["error"]
        assert sel_spy.calls[0]["tool_name"] == "not_a_real_directive"

    @pytest.mark.asyncio
    async def test_internal_exception_is_caught_and_audited(self, monkeypatch, sel_spy):
        """An applier that raises internally is caught: returns a string starting
        'Error applying', audits outcome='error', and does NOT propagate."""
        def _boom() -> object:
            raise RuntimeError("autonudge exploded")

        # _monitor_start does `from kiro_crew.autonudge import get_instance`
        # then calls it first — patch the source symbol so it raises.
        monkeypatch.setattr("kiro_crew.autonudge.get_instance", _boom)
        slot = _FakeSlot()
        state = _FakeState()
        result = await apply_session_directive(
            state, slot, "dashboard:chat-1", "monitor_start", {"message": "x"}
        )
        assert result.startswith("Error applying monitor_start")
        assert [c["outcome"] for c in sel_spy.calls] == ["error"]
        assert sel_spy.calls[0]["tool_name"] == "monitor_start"

    @pytest.mark.asyncio
    async def test_sensitive_path_denied_without_filesystem_probe(self, monkeypatch, sel_spy):
        """A sensitive path is refused BEFORE it is resolved/stat'ed, so a
        nonexistent sensitive path cannot be probed via the not-a-directory
        error. Still audited denied, and never leaks the isdir outcome."""
        monkeypatch.setattr("kiro_crew.security.is_sensitive_path", lambda *a, **k: True)
        probed: list[str] = []

        def _no_stat(p):
            probed.append(p)
            return False

        monkeypatch.setattr("os.path.isdir", _no_stat)
        slot = _FakeSlot(project="/existing")
        state = _FakeState()
        result = await apply_session_directive(
            state, slot, "dashboard:chat-1", "set_project",
            {"project": "~/.aws/definitely-not-there", "clear": False},
        )
        assert result == "Error: access denied (sensitive path)."
        assert probed == [], f"sensitive path was stat'ed before the deny gate: {probed!r}"
        assert [c["outcome"] for c in sel_spy.calls] == ["denied"]
        assert slot.project == "/existing"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("kind", ["set_project", "suggest_followup", "ask_question"])
    @pytest.mark.parametrize("session_key", ["cron:job-abc", "slack:C123.456", "sub:agent-1", ""])
    async def test_slot_targeting_directives_are_dashboard_only(
        self, kind, session_key, tmp_path, monkeypatch, sel_spy
    ):
        """These three act on a dashboard SLOT, and the endpoints they replaced
        rejected cron / Slack / sub-agent callers. A cron-linked slot flowing
        through _run_chat must not have its project retargeted or get a card."""
        monkeypatch.setattr("kiro_crew.security.is_sensitive_path", lambda *a, **k: False)
        slot = _FakeSlot(project="/original")
        state = _FakeState()
        args = {
            "set_project": {"project": str(tmp_path), "clear": False},
            "suggest_followup": {"items": [{"title": "t", "prompt": "p"}]},
            "ask_question": {"questions": [{"question": "q", "options": [{"label": "a"}]}]},
        }[kind]
        result = await apply_session_directive(state, slot, session_key, kind, args)
        assert "only works from a dashboard chat session" in result
        assert [c["outcome"] for c in sel_spy.calls] == ["denied"]
        # No effect landed on the slot.
        assert slot.project == "/original"

    @pytest.mark.asyncio
    async def test_returned_failure_is_audited_as_error_not_success(
        self, tmp_path, monkeypatch, sel_spy
    ):
        """Some appliers RETURN a readable failure instead of raising (invalid
        project dir). The audit must reflect that, not blanket 'success'."""
        monkeypatch.setattr("kiro_crew.security.is_sensitive_path", lambda *a, **k: False)
        slot = _FakeSlot(project="/original")
        state = _FakeState()
        missing = str(tmp_path / "definitely-not-a-directory")
        result = await apply_session_directive(
            state, slot, "dashboard:chat-1", "set_project",
            {"project": missing, "clear": False},
        )
        assert result.startswith("Error: not a directory")
        assert [c["outcome"] for c in sel_spy.calls] == ["error"]
        assert slot.project == "/original"

    @pytest.mark.asyncio
    async def test_refused_monitor_update_audits_denied(self, monkeypatch, sel_spy):
        """A monitor_update whose new cap is at/below the delivered cycle count
        is a REFUSED unattended-loop mutation — audited outcome='denied' (not
        'success'), returns its guidance message verbatim, and never raises."""

        class _Loop:
            id = "L1"
            cycle_count = 5
            max_cycles = 5
            active = True

        class _Svc:
            def get_by_slot(self, _b):
                return _Loop()

        monkeypatch.setattr("kiro_crew.autonudge.get_instance", lambda: _Svc())
        monkeypatch.setattr("kiro_crew.autonudge.binding_key_for", lambda sk: "bind-1")
        slot = _FakeSlot()
        state = _FakeState()
        result = await apply_session_directive(
            state, slot, "dashboard:chat-1", "monitor_update", {"patch": {"max_cycles": 3}}
        )
        assert "at or below" in result  # verbatim guidance, no "Error:" prefix
        assert [c["outcome"] for c in sel_spy.calls] == ["denied"]
        assert sel_spy.calls[0]["tool_name"] == "monitor_update"
