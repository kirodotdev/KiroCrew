"""A failed script's error names the exception type, so it is never empty.

``str(e)`` is empty for a bare ``assert`` or ``raise ValueError()``, and a
``KeyError`` prints only the missing key. The launcher's error envelope is what
the run history and the failure alert show, so the type comes first.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from cron_script_helpers import (  # noqa: F401 -- the autouse fixtures register by import
    _cron_caller_is_named,
    _crons_dir_tracks_patched_home,
)

from kiro_crew.cron_script import run_script_sandboxed


@pytest.fixture(autouse=True)
def _passthrough_sandbox(_floor_monkeypatch, tmp_path):
    """Run the launcher directly, with ``src/`` importable in the child."""
    src_dir = str(Path(__file__).resolve().parents[1] / "src")
    # A relative write in the child lands here, not in the checkout.
    _floor_monkeypatch.chdir(tmp_path)
    _floor_monkeypatch.setenv("PYTHONPATH", src_dir)
    _floor_monkeypatch.setattr(
        "kiro_crew.cron_script.wrap_argv", lambda argv, **k: (list(argv), None)
    )


def _run_body(tmp_path: Path, body: str) -> dict:
    crons = tmp_path / ".kirocrew" / "crons"
    crons.mkdir(parents=True)
    script = crons / "scan.py"
    script.write_text(body, newline="\n", encoding="utf-8")
    with patch("pathlib.Path.home", return_value=tmp_path):
        return run_script_sandboxed(
            f"{script}:check", "sj1", timeout=60
        )  # flake-ok: run bound, not a wait


@pytest.mark.parametrize(
    "line,error",
    [
        ("assert 1 == 2", "AssertionError"),
        ("raise ValueError()", "ValueError"),
        ('{}["token_count"]', "KeyError: 'token_count'"),
        ('raise RuntimeError("feed down")', "RuntimeError: feed down"),
    ],
)
def test_the_error_names_the_exception_type(tmp_path, line, error):
    result = _run_body(tmp_path, f"def check(ctx):\n    {line}\n")

    assert result["status"] == "error"
    assert result["error"] == error
