"""A spec's ``toolsSettings`` is never dropped silently on a backend that applies none of it.

kiro-cli reads the field itself and KAS receives it as ``permissions`` rules and gate
allowlists. Every mirrored harness rules it ``no-channel`` or ``withheld``, so a new
chat session there whose spec sets it gets one notice naming the key. Backends Crew
fires spec hooks on (goose, opencode, strands) already name it in the notice their
spec-key read posts, and keep exactly that one row. ``subagent.availableAgents`` is
the exception: Crew's spawn gate reads it on every backend, so no notice names it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKEND_PI,
    ACP_BACKEND_STRANDS,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner
from kiro_crew.providers.mirrors import MIRRORS, mirror_for
from kiro_crew.providers.mirrors.base import Concern, Disposition

_SHELL_ALLOWLIST = {"shell": {"denyByDefault": True, "allowedCommands": ["git status"]}}
_NOT_RECEIVED = "sets toolsSettings, which this backend does not receive"
_APPLIED_NOWHERE = [
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_COPILOT,
    ACP_BACKEND_DEEPSEEK,
]
_CREW_FIRED = [ACP_BACKEND_GOOSE, ACP_BACKEND_OPENCODE, ACP_BACKEND_STRANDS]
_SPAWN_GATED = {"subagent": {"availableAgents": ["helper-*"]}}


@pytest.fixture(autouse=True)
def _fresh_cache():
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


@pytest.fixture
def agents_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "agents"
    d.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: d)
    return d


@pytest.fixture
def notices(monkeypatch) -> list:
    seen: list = []
    monkeypatch.setattr(
        chat_runner,
        "append_and_surface",
        lambda state, slot, role, text, cls, **kw: seen.append(text),
    )
    return seen


def _client(backend: str) -> SimpleNamespace:
    return SimpleNamespace(capabilities=capabilities_for(backend), cwd="")


def _write_spec(agents_dir: Path, name: str, **fields) -> None:
    spec = {"name": name, "prompt": "p", **fields}
    (agents_dir / f"{name}.json").write_text(json.dumps(spec), encoding="utf-8")


def _prepare(backend: str, agent: str, *, is_new: bool):
    return asyncio.run(
        chat_runner._prepare_spec_hooks(
            SimpleNamespace(), SimpleNamespace(), _client(backend), agent, is_new=is_new
        )
    )


def _tools_settings_rows(notices: list) -> list:
    return [text for text in notices if "toolsSettings" in text]


@pytest.mark.parametrize("backend", sorted(MIRRORS))
def test_every_mirror_rules_tools_settings_applied_nowhere(backend):
    ruling = mirror_for(backend).rulings()[Concern.TOOLS_SETTINGS]
    assert ruling.disposition in (Disposition.NO_CHANNEL, Disposition.WITHHELD)


@pytest.mark.parametrize("backend", [ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX])
def test_claude_and_codex_rule_it_a_gap_with_a_destination(backend):
    ruling = mirror_for(backend).rulings()[Concern.TOOLS_SETTINGS]
    assert ruling.disposition is Disposition.NO_CHANNEL
    assert "spec-shell-allowlist" in ruling.channel


@pytest.mark.parametrize("backend", _APPLIED_NOWHERE)
def test_a_new_session_gets_one_notice_and_a_later_turn_none(backend, agents_dir, notices):
    _write_spec(agents_dir, "a1", toolsSettings=_SHELL_ALLOWLIST)
    assert _prepare(backend, "a1", is_new=True) == ([], False, None)
    _prepare(backend, "a1", is_new=False)
    rows = _tools_settings_rows(notices)
    assert len(rows) == 1
    assert f"Agent a1 {_NOT_RECEIVED}" in rows[0]


@pytest.mark.parametrize("backend", _APPLIED_NOWHERE)
def test_no_notice_for_a_spec_without_tools_settings(backend, agents_dir, notices):
    _write_spec(agents_dir, "plain")
    _write_spec(agents_dir, "empty", toolsSettings={})
    _prepare(backend, "plain", is_new=True)
    _prepare(backend, "empty", is_new=True)
    assert _tools_settings_rows(notices) == []


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_KAS, ACP_BACKEND_PI])
def test_no_applied_nowhere_ruling_without_a_mirror(backend, agents_dir, notices):
    """kiro-cli reads the field and KAS translates it; pi has no mirror to rule it."""
    assert spec_hooks.spec_tools_settings_applied_nowhere(_client(backend)) is False
    _write_spec(agents_dir, "a1", toolsSettings={"read": {"deniedPaths": ["~/.ssh"]}})
    _prepare(backend, "a1", is_new=True)
    assert not any(_NOT_RECEIVED in text for text in notices)


@pytest.mark.parametrize("backend", _CREW_FIRED)
def test_a_crew_fired_backend_keeps_exactly_its_one_spec_key_row(backend, agents_dir, notices):
    _write_spec(agents_dir, "a1", toolsSettings=_SHELL_ALLOWLIST)
    _prepare(backend, "a1", is_new=True)
    assert len(_tools_settings_rows(notices)) == 1


def test_an_unreadable_spec_renders_no_notice_and_fails_nothing(agents_dir, notices):
    (agents_dir / "a1.json").write_text("{not json", encoding="utf-8")
    assert _prepare(ACP_BACKEND_CLAUDE, "a1", is_new=True) == ([], False, None)
    # claude's observer-hook read posts its own unread-hooks row for this spec.
    assert not any(_NOT_RECEIVED in text for text in notices)


def test_an_unreadable_ruling_establishes_nothing(monkeypatch):
    import kiro_crew.providers.mirrors as mirrors_mod

    def _boom(_backend):
        raise RuntimeError("ruling unreadable")

    monkeypatch.setattr(mirrors_mod, "mirror_for", _boom)
    assert spec_hooks.spec_tools_settings_applied_nowhere(_client(ACP_BACKEND_CLAUDE)) is False


@pytest.mark.parametrize(
    ("settings", "named"),
    [
        (None, []),
        ({}, []),
        ("not an object", ["toolsSettings"]),
        (_SHELL_ALLOWLIST, ["toolsSettings"]),
        ({"use_subagent": {"availableAgents": []}}, ["toolsSettings"]),
        ({"subagent": {"trustedAgents": ["x"]}}, ["toolsSettings"]),
        (_SPAWN_GATED, []),
        ({"subagent": {"availableAgents": [], "trustedAgents": []}, "shell": {}}, []),
        (
            {**_SHELL_ALLOWLIST, "subagent": {"availableAgents": [], "trustedAgents": ["x"]}},
            ["toolsSettings.shell", "toolsSettings.subagent.trustedAgents"],
        ),
    ],
)
def test_only_what_the_spawn_gate_does_not_read_is_named(settings, named):
    """``subagent.availableAgents`` is read by the spawn gate on every backend."""
    from kiro_crew.agent_sdk.tools_settings_reach import tools_settings_unapplied

    assert tools_settings_unapplied(settings) == named


@pytest.mark.parametrize("backend", _APPLIED_NOWHERE + _CREW_FIRED)
def test_a_spawn_allowlist_alone_gets_no_notice(backend, agents_dir, notices):
    _write_spec(agents_dir, "a1", toolsSettings=_SPAWN_GATED)
    _prepare(backend, "a1", is_new=True)
    assert _tools_settings_rows(notices) == []


@pytest.mark.parametrize("backend", _APPLIED_NOWHERE + _CREW_FIRED)
def test_beside_a_spawn_allowlist_only_the_rest_is_named(backend, agents_dir, notices):
    _write_spec(agents_dir, "a1", toolsSettings={**_SHELL_ALLOWLIST, **_SPAWN_GATED})
    _prepare(backend, "a1", is_new=True)
    rows = _tools_settings_rows(notices)
    assert len(rows) == 1
    assert "sets toolsSettings.shell, which this backend does not receive" in rows[0]
    assert "availableAgents" not in rows[0]
