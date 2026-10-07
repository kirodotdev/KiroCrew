"""A checkout's own agent spec ``hooks`` reach Crew's turn loops only when trusted.

On goose and opencode Crew fires the spec's ``hooks`` itself and resolves that spec
project-nearest first. A hook is a command, so the checkout's spec supplies hooks
only under the verdict that also decides whether its ``mcpServers`` launch
(``session_mcp._project_mcp_trusted``). Refused, the user-level spec of that name
is read instead. Backends that do not have Crew fire spec hooks never read it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import kiro_crew.config.paths as paths_mod
from kiro_crew.acp import session_mcp
from kiro_crew.agent_sdk import spec_hooks
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_GOOSE,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import chat_runner

_MIRRORS = [ACP_BACKEND_GOOSE, ACP_BACKEND_OPENCODE]
_AGENT = "kirocrew"
_REPO_CMD = "repo-hook"
_USER_CMD = "user-hook"


@pytest.fixture(autouse=True)
def _fresh_cache(_floor_monkeypatch):
    spec_hooks._cache.clear()
    yield
    spec_hooks._cache.clear()


@pytest.fixture
def agents_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "user-agents"
    d.mkdir()
    monkeypatch.setattr(paths_mod, "kiro_agents_dir", lambda: d)
    return d


def _write(path: Path, spec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"name": _AGENT, "prompt": "p", **spec}), encoding="utf-8")


def _hooks(cmd: str) -> dict:
    return {"hooks": {"agentSpawn": [{"command": cmd}], "userPromptSubmit": [{"command": cmd}]}}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _write(repo / ".kiro" / "agents" / f"{_AGENT}.json", _hooks(_REPO_CMD))
    return repo


def _provider(backend: str, cwd: str) -> SimpleNamespace:
    return SimpleNamespace(capabilities=capabilities_for(backend), cwd=cwd)


def _chat(backend: str, cwd: Path) -> tuple[list[str], bool]:
    hooks, unreadable, _ = asyncio.run(
        chat_runner._prepare_spec_hooks(
            None, None, _provider(backend, str(cwd)), _AGENT, is_new=False
        )
    )
    return [h.command for h in hooks], unreadable


def _turn(backend: str, cwd: Path) -> list[str]:
    turn = asyncio.run(spec_hooks.turn_spec_hooks(_provider(backend, str(cwd)), _AGENT))
    assert not turn.unreadable
    return [h.command for h in turn.hooks]


@pytest.mark.parametrize("backend", _MIRRORS)
def test_untrusted_project_hooks_yield_the_user_level_hooks(backend, agents_dir, tmp_path):
    _write(agents_dir / f"{_AGENT}.json", _hooks(_USER_CMD))
    repo = _repo(tmp_path)
    assert _chat(backend, repo) == ([_USER_CMD, _USER_CMD], False)
    assert _turn(backend, repo) == [_USER_CMD, _USER_CMD]


@pytest.mark.parametrize("backend", _MIRRORS)
def test_untrusted_project_hooks_with_no_user_spec_load_nothing(backend, agents_dir, tmp_path):
    repo = _repo(tmp_path)
    assert _chat(backend, repo) == ([], False)
    assert _turn(backend, repo) == []


@pytest.mark.parametrize("backend", _MIRRORS)
def test_the_mcp_trust_verdict_is_what_admits_project_hooks(
    backend, agents_dir, tmp_path, monkeypatch
):
    # One verdict for both fields: flip it and the checkout's hooks are the ones read.
    _write(agents_dir / f"{_AGENT}.json", _hooks(_USER_CMD))
    repo = _repo(tmp_path)
    seen: list[object] = []

    def _trusted(work_dir):
        seen.append(work_dir)
        return True

    monkeypatch.setattr(session_mcp, "_project_mcp_trusted", _trusted)
    assert _chat(backend, repo) == ([_REPO_CMD, _REPO_CMD], False)
    assert seen and all(str(w) == str(repo) for w in seen)


@pytest.mark.parametrize("backend", _MIRRORS)
def test_an_unreadable_untrusted_project_spec_still_fails_closed(backend, agents_dir, tmp_path):
    _write(agents_dir / f"{_AGENT}.json", _hooks(_USER_CMD))
    repo = tmp_path / "repo"
    spec = repo / ".kiro" / "agents" / f"{_AGENT}.json"
    spec.parent.mkdir(parents=True)
    spec.write_text("{not json", encoding="utf-8")
    assert _chat(backend, repo) == ([], True)


@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CLAUDE, ACP_BACKEND_CODEX])
def test_backends_crew_does_not_fire_spec_hooks_for_read_no_spec(
    backend, agents_dir, tmp_path, monkeypatch
):
    _write(agents_dir / f"{_AGENT}.json", _hooks(_USER_CMD))
    repo = _repo(tmp_path)

    def _no_read(*_a, **_k):
        raise AssertionError(f"{backend}: Crew read a spec it does not fire hooks for")

    monkeypatch.setattr(spec_hooks, "_agent_spec", _no_read)
    monkeypatch.setattr(session_mcp, "project_agent_spec", _no_read)
    assert _chat(backend, repo) == ([], False)
    turn = asyncio.run(spec_hooks.turn_spec_hooks(_provider(backend, str(repo)), _AGENT))
    assert turn == spec_hooks._NOT_GATED
