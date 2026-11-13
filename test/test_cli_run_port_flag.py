"""``kirocrew run`` does not offer a ``--port`` it never reads.

The task runner reports to the terminal, so a dashboard port changes nothing.
Help must not list the flag, and an old script that still passes it must parse.
"""

from __future__ import annotations

import sys

import pytest


def test_run_help_does_not_offer_port(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    from kiro_crew import cli

    monkeypatch.setattr(sys, "argv", ["kirocrew", "run", "--help"])
    with pytest.raises(SystemExit) as excinfo:
        cli.main()
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--fresh" in out
    assert "--port" not in out


def test_run_still_parses_a_passed_port(monkeypatch: pytest.MonkeyPatch) -> None:
    from kiro_crew import cli

    seen: dict[str, object] = {}

    async def fake_run_task(args: object) -> None:
        seen["spec"] = getattr(args, "spec")

    monkeypatch.setattr("kiro_crew.cli_server._run_task", fake_run_task)
    monkeypatch.setattr(sys, "argv", ["kirocrew", "run", "TASK.md", "--port", "7001"])
    cli.main()
    assert seen == {"spec": "TASK.md"}
