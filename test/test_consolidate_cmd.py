"""Tests for _consolidate_cmd CLI function and session expire callback."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _draining(mock_log):
    """A ``consolidate_now`` that leaves the tail empty, as the real one does.

    The real call repeats bounded passes until nothing is left, so the count the
    command re-reads afterwards is what says whether it got there.
    """

    async def _consolidate_now(_key: str) -> bool:
        mock_log.unconsolidated_count.return_value = 0
        return True

    return _consolidate_now


class TestConsolidateCmd:
    """Cover _consolidate_cmd paths in cli_knowledge.py."""

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
        # consolidate_now drains the tail before it returns, so the count the
        # command reports from afterwards is zero. The command prints what the
        # transcript says rather than the call's flag — see the partial-drain
        # test below.
        mock_consolidator.consolidate_now = AsyncMock(side_effect=_draining(mock_log))

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
        mock_consolidator.consolidate_now = AsyncMock(side_effect=_draining(mock_log))

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
    def test_consolidate_reports_a_remainder_instead_of_done(
        self, mock_cfg_cls, mock_log_cls, mock_consolidator_cls,
        mock_mem_cls, mock_sess_cls, mock_skills_cls, mock_sel,
        tmp_path, capsys,
    ):
        """A drain that stops short must not be reported as a completed pass.

        This process exits when the command returns — no idle sweep follows it —
        so ``done`` over a surviving tail is the last word on messages nothing
        has read.
        """
        sessions_dir = self._make_session_file(tmp_path)
        mock_cfg_cls.load.return_value = MagicMock()
        mock_log = mock_log_cls.return_value
        mock_log._dir = sessions_dir
        mock_log.unconsolidated_count.return_value = 40

        mock_consolidator = mock_consolidator_cls.return_value
        mock_consolidator.consolidate_now = AsyncMock(return_value=True)

        from kiro_crew.cli import _consolidate_cmd

        args = argparse.Namespace(session_key="test_session", consolidate_all=False)
        with pytest.raises(SystemExit) as exit_info:
            _consolidate_cmd(args)

        assert exit_info.value.code == 1
        captured = capsys.readouterr()
        assert "test_session: partially consolidated, 40 message(s) remain" in captured.err
        assert "done" not in captured.out
        assert "Done." not in captured.out

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
        # The idle sweep now skips sessions with a turn in flight, so the stub
        # has to answer semaphore.locked() the way a real _Session does.
        # spec=_Session does not supply it: dataclass fields built with
        # default_factory are instance attributes, not class attributes.
        sess.semaphore = MagicMock()
        sess.semaphore.locked.return_value = False
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
        # The idle sweep now skips sessions with a turn in flight, so the stub
        # has to answer semaphore.locked() the way a real _Session does.
        # spec=_Session does not supply it: dataclass fields built with
        # default_factory are instance attributes, not class attributes.
        sess.semaphore = MagicMock()
        sess.semaphore.locked.return_value = False
        sm._sessions["expired-key"] = sess

        import asyncio
        asyncio.run(sm._expire_idle(60))

        # reset still called despite callback failure
        mock_reset.assert_called_once_with(
            "expired-key", expect_session=sess, skip_if_busy=True, skip_if_injecting=True
        )


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
        with pytest.raises(SystemExit) as exit_info:
            _consolidate_cmd(args)

        assert exit_info.value.code == 1
        captured = capsys.readouterr()
        assert "Consolidating session: test_session" in captured.out
        assert "done" not in captured.out
        assert "Done." not in captured.out
        # One line naming the key and the exception type; the message itself
        # can carry transcript text, so it stays out of the terminal.
        failure_lines = [line for line in captured.err.splitlines() if "test_session" in line]
        assert failure_lines == ["  test_session: failed (RuntimeError)"]
        assert "LLM down" not in captured.err

    @patch("kiro_crew.cli.sel")
    @patch("kiro_crew.cli.SkillsLoader")
    @patch("kiro_crew.cli.SessionManager")
    @patch("kiro_crew.cli.MemoryStore")
    @patch("kiro_crew.cli.HistoryConsolidator")
    @patch("kiro_crew.cli.ConversationLog")
    @patch("kiro_crew.cli.KiroCrewConfig")
    def test_consolidate_all_reports_each_failed_key_and_exits_1(
        self, mock_cfg_cls, mock_log_cls, mock_consolidator_cls,
        mock_mem_cls, mock_sess_cls, mock_skills_cls, mock_sel,
        tmp_path, capsys,
    ):
        """One failed key in an --all run is named on stderr and fails the run.

        The keys that finish keep their ``done`` line on stdout, and the loop
        still reaches every key after the failure.
        """
        sessions_dir = self._make_session_file(tmp_path, "good")
        self._make_session_file(tmp_path, "bad")
        self._make_session_file(tmp_path, "later")
        mock_cfg_cls.load.return_value = MagicMock()
        mock_log = mock_log_cls.return_value
        mock_log._dir = sessions_dir
        remaining = {"good": 3, "bad": 3, "later": 3}
        mock_log.unconsolidated_count.side_effect = lambda key: remaining[key]

        async def _consolidate_now(key: str) -> bool:
            if key == "bad":
                raise RuntimeError("secret transcript text")
            remaining[key] = 0
            return True

        mock_consolidator = mock_consolidator_cls.return_value
        mock_consolidator.consolidate_now = AsyncMock(side_effect=_consolidate_now)

        from kiro_crew.cli import _consolidate_cmd

        args = argparse.Namespace(session_key=None, consolidate_all=True)
        with pytest.raises(SystemExit) as exit_info:
            _consolidate_cmd(args)

        assert exit_info.value.code == 1
        captured = capsys.readouterr()
        assert "  good: done ✓" in captured.out
        assert "  later: done ✓" in captured.out
        assert "  bad: failed (RuntimeError)" in captured.err
        assert "1 session(s) did not finish consolidating." in captured.err
        assert "secret transcript text" not in captured.err + captured.out

    @patch("kiro_crew.cli.sel")
    @patch("kiro_crew.cli.SkillsLoader")
    @patch("kiro_crew.cli.SessionManager")
    @patch("kiro_crew.cli.MemoryStore")
    @patch("kiro_crew.cli.HistoryConsolidator")
    @patch("kiro_crew.cli.ConversationLog")
    @patch("kiro_crew.cli.KiroCrewConfig")
    def test_consolidate_finished_and_skipped_keys_exit_0(
        self, mock_cfg_cls, mock_log_cls, mock_consolidator_cls,
        mock_mem_cls, mock_sess_cls, mock_skills_cls, mock_sel,
        tmp_path, capsys,
    ):
        """A key that finishes and a key with nothing to do keep today's output."""
        sessions_dir = self._make_session_file(tmp_path, "good")
        mock_cfg_cls.load.return_value = MagicMock()
        mock_log = mock_log_cls.return_value
        mock_log._dir = sessions_dir
        mock_log.unconsolidated_count.return_value = 3
        mock_consolidator = mock_consolidator_cls.return_value
        mock_consolidator.consolidate_now = AsyncMock(side_effect=_draining(mock_log))

        from kiro_crew.cli import _consolidate_cmd

        _consolidate_cmd(argparse.Namespace(session_key="good", consolidate_all=False))
        _consolidate_cmd(argparse.Namespace(session_key="empty", consolidate_all=False))

        captured = capsys.readouterr()
        assert "  good: done ✓" in captured.out
        assert "  empty: no unconsolidated messages, skipping" in captured.out
        assert captured.out.count("Done. Check ~/.kiro/crew/skills/auto/ for new skills.") == 2
        assert captured.err == ""

    @patch("kiro_crew.cli.sel")
    @patch("kiro_crew.cli.SkillsLoader")
    @patch("kiro_crew.cli.SessionManager")
    @patch("kiro_crew.cli.MemoryStore")
    @patch("kiro_crew.cli.HistoryConsolidator")
    @patch("kiro_crew.cli.ConversationLog")
    @patch("kiro_crew.cli.KiroCrewConfig")
    def test_consolidate_sensitive_session_skip_exits_0(
        self, mock_cfg_cls, mock_log_cls, mock_consolidator_cls,
        mock_mem_cls, mock_sess_cls, mock_skills_cls, mock_sel,
        tmp_path, capsys,
    ):
        """consolidate_now's deliberate sensitive-session skip is a skip, not a failure.

        It returns True and leaves the tail, so the remainder alone cannot tell
        it from a stalled drain; a rerun never changes it, so exit 1 here would
        fail every later run.
        """
        sessions_dir = self._make_session_file(tmp_path, "secretive")
        mock_cfg_cls.load.return_value = MagicMock()
        mock_log = mock_log_cls.return_value
        mock_log._dir = sessions_dir
        mock_log.unconsolidated_count.return_value = 4
        mock_log._read_messages.return_value = [
            {"role": "tool", "content": "fs_read ~/.ssh/config"},
        ]
        mock_consolidator = mock_consolidator_cls.return_value
        mock_consolidator.consolidate_now = AsyncMock(return_value=True)

        from kiro_crew.cli import _consolidate_cmd

        _consolidate_cmd(argparse.Namespace(session_key="secretive", consolidate_all=False))

        captured = capsys.readouterr()
        assert "  secretive: sensitive session, skipping (4 message(s) left)" in captured.out
        assert "Done." in captured.out
        assert captured.err == ""


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
        # The idle sweep now skips sessions with a turn in flight, so the stub
        # has to answer semaphore.locked() the way a real _Session does.
        # spec=_Session does not supply it: dataclass fields built with
        # default_factory are instance attributes, not class attributes.
        sess.semaphore = MagicMock()
        sess.semaphore.locked.return_value = False
        sm._sessions["expired-sel"] = sess

        import asyncio
        asyncio.run(sm._expire_idle(60))

        callback.assert_not_called()
        mock_reset.assert_called_once_with(
            "expired-sel", expect_session=sess, skip_if_busy=True, skip_if_injecting=True
        )

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
        # The idle sweep now skips sessions with a turn in flight, so the stub
        # has to answer semaphore.locked() the way a real _Session does.
        # spec=_Session does not supply it: dataclass fields built with
        # default_factory are instance attributes, not class attributes.
        sess.semaphore = MagicMock()
        sess.semaphore.locked.return_value = False
        sm._sessions["expired-cb"] = sess

        import asyncio
        asyncio.run(sm._expire_idle(60))

        callback.assert_called_once_with("expired-cb")
        mock_reset.assert_called_once_with(
            "expired-cb", expect_session=sess, skip_if_busy=True, skip_if_injecting=True
        )


def test_refused_consolidation_cli_does_not_claim_zero_spend(capsys):
    from kiro_crew.cli import _consolidate_cmd

    with (
        patch("kiro_crew.cli.sel"),
        patch("kiro_crew.cli.SkillsLoader"),
        patch("kiro_crew.cli.SessionManager"),
        patch("kiro_crew.cli.MemoryStore"),
        patch("kiro_crew.cli.KiroCrewConfig"),
        patch("kiro_crew.cli.build_provider_factory"),
        patch("kiro_crew.cli.ConversationLog") as log,
        patch("kiro_crew.cli.HistoryConsolidator") as consolidator,
    ):
        log.return_value.unconsolidated_count.return_value = 3
        consolidator.return_value.consolidate_now = AsyncMock(return_value=False)
        with pytest.raises(SystemExit) as exit_info:
            _consolidate_cmd(argparse.Namespace(session_key="test_session", consolidate_all=False))
    assert exit_info.value.code == 1
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "test_session: no completed pass recorded" in captured.err
    assert "nothing ran" not in output
    assert "done" not in output
