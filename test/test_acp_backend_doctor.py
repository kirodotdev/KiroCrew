"""The doctor's ACP backend section.

An ``issues`` entry means a human must act. A capability difference is a NOTE, not
an issue: degraded reasoning-effort support is a documented property of the
backend, and listing it as a problem would train the reader to skip the section —
which is where the tool-gate row lives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.acp import claude
from kiro_crew.acp import client as acp_client
from kiro_crew.acp import codex, doctor, goose, opencode, pi
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
)


@pytest.fixture()
def codex_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "codexhome"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    return home


@pytest.fixture(autouse=True)
def _reset_adapter_caches() -> None:
    """Doctor now shares spawn's success-only caches; isolate each test."""
    acp_client._claude_acp_argv_cache = acp_client._UNRESOLVED
    acp_client._claude_code_executable_cache = acp_client._UNRESOLVED
    codex._argv_cache = codex._UNRESOLVED
    goose._argv_cache = goose._UNRESOLVED
    opencode._argv_cache = opencode._UNRESOLVED
    pi._argv_cache = pi._UNRESOLVED


def _run(backend: str, work_dir: Path, allow: bool = False) -> tuple[str, list[str]]:
    lines: list[str] = []
    issues: list[str] = []
    doctor.report(backend, work_dir, allow_ungated=allow, emit=lines.append, issues=issues)
    return ("\n".join(lines), issues)


class TestDefaultBackend:
    def test_prints_nothing_and_reports_nothing(self, tmp_path: Path) -> None:
        """An installation that never opted in sees today's output."""
        text, issues = _run(ACP_BACKEND_KIRO, tmp_path)
        assert text == ""
        assert issues == []


class TestCodexRows:
    def test_names_the_backend_and_marks_it_experimental(
        self, codex_home: Path, tmp_path: Path
    ) -> None:
        text, _ = _run(ACP_BACKEND_CODEX, tmp_path)
        assert "OpenAI Codex" in text
        assert "experimental" in text
        assert "agent.acp_backend=codex" in text

    def test_a_missing_adapter_is_an_issue_and_warns_about_the_cli(
        self, codex_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kiro_crew.acp.codex.resolve_argv", lambda: None)
        text, issues = _run(ACP_BACKEND_CODEX, tmp_path)
        assert "adapter:     ❌" in text
        assert "does not serve ACP" in text
        assert any("adapter not found" in i for i in issues)

    def test_a_resolved_adapter_is_not_an_issue(
        self, codex_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kiro_crew.acp.codex.resolve_argv", lambda: ["/usr/bin/codex-acp"])
        text, issues = _run(ACP_BACKEND_CODEX, tmp_path)
        assert "/usr/bin/codex-acp" in text
        assert not any("adapter" in i for i in issues)

    def test_missing_auth_is_reported_without_becoming_an_issue(
        self, codex_home: Path, tmp_path: Path
    ) -> None:
        """An absent token file must not fail the doctor.

        It reads UNKNOWN, not "not signed in": a turn has completed on a host in
        exactly this state, so appending an issue would report a working install
        as broken and prescribe a login that changes nothing. The hint is still
        printed, and still names codex's own command rather than kiro-cli's.
        """
        text, issues = _run(ACP_BACKEND_CODEX, tmp_path)
        assert "codex login" in text
        assert "kiro-cli login" not in text
        assert not any("signed in" in i for i in issues)

    def test_present_auth_is_not_an_issue(self, codex_home: Path, tmp_path: Path) -> None:
        (codex_home / "auth.json").write_text("{}")
        _, issues = _run(ACP_BACKEND_CODEX, tmp_path)
        assert not any("signed in" in i for i in issues)


class TestToolGateRow:
    """The probe-backed half of the row.

    Written against claude, the backend whose verdict is still resolved by reading
    a file. Codex used to be the example here; its routing moved to SESSION_CONFIG,
    where the verdict is a contract the session discharges rather than something
    the doctor can read off disk, so keeping the coverage on codex would have
    asserted a bypass path that no longer exists.
    """

    def _bypass(self, work_dir: Path) -> None:
        path = claude.local_settings_path(work_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"permissions": {"defaultMode": "auto"}}))

    def test_a_bypassing_policy_is_an_issue_naming_the_controls(self, tmp_path: Path) -> None:
        self._bypass(tmp_path)
        text, issues = _run(ACP_BACKEND_CLAUDE, tmp_path)
        assert "denied-command rules" in text
        assert "sensitive-path block" in text
        assert "governance ceiling" in text
        assert any("bypass the PreToolUse gate" in i for i in issues)

    def test_a_routed_policy_is_not_an_issue(self, tmp_path: Path) -> None:
        claude.ensure_routed_settings(tmp_path)
        text, issues = _run(ACP_BACKEND_CLAUDE, tmp_path)
        assert "tool gate:   ✅" in text
        assert not any("bypass" in i for i in issues)

    def test_an_indeterminate_verdict_is_an_issue(self, tmp_path: Path) -> None:
        """Cannot establish is treated exactly like cannot enforce.

        An unrecognized mode is the reachable indeterminate case: it is somebody's
        configured value, so the seed leaves it, and Kiro Crew cannot know whether
        a mode the adapter added asks or self-approves. A malformed file is NOT
        this case — the seed replaces it and the readback then routes.
        """
        path = claude.local_settings_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"permissions": {"defaultMode": "someFutureMode"}}))
        _, issues = _run(ACP_BACKEND_CLAUDE, tmp_path)
        assert any("bypass the PreToolUse gate" in i for i in issues)

    def test_the_remedy_is_quoted(self, tmp_path: Path) -> None:
        self._bypass(tmp_path)
        text, _ = _run(ACP_BACKEND_CLAUDE, tmp_path)
        assert "Fix:" in text


class TestSessionConfigGateRow:
    """A SESSION_CONFIG backend's row states the contract, not a file reading.

    The row must still say WHAT makes the backend ask, because ✅ with no reason
    is indistinguishable from ✅ earned by a probe — and here nothing has been
    read yet: the option lives on a session that does not exist during a doctor
    run, and is verified inside the handshake before the first prompt.
    """

    def test_the_row_names_the_enforced_option_and_value(
        self, codex_home: Path, tmp_path: Path
    ) -> None:
        text, issues = _run(ACP_BACKEND_CODEX, tmp_path)
        assert "tool gate:   ✅" in text
        assert "mode=read-only" in text
        assert not any("bypass" in i for i in issues)

    def test_the_adapters_own_config_no_longer_decides_the_row(
        self, codex_home: Path, tmp_path: Path
    ) -> None:
        """The file the old probe read cannot flip this backend's verdict.

        codex-acp's ACP sessions do not honour it: they default to a mode that
        writes inside the workspace without asking, which is exactly the reading
        that made a bypassing session report as governed.
        """
        (codex_home / "config.toml").write_text('approval_policy = "never"')
        text, issues = _run(ACP_BACKEND_CODEX, tmp_path)
        assert "tool gate:   ✅" in text
        assert not any("bypass" in i for i in issues)

    def test_passive_reads_are_disclosed_as_ungated(self, codex_home: Path, tmp_path: Path) -> None:
        """ACP v1 cannot make an adapter ask before a read.

        The approval-mode copy promises a check before changes; saying so here is
        what keeps ✅ from being read as "every tool asks".
        """
        text, issues = _run(ACP_BACKEND_CODEX, tmp_path)
        assert "read" in text
        assert not any("bypass" in i for i in issues)


class TestOptOutRow:
    def test_the_opt_out_is_an_issue_even_when_routed(
        self, codex_home: Path, tmp_path: Path
    ) -> None:
        """It disarms the refusal for every FUTURE session too.

        So the operator should learn it is on regardless of today's config.
        """
        (codex_home / "config.toml").write_text('approval_policy = "untrusted"')
        text, issues = _run(ACP_BACKEND_CODEX, tmp_path, allow=True)
        assert "ENABLED" in text
        assert any("allow_ungated_tools" in i for i in issues)

    def test_absent_when_off(self, codex_home: Path, tmp_path: Path) -> None:
        text, _ = _run(ACP_BACKEND_CODEX, tmp_path, allow=False)
        assert "opt-out" not in text


class TestClaudeRows:
    def test_sign_in_is_reported_as_vendor_owned(self, tmp_path: Path) -> None:
        """Kiro Crew reads no credential for this backend."""
        text, _ = _run(ACP_BACKEND_CLAUDE, tmp_path)
        assert "owned by the vendor CLI" in text

    def test_a_missing_adapter_is_an_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Claude has an owned resolution ladder; doctor must report it.

        An operator who never installed ``claude-agent-acp`` otherwise only
        learns at spawn, after they already picked the backend.
        """
        monkeypatch.setattr("kiro_crew.acp.client._resolve_claude_acp_bin", lambda: None)
        text, issues = _run(ACP_BACKEND_CLAUDE, tmp_path)
        assert "adapter:     ❌" in text
        assert any("adapter not found" in i for i in issues)

    def test_a_resolved_adapter_is_not_an_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.acp.client._resolve_claude_acp_bin",
            lambda: ["/usr/bin/node", "/opt/claude-agent-acp/dist/index.js"],
        )
        text, issues = _run(ACP_BACKEND_CLAUDE, tmp_path)
        assert "claude-agent-acp" in text or "/opt/claude-agent-acp" in text
        assert not any("adapter" in i for i in issues)

    def test_seeding_makes_the_gate_row_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.acp.client._resolve_claude_acp_bin",
            lambda: ["/usr/bin/node", "/opt/claude-agent-acp/dist/index.js"],
        )
        claude.ensure_routed_settings(tmp_path)
        text, issues = _run(ACP_BACKEND_CLAUDE, tmp_path)
        assert "tool gate:   ✅" in text
        assert issues == []

    def test_a_configured_bypass_is_reported(self, tmp_path: Path) -> None:
        path = claude.local_settings_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"permissions": {"defaultMode": "auto"}}))
        _, issues = _run(ACP_BACKEND_CLAUDE, tmp_path)
        assert any("bypass the PreToolUse gate" in i for i in issues)


class TestGooseRows:
    def test_a_missing_adapter_is_an_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kiro_crew.acp.goose.resolve_argv", lambda: None)
        text, issues = _run(ACP_BACKEND_GOOSE, tmp_path)
        assert "adapter:     ❌" in text
        assert "npm install" not in text
        assert any("adapter not found" in i for i in issues)

    def test_a_resolved_adapter_is_not_an_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.acp.goose.resolve_argv", lambda: ["/usr/local/bin/goose", "acp"]
        )
        text, issues = _run(ACP_BACKEND_GOOSE, tmp_path)
        assert "/usr/local/bin/goose" in text
        assert not any("adapter" in i for i in issues)

    def test_sign_in_is_reported_as_vendor_owned(self, tmp_path: Path) -> None:
        text, _ = _run(ACP_BACKEND_GOOSE, tmp_path)
        assert "owned by the vendor CLI" in text
        assert "goose configure" in text

    def test_tool_gate_is_routed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "kiro_crew.acp.goose.resolve_argv", lambda: ["/usr/local/bin/goose", "acp"]
        )
        text, issues = _run(ACP_BACKEND_GOOSE, tmp_path)
        assert "tool gate:   ✅" in text
        assert "approve" in text
        assert not any("bypass" in i for i in issues)


class TestOpenCodeRows:
    def test_a_missing_adapter_is_an_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kiro_crew.acp.opencode.resolve_argv", lambda: None)
        text, issues = _run(ACP_BACKEND_OPENCODE, tmp_path)
        assert "adapter:     ❌" in text
        assert "npm install" not in text
        assert any("adapter not found" in i for i in issues)

    def test_a_resolved_adapter_is_not_an_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.acp.opencode.resolve_argv",
            lambda: ["/usr/local/bin/opencode", "acp"],
        )
        text, issues = _run(ACP_BACKEND_OPENCODE, tmp_path)
        assert "/usr/local/bin/opencode" in text
        assert not any("adapter" in i for i in issues)

    def test_sign_in_is_reported_as_vendor_owned(self, tmp_path: Path) -> None:
        text, _ = _run(ACP_BACKEND_OPENCODE, tmp_path)
        assert "owned by the vendor CLI" in text
        assert "opencode auth login" in text

    def test_seeding_makes_the_gate_row_pass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "kiro_crew.acp.opencode.resolve_argv",
            lambda: ["/usr/local/bin/opencode", "acp"],
        )
        opencode.ensure_routed_settings(tmp_path)
        text, issues = _run(ACP_BACKEND_OPENCODE, tmp_path)
        assert "tool gate:   ✅" in text
        assert not any("bypass" in i for i in issues)
        written = json.loads(opencode.project_config_path(tmp_path).read_text())
        assert written["permission"] == "ask"

    def test_a_configured_allow_is_reported(self, tmp_path: Path) -> None:
        path = tmp_path / "opencode.json"
        path.write_text(json.dumps({"permission": "allow"}))
        _, issues = _run(ACP_BACKEND_OPENCODE, tmp_path)
        assert any("bypass the PreToolUse gate" in i for i in issues)


class TestPiRows:
    def test_a_missing_adapter_is_an_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kiro_crew.acp.pi.resolve_argv", lambda: None)
        text, issues = _run(ACP_BACKEND_PI, tmp_path)
        assert "adapter:     ❌" in text
        assert "pi-acp" in text
        assert any("adapter not found" in i for i in issues)

    def test_a_resolved_adapter_is_not_an_issue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("kiro_crew.acp.pi.resolve_argv", lambda: ["/usr/local/bin/pi-acp"])
        text, issues = _run(ACP_BACKEND_PI, tmp_path)
        assert "/usr/local/bin/pi-acp" in text
        assert not any("adapter" in i for i in issues)

    def test_sign_in_is_reported_as_vendor_owned(self, tmp_path: Path) -> None:
        text, _ = _run(ACP_BACKEND_PI, tmp_path)
        assert "owned by the vendor CLI" in text
        assert "owned by the vendor CLI (pi)" in text

    def test_crew_mcp_forwarding_is_a_note_not_an_issue(self, tmp_path: Path) -> None:
        text, issues = _run(ACP_BACKEND_PI, tmp_path)
        assert "crew mcp:" in text
        assert "may" in text and "not forward" in text
        assert not any("forward" in i for i in issues)


class TestCapabilityNotes:
    def test_degraded_capabilities_are_notes_not_issues(
        self, codex_home: Path, tmp_path: Path
    ) -> None:
        (codex_home / "config.toml").write_text('approval_policy = "untrusted"')
        (codex_home / "auth.json").write_text("{}")
        text, issues = _run(ACP_BACKEND_CODEX, tmp_path)
        assert "capabilities:" in text
        assert not any("capabilities" in i for i in issues)

    def test_every_non_supported_capability_is_listed(
        self, codex_home: Path, tmp_path: Path
    ) -> None:
        from kiro_crew.acp import backends

        text, _ = _run(ACP_BACKEND_CODEX, tmp_path)
        for capability in backends.ALL_CAPABILITIES:
            level = backends.level(ACP_BACKEND_CODEX, capability)
            if level is not backends.Level.SUPPORTED:
                expected = "cost_reporting" if capability == backends.CAP_BILLING else capability
                assert expected in text, capability
        assert "billing=" not in text
