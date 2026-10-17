"""Packaged docs (slack..telegram) state facts that match the code they describe.

Each test reads one shipped doc under ``src/kiro_crew/docs`` and checks a fact
against the constant or grammar that defines it, so a later code change that
moves the fact fails here instead of leaving a running agent reading stale text.
"""

from __future__ import annotations

import re
from pathlib import Path

DOCS = Path(__file__).resolve().parent.parent / "src" / "kiro_crew" / "docs"


def _doc(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def test_task_runner_docs_use_the_task_run_keyword_grammar():
    """Only ``task run``/``project run`` is intercepted; a bare ``run`` is not."""
    from kiro_crew.messaging.commands import task_command_reply  # noqa: F401

    for name in ("task-runner.md", "slack-integration.md"):
        text = _doc(name)
        assert "task run status" in text, name
        assert "task run cancel" in text, name
        # No code-block line or command-table row offers a bare `run ...` command.
        assert not re.search(r"^run |^\| `run ", text, re.MULTILINE), name


def test_slack_sessions_limit_numbers_match_the_code():
    from kiro_crew.config.sections import SlackConfig
    from kiro_crew.slack.sessions_view import MAX_MESSAGE_SESSION_ROWS

    default = SlackConfig.__dataclass_fields__["sessions_limit"].default
    text = _doc("slack-integration.md")
    assert f"default {default}, at most {MAX_MESSAGE_SESSION_ROWS}" in text


def test_hook_context_cap_is_documented():
    import inspect

    from kiro_crew import hooks

    src = inspect.getsource(hooks._hook_subprocess_env)
    cap = int(re.search(r"context\[:(\d+)\]", src).group(1))
    assert f"capped\nat {cap} characters" in _doc("steering-and-hooks.md")


def test_telegram_agent_picker_cap_is_documented():
    from kiro_crew.telegram.dispatch.pickers import _PICKER_LIMIT

    assert f"It shows at most {_PICKER_LIMIT};" in _doc("telegram-integration.md")


def test_snapshot_component_table_lists_crew_teams():
    from kiro_crew.snapshot_components import COMPONENTS

    assert "crew-teams" in COMPONENTS
    assert "| crew-teams | `crew-teams/teams.json`" in _doc("snapshot-and-restore.md")


def test_teams_doc_names_the_real_dashboard_url_key():
    from kiro_crew.config.sections import DashboardConfig

    fields = DashboardConfig.__dataclass_fields__
    assert "url" in fields and "host" not in fields
    text = _doc("teams-integration.md")
    assert "`dashboard.url`" in text
    assert "dashboard.host" not in text


def _registered_slash_commands() -> set[str]:
    """Every name passed to ``register_slash_command`` anywhere in the package.

    Read from the source, not from ``SLASH_REGISTRY``: tests register throwaway
    ``zz*`` commands into that shared dict, so its runtime contents depend on
    which tests ran first in the worker.
    """
    import ast

    names: set[str] = set()
    for path in sorted((DOCS.parent).rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "register_slash_command(" not in source:
            continue
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            called = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if called != "register_slash_command":
                continue
            first = node.args[0] if node.args else None
            assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
                f"{path}:{node.lineno}: register_slash_command needs a literal name "
                "so the Slash Commands table can be checked against it"
            )
            names.add(first.value)
    return names


def test_slack_slash_command_table_lists_every_registered_command():
    """The doc's Slash Commands table names exactly the registered sub-commands."""
    text = _doc("slack-integration.md")
    section = text.split("\n## Slash Commands\n", 1)[1].split("\n## ", 1)[0]
    documented = set(re.findall(r"^\| `/kirocrew (\w+)", section, re.MULTILINE))

    registered = _registered_slash_commands()
    assert registered, "no register_slash_command call found"
    assert documented == registered, (
        f"missing from the table: {sorted(registered - documented)}; "
        f"not registered: {sorted(documented - registered)}"
    )
