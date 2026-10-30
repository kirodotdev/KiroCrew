"""The doctor and startup scan for saved hooks the shell deny set will refuse.

A refused PreToolUse hook blocks every tool call it matches, and the refusal only
shows at fire time. ``hook_deny_scan`` runs the same fire-time check over the hooks
on disk; these tests pin that it reports a hook the check refuses, stays silent for
one it does not, and never prints the hook command.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from kiro_crew import hook_deny_scan, hooks
from kiro_crew.doctor_checks import agents as doctor_agents
from kiro_crew.hook_runtime import script_hooks
from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHook

#: A command fragment only the test's deny rule matches, so no built-in or edition
#: rule decides the verdict.
_MARKER = "refuse-me-7f3a"
_SECRET = "s3cr3t-token-value"


class _Store:
    def __init__(self, items: list[ScriptHook]) -> None:
        self._items = items

    def list_all(self) -> list[ScriptHook]:
        return list(self._items)


@pytest.fixture
def deny_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hooks, "effective_denied_regexes_from_config", lambda: [rf".*{_MARKER}.*"])


def _store(monkeypatch: pytest.MonkeyPatch, items: list[ScriptHook]) -> None:
    monkeypatch.setattr(hooks, "persisted_hook_store", lambda: _Store(items))


def _spec(agents_dir: Path, agent_id: str, command: str) -> None:
    spec = {"name": agent_id, "hooks": {"preToolUse": [{"matcher": "*", "command": command}]}}
    (agents_dir / f"{agent_id}.json").write_text(json.dumps(spec), encoding="utf-8")


def _refused_command() -> str:
    return f"java -jar /opt/{_MARKER}/hook.jar --token {_SECRET}"


def test_the_scan_reports_a_hook_the_fire_time_check_refuses(deny_marker, monkeypatch, tmp_path):
    _store(
        monkeypatch,
        [
            ScriptHook(
                id="h1", name="guard", event=HOOK_EVENT_PRE_TOOL_USE, command=_refused_command()
            )
        ],
    )
    _spec(tmp_path, "builder", _refused_command())

    found = hook_deny_scan.refused_hooks(tmp_path)

    assert [(h.source, h.event, h.hook_name) for h in found][0] == (
        hook_deny_scan.HOOKS_PAGE,
        HOOK_EVENT_PRE_TOOL_USE,
        "guard",
    )
    assert [h.source for h in found] == [hook_deny_scan.HOOKS_PAGE, "agent 'builder'"]
    assert all(h.rule for h in found)
    # The scan and the fire-time gate are one function: a hook it reports is one
    # run_script_hook refuses.
    assert script_hooks.shell_deny_reason(_refused_command())


def test_the_scan_is_silent_when_no_hook_is_refused(deny_marker, monkeypatch, tmp_path):
    fine = ScriptHook(id="h1", name="fine", event=HOOK_EVENT_PRE_TOOL_USE, command="echo ok")
    disabled = ScriptHook(id="h2", name="off", command=_refused_command(), enabled=False)
    _store(monkeypatch, [fine, disabled])
    _spec(tmp_path, "builder", "echo ok")

    assert hook_deny_scan.refused_hooks(tmp_path) == []


def test_a_markdown_spec_is_scanned_too(deny_marker, monkeypatch, tmp_path):
    _store(monkeypatch, [])
    (tmp_path / "writer.md").write_text(
        "---\nname: writer\nhooks:\n  preToolUse:\n    - matcher: '*'\n"
        f"      command: {_refused_command()!r}\n---\nYou write.\n",
        encoding="utf-8",
    )

    assert [h.source for h in hook_deny_scan.refused_hooks(tmp_path)] == ["agent 'writer'"]


def test_an_agent_is_named_and_read_by_its_declared_name(deny_marker, monkeypatch, tmp_path):
    _store(monkeypatch, [])
    spec = {"name": "reviewer", "hooks": {"preToolUse": [{"command": _refused_command()}]}}
    (tmp_path / "pkg-reviewer.json").write_text(json.dumps(spec), encoding="utf-8")

    assert [h.source for h in hook_deny_scan.refused_hooks(tmp_path)] == ["agent 'reviewer'"]


def test_doctor_names_the_hook_and_rule_but_never_the_command(
    deny_marker, monkeypatch, tmp_path, capsys
):
    _store(monkeypatch, [])
    _spec(tmp_path, "builder", _refused_command())
    monkeypatch.setattr(doctor_agents.cli_doctor, "_agents_dir", lambda: tmp_path)
    issues: list[str] = []

    doctor_agents._doctor_refused_hooks(issues)

    out = capsys.readouterr().out
    assert "\nHooks\n" in out and "agent 'builder'" in out and "is refused" in out
    assert _SECRET not in out and "hook.jar" not in out
    assert issues == ["hooks refused by the deny rules"]


def test_doctor_prints_nothing_when_clean(deny_marker, monkeypatch, tmp_path, capsys):
    _store(monkeypatch, [])
    _spec(tmp_path, "builder", "echo ok")
    monkeypatch.setattr(doctor_agents.cli_doctor, "_agents_dir", lambda: tmp_path)
    issues: list[str] = []

    doctor_agents._doctor_refused_hooks(issues)

    assert capsys.readouterr().out == "" and issues == []


def test_startup_warning_logs_each_refused_hook_without_the_command(
    deny_marker, monkeypatch, tmp_path, caplog
):
    _store(monkeypatch, [])
    _spec(tmp_path, "builder", _refused_command())
    real = hook_deny_scan.refused_hooks
    monkeypatch.setattr(hook_deny_scan, "refused_hooks", lambda agents_dir=None: real(tmp_path))

    with caplog.at_level(logging.WARNING, logger=hook_deny_scan.logger.name):
        hook_deny_scan.warn_refused_hooks()

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "agent 'builder'" in warnings[0]
    assert _SECRET not in warnings[0]


def test_startup_warning_never_raises(monkeypatch):
    def boom(agents_dir=None):
        raise RuntimeError("broken")

    monkeypatch.setattr(hook_deny_scan, "refused_hooks", boom)
    hook_deny_scan.warn_refused_hooks()
