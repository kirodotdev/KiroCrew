"""Fixtures and helpers the agent-spec home isolation modules share.

``test_agent_home_isolation.py``, ``test_agent_home_isolation_override_home.py``
and ``test_agent_home_isolation_derived_specs.py`` import what each uses by name. Two autouse
fixtures keep every test off the operator's real data home: one redirects the whole
home to tmp (declared ``@pytest.fixture(name=...)`` and imported under its function
name), the other pins the default home and stubs the breadcrumb writer. The helpers
present a target as shared, capture SEL events, present the checkout as a durable
install and build the text of a spec pinned to a home. ``test_conductor_allowed_tools_survive.py``
reaches ``_durable_checkout`` and ``_pretend_target_is_shared`` through
``test_agent_home_isolation``.
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

    A target the ambient environment would never produce is by definition private
    to whoever redirected it, so a test must line the two up to exercise the
    guard.

    The ambient side is ``config.paths.ambient_agents_dir``, patched at its
    DEFINITION: it is the override-blind resolver the guard reads, and ``agent``
    binds it by name so a module-attribute patch on ``agent`` would be a second
    copy that the guard's own call still ignores. ``KIRO_AGENTS_DIR`` stays the
    target side because ``kiro_agents_dir_path()`` prefers it.
    """
    monkeypatch.setattr(agent_mod, "KIRO_AGENTS_DIR", agents_dir)
    monkeypatch.setattr(agent_mod, "ambient_agents_dir", lambda: agents_dir)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the home isolation modules' autouse default-home and breadcrumb pin, moved verbatim; tracked for a de-flake pass
def _pin_default_home_and_breadcrumb(request, monkeypatch, tmp_path):
    """Keep every test in this module off the operator's real data home.

    The guard's writability probe compares the override against the DEFAULT
    home, and several tests exercise the default path outright (no override),
    so production would resolve — and create — the real ``~/.kiro/crew`` plus
    the ``~/.kirocrew.breadcrumb`` beside it. Conftest's real-home ratchet
    fails such tests at teardown, and its breadcrumb guard fails them at write
    time. None of these tests is ABOUT the breadcrumb or the real default, so
    the whole module pins the default to tmp and stubs the breadcrumb writer,
    the two remedies the ratchet's messages prescribe.

    Stands aside for a test that requests ``_isolate_default_home``: that
    fixture redirects the WHOLE home — ``HOME``, ``USERPROFILE``,
    ``Path.home`` — to tmp, so the real default is unreachable anyway, and
    such a test's subject is exactly the real cold-resolution path (the
    memoization into ``_resolved_home`` and the breadcrumb write) that this
    pin would otherwise stub out. Only the cache reset is shared: cold-start
    assertions must hold regardless of test order.
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
