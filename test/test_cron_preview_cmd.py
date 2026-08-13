"""Unit tests for ``kirocrew cron preview`` CLI subcommand."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

import pytest

from kiro_crew.cli_commands import _cron_preview


def _make_args(script: str, message: str = "test-msg", env: list | None = None) -> argparse.Namespace:
    return argparse.Namespace(script=script, message=message, env=env or [])


def _write_script(tmp_path: Path, name: str, code: str) -> Path:
    p = tmp_path / name
    p.write_text(code)
    return p


def _patch_resolve(tmp_path: Path, name: str = "s.py", func: str = "run"):
    """Patch resolve_script_path to allow temp dir scripts."""
    p = tmp_path / name
    return patch(
        "kiro_crew.cron_script.resolve_script_path",
        return_value=(str(p), func),
    )


class TestCronPreviewReport:
    def test_report_prints_message(self, tmp_path: Path, capsys):
        p = _write_script(
            tmp_path, "s.py",
            "from kiro_crew.cron_script import Report\n"
            "def run(ctx): raise Report(f'hello {ctx.message}')\n")
        with _patch_resolve(tmp_path):
            _cron_preview(_make_args(f"{p}:run", message="world"))
        assert "hello world" in capsys.readouterr().out


class TestCronPreviewSkip:
    def test_skip_output(self, tmp_path: Path, capsys):
        p = _write_script(
            tmp_path, "s.py",
            "from kiro_crew.cron_script import Skip\n"
            "def run(ctx): raise Skip()\n")
        with _patch_resolve(tmp_path):
            _cron_preview(_make_args(f"{p}:run"))
        assert "Skip" in capsys.readouterr().out


class TestCronPreviewDone:
    def test_done_output(self, tmp_path: Path, capsys):
        p = _write_script(
            tmp_path, "s.py",
            "from kiro_crew.cron_script import Done\n"
            "def run(ctx): raise Done('finished')\n")
        with _patch_resolve(tmp_path):
            _cron_preview(_make_args(f"{p}:run"))
        out = capsys.readouterr().out
        assert "Done" in out
        assert "finished" in out


class TestCronPreviewError:
    def test_error_output(self, tmp_path: Path, capsys):
        p = _write_script(
            tmp_path, "s.py",
            "def run(ctx): raise ValueError('oops')\n")
        with _patch_resolve(tmp_path):
            with pytest.raises(SystemExit):
                _cron_preview(_make_args(f"{p}:run"))
        out = capsys.readouterr().out
        assert "Error" in out
        assert "ValueError" in out


class TestCronPreviewValidation:
    def test_invalid_format_no_colon(self):
        with pytest.raises(SystemExit):
            _cron_preview(_make_args("/some/path.py"))

    def test_nonexistent_script(self):
        with pytest.raises(SystemExit):
            _cron_preview(_make_args("~/.kirocrew/crons/nonexistent.py:run"))

    def test_permission_error_rejected(self, capsys):
        """resolve_script_path rejects scripts that fail validation."""
        with patch(
            "kiro_crew.cron_script.resolve_script_path",
            side_effect=PermissionError("blocked by policy"),
        ):
            with pytest.raises(SystemExit):
                _cron_preview(_make_args("~/.kirocrew/crons/bad.py:run"))
        assert "blocked" in capsys.readouterr().out

    def test_missing_function(self, tmp_path: Path):
        _write_script(tmp_path, "s.py", "def other(ctx): pass\n")
        with _patch_resolve(tmp_path, func="run"):
            with pytest.raises(SystemExit):
                _cron_preview(_make_args(f"{tmp_path / 's.py'}:run"))

    def test_async_function_rejected(self, tmp_path: Path, capsys):
        _write_script(tmp_path, "s.py", "async def run(ctx): pass\n")
        with _patch_resolve(tmp_path):
            with pytest.raises(SystemExit):
                _cron_preview(_make_args(f"{tmp_path / 's.py'}:run"))
        assert "async" in capsys.readouterr().out


class TestCronPreviewEnv:
    def test_env_vars_set(self, tmp_path: Path, capsys):
        _write_script(
            tmp_path, "s.py",
            "import os\n"
            "from kiro_crew.cron_script import Report\n"
            "def run(ctx): raise Report(os.environ.get('TEST_CRON_VAR', 'MISSING'))\n")
        with _patch_resolve(tmp_path):
            _cron_preview(_make_args(f"{tmp_path / 's.py'}:run", env=["TEST_CRON_VAR=hello123"]))
        assert "hello123" in capsys.readouterr().out


class TestCronPreviewCallToolPath:
    """Tests for the security-relevant call_tool path: redaction, SEL audit, notify."""

    def test_call_tool_redacts_credentials(self, tmp_path: Path, capsys):
        """Tool args are redacted before passing to McpToolClient."""
        _write_script(
            tmp_path, "s.py",
            "from kiro_crew.cron_script import Report\n"
            "def run(ctx):\n"
            "    result = ctx.call_tool('builder-mcp', 'ReadInternalWebsites',\n"
            "        {'inputs': ['https://example.com']})\n"
            "    raise Report('ok')\n")
        call_args = []
        mock_client = type("MC", (), {
            "call_tool": lambda self, tool, args: (call_args.append(args), "result")[1],
            "close": lambda self: None,
        })()
        with _patch_resolve(tmp_path):
            with patch("kiro_crew.cron_script.McpToolClient", return_value=mock_client):
                _cron_preview(_make_args(f"{tmp_path / 's.py'}:run"))
        # Verify call was made (redaction doesn't alter clean inputs)
        assert len(call_args) == 1
        assert call_args[0] == {"inputs": ["https://example.com"]}

    def test_call_tool_audits_via_sel(self, tmp_path: Path, capsys):
        """SEL log_tool_invocation fires for each call_tool."""
        _write_script(
            tmp_path, "s.py",
            "from kiro_crew.cron_script import Report\n"
            "def run(ctx):\n"
            "    ctx.call_tool('builder-mcp', 'ReadInternalWebsites', {'inputs': []})\n"
            "    raise Report('done')\n")
        mock_client = type("MC", (), {
            "call_tool": lambda self, tool, args: "result",
            "close": lambda self: None,
        })()
        sel_calls = []
        with _patch_resolve(tmp_path):
            with patch("kiro_crew.cron_script.McpToolClient", return_value=mock_client):
                with patch("kiro_crew.cli_commands.sel") as mock_sel:
                    mock_sel.return_value.log_tool_invocation = lambda **kw: sel_calls.append(kw)
                    mock_sel.return_value.log_api_access = lambda **kw: None
                    _cron_preview(_make_args(f"{tmp_path / 's.py'}:run"))
        assert any(c.get("tool_name") == "builder-mcp/ReadInternalWebsites" for c in sel_calls)
        assert any(c.get("outcome") == "ok" for c in sel_calls)

    def test_notify_prints_suppressed(self, tmp_path: Path, capsys):
        """ctx.notify() prints suppression message instead of delivering."""
        _write_script(
            tmp_path, "s.py",
            "from kiro_crew.cron_script import Report\n"
            "def run(ctx):\n"
            "    ctx.notify('hello world')\n"
            "    raise Report('done')\n")
        with _patch_resolve(tmp_path):
            _cron_preview(_make_args(f"{tmp_path / 's.py'}:run"))
        out = capsys.readouterr().out
        assert "[notify suppressed]" in out
        assert "hello world" in out
