"""The tool-gate precondition: a session that cannot be governed must not start.

Kiro Crew's PreToolUse gate — the bundled denied-command rules, the
sensitive-path block, the governance ceiling — runs only from
``HookManager.on_tool_call``, reached only from the permission-request branch of
the dispatch parser. So a backend that does not ask per tool call is a backend
where none of those controls execute.

Every test here is revert-verified: with the guard patched out they fail.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kiro_crew.acp import claude, opencode, tool_gate
from kiro_crew.acp.codex import Verdict
from kiro_crew.acp.tool_gate import ToolGateUnroutable
from kiro_crew.acp.types import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
)


class TestAgentSpecBackendsNeedNoProbe:
    """kiro and KAS are made to ask by naming an agent on the spawn."""

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
    def test_verdict_is_routed_by_construction(self, backend: str, tmp_path: Path) -> None:
        verdict, _ = tool_gate.resolve_verdict(backend, tmp_path)
        assert verdict is Verdict.ROUTED

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
    def test_enforce_permits(self, backend: str, tmp_path: Path) -> None:
        tool_gate.enforce(backend, tmp_path, allow_ungated=False)

    def test_no_probe_touches_the_filesystem_for_kiro(self, tmp_path: Path) -> None:
        """The default path must add no filesystem work.

        The whole series is only safe if an un-opted-in installation is
        untouched, and a stat per session start is exactly the kind of thing that
        creeps in unnoticed.
        """
        work_dir = tmp_path / "wd"
        tool_gate.enforce(ACP_BACKEND_KIRO, work_dir, allow_ungated=False)
        assert not work_dir.exists()


class TestCodexSessionConfig:
    def _config_options(self, *values: str) -> list[dict]:
        return [
            {
                "id": "mode",
                "options": [{"value": value} for value in values],
            }
        ]

    def test_preflight_is_routed_by_the_session_contract(self, tmp_path: Path) -> None:
        verdict, reason = tool_gate.resolve_verdict(ACP_BACKEND_CODEX, tmp_path)
        assert verdict is Verdict.ROUTED
        assert "mode=read-only" in reason
        tool_gate.enforce(ACP_BACKEND_CODEX, tmp_path, allow_ungated=False)

    def test_required_mode_is_accepted(self) -> None:
        assert (
            tool_gate.session_config_issue(
                ACP_BACKEND_CODEX, self._config_options("agent", "read-only")
            )
            == ""
        )

    @pytest.mark.parametrize(
        "config_options, expected",
        [
            (None, "did not advertise configOptions"),
            ([], "did not advertise config option"),
            ([{"id": "model", "options": []}], "did not advertise config option"),
            ([{"id": "mode", "options": [{"value": "agent"}]}], "required value"),
        ],
    )
    def test_missing_required_mode_fails_closed(self, config_options, expected: str) -> None:
        assert expected in tool_gate.session_config_issue(ACP_BACKEND_CODEX, config_options)

    def test_runtime_refusal_names_remedy_and_opt_out(self) -> None:
        with pytest.raises(ToolGateUnroutable) as caught:
            tool_gate.enforce_runtime_routing(
                ACP_BACKEND_CODEX,
                "session/new did not advertise config option 'mode'",
                allow_ungated=False,
                remedy=tool_gate.remediation_for(ACP_BACKEND_CODEX, Path(".")),
            )
        message = str(caught.value)
        assert "mode" in message
        assert "read-only" in message
        assert "acp_backend_allow_ungated_tools" in message


class TestReadOnlyRoutingProbe:
    """GET / doctor must observe disk, not create the seed that makes ROUTED."""

    @pytest.mark.parametrize("backend", [ACP_BACKEND_CLAUDE, ACP_BACKEND_OPENCODE])
    def test_routing_verdict_does_not_write_settings(self, backend: str, tmp_path: Path) -> None:
        verdict, _ = tool_gate.routing_verdict(backend, tmp_path)
        assert verdict is Verdict.INDETERMINATE
        assert not any(tmp_path.iterdir())

    @pytest.mark.parametrize("backend", [ACP_BACKEND_CLAUDE, ACP_BACKEND_OPENCODE])
    def test_resolve_verdict_is_also_read_only(self, backend: str, tmp_path: Path) -> None:
        """``resolve_verdict`` is the same read-only probe as ``routing_verdict``."""
        verdict, _ = tool_gate.resolve_verdict(backend, tmp_path)
        assert verdict is Verdict.INDETERMINATE
        assert not any(tmp_path.iterdir())


class TestClaudeSeededSettings:
    def test_unseeded_is_made_routed_by_seeding(self, tmp_path: Path) -> None:
        tool_gate.enforce(ACP_BACKEND_CLAUDE, tmp_path, allow_ungated=False)
        written = json.loads(claude.local_settings_path(tmp_path).read_text())
        assert written["permissions"]["defaultMode"] == "default"

    def test_an_explicit_bypass_mode_is_not_overwritten(self, tmp_path: Path) -> None:
        """An explicitly configured mode is somebody's decision.

        Silently rewriting it would be Kiro Crew overruling a choice it can see,
        so the session refuses instead and the opt-out is the documented way
        through.
        """
        path = claude.local_settings_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"permissions": {"defaultMode": "auto"}, "keep": 1}))

        with pytest.raises(ToolGateUnroutable):
            tool_gate.enforce(ACP_BACKEND_CLAUDE, tmp_path, allow_ungated=False)

        after = json.loads(path.read_text())
        assert after["permissions"]["defaultMode"] == "auto"
        assert after["keep"] == 1, "unrelated keys must survive"

    def test_seeding_preserves_unrelated_keys(self, tmp_path: Path) -> None:
        path = claude.local_settings_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"availableModels": ["a", "b"]}))

        tool_gate.enforce(ACP_BACKEND_CLAUDE, tmp_path, allow_ungated=False)

        after = json.loads(path.read_text())
        assert after["permissions"]["defaultMode"] == "default"
        assert after["availableModels"] == ["a", "b"]


class TestOpenCodeSeededSettings:
    def test_unseeded_is_made_routed_by_seeding(self, tmp_path: Path) -> None:
        tool_gate.enforce(ACP_BACKEND_OPENCODE, tmp_path, allow_ungated=False)
        written = json.loads(opencode.project_config_path(tmp_path).read_text())
        assert written["permission"] == "ask"

    def test_an_explicit_allow_is_not_overwritten(self, tmp_path: Path) -> None:
        path = tmp_path / "opencode.json"
        path.write_text(json.dumps({"permission": "allow", "keep": 1}))

        with pytest.raises(ToolGateUnroutable):
            tool_gate.enforce(ACP_BACKEND_OPENCODE, tmp_path, allow_ungated=False)

        after = json.loads(path.read_text())
        assert after["permission"] == "allow"
        assert after["keep"] == 1

    def test_seeding_preserves_unrelated_keys(self, tmp_path: Path) -> None:
        path = tmp_path / "opencode.json"
        path.write_text(json.dumps({"model": "kept"}))
        tool_gate.enforce(ACP_BACKEND_OPENCODE, tmp_path, allow_ungated=False)
        after = json.loads(path.read_text())
        assert after["permission"] == "ask"
        assert after["model"] == "kept"

    def test_probe_reads_the_file_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A write that never landed must not be trusted as ROUTED."""
        monkeypatch.setattr(
            "kiro_crew.acp.opencode.ensure_routed_settings",
            lambda work_dir: True,
        )
        with pytest.raises(ToolGateUnroutable):
            tool_gate.enforce(ACP_BACKEND_OPENCODE, tmp_path, allow_ungated=False)
        assert not (tmp_path / "opencode.json").exists()


class TestInBandDetection:
    """Layer 2: observed behaviour, for when the config claimed otherwise.

    Detection, not prevention — by the time a tool_call frame arrives the adapter
    has already run it. These tests pin that the report fires when it should and,
    just as importantly, does not fire when it should not.
    """

    def _client(self, backend: str, tmp_path: Path):
        from kiro_crew.acp.client import AcpClient

        return AcpClient(agent="kirocrew", work_dir=str(tmp_path), acp_backend=backend)

    def test_reports_for_an_external_policy_backend(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        client = self._client(ACP_BACKEND_CODEX, tmp_path)
        with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
            client._note_ungated_execute_tool()
        assert "without sending session/request_permission" in caplog.text

    def test_silent_once_a_permission_request_was_seen(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A backend that asks at all is gated; do not cry wolf."""
        client = self._client(ACP_BACKEND_CODEX, tmp_path)
        client._saw_permission_request = True
        with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
            client._note_ungated_execute_tool()
        assert caplog.text == ""

    def test_reports_at_most_once_per_process(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A long ungated session must not flood the log."""
        client = self._client(ACP_BACKEND_CODEX, tmp_path)
        with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
            for _ in range(5):
                client._note_ungated_execute_tool()
        assert caplog.text.count("without sending session/request_permission") == 1

    @pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS])
    def test_never_reports_for_agent_spec_backends(
        self, backend: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """kiro and KAS auto-approve reads without a permission frame by design.

        Applying the check to them would report a false positive on every
        session, which is how a real signal gets trained away.
        """
        client = self._client(backend, tmp_path)
        with caplog.at_level("WARNING", logger="kiro_crew.acp.client"):
            client._note_ungated_execute_tool()
        assert caplog.text == ""


class TestOptOut:
    def test_opt_out_permits_a_runtime_routing_failure(self) -> None:
        assert not tool_gate.enforce_runtime_routing(
            ACP_BACKEND_CODEX,
            "required session mode was rejected",
            allow_ungated=True,
        )

    def test_opt_out_warns_naming_the_unenforced_controls(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="kiro_crew.acp.tool_gate"):
            tool_gate.enforce_runtime_routing(
                ACP_BACKEND_CODEX,
                "required session mode was rejected",
                allow_ungated=True,
            )
        text = caplog.text
        assert "denied-command rules" in text
        assert "sensitive-path block" in text
        assert "governance ceiling" in text
        assert "acp_backend_allow_ungated_tools" in text

    def test_opt_out_does_not_warn_when_preflight_is_routed(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("WARNING", logger="kiro_crew.acp.tool_gate"):
            tool_gate.enforce(ACP_BACKEND_CODEX, tmp_path, allow_ungated=True)
        assert "NOT consulted" not in caplog.text
