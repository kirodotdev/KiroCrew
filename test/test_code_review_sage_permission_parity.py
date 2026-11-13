"""Code Review Sage's wheel copy declares the pull-request family its page already calls.

The built-in page reads a pull request and acts on it through the dashboard's
``/api/source/pull-request`` family: the SDK ``PullRequestPanel``'s members (the
read, ``checks``, ``status``, ``ready``, ``auto-merge``, ``reply``, ``comment``,
``resolve``, ``unresolve``) and the publish bar's ``pending-review`` and
``submit-review``. An adopted bundle reaches them through the scoped client, which
refuses an undeclared path, so the wheel copy declares ``/api/source/pull-request``:
a bare entry covers every path below it. The app repository declares the same
list, so its adoption is no widening (E9).

Owning spec: docs/system-specs/modules/app-kit-platform.md, section 18g.
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

NAME = "code-review-sage"
SHIPPED = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "kiro_crew"
    / "apps"
    / "builtins"
    / "code_review_sage"
    / "app.json"
)
OWN = ["/api/apps/code-review-sage", "/api/apps/code-review-sage/*"]
HOST = ["/api/source/pull-request"]
#: Every host path the built-in page calls (``website/src/apps/code-review-sage``,
#: through ``api/client/sourceControl.ts`` and the SDK panel's ``PULL_REQUEST_PANEL_PATHS``).
PAGE_CALLS = [
    "/api/source/pull-request",
    "/api/source/pull-request/checks",
    "/api/source/pull-request/status",
    "/api/source/pull-request/ready",
    "/api/source/pull-request/auto-merge",
    "/api/source/pull-request/reply",
    "/api/source/pull-request/comment",
    "/api/source/pull-request/resolve",
    "/api/source/pull-request/unresolve",
    "/api/source/pull-request/pending-review",
    "/api/source/pull-request/submit-review",
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


def test_the_wheel_copy_declares_exactly_its_own_paths_and_the_source_family():
    assert _shipped()["permissions"]["api"] == OWN + HOST


def test_every_host_path_the_page_calls_is_declared():
    declared = _shipped()["permissions"]["api"]
    for path in PAGE_CALLS:
        assert any(scope._api_pattern_matches(p, path) for p in declared), path


def test_the_sdk_panel_paths_are_the_ones_listed_here():
    """The SDK panel's endpoint table, read from its source, is in ``PAGE_CALLS``."""
    source = (
        Path(__file__).resolve().parents[1] / "website" / "src" / "app-sdk" / "PullRequestPanel.tsx"
    ).read_text(encoding="utf-8")
    table = source.split("PULL_REQUEST_PANEL_PATHS = {", 1)[1].split("}", 1)[0]
    paths = [line.split("'")[1] for line in table.splitlines() if "'" in line]
    assert paths and set(paths) <= set(PAGE_CALLS)


def test_a_registered_builtin_approves_what_the_wheel_ships(registered):
    grants = manager._read_approved_grants(NAME)
    assert grants is not None and grants["api"] == OWN + HOST


def test_the_repository_copy_adopts_without_widening(registered):
    assert checks.permission_refusal(NAME, _repository_manifest(OWN + HOST)) is None


def test_one_more_host_path_is_still_a_widening(registered):
    widened = _repository_manifest(OWN + HOST + ["/api/source/issues"])
    assert checks.permission_refusal(NAME, widened) == "permission_widening"


def test_the_app_token_matches_the_family_and_nothing_undeclared(registered, monkeypatch):
    from kiro_crew.apps import host_exec_grants

    monkeypatch.setattr(host_exec_grants, "host_exec_granted", lambda name: name == NAME)
    monkeypatch.setattr(scope, "_app_perms_cache", {})
    monkeypatch.setattr(scope, "_app_perms_denied", set())
    for path in PAGE_CALLS:
        assert scope.app_token_path_allowed(NAME, path), path
    for path in (
        "/api/source/pull-requestx",
        "/api/source/issue",
        "/api/source/issues",
        "/api/chat",
        "/api/apps",
    ):
        assert not scope.app_token_path_allowed(NAME, path), path
