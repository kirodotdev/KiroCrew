"""Fixtures and helpers the agent-spec home isolation modules share.

Imported by name from the ``test_agent_home_isolation*.py`` modules (and, through
``test_agent_home_isolation``, by ``test_conductor_allowed_tools_survive.py``). The two
autouse fixtures keep every test off the operator's real data home; the
``@pytest.fixture(name=...)`` one is imported under its function name.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True, name="_isolate_default_home")
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the home isolation modules' autouse whole-home redirect, moved verbatim; tracked for a de-flake pass
def _isolate_default_home_fixture(monkeypatch, tmp_path):
    """Unpinning KIROCREW_HOME must remain safe even when SEL initializes cold."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _pretend_target_is_shared(monkeypatch, agent_mod, agents_dir: Path) -> None:
    """Present *agents_dir* as BOTH the write target and what the AMBIENT
    environment resolves — which is what makes a target "shared".

    The ambient side is ``ambient_agents_dir``, the override-blind resolver the guard
    reads; ``KIRO_AGENTS_DIR`` is the target side because ``kiro_agents_dir_path()``
    prefers it.
    """
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(agent_mod, "ambient_agents_dir", lambda: agents_dir)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the home isolation modules' autouse default-home and breadcrumb pin, moved verbatim; tracked for a de-flake pass
def _pin_default_home_and_breadcrumb(request, monkeypatch, tmp_path):
    """Keep every test in this module off the operator's real data home.

    The default path would create the real ``~/.kiro/crew`` and breadcrumb, which
    conftest's ratchets fail, so the default is pinned to tmp and the breadcrumb stubbed.
    A test requesting ``_isolate_default_home`` tests real cold resolution, so only the
    cache reset applies to it.
    """
    from kiro_crew.config import paths

    monkeypatch.setattr(paths, "_resolved_home", None, raising=False)
    if "_isolate_default_home" in request.fixturenames:
        return
    monkeypatch.setattr(paths, "_resolve_default_home", lambda: tmp_path / "pinned-default-home")
    monkeypatch.setattr(paths, "_write_recovery_breadcrumb", lambda data_home: None, raising=False)


def _capture_sel(monkeypatch, agent_mod) -> list[dict]:
    events: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            events.append(kw)

    monkeypatch.setattr(agent_mod, "sel", lambda: _Sel())
    return events


def _durable_checkout(monkeypatch, agent_mod) -> None:
    """Present the checkout as a durable install (non-temp, non-worktree).

    Fabricated, never created: the guard's predicates are lexical on the
    resolved path (same trick as ``test_does_not_decline_from_a_durable_clone``),
    and a real path a test can create lives under the redirected temp root,
    which the temp arm would (correctly) decline for its own reason.
    """
    durable = Path("/durable-install/KiroCrew/src/kiro_crew/agent.py")
    monkeypatch.setattr(agent_mod, "__file__", str(durable))


def _spec_pinned_to(home) -> str:
    """A minimal owned spec whose managed server records *home* as its writer."""
    import json

    return json.dumps(
        {
            "name": "kirocrew",
            "mcpServers": {
                "kirocrew-core": {"command": "kirocrew", "env": {"KIROCREW_HOME": str(home)}}
            },
        }
    )
