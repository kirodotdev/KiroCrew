"""Design Tweak's wheel copy declares the host chat path its page already calls.

The page creates and binds its per-project chat slot, reads the slot back to
confirm a batch arrived, and sends the batch: ``POST /api/chat``,
``POST /api/chat/slots``, ``POST /api/chat/slots/{key}/project``,
``POST /api/chat/slots/{key}/resume`` and ``GET /api/chat/slots/{key}``. A bundle
reaches them through the scoped client, which refuses an undeclared path, so the
wheel copy declares ``/api/chat``: a bare entry covers every path below it, which
the slot routes need because their key is a path segment, and on those routes an
app token reaches only its own slots (``dashboard/slot_ownership.py``).
The app repository declares the same list, so its adoption is no widening (E9).

Owning spec: docs/system-specs/modules/app-kit-platform.md, "Permission parity of
the wheel copies".
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

NAME = "design-tweak"
SHIPPED = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "apps"
    / "builtins"
    / "design_tweak"
    / "app.json"
)
OWN = ["/apps/design-tweak/api", "/apps/design-tweak/api/*"]
HOST = ["/api/chat"]
#: Every host path the page calls today (``ui/src/api.ts`` in the app repository).
PAGE_CALLS = [
    "/api/chat",
    "/api/chat/slots",
    "/api/chat/slots/design-tweak-abc/project",
    "/api/chat/slots/design-tweak-abc/resume",
    "/api/chat/slots/design-tweak-abc",
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


def test_the_wheel_copy_declares_exactly_its_own_paths_and_the_chat_path():
    assert _shipped()["permissions"]["api"] == OWN + HOST


def test_every_host_path_the_page_calls_is_declared():
    declared = _shipped()["permissions"]["api"]
    for path in PAGE_CALLS:
        assert any(scope._api_pattern_matches(p, path) for p in declared), path


def test_a_registered_builtin_approves_what_the_wheel_ships(registered):
    grants = manager._read_approved_grants(NAME)
    assert grants is not None and grants["api"] == OWN + HOST


def test_the_repository_copy_adopts_without_widening(registered):
    assert checks.permission_refusal(NAME, _repository_manifest(OWN + HOST)) is None


def test_one_more_host_path_is_still_a_widening(registered):
    widened = _repository_manifest(OWN + HOST + ["/api/sessions"])
    assert checks.permission_refusal(NAME, widened) == "permission_widening"


def test_the_app_token_reaches_the_chat_paths_and_nothing_undeclared(registered, monkeypatch):
    from kiro_crew.apps import host_exec_grants

    monkeypatch.setattr(host_exec_grants, "host_exec_granted", lambda name: name == NAME)
    monkeypatch.setattr(scope, "_app_perms_cache", {})
    monkeypatch.setattr(scope, "_app_perms_denied", set())
    for path in PAGE_CALLS:
        assert scope.app_token_path_allowed(NAME, path), path
    for path in ("/api/chatx", "/api/sessions", "/api/source/pull-request", "/api/apps"):
        assert not scope.app_token_path_allowed(NAME, path), path
