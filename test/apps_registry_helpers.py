"""Shared autouse fixtures and helpers for the app registry suites.

``test_apps_registry.py`` and its topic siblings (``test_apps_registry_timeout_reaping.py``,
``test_apps_registry_identity_admission.py``, ``test_apps_registry_git_transport.py`` and
``test_apps_registry_desktop_layout.py``) import the three module-wide autouse fixtures from
here, so every registry test keeps the same pins: registry execution is admitted, the
official catalog answers "app absent", and the sandbox wrapper does not depend on this
host's namespace support. ``_identity_harness`` drives ``install_from_registry`` to the
identity gate. This is a plain module, not a conftest.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from kiro_crew.apps import registry


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the registry suites' module-wide pin, moved verbatim; no test in a module that imports this fixture calls monkeypatch.undo()
def _explicit_registry_execution_admission(monkeypatch):
    """These tests must reach admitted registry subprocess paths."""
    monkeypatch.setattr("kiro_crew.apps.execution.third_party_execution_allowed", lambda: True)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the registry suites' module-wide pin, moved verbatim; no test in a module that imports this fixture calls monkeypatch.undo()
def _catalog_absent(monkeypatch):
    """Pin "official catalog reachable, app absent" for the whole module.

    ``get_registry_app`` (patched to a stub in most tests here) is, in its real
    implementation, upstream of ``official_catalog.inventory_for_install``, whose
    real lookup performs a fresh uncached HTTPS fetch on every call. A test that
    exercises the real ``get_registry_app``/catalog path (rather than stubbing it
    directly) would otherwise depend on the runner's live network being up. A
    test that wants a different catalog answer overrides this by monkeypatching
    the same seam itself (see ``TestCatalogAppsIncludesExternalRegistries``).
    """
    monkeypatch.setattr(
        "kiro_crew.apps.official_catalog.inventory_for_install",
        lambda name: None,
    )


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the registry suites' module-wide pin, moved verbatim; no test in a module that imports this fixture calls monkeypatch.undo()
def unsandboxed_spawn(monkeypatch):
    """Decouple this module's timeout/reap tests from the host's sandbox capability.

    Every test here asserts process-group signalling and reaping, and they mock
    ``create_subprocess_exec``, so no child process ever actually runs. What they
    must not depend on is whether THIS host can build a namespace sandbox: a CI
    runner with ``kernel.apparmor_restrict_unprivileged_userns=1`` legitimately
    cannot, and ``wrap_argv`` then fail-closes by design. Without this fixture the
    tests pass only when the capability probe returns a false positive on such
    hosts. Autouse because the coupling is a property of the whole module, not of
    individual tests. Sandbox construction is covered by ``test_sandbox_*.py``.
    """
    from kiro_crew import sandbox

    monkeypatch.setattr(sandbox, "_allow_unsandboxed_exec", lambda: True)


def _identity_harness(monkeypatch, src, *, cloned_manifest, prefetched=None):
    """Common monkeypatch set for driving install_from_registry to the identity gate."""
    entry = {"name": "demoapp", "repo": "https://example.com/demo.git", "branch": "main"}
    monkeypatch.setattr(registry, "get_registry_app", lambda n: entry)
    monkeypatch.setattr(registry, "_entry_git_url", lambda e: "https://example.com/demo.git")

    async def _fake_manifest(*args, **kwargs):
        return prefetched or {}

    monkeypatch.setattr(registry, "_fetch_app_manifest", _fake_manifest)
    monkeypatch.setattr(registry, "app_admission_denied", lambda *a, **k: None)
    monkeypatch.setattr(registry, "sel", lambda: MagicMock())
    monkeypatch.setattr(registry, "app_source_dir", lambda n: src)

    async def _fake_build(git_url, name, log_lines, branch="main", **kwargs):
        # Capture BEFORE materializing, mirroring production's pre-clone
        # snapshot (and its effective-fresh reset after a move-aside).
        preexisted = (src / ".git").is_dir()
        src.mkdir(parents=True, exist_ok=True)
        (src / "app.json").write_text(json.dumps(cloned_manifest), encoding="utf-8")
        return {
            "ok": True,
            "pkg_dir": src,
            "_checkout_preexisted": preexisted,
            "_pre_pull_commit": "",
        }

    monkeypatch.setattr(registry, "_clone_build_app", _fake_build)
