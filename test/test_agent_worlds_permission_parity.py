"""Agent Worlds' wheel copy declares the host paths its page already calls.

The page lists the live sessions, reads a session's recent messages, replies to
or steers it, decides its pending tool approval, starts a new session, and polls
the cron and subagent lists: ``POST /api/chat``, ``GET /api/chat/slots/{key}``,
``POST /api/chat/slots``, ``POST /api/chat/slots/{key}/approve``,
``POST /api/approvals/{id}/{action}``, ``GET /api/crons``, ``GET /api/spawn``,
``GET /api/config/default-agent`` (the label of an agent-less session) and
``GET /api/dashboard/config`` (the memory mode a new session starts with). An
installed bundle reaches them through the SDK's ``useHostSessions`` and the
scoped client, which refuse an undeclared path, so the wheel copy declares them
in the narrowest entries the matcher allows: the slot and approval families bare,
since their key is a path segment, and every other path exactly.
The app repository declares the same list, so its adoption is no widening (E9).

Owning spec: docs/system-specs/modules/app-kit-platform.md, "Permission parity of
the wheel copies" and section 18j.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from kiro_crew.apps import builtin_adoption_checks as checks
from kiro_crew.apps import manager
from kiro_crew.apps.manifest import AppManifest
from kiro_crew.dashboard import token_auth_app_scope as scope

NAME = "agent-worlds"
SHIPPED = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "apps"
    / "builtins"
    / "agent_worlds"
    / "app.json"
)
#: Agent Worlds has no API namespace of its own; every entry is a host path.
OWN: list[str] = []
HOST = [
    "=/api/chat",
    "/api/chat/slots",
    "/api/approvals",
    "=/api/crons",
    "=/api/spawn",
    "=/api/config/default-agent",
    "=/api/dashboard/config",
]
#: Every host path the page calls today.
PAGE_CALLS = [
    "/api/chat",
    "/api/chat/slots",
    "/api/chat/slots/chat-1-1787502679",
    "/api/chat/slots/chat-1-1787502679/approve",
    "/api/approvals/req-7/approve",
    "/api/approvals/req-7/reject",
    "/api/crons",
    "/api/spawn",
    "/api/config/default-agent",
    "/api/dashboard/config",
]


def _shipped() -> dict:
    return json.loads(SHIPPED.read_text(encoding="utf-8"))


@pytest.fixture
def registered(tmp_path, monkeypatch):
    """The real wheel copy registered as a built-in in an isolated data home."""
    home = tmp_path / "kirocrew-home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    shipped = _shipped()
    monkeypatch.setattr(manager, "_BUILTIN_APPS", [])
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda *a, **k: [shipped])
    monkeypatch.setattr(manager, "_edition_builtin_apps", lambda: [])
    manager.register_builtin_apps()
    return shipped


def _repository_manifest(api: list[str]) -> AppManifest:
    """The app repository's manifest: the wheel copy's, with *api* as its grant."""
    data = copy.deepcopy(_shipped())
    data["permissions"]["api"] = api
    return AppManifest.from_dict(data)


def test_the_wheel_copy_declares_api_paths_and_no_other_permission():
    assert set(_shipped()["permissions"]) == {"api"}


def test_one_more_host_path_is_still_a_widening(registered):
    widened = _repository_manifest(HOST + ["/api/sessions"])
    assert checks.permission_refusal(NAME, widened) == "permission_widening"


def test_a_bare_chat_entry_is_still_a_widening(registered):
    widened = _repository_manifest(["/api/chat"] + HOST[1:])
    assert checks.permission_refusal(NAME, widened) == "permission_widening"


def test_the_app_token_reaches_the_page_paths_and_nothing_undeclared(registered, monkeypatch):
    from kiro_crew.apps import host_exec_grants

    monkeypatch.setattr(host_exec_grants, "host_exec_granted", lambda name: name == NAME)
    monkeypatch.setattr(scope, "_app_perms_cache", {})
    monkeypatch.setattr(scope, "_app_perms_denied", set())
    for path in PAGE_CALLS:
        assert scope.app_token_path_allowed(NAME, path), path
    for path in (
        "/api/chatx",
        "/api/chat/folders",
        "/api/chat/mode",
        "/api/chat/headroom",
        "/api/chat/slotsx",
        "/api/crons/job-1",
        "/api/crons/job-1/run",
        "/api/spawn/child-1",
        "/api/config/default-agentx",
        "/api/config",
        "/api/dashboard/config/x",
        "/api/sessions",
        "/api/apps",
    ):
        assert not scope.app_token_path_allowed(NAME, path), path
