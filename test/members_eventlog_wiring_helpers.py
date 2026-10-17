"""Fixtures and builders shared by test_members_eventlog_wiring.py and its parts.

The parts are the ``test_members_eventlog_wiring_<topic>.py`` modules, split by
behaviour. ``_fresh_eventlog`` is autouse, and an autouse fixture applies only in
a module that imports it by name, so every one of them imports it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kiro_crew import members
from kiro_crew.config.loader import KiroCrewAgentConfig
from kiro_crew.eventlog.service import set_service

CREW = "code-reviewer"


@pytest.fixture(autouse=True)
# flake-ok: pre-existing autouse fixture on the shared monkeypatch, moved verbatim; no test that imports it calls monkeypatch.undo()
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture, moved verbatim; tracked for a de-flake pass
def _fresh_eventlog(tmp_path, monkeypatch):
    """Re-root the members space at tmp_path and drop any cached service.

    ``get_service()`` re-roots itself when ``members_root()`` (hence
    ``data_home()``) moves, but the singleton is also cleared explicitly so a
    prior test's in-memory logs can never answer here.
    """
    monkeypatch.setattr(members, "data_home", lambda: tmp_path)
    set_service(None)
    yield
    set_service(None)


#: The digest every ``_fake_config`` carries. A real load names the bytes it parsed and
#: the roster reconciles only from a config that names them, so a double without one
#: silently turns off the reconcile and leaves every pin that depends on it passing
#: vacuously. Tests that need the live file to DISAGREE set ``_content_digest`` or the
#: patched ``config_content_stamp`` to something else on purpose.
FAKE_CONFIG_DIGEST = "the-bytes-this-fake-config-was-parsed-from"


def _fake_config(agents: dict[str, KiroCrewAgentConfig], default=CREW):
    # memory_stores mirrors KiroCrewConfig: the agent-update handler reads it
    # (cfg.memory_stores.get(...)) to resolve a member's private-memory record.
    # degraded_sections mirrors it too: the roster withholds the reconcile from a
    # config degraded to defaults, so a double must state that it is not.
    cfg = SimpleNamespace(
        agents=agents,
        default_agent=default,
        memory_stores={},
        degraded_sections=frozenset(),
    )
    cfg._content_digest = FAKE_CONFIG_DIGEST
    return cfg


def _agent(**kw) -> KiroCrewAgentConfig:
    return KiroCrewAgentConfig(kiro_agent=kw.pop("kiro_agent", "reviewer"), **kw)
