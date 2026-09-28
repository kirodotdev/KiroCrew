"""Redact-before-bound + tail anchoring on failed-command stderr excerpts.

Several sites bound a failed subprocess's stderr to a fixed character count
before surfacing it in a return value, an exception, or a log line. Two defects
share those lines:

* **Anchor.** A crashed command (``gh``, ``systemd-run``, ``launchctl``, npm,
  mise, tailscale, a TTS backend, a non-deny hook exit) prints its diagnosis
  LAST, so a head cut (``[:N]``) shows startup noise and drops the reason.
  Text a program AUTHORED for a reader -- a hook's stdout, or its exit-2 deny
  reason -- starts at the head and keeps ``[:N]``.
* **Order.** Bounding BEFORE redaction can cut a credential mid-match, leaving
  a fragment no redaction regex recognises. Redacting the WHOLE stream first
  means a cut can at worst split a redaction marker, never a secret.

The straddle tests place a fabricated key so the tail window's LEFT edge falls
inside it: a raw slice keeps the key's right half, a slice-then-redact reorder
keeps the same unmatched half, and only redact-then-slice yields a marker. The
tail-anchor tests put one marker on the first line and one on the last of a
stream longer than the bound: exactly the last-line marker must survive.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_theme_clone_stderr_redact_before_bound import _find_slice_before_redact_offenders

import kiro_crew.apps.builtins.dev_fleet.gateway_service as gateway_service
import kiro_crew.apps.builtins.ops_mission_control.backend.providers.github_issues as github_issues
import kiro_crew.cli_setup as cli_setup
import kiro_crew.dashboard.tailnet as tailnet
import kiro_crew.env as env_mod
import kiro_crew.voice_reply as voice_reply
from kiro_crew.apps.builtins.ops_mission_control.backend.models import ACTION_COMMENT, Signal
from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    HOOK_EVENT_USER_PROMPT_SUBMIT,
    ScriptHook,
    run_script_hook,
)

# Built at runtime so no credential-shaped literal lands in the repo.
_FAKE_KEY = "AKIA" + "B" * 16  # matches the AWS access-key-id pattern
_MARKER = "[REDACTED:"
_MARKER_TAIL = "credential]"  # the redaction marker ends "...: credential]"


def _straddling(bound: int) -> str:
    """A stream whose last *bound* chars begin 4 chars INTO the fake key.

    A raw ``[-bound:]`` keeps ``"B" * 16 + tail``: no ``AKIA`` prefix, so the
    fragment matches nothing and leaks; the same is true after a slice-then-
    redact reorder. Redact-then-slice yields the marker's tail instead.
    """
    tail = "e" * (bound - 16)
    return "p" * 40 + _FAKE_KEY + tail


def _two_markers(bound: int) -> str:
    """A stream longer than *bound* with a distinct marker on its first and last line."""
    return "FIRST-LINE-MARKER\n" + "z" * (bound + 50) + "\nLAST-LINE-MARKER"


def _assert_straddle_redacted(excerpt: str, bound: int) -> None:
    assert _FAKE_KEY not in excerpt
    assert "B" * 16 not in excerpt, excerpt  # the raw right half must not survive
    # Positive proof the payload flowed through redaction (not a vacuous pass on
    # an empty excerpt): the tail window opens INSIDE the replacement marker, so
    # the marker's tail, not the key's, is what survives the cut.
    assert _MARKER_TAIL in excerpt
    assert len(excerpt) <= bound


def _assert_tail_anchored(excerpt: str) -> None:
    assert "LAST-LINE-MARKER" in excerpt
    assert "FIRST-LINE-MARKER" not in excerpt


# ── ops_mission_control github_issues (dashboard-visible) ────────────────────


def _configured_adapter(monkeypatch: pytest.MonkeyPatch) -> github_issues.GitHubIssuesAdapter:
    monkeypatch.setattr(github_issues.GitHubIssuesAdapter, "configured", lambda self: True)
    monkeypatch.setattr(github_issues, "config_value", lambda *_a, **_k: "o/r")
    monkeypatch.setattr(github_issues, "config_list", lambda *_a, **_k: [])
    return github_issues.GitHubIssuesAdapter()


def _signal() -> Signal:
    return Signal(
        id="s1", source="github-issues", title="t", labels={"repo": "o/r", "issue_number": "7"}
    )


class TestGitHubIssuesStderrExcerpts:
    @pytest.mark.asyncio
    async def test_poll_failure_redacts_the_whole_stream_before_the_tail_cut(self, monkeypatch):
        adapter = _configured_adapter(monkeypatch)
        monkeypatch.setattr(
            github_issues, "_run_gh", AsyncMock(return_value=(1, "", _straddling(200)))
        )
        with pytest.raises(RuntimeError) as info:
            await adapter.poll()
        _assert_straddle_redacted(str(info.value).removeprefix("gh issue list failed: "), 200)

    @pytest.mark.asyncio
    async def test_poll_failure_keeps_the_tail_of_gh_stderr(self, monkeypatch):
        adapter = _configured_adapter(monkeypatch)
        monkeypatch.setattr(
            github_issues, "_run_gh", AsyncMock(return_value=(1, "", _two_markers(200)))
        )
        with pytest.raises(RuntimeError) as info:
            await adapter.poll()
        _assert_tail_anchored(str(info.value))

    @pytest.mark.asyncio
    async def test_execute_failure_redacts_the_whole_stream_before_the_tail_cut(self, monkeypatch):
        adapter = _configured_adapter(monkeypatch)
        monkeypatch.setattr(
            github_issues, "_run_gh", AsyncMock(return_value=(1, "", _straddling(200)))
        )
        result = await adapter.execute(_signal(), ACTION_COMMENT, {"note": "n"})
        assert result.ok is False
        _assert_straddle_redacted(result.error, 200)

    @pytest.mark.asyncio
    async def test_execute_failure_keeps_the_tail_of_gh_stderr(self, monkeypatch):
        adapter = _configured_adapter(monkeypatch)
        monkeypatch.setattr(
            github_issues, "_run_gh", AsyncMock(return_value=(1, "", _two_markers(200)))
        )
        result = await adapter.execute(_signal(), ACTION_COMMENT, {"note": "n"})
        _assert_tail_anchored(result.error)


# ── dev_fleet gateway_service (returned as the user-visible failure reason) ──


def _systemd(run_cmd, tmp_path: Path) -> gateway_service.SystemdBackend:
    return gateway_service.SystemdBackend(
        run_cmd,
        lambda: "kirocrew.service",
        platform="linux",
        which=lambda _name: "/usr/bin/systemctl",
        dropin_path=lambda: tmp_path / "make-live.conf",
        dropin_content=lambda _wt, _kcbin: "[Service]\n",
    )


class TestGatewayServiceStderrExcerpts:
    @pytest.mark.asyncio
    async def test_systemd_restart_redacts_the_whole_stream_before_the_tail_cut(self, tmp_path):
        backend = _systemd(AsyncMock(return_value=(1, "", _straddling(200))), tmp_path)
        ok, reason = await backend.restart_detached()
        assert ok is False
        _assert_straddle_redacted(reason, 200)

    @pytest.mark.asyncio
    async def test_systemd_restart_keeps_the_tail_of_stderr(self, tmp_path):
        backend = _systemd(AsyncMock(return_value=(1, "", _two_markers(200))), tmp_path)
        _ok, reason = await backend.restart_detached()
        _assert_tail_anchored(reason)

    @pytest.mark.asyncio
    async def test_systemd_stage_reload_failure_keeps_the_tail_of_stderr(self, tmp_path):
        backend = _systemd(AsyncMock(return_value=(1, "", _two_markers(200))), tmp_path)
        ok, code, reason = await backend.stage(tmp_path / "wt", tmp_path / "kcbin")
        assert (ok, code) == (False, "reload_failed")
        _assert_tail_anchored(reason)
        assert _MARKER not in reason  # a marker-free stream passes through unchanged

    @pytest.mark.asyncio
    async def test_launchd_restart_keeps_the_tail_of_stderr(self, monkeypatch, tmp_path):
        monkeypatch.setattr(gateway_service, "restart_contract_current", lambda _plist: True)
        monkeypatch.setattr(
            gateway_service, "loaded_restart_contract_current", lambda _printed: True
        )
        run_cmd = AsyncMock(side_effect=[(0, "printed", ""), (1, "", _two_markers(200))])
        backend = gateway_service.LaunchdBackend(
            run_cmd,
            lambda: "com.kirocrew.gateway",
            platform="darwin",
            which=lambda _n: "/bin/launchctl",
        )
        ok, reason = await backend.restart_detached()
        assert ok is False
        _assert_tail_anchored(reason)


# ── cli_setup (operator terminal) ───────────────────────────────────────────


class TestCliSetupElectronBuildExcerpts:
    def _run_setup(self, monkeypatch, capsys, *, fail_on: str) -> str:
        monkeypatch.setattr(cli_setup.platform, "system", lambda: "Darwin")
        monkeypatch.setattr(cli_setup.shutil, "which", lambda _name: "/usr/bin/node")
        monkeypatch.setattr(cli_setup, "_find_electron_dir", lambda: Path("/electron"))

        def _fake_run(argv, **_kw):
            failing = argv[0] == fail_on
            return SimpleNamespace(
                returncode=1 if failing else 0, stdout="", stderr=_two_markers(200)
            )

        monkeypatch.setattr(cli_setup.subprocess, "run", _fake_run)
        cli_setup._setup_electron()
        return capsys.readouterr().out

    def test_npm_install_failure_prints_the_tail_of_stderr(self, monkeypatch, capsys):
        out = self._run_setup(monkeypatch, capsys, fail_on="npm")
        assert "npm install failed" in out
        _assert_tail_anchored(out)

    def test_electron_build_failure_prints_the_tail_of_stderr(self, monkeypatch, capsys):
        out = self._run_setup(monkeypatch, capsys, fail_on="npx")
        assert "Electron build failed" in out
        _assert_tail_anchored(out)


# ── env.activate_mise (debug log) ───────────────────────────────────────────


class TestMiseActivationLog:
    def test_mise_failure_log_is_redacted_and_tail_anchored(self, monkeypatch, caplog):
        monkeypatch.setattr(env_mod, "_mise_bin", lambda: "/usr/bin/mise")
        stream = _two_markers(200) + "\n" + _straddling(200)
        monkeypatch.setattr(
            env_mod.subprocess,
            "run",
            lambda *_a, **_k: subprocess.CompletedProcess(_a, 1, stdout="", stderr=stream),
        )
        target = {"PATH": "/usr/bin"}
        with caplog.at_level(logging.DEBUG, logger=env_mod.logger.name):
            assert env_mod.activate_mise(target) == []
        line = next(
            r.getMessage() for r in caplog.records if "mise env --json exited" in r.getMessage()
        )
        assert "FIRST-LINE-MARKER" not in line
        assert _FAKE_KEY not in line
        assert _MARKER_TAIL in line


# ── dashboard.tailnet._run_json_detail (debug log) ──────────────────────────


class TestTailscaleFailureLog:
    def test_tailscale_failure_log_is_redacted_and_tail_anchored(self, monkeypatch, caplog):
        stream = _two_markers(200) + "\n" + _straddling(200)
        monkeypatch.setattr(tailnet, "_cli_path", lambda: "/usr/bin/tailscale")
        monkeypatch.setattr(
            tailnet.subprocess,
            "run",
            lambda *_a, **_k: subprocess.CompletedProcess(_a, 1, stdout="", stderr=stream),
        )
        with caplog.at_level(logging.DEBUG, logger=tailnet.logger.name):
            tailnet._run_json_detail(["status", "--json"])
        line = next(r.getMessage() for r in caplog.records if "exited 1" in r.getMessage())
        assert "FIRST-LINE-MARKER" not in line
        assert _FAKE_KEY not in line
        assert _MARKER_TAIL in line


# ── voice_reply._run_tts_subprocess (error log) ─────────────────────────────


class TestTtsFailureLog:
    @pytest.mark.asyncio
    async def test_tts_failure_log_is_redacted_and_tail_anchored(
        self, monkeypatch, caplog, tmp_path
    ):
        stream = (_two_markers(500) + "\n" + _straddling(500)).encode()
        proc = MagicMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(return_value=(b"", stream))

        async def _identity(cmd, *_a, **_k):
            return list(cmd), {}, None

        async def _spawn(*_argv, **_kw):
            return proc

        monkeypatch.setattr(voice_reply, "sandboxed_spawn_argv_async", _identity)
        monkeypatch.setattr(voice_reply, "cgroup_scope_argv", lambda argv: list(argv))
        monkeypatch.setattr(voice_reply, "create_subprocess_limited", _spawn)
        with caplog.at_level(logging.ERROR, logger=voice_reply.logger.name):
            ok = await voice_reply._run_tts_subprocess(
                ["say"], str(tmp_path / "out.wav"), label="say", display="say"
            )
        assert ok is False
        line = next(r.getMessage() for r in caplog.records if "failed (rc=1)" in r.getMessage())
        assert "LAST-LINE-MARKER" not in line  # the straddle block is the true tail here
        assert "FIRST-LINE-MARKER" not in line
        assert _FAKE_KEY not in line
        assert _MARKER_TAIL in line


# ── hooks.run_script_hook: last_error anchor follows the exit code ──────────


def _hook_command(script: Path, exit_code: int) -> str:
    script.write_text(
        "import sys\n"
        "sys.stderr.write('FIRST-LINE-MARKER\\n' + 'z' * 600 + '\\nLAST-LINE-MARKER\\n')\n"
        f"raise SystemExit({exit_code})\n",
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


class TestHookLastErrorAnchor:
    @pytest.fixture(autouse=True)
    def _passthrough_sandbox(self, monkeypatch):
        # These tests exercise the last_error cut, not host sandbox discovery: a
        # host without a sandbox backend (Windows CI) otherwise fails closed
        # with exit -1 before the hook runs.
        monkeypatch.setattr("kiro_crew.sandbox.wrap_argv", lambda argv, **k: (list(argv), None))

    @pytest.mark.asyncio
    async def test_a_crashed_hook_keeps_the_tail_of_its_stderr(self, tmp_path):
        hook = ScriptHook(
            id="crash",
            name="crash",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command=_hook_command(tmp_path / "crash.py", 1),
            timeout=30,
        )
        result = await run_script_hook(hook, "ctx")
        assert result.exit_code == 1
        assert hook.last_status == "error"
        assert len(hook.last_error) <= 500
        _assert_tail_anchored(hook.last_error)
        # The result carries the full redacted stream, so callers may pick their own anchor.
        assert "FIRST-LINE-MARKER" in result.stderr and "LAST-LINE-MARKER" in result.stderr

    @pytest.mark.asyncio
    async def test_a_crashed_hook_whose_stream_hit_the_cap_keeps_the_head(self, tmp_path):
        """Past the byte cap the true tail is gone, so the head is the honest excerpt."""
        script = tmp_path / "flood.py"
        script.write_text(
            "import sys\n"
            "sys.stderr.write('FIRST-LINE-MARKER\\n' + 'z' * (70 * 1024) + '\\nLAST-LINE-MARKER\\n')\n"
            "raise SystemExit(1)\n",
            encoding="utf-8",
        )
        hook = ScriptHook(
            id="flood",
            name="flood",
            event=HOOK_EVENT_USER_PROMPT_SUBMIT,
            command=f'"{sys.executable}" "{script}"',
            timeout=30,
        )
        result = await run_script_hook(hook, "ctx")
        assert result.exit_code == 1
        assert "[output truncated]" in result.stderr
        assert "LAST-LINE-MARKER" not in result.stderr  # discarded by the cap, not by a slice
        assert "FIRST-LINE-MARKER" in hook.last_error
        # The excerpt still says it is clipped.
        assert hook.last_error.endswith("[output truncated]")
        assert len(hook.last_error) <= 500

    @pytest.mark.asyncio
    async def test_a_deny_hook_keeps_the_head_of_its_authored_reason(self, tmp_path):
        hook = ScriptHook(
            id="deny",
            name="deny",
            event=HOOK_EVENT_PRE_TOOL_USE,
            command=_hook_command(tmp_path / "deny.py", 2),
            timeout=30,
        )
        result = await run_script_hook(hook, "ctx")
        assert result.exit_code == 2
        assert hook.last_status == "blocked"
        assert len(hook.last_error) <= 500
        assert "FIRST-LINE-MARKER" in hook.last_error
        assert "LAST-LINE-MARKER" not in hook.last_error


# ── structural pins ─────────────────────────────────────────────────────────


_CONVERTED_MODULES = (
    Path(github_issues.__file__),
    Path(gateway_service.__file__),
    Path(cli_setup.__file__),
    Path(env_mod.__file__),
    Path(tailnet.__file__),
    Path(voice_reply.__file__),
)


@pytest.mark.parametrize("module_path", _CONVERTED_MODULES, ids=lambda p: p.name)
def test_no_stderr_slice_precedes_redaction_in_converted_modules(module_path: Path) -> None:
    """Every bounded stderr/stdout slice in these modules wraps a redact call."""
    assert _find_slice_before_redact_offenders(module_path) == []


def _hook_fire_block() -> str:
    """The hook-result loop in chat_runner's ``_fire`` closure, by its anchors."""
    import kiro_crew.dashboard.chat_runner as chat_runner

    text = Path(chat_runner.__file__).read_text(encoding="utf-8")
    start = text.index('logger.info("Hook %s stdout: %s", r.hook_name, r.stdout[:200])')
    end = text.index('logger.warning("Hook %s warning: %s", r.hook_name, r.stderr[', start)
    return text[start : text.index("\n", end)]


def test_chat_runner_hook_excerpts_anchor_by_exit_semantics() -> None:
    """``_fire`` is a closure over the runner state, so its anchors are pinned by text.

    Authored text (stdout, the exit-2 deny reason) keeps the head; the crashed-
    hook paths (fail-closed detail and the non-gating warning) keep the tail.
    """
    block = _hook_fire_block()
    assert block.count("r.stdout[:200]") == 1
    assert block.count("r.stderr[:200] if r.stderr else") == 2  # BLOCKED payload + warning log
    assert block.count("r.stderr[:100] if r.stderr else") == 1  # BLOCKED activity event
    assert "r.stderr[-200:] if r.stderr else" in block  # fail-closed crash detail
    assert block.rstrip().endswith("r.stderr[-200:])")  # non-gating crash warning
    assert "r.stderr[:200])" not in block


def test_slack_pip_failure_logs_keep_the_redacted_tail() -> None:
    """Both pip-failure logs in the Slack gateway bound the REDACTED text at its tail."""
    import kiro_crew.slack.gateway as slack_gateway

    text = Path(slack_gateway.__file__).read_text(encoding="utf-8")
    assert 'logger.error("Dep repair failed: %s", dep_err[-500:])' in text
    assert 'redact_log_via_context(fb_err.decode(errors="replace"))[-300:]' in text
    assert 'fb_err.decode(errors="replace")[:300]' not in text
    assert "dep_err[:500]" not in text
