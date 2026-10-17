"""Fixtures and helpers shared by the capability-editing tests.

``test_agent_capabilities.py`` and its ``_publication``, ``_http`` and
``_stop_following`` siblings import these by name. ``_accepting_kiro_cli`` is
autouse, so every one of them pins an accepting kiro-cli. The fixtures tests request
by parameter are declared with ``name=`` and imported under their function names, so
a parameter never shadows an import.
"""

from __future__ import annotations

import json

import pytest

from kiro_crew import agent, agent_state
from kiro_crew.agent_capabilities import CapabilityService
from kiro_crew.config import loader
from kiro_crew.kiro_cli import SPEC_PERMISSIONS_MIN_VERSION

#: A release that accepts a spec ``permissions`` block, and one that refuses it,
#: expressed against the floor so raising it cannot strand these tests.
_ACCEPTS = SPEC_PERMISSIONS_MIN_VERSION
_REFUSES = (SPEC_PERMISSIONS_MIN_VERSION[0], SPEC_PERMISSIONS_MIN_VERSION[1] - 1, 0)


def _pin_spec_permissions_cli(monkeypatch, which):
    """Pin what the shared writer gate believes the installed kiro-cli is.

    Same helper the five generated writers' suites use: the gate reads
    ``installed_kiro_cli_version`` function-locally from ``kiro_crew.kiro_cli``,
    so the patch lands there. ``which`` is ``"accepts"``, ``"refuses"`` or
    ``"unknown"``. A developer box below the floor -- or with no resolvable
    binary at all -- would otherwise decide these assertions.
    """
    version = {"accepts": _ACCEPTS, "refuses": _REFUSES, "unknown": None}[which]
    monkeypatch.setattr("kiro_crew.kiro_cli.installed_kiro_cli_version", lambda: version)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in the test_agent_capabilities*.py modules calls monkeypatch.undo()
def _accepting_kiro_cli(monkeypatch):
    """Every other test in the ``test_agent_capabilities*.py`` modules predates the gate
    and assumes it passes.

    The two tests that exercise the gate itself re-pin a refusing and an unknown
    release over this default.
    """
    _pin_spec_permissions_cli(monkeypatch, "accepts")


@pytest.fixture(name="editor")
def editor_fixture(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    specs = home / "agents"
    specs.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    monkeypatch.setenv("KIRO_HOME", str(home / "kiro"))
    monkeypatch.setattr(agent, "KIRO_AGENTS_DIR", specs)
    monkeypatch.setattr(agent_state, "_state_path", lambda: home / "agent_model_state.json")
    monkeypatch.setattr("kiro_crew.agent_capabilities.default_project_dir", lambda _: "")
    loader._invalidate_config_cache()
    config = {
        "agents": {
            "A": {"kiro_agent": "parent", "workspace": "default", "memory_store": "default"},
            "B": {"kiro_agent": "parent", "workspace": "default", "memory_store": "default"},
        },
        "dashboard": {"bot_name": "unchanged"},
    }
    (home / "config.json").write_text(json.dumps(config), encoding="utf-8")
    parent = {
        "includeMcpJson": False,
        "name": "parent",
        "prompt": "original",
        "tools": ["read"],
        "allowedTools": [],
        "mcpServers": {
            "search": {"command": "search", "args": ["old"], "env": {"API_KEY": "opaque-secret"}}
        },
        "resources": ["file://guide.md", "skill://manual/*"],
    }
    (specs / "parent.json").write_text(json.dumps(parent), encoding="utf-8")
    return CapabilityService(), home, specs, parent


def save(service, operations=None, *, enroll=False, accept_parent=None, accept_members=None):
    body = {
        "revision": service.get("A")["revision"],
        "enroll": enroll,
        "operations": operations or [],
        "accept_parent": accept_parent or [],
        "accept_members": accept_members or [],
    }
    preview = service.preview("A", body)
    return service.put("A", {**body, "preview_token": preview["preview_token"]})


def spec_for(home, specs, member="A"):
    config = json.loads((home / "config.json").read_text())
    return json.loads((specs / (config["agents"][member]["kiro_agent"] + ".json")).read_text())


@pytest.fixture(name="capability_app")
def capability_app_fixture(editor):
    from types import SimpleNamespace

    from aiohttp import web

    from kiro_crew.dashboard.handlers.agent_capabilities import _SERVICE
    from kiro_crew.dashboard.routes.agents import register

    service, _, _, _ = editor
    service.get("A")
    refreshes = []

    @web.middleware
    async def owner(request, handler):
        request["user"], request["app"] = "owner", ""
        return await handler(request)

    app = web.Application(middlewares=[owner])
    app["state"] = SimpleNamespace(owner_id="owner", push_refresh=refreshes.append)
    app[_SERVICE] = service
    register(app)
    return app, refreshes
