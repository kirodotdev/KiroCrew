"""The ``kirocrew update`` agent-only refresh must be hardened at its call site.

The refresh runs ``setup --agent-only`` as a child with ``capture_output=True``,
so two properties are load-bearing (issue #5616):

* ``stdin`` is redirected to ``DEVNULL`` — a captured-output child must never
  inherit the parent terminal, or any prompt it asks is invisible and blocks
  silently until the timeout.
* ``subprocess.TimeoutExpired`` is swallowed — the refresh is best-effort and
  runs after the update already succeeded, so a timeout must downgrade to a
  warning, never traceback out of ``kirocrew update``.
"""

from __future__ import annotations

import inspect
import subprocess

from kiro_crew import cli_server


class _RunRecorder:
    """Record the kwargs ``_refresh_agent_config`` passes to ``subprocess.run``."""

    def __init__(self, returncode: int = 0) -> None:
        self.calls: list[dict] = []
        self._returncode = returncode

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": argv, **kwargs})
        completed = subprocess.CompletedProcess(argv, self._returncode, stdout="", stderr="")
        return completed


class TestRefreshAgentConfigStdin:
    def test_child_stdin_is_devnull(self, monkeypatch, tmp_path) -> None:
        """The child must read EOF, not the parent terminal.

        Asserted on the recorded kwargs rather than by running a real child:
        the property under test is the call-site contract itself.
        """
        recorder = _RunRecorder()
        monkeypatch.setattr(cli_server.subprocess, "run", recorder)

        cli_server._refresh_agent_config(str(tmp_path))

        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call.get("stdin") is subprocess.DEVNULL
        # The refresh keeps its pre-existing shape alongside the hardening.
        assert call.get("capture_output") is True
        assert call.get("timeout") == 30
        assert call["argv"][-2:] == ["setup", "--agent-only"]


class TestRefreshAgentConfigOutcome:
    def test_success_path_reports_refreshed(self, monkeypatch, tmp_path, capsys) -> None:
        monkeypatch.setattr(cli_server.subprocess, "run", _RunRecorder(returncode=0))

        cli_server._refresh_agent_config(str(tmp_path))

        assert "Agent config refreshed" in capsys.readouterr().out

    def test_nonzero_exit_downgrades_to_warning(self, monkeypatch, tmp_path, capsys) -> None:
        monkeypatch.setattr(cli_server.subprocess, "run", _RunRecorder(returncode=1))

        cli_server._refresh_agent_config(str(tmp_path))

        assert "Agent config refresh failed" in capsys.readouterr().out


class TestRefreshAgentConfigTimeout:
    def test_timeout_is_swallowed_and_warned(self, monkeypatch, tmp_path, capsys, caplog) -> None:
        """A timed-out refresh must not raise — the update already succeeded."""

        def _timeout(argv, **kwargs):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout", 30))

        monkeypatch.setattr(cli_server.subprocess, "run", _timeout)

        # The assertion is the absence of an exception; the messages pin the
        # downgrade path so a silent swallow cannot pass either.
        cli_server._refresh_agent_config(str(tmp_path))

        out = capsys.readouterr().out
        assert "Agent config refresh timed out" in out
        assert "kirocrew setup --agent-only" in out
        assert any("timed out" in rec.message for rec in caplog.records)


class TestUpdateWiring:
    def test_update_git_path_calls_the_refresh_helper(self) -> None:
        """``_update()`` must keep routing its refresh through the hardened helper.

        Source-level pin (the pattern test_update_git_guard.py already uses):
        driving the whole ``_update()`` flow needs a git checkout, a release
        feed, and an installer, all irrelevant to this contract.
        """
        src = inspect.getsource(cli_server._update)
        assert "_refresh_agent_config(proj)" in src

    def test_the_hardened_helper_is_the_only_agent_only_spawn(self) -> None:
        """No second, unhardened copy of the refresh anywhere in the module.

        Counted over the whole module rather than asserted absent from
        ``_update()`` alone, so a future prose mention of the flag in some
        other branch cannot be misread as a duplicated spawn.
        """
        module_src = inspect.getsource(cli_server)
        assert module_src.count('"setup", "--agent-only"') == 1
