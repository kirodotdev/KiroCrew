"""Tests for _consolidate_cmd CLI function and session expire callback."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


class TestConsolidateCmd:
    """Cover _consolidate_cmd paths in cli.py."""

    def _make_session_file(self, tmp_path: Path, name: str = "test_session") -> Path:
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(exist_ok=True)
        f = sessions_dir / f"{name}.jsonl"
        f.write_text('{"role":"user","content":"hi","ts":"2026-05-18T10:00:00"}\n')
        return sessions_dir

    @patch("kiro_crew.cli.sel")
    @patch("kiro_crew.cli.SkillsLoader")
    @patch("kiro_crew.cli.SessionManager")
    @patch("kiro_crew.cli.MemoryStore")
    @patch("kiro_crew.cli.HistoryConsolidator")
    @patch("kiro_crew.cli.ConversationLog")
    @patch("kiro_crew.cli.KiroCrewConfig")
    def test_list_sessions(
        self, mock_cfg_cls, mock_log_cls, mock_consolidator_cls,
        mock_mem_cls, mock_sess_cls, mock_skills_cls, mock_sel,
        tmp_path, capsys,
    ):
        sessions_dir = self._make_session_file(tmp_path)
        mock_cfg_cls.load.return_value = MagicMock()
        mock_log = mock_log_cls.return_value
        mock_log._dir = sessions_dir
        mock_log.unconsolidated_count.return_value = 5

        from kiro_crew.cli import _consolidate_cmd

        args = argparse.Namespace(session_key=None, consolidate_all=False)
        _consolidate_cmd(args)

        captured = capsys.readouterr()
        assert "test_session" in captured.out
        assert "5 messages" in captured.out

    @patch("kiro_crew.cli.sel")
    @patch("kiro_crew.cli.SkillsLoader")
    @patch("kiro_crew.cli.SessionManager")
    @patch("kiro_crew.cli.MemoryStore")
    @patch("kiro_crew.cli.HistoryConsolidator")
    @patch("kiro_crew.cli.ConversationLog")
    @patch("kiro_crew.cli.KiroCrewConfig")
    def test_list_sessions_none_found(
        self, mock_cfg_cls, mock_log_cls, mock_consolidator_cls,
        mock_mem_cls, mock_sess_cls, mock_skills_cls, mock_sel,
        tmp_path, capsys,
    ):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        mock_cfg_cls.load.return_value = MagicMock()
        mock_log = mock_log_cls.return_value
        mock_log._dir = sessions_dir
        mock_log.unconsolidated_count.return_value = 0

        from kiro_crew.cli import _consolidate_cmd

        args = argparse.Namespace(session_key=None, consolidate_all=False)
        _consolidate_cmd(args)

        captured = capsys.readouterr()
        assert "No sessions with unconsolidated messages" in captured.out

    @patch("kiro_crew.cli.sel")
    @patch("kiro_crew.cli.SkillsLoader")
    @patch("kiro_crew.cli.SessionManager")
    @patch("kiro_crew.cli.MemoryStore")
    @patch("kiro_crew.cli.HistoryConsolidator")
    @patch("kiro_crew.cli.ConversationLog")
    @patch("kiro_crew.cli.KiroCrewConfig")
    def test_consolidate_all(
        self, mock_cfg_cls, mock_log_cls, mock_consolidator_cls,
        mock_mem_cls, mock_sess_cls, mock_skills_cls, mock_sel,
        tmp_path, capsys,
    ):
        sessions_dir = self._make_session_file(tmp_path, "sess1")
        self._make_session_file(tmp_path, "sess2")
        mock_cfg_cls.load.return_value = MagicMock()
        mock_log = mock_log_cls.return_value
        mock_log._dir = sessions_dir
        mock_log.unconsolidated_count.return_value = 3

        mock_consolidator = mock_consolidator_cls.return_value
        mock_consolidator.consolidate_now = AsyncMock()

        from kiro_crew.cli import _consolidate_cmd

        args = argparse.Namespace(session_key=None, consolidate_all=True)
        _consolidate_cmd(args)

        captured = capsys.readouterr()
        assert "Consolidating" in captured.out
        assert "done" in captured.out
        assert mock_sel.return_value.log_api_access.call_count >= 1

    @patch("kiro_crew.cli.sel")
    @patch("kiro_crew.cli.SkillsLoader")
    @patch("kiro_crew.cli.SessionManager")
    @patch("kiro_crew.cli.MemoryStore")
    @patch("kiro_crew.cli.HistoryConsolidator")
    @patch("kiro_crew.cli.ConversationLog")
    @patch("kiro_crew.cli.KiroCrewConfig")
    def test_consolidate_all_none_found(
        self, mock_cfg_cls, mock_log_cls, mock_consolidator_cls,
        mock_mem_cls, mock_sess_cls, mock_skills_cls, mock_sel,
        tmp_path, capsys,
    ):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        mock_cfg_cls.load.return_value = MagicMock()
        mock_log = mock_log_cls.return_value
        mock_log._dir = sessions_dir
        mock_log.unconsolidated_count.return_value = 0

        from kiro_crew.cli import _consolidate_cmd

        args = argparse.Namespace(session_key=None, consolidate_all=True)
        _consolidate_cmd(args)

        captured = capsys.readouterr()
        assert "No sessions with unconsolidated messages" in captured.out

    @patch("kiro_crew.cli.sel")
    @patch("kiro_crew.cli.SkillsLoader")
    @patch("kiro_crew.cli.SessionManager")
    @patch("kiro_crew.cli.MemoryStore")
    @patch("kiro_crew.cli.HistoryConsolidator")
    @patch("kiro_crew.cli.ConversationLog")
    @patch("kiro_crew.cli.KiroCrewConfig")
    def test_consolidate_single_session(
        self, mock_cfg_cls, mock_log_cls, mock_consolidator_cls,
        mock_mem_cls, mock_sess_cls, mock_skills_cls, mock_sel,
        tmp_path, capsys,
    ):
        sessions_dir = self._make_session_file(tmp_path)
        mock_cfg_cls.load.return_value = MagicMock()
        mock_log = mock_log_cls.return_value
        mock_log._dir = sessions_dir
        mock_log.unconsolidated_count.return_value = 7

        mock_consolidator = mock_consolidator_cls.return_value
        mock_consolidator.consolidate_now = AsyncMock()

        from kiro_crew.cli import _consolidate_cmd

        args = argparse.Namespace(session_key="test_session", consolidate_all=False)
        _consolidate_cmd(args)

        captured = capsys.readouterr()
        assert "Consolidating session: test_session" in captured.out
        assert "done" in captured.out
        mock_sel.return_value.log_api_access.assert_called_with(
            caller="cli", operation="consolidate", outcome="allowed",
            source="cli", resources="test_session",
        )

    @patch("kiro_crew.cli.sel")
    @patch("kiro_crew.cli.SkillsLoader")
    @patch("kiro_crew.cli.SessionManager")
    @patch("kiro_crew.cli.MemoryStore")
    @patch("kiro_crew.cli.HistoryConsolidator")
    @patch("kiro_crew.cli.ConversationLog")
    @patch("kiro_crew.cli.KiroCrewConfig")
    def test_consolidate_single_no_messages(
        self, mock_cfg_cls, mock_log_cls, mock_consolidator_cls,
        mock_mem_cls, mock_sess_cls, mock_skills_cls, mock_sel,
        tmp_path, capsys,
    ):
        sessions_dir = self._make_session_file(tmp_path)
        mock_cfg_cls.load.return_value = MagicMock()
        mock_log = mock_log_cls.return_value
        mock_log._dir = sessions_dir
        mock_log.unconsolidated_count.return_value = 0

        from kiro_crew.cli import _consolidate_cmd

        args = argparse.Namespace(session_key="test_session", consolidate_all=False)
        _consolidate_cmd(args)

        captured = capsys.readouterr()
        assert "no unconsolidated messages, skipping" in captured.out


class TestOnSessionExpire:
    """Cover on_session_expire callback via real SessionManager._expire_idle."""

    @patch("kiro_crew.session.sel")
    @patch("kiro_crew.session.SessionManager.reset", new_callable=AsyncMock)
    @patch("kiro_crew.session.Stats")
    def test_expire_idle_fires_callback(self, mock_stats, mock_reset, mock_sel):
        """A real SessionManager._expire_idle invokes on_session_expire for expired keys."""
        import time as _time

        from kiro_crew.session import SessionManager, _Session

        cfg = MagicMock()
        cfg.session.pool_size = 0
        cfg.session.pool_agent = ""
        cfg.session.pool_ttl_secs = 0
        sm = SessionManager(cfg, provider_factory=MagicMock())
        callback = MagicMock()
        sm.on_session_expire = callback

        # Inject an expired session
        sess = MagicMock(spec=_Session)
        sess.last_used = _time.monotonic() - 9999
        sm._sessions["expired-key"] = sess

        import asyncio
        asyncio.run(sm._expire_idle(60))

        callback.assert_called_once_with("expired-key")

    @patch("kiro_crew.session.sel")
    @patch("kiro_crew.session.SessionManager.reset", new_callable=AsyncMock)
    @patch("kiro_crew.session.Stats")
    def test_expire_idle_callback_exception_swallowed(self, mock_stats, mock_reset, mock_sel):
        """Callback exceptions don't prevent session reset."""
        import time as _time

        from kiro_crew.session import SessionManager, _Session

        cfg = MagicMock()
        cfg.session.pool_size = 0
        cfg.session.pool_agent = ""
        cfg.session.pool_ttl_secs = 0
        sm = SessionManager(cfg, provider_factory=MagicMock())
        sm.on_session_expire = MagicMock(side_effect=RuntimeError("boom"))

        sess = MagicMock(spec=_Session)
        sess.last_used = _time.monotonic() - 9999
        sm._sessions["expired-key"] = sess

        import asyncio
        asyncio.run(sm._expire_idle(60))

        # reset still called despite callback failure
        mock_reset.assert_called_once_with("expired-key")


class TestGatewayExpireWiring:
    """Verify on_session_expire attribute is settable on a real SessionManager."""

    def test_session_expire_wiring_on_real_instance(self):
        """A real SessionManager instance accepts on_session_expire assignment."""
        from kiro_crew.session import SessionManager

        cfg = MagicMock()
        cfg.session.pool_size = 0
        cfg.session.pool_agent = ""
        cfg.session.pool_ttl_secs = 0
        sm = SessionManager(cfg, provider_factory=MagicMock())

        callback = MagicMock()
        sm.on_session_expire = callback
        assert sm.on_session_expire is callback


class TestConsolidateCmdExceptionPath:
    """Cover the exception path in _consolidate_cmd's _run loop."""

    def _make_session_file(self, tmp_path, name="test_session"):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir(exist_ok=True)
        f = sessions_dir / f"{name}.jsonl"
        f.write_text('{"role":"user","content":"hi","ts":"2026-05-18T10:00:00"}\n')
        return sessions_dir

    @patch("kiro_crew.cli.sel")
    @patch("kiro_crew.cli.SkillsLoader")
    @patch("kiro_crew.cli.SessionManager")
    @patch("kiro_crew.cli.MemoryStore")
    @patch("kiro_crew.cli.HistoryConsolidator")
    @patch("kiro_crew.cli.ConversationLog")
    @patch("kiro_crew.cli.KiroCrewConfig")
    def test_consolidate_single_exception_logged(
        self, mock_cfg_cls, mock_log_cls, mock_consolidator_cls,
        mock_mem_cls, mock_sess_cls, mock_skills_cls, mock_sel,
        tmp_path, capsys,
    ):
        """When consolidate_now raises, the exception is caught and logged."""
        sessions_dir = self._make_session_file(tmp_path)
        mock_cfg_cls.load.return_value = MagicMock()
        mock_log = mock_log_cls.return_value
        mock_log._dir = sessions_dir
        mock_log.unconsolidated_count.return_value = 5

        mock_consolidator = mock_consolidator_cls.return_value
        mock_consolidator.consolidate_now = AsyncMock(side_effect=RuntimeError("LLM down"))

        from kiro_crew.cli import _consolidate_cmd

        args = argparse.Namespace(session_key="test_session", consolidate_all=False)
        _consolidate_cmd(args)

        captured = capsys.readouterr()
        assert "Consolidating session: test_session" in captured.out
        assert "done" not in captured.out


class TestExpireIdleSelFailure:
    """Cover the SEL failure path in session.py _expire_idle."""

    @patch("kiro_crew.session.sel")
    @patch("kiro_crew.session.SessionManager.reset", new_callable=AsyncMock)
    @patch("kiro_crew.session.Stats")
    def test_expire_idle_sel_failure_still_resets(self, mock_stats, mock_reset, mock_sel):
        """When sel().log_api_access raises, the session is still reset."""
        import time as _time

        from kiro_crew.session import SessionManager, _Session

        cfg = MagicMock()
        cfg.session.pool_size = 0
        cfg.session.pool_agent = ""
        cfg.session.pool_ttl_secs = 0
        sm = SessionManager(cfg, provider_factory=MagicMock())

        mock_sel.return_value.log_api_access.side_effect = RuntimeError("SEL broken")
        callback = MagicMock()
        sm.on_session_expire = callback

        sess = MagicMock(spec=_Session)
        sess.last_used = _time.monotonic() - 9999
        sm._sessions["expired-sel"] = sess

        import asyncio
        asyncio.run(sm._expire_idle(60))

        callback.assert_not_called()
        mock_reset.assert_called_once_with("expired-sel")

    @patch("kiro_crew.session.sel")
    @patch("kiro_crew.session.SessionManager.reset", new_callable=AsyncMock)
    @patch("kiro_crew.session.Stats")
    def test_expire_idle_callback_failure_still_resets(self, mock_stats, mock_reset, mock_sel):
        """When on_session_expire raises, the session is still reset."""
        import time as _time

        from kiro_crew.session import SessionManager, _Session

        cfg = MagicMock()
        cfg.session.pool_size = 0
        cfg.session.pool_agent = ""
        cfg.session.pool_ttl_secs = 0
        sm = SessionManager(cfg, provider_factory=MagicMock())

        callback = MagicMock(side_effect=RuntimeError("callback boom"))
        sm.on_session_expire = callback

        sess = MagicMock(spec=_Session)
        sess.last_used = _time.monotonic() - 9999
        sm._sessions["expired-cb"] = sess

        import asyncio
        asyncio.run(sm._expire_idle(60))

        callback.assert_called_once_with("expired-cb")
        mock_reset.assert_called_once_with("expired-cb")
