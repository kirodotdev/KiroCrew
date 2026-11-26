"""A pin row whose wheel copy is gone: what a release that deletes the copy ships.

The pin row stays, so adoption keeps working from it alone. A host that adopted keeps
running the app from the data home; a host that never adopted stages the pin and
adopts at the next restart, keeping the record's data, secret, grants and enabled
state; and until then the builtin-owned record is neither an orphan nor cleaned up.
Only the fixture app's wheel package is deleted (``drop_wheel_copy``); no real app is
touched. Offline and deterministic: the git fetch clones a local bare repository and
the clock is injected. Owning spec: app-kit-platform.md § Former built-in adoption.
"""

from __future__ import annotations

import urllib.error
from datetime import datetime, timezone

import git_repo_templates
import pytest
from builtin_adoption_helpers import (
    NAME,
    REPOSITORY,
    build_world,
    drop_wheel_copy,
    install_pins,
    requires_git,
    write_builtin_record,
)

from kiro_crew.apps import builtin_adoption as adoption
from kiro_crew.apps import builtin_adoption_apply as apply_mod
from kiro_crew.apps import execution as exec_mod
from kiro_crew.apps import manager
from kiro_crew.apps.bridges_sources import _app_resource_root, _registration_source
from kiro_crew.apps.builtin_adoption_state import read_adoption_markers, read_ledger
from kiro_crew.apps.hooks_integration import _app_hook_root
from kiro_crew.config.loader import _invalidate_config_cache

pytestmark = [requires_git, pytest.mark.asyncio]

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
NAV = {"route": f"/{NAME}", "icon": "Box"}
APPROVED = [f"/api/apps/{NAME}", f"/api/apps/{NAME}/*"]

repo_templates = git_repo_templates.repo_templates  # the fixture, bound for this module


class _Audit:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def log_api_access(self, **kwargs) -> None:
        self.rows.append(kwargs)

    def outcomes(self) -> list[str]:
        return [r["outcome"] for r in self.rows if r.get("operation") == "app_builtin_adoption"]


@pytest.fixture
def audit(monkeypatch):
    recorder = _Audit()
    import kiro_crew.sel as sel_mod

    monkeypatch.setattr(sel_mod, "sel", lambda: recorder)
    return recorder


@pytest.fixture
def world(tmp_path, monkeypatch, audit, repo_templates):
    """The fixture app pinned, with a builtin-owned record holding data, a secret and grants.

    Discovery answers nothing, so ``register_builtin_apps`` writes none of the core's
    own built-ins into the data home and never registers the fixture app over its record.
    """
    w = build_world(tmp_path, monkeypatch, templates=repo_templates)
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [])
    write_builtin_record(enabled=True, data="user data", secret=b"keep-this")
    manager._write_approved_grants(NAME, {"api": APPROVED, "events": []})
    _invalidate_config_cache()
    yield w
    manager.invalidate_orphan_cache()


async def _stage():
    return await adoption.run_staging_pass(now=lambda: T0)


async def _boot():
    """The two boot steps that touch the record: APPLY, then built-in registration."""
    outcomes = await adoption.apply_builtin_adoptions()
    manager.register_builtin_apps()
    return outcomes


def _record():
    record = manager._read_installed(NAME)
    assert record is not None
    return record


def _assert_kept(*, enabled: bool) -> None:
    dest = manager.app_dir(NAME)
    assert (dest / "data" / "store.txt").read_text(encoding="utf-8") == "user data"
    assert (dest / ".app_secret").read_bytes() == b"keep-this"
    assert _record().enabled is enabled
    grants = manager._read_approved_grants(NAME)
    assert grants is not None and sorted(grants["api"]) == sorted(APPROVED)


def _row() -> dict:
    return {row["name"]: row for row in manager.list_apps()}[NAME]


async def test_a_host_that_adopted_keeps_running_the_app_from_the_data_home(world, audit):
    assert await _stage() == {NAME: "staged"}
    assert (await _boot())[NAME] == "applied"
    drop_wheel_copy(world)

    # The first start of the release that ships no wheel copy.
    assert NAME not in await _boot()
    assert await _stage() == {NAME: "skipped"}

    record = _record()
    assert (record.origin, record.sourceUrl, record.sourceCommit) == (
        "registry",
        REPOSITORY,
        world.commit,
    )
    _assert_kept(enabled=True)
    dest = manager.app_dir(NAME)
    assert (dest / "ui" / "dist" / "index.mjs").is_file()
    assert (dest / "backend" / "routes.py").is_file()
    assert adoption.builtin_released(NAME)
    assert exec_mod.shipped_builtin_app_root(NAME) is None
    assert _app_hook_root(NAME) == dest
    assert _app_resource_root(NAME) == dest
    assert _registration_source(NAME)[1] == dest
    assert NAME not in manager.detect_orphaned_builtins(force_refresh=True)
    assert _row()["adoption"] == {"state": "adopted", "legacyNav": NAV}
    assert read_ledger(NAME).state == "adopted"
    assert world.git_calls == [world.commit], "an adopted record is never fetched again"
    assert audit.outcomes() == ["staged", "applied"]


async def test_a_host_that_never_adopted_stages_then_adopts_at_the_next_restart(world, audit):
    drop_wheel_copy(world)
    # The checks find no wheel manifest, so the version the wheel copy would carry
    # (D12) is not asked; the pin's own version and the record's still are.
    assert apply_mod._shipped_manifest(NAME) is None

    # The first start without the copy: nothing staged yet, so the record stays.
    assert (await _boot())[NAME] == "not_staged"
    assert manager.builtin_owns_installed(NAME)
    assert await _stage() == {NAME: "staged"}
    assert manager.builtin_owns_installed(NAME), "STAGE never mutates a record"
    assert _row()["adoption"] == {"state": "staged"}

    # The next restart swaps the record in place.
    assert (await _boot())[NAME] == "applied"
    record = _record()
    assert (record.origin, record.lifecycle, record.source) == (
        "registry",
        "gateway",
        f"registry:{NAME}",
    )
    assert (record.sourceUrl, record.sourceCommit) == (REPOSITORY, world.commit)
    _assert_kept(enabled=True)
    assert (manager.app_dir(NAME) / "ui" / "dist" / "index.mjs").is_file()
    assert adoption.builtin_released(NAME)
    assert read_adoption_markers()[NAME]["state"] == "adopted"
    assert _row()["adoption"] == {"state": "adopted", "legacyNav": NAV}
    assert audit.outcomes() == ["staged", "applied"]


async def test_a_disabled_record_adopts_disabled(world):
    write_builtin_record(enabled=False, data="user data", secret=b"keep-this")
    drop_wheel_copy(world)
    assert await _stage() == {NAME: "staged"}
    assert (await _boot())[NAME] == "applied"
    _assert_kept(enabled=False)


async def test_a_pending_record_is_never_orphaned_or_cleaned_up_while_pinned(
    world, monkeypatch, tmp_path
):
    drop_wheel_copy(world)
    world.git_outcome = "network"
    world.archive_error = urllib.error.URLError(OSError("offline"))
    assert await _stage() == {NAME: "pending"}
    assert (await _boot())[NAME] == "not_staged"

    assert NAME not in manager.detect_orphaned_builtins(force_refresh=True)
    assert not manager.migrated_builtin_cleanup_applies(NAME)
    refused = manager.uninstall_app(NAME, keep_data=False, retired_builtin=True)
    assert (refused.ok, refused.error_code) == (False, "not_orphaned")
    assert not manager.cleanup_migrated_builtin(NAME).ok
    assert "orphaned" not in _row()
    assert _row()["adoption"]["state"] == "pending"
    assert manager.builtin_owns_installed(NAME)
    _assert_kept(enabled=True)
    # It holds no first-party status: with no wheel copy there is no shipped code.
    assert not exec_mod.is_builtin_app(app_name=NAME)
    assert NAME not in exec_mod.builtin_app_names()

    # The pin row is what keeps it: the release that drops the row too leaves an orphan.
    install_pins(monkeypatch, tmp_path / "unpinned.json", [])
    assert NAME in manager.detect_orphaned_builtins(force_refresh=True)
