"""Platform + manifest-contract tests for the ``project-scaffolder`` builtin app.

Create Folders From Project is a MANIFEST-ONLY builtin: this directory's app.json
is the whole app. Its UI is a lazy-loaded page in the host bundle
(``website/src/apps/project-scaffolder/ProjectScaffolderPage.tsx``) and its two
endpoints (``POST /api/project-scaffold/scan`` and ``/create``) are host routes in
``kiro_crew.dashboard.chat_folder_scaffold``. So the tests here validate the
*static* contract — which platforms the manifest claims, and the no-backend shape
that makes the Windows claim true — plus the one host behaviour that decides
whether the claim holds on a Windows volume (see the second section).

Everything resolves relative to this file, so the suite is machine independent.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from kiro_crew.apps.discovery import discover_builtin_apps
from kiro_crew.apps.manifest import AppManifest, PlatformConfig

# .../project_scaffolder/tests/test_manifest.py -> parents[1] is the app root.
APP_ROOT = Path(__file__).resolve().parents[1]
APP_JSON = APP_ROOT / "app.json"

APP_NAME = "project-scaffolder"
DECLARED_OS = ["macos", "linux", "windows"]


@pytest.fixture(scope="module")
def raw_manifest() -> dict:
    return json.loads(APP_JSON.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest() -> AppManifest:
    return AppManifest.from_json_file(APP_JSON)


# --- manifest platform declaration -------------------------------------------


def test_manifest_declares_every_platform_the_app_runs_on(raw_manifest: dict):
    """``platform.os`` summarises the whole app, and the whole app is a manifest
    plus a page in the host bundle — no backend entry point, no subprocess, no
    POSIX-only API — so it runs wherever the gateway runs.

    Pinned because omitting the block is not neutral: ``PlatformConfig.os``
    defaults to ``["macos", "linux"]``, which silently drops Windows and hides
    the app from the App Store on win32. That default was the ONLY thing
    blocking native Windows here.
    """
    assert raw_manifest["platform"]["os"] == DECLARED_OS


def test_declared_platforms_all_resolve_to_a_real_sys_platform(raw_manifest: dict):
    """Every declared name must map to a sys.platform value.

    An unmapped name is silently accepted into the list and then never matches,
    so a declaration can claim a platform the gate rejects.
    """
    cfg = PlatformConfig(os=raw_manifest["platform"]["os"])
    for sys_platform in ("darwin", "linux", "win32"):
        assert cfg.supports_platform(sys_platform), sys_platform


def test_typed_manifest_carries_the_widened_os_list(manifest: AppManifest):
    """The widened list must survive parsing AND re-serialization.

    ``PlatformConfig.to_dict()`` omits ``os`` when it still equals the implicit
    ``["macos", "linux"]`` default, so a manifest that lost the ``windows`` entry
    would round-trip through the App Store payload as a block with no ``os`` key
    at all — indistinguishable from never having declared one.
    """
    assert manifest.platform.os == DECLARED_OS
    assert manifest.platform.to_dict()["os"] == DECLARED_OS


@pytest.mark.skipif(
    sys.platform not in ("darwin", "linux", "win32"),
    reason="only the three platforms the manifest names are asserted here",
)
def test_app_is_supported_on_the_host_running_this_suite(manifest: AppManifest):
    """The gate that hides an app is evaluated against the live ``sys.platform``,
    so on a Windows CI runner this is the assertion that would have failed before
    the manifest was widened."""
    assert manifest.platform.supports_platform(sys.platform)


def test_scaffolding_does_not_require_the_desktop_shell(manifest: AppManifest):
    """``requiresDesktopApp`` is a different axis from ``os`` and must stay false:
    picking a directory and ticking rows is ordinary DOM work, and the scan runs
    server-side. Setting it would withhold the enable action from every browser
    session for no capability the page actually needs.
    """
    assert manifest.platform.requiresDesktopApp is False


def test_manifest_validates_with_no_errors(manifest: AppManifest):
    """A malformed ``platform`` block would make validation fail, and discovery
    drops any app whose manifest fails to validate — turning a platform widening
    into the app disappearing everywhere."""
    assert manifest.validate(app_root=APP_ROOT) == []


def test_discovery_still_includes_project_scaffolder():
    apps = discover_builtin_apps()
    names = [a.get("name") for a in apps]
    assert APP_NAME in names, f"{APP_NAME!r} not discovered. Discovered: {names}"


# --- the no-backend shape the Windows claim rests on -------------------------


def test_declares_no_backend_so_windows_needs_no_sandbox(raw_manifest: dict):
    """Windows has no native OS sandbox backend, so an app that spawns an external
    process fail-closes there until an operator opts in.

    This app claims FULL Windows support precisely because it spawns nothing: the
    scan is ``os.scandir`` on a worker thread and the create step writes folder
    records. If a ``backend`` or ``dependencies`` key is ever added, the
    ``windows`` declaration above stops being free and has to be re-argued
    against the sandbox story — that is what this test is here to force.
    """
    assert "backend" not in raw_manifest
    assert "dependencies" not in raw_manifest


def test_app_ships_no_executable_code(raw_manifest: dict):
    """Corollary of the test above, checked against disk rather than the manifest:
    the only Python in this builtin is the package marker and this test package.
    Any new module here could introduce a POSIX-only path, which is the class of
    bug the platform declaration would then be hiding.
    """
    assert raw_manifest["name"] == APP_NAME
    offenders = [
        str(p.relative_to(APP_ROOT))
        for p in APP_ROOT.rglob("*.py")
        if "tests" not in p.relative_to(APP_ROOT).parts and p.name != "__init__.py"
    ]
    assert not offenders, (
        "project-scaffolder is manifest-only; new code here must be audited for "
        f"platform-specific behaviour before app.json keeps claiming Windows: {offenders}"
    )


# --- the host behaviour that decides whether the claim holds on Windows -------
#
# The scan root is pinned by ``(st_dev, st_ino)``. Windows CPython fills st_ino
# from the file index only when the directory can be OPENED for it; on a volume
# that falls back to attribute-only data (some SMB shares, FAT/exFAT) st_ino is
# 0. Nothing exercised that branch in either direction, so the two tests below
# pin the contract the app's endpoints depend on: a zero inode is NO identity,
# and no identity is a REFUSAL rather than an unpinned scan.


class _ZeroInodeStat:
    """What ``os.lstat`` yields on a volume that reports no file index."""

    st_dev = 1
    st_ino = 0


class _OsShim:
    """The real ``os`` with ``lstat`` swapped out.

    Patched onto ``project_scan.os`` rather than onto the ``os`` module itself:
    a global patch also reaches pytest's own ``tmp_path`` cleanup, which reads
    Windows-only stat fields a stand-in does not carry.
    """

    def __init__(self, lstat):
        self._lstat = lstat

    def __getattr__(self, name):
        return getattr(os, name)

    def lstat(self, path, *args, **kwargs):
        return self._lstat(path, *args, **kwargs)


def test_zero_inode_reads_as_no_identity_not_as_identity_zero(monkeypatch):
    """A volume that reports ``st_ino == 0`` must produce ``None``.

    Returning ``(dev, 0)`` would be worse than refusing: every directory on such
    a volume would share one identity, so the scan's mismatch check would accept
    a root swapped for a different directory on the same volume.
    """
    from kiro_crew import project_scan

    monkeypatch.setattr(project_scan, "os", _OsShim(lambda path, *a, **k: _ZeroInodeStat()))
    assert project_scan.root_identity("any-volume-without-a-file-index") is None


def test_readable_root_has_an_identity_and_a_missing_one_does_not(tmp_path):
    """The happy path on any platform: a real directory pins, a vanished one does
    not. This is what keeps the test above honest — without it, a broken
    ``root_identity`` returning ``None`` unconditionally would still pass."""
    from kiro_crew import project_scan

    identity = project_scan.root_identity(str(tmp_path))
    assert identity is not None and identity[1] != 0
    assert project_scan.root_identity(str(tmp_path / "does-not-exist")) is None


def test_unreadable_identity_fails_closed_with_a_400(tmp_path, monkeypatch):
    """An unpinnable root must be REFUSED, not scanned unpinned.

    ``scan`` reads a missing expectation as "no caller pinned this", which is the
    state the gate exists to rule out — so the endpoint has to stop here. The
    message is deliberately not asserted: it is advisory prose and currently
    reads "must be an existing directory", which is misleading on a zero-inode
    volume where the directory plainly exists. ``code`` is the contract a client
    branches on, so that is what is pinned; a clearer message may land without
    touching this test, but relaxing the refusal itself must break it.
    """
    pytest.importorskip("aiohttp")
    from kiro_crew.dashboard import chat_folder_scaffold as mod

    resolved = str(tmp_path)
    monkeypatch.setattr(mod, "_validate_project_dir", lambda raw: (resolved, None))
    monkeypatch.setattr(mod, "path_contains_sensitive", lambda path: False)
    monkeypatch.setattr(mod, "root_identity", lambda path: None)

    with pytest.raises(mod._BadRequest) as caught:
        mod._resolve_root({"root": resolved})
    assert caught.value.code == "folder_scan_root_invalid"
