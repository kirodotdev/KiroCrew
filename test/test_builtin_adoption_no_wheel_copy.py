"""A pin row whose wheel copy is gone: what a release that deletes the copy ships.

The pin row stays, so adoption keeps working from it alone. A host that adopted keeps
running the app from the data home; a host that never adopted stages the pin and
adopts at the next restart, keeping the record's data, secret, grants and enabled
state; and until then the builtin-owned record is neither an orphan nor cleaned up.
Only the fixture app's wheel package is deleted (``drop_wheel_copy``, which also freezes
the copy into the row as a deletion release does); no real app is touched. With no copy
left, E9 and the swap compare with the row's frozen copy, not the record a host's last
booted release wrote, so a host that skipped the release with the newest copy adopts as
one that booted it. Offline and deterministic: the git fetch clones a local bare
repository and the clock is injected. Owning spec: app-kit-platform.md § Former built-in
adoption, "A pin row with no wheel copy".
"""

from __future__ import annotations

import json
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
    shipped_manifest,
    write_builtin_record,
)

from kiro_crew.apps import builtin_adoption as adoption
from kiro_crew.apps import builtin_adoption_apply as apply_mod
from kiro_crew.apps import builtin_adoption_pins as pins_mod
from kiro_crew.apps import execution as exec_mod
from kiro_crew.apps import manager
from kiro_crew.apps.bridges_sources import _app_resource_root, _registration_source
from kiro_crew.apps.builtin_adoption_baseline import (
    deleted_copy_baseline,
    freeze_grant_inputs,
    no_copy_baseline,
)
from kiro_crew.apps.builtin_adoption_state import read_adoption_markers, read_ledger
from kiro_crew.apps.hooks_integration import _app_hook_root
from kiro_crew.apps.point_grants import grant_source
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


# --------------------------------------------------------------------------- #
# E9 and the swap compare with the row's frozen copy once the wheel copy is gone
# --------------------------------------------------------------------------- #

OWN = [f"/api/apps/{NAME}", f"/api/apps/{NAME}/*"]
#: The last copy a release shipped: a host path, a direct-check flag (``chatSlots``)
#: and an explicit-only family (``bots``), all of which a built-in approves at start.
NEWEST = {"api": [*OWN, "/api/browse-dirs"], "storage": True, "chatSlots": True, "bots": ["read"]}
#: An older copy, which a host last booted on an older release still holds.
OLDER = {"api": OWN, "storage": True}
#: What the adopted record approves when the pinned tree declares :data:`NEWEST`.
NEWEST_APPROVED = {"api": [*OWN, "/api/browse-dirs"], "bots": ["read"]}


@pytest.fixture
def host(tmp_path, monkeypatch, audit, repo_templates):
    """A factory: the fixture app's *wheel* copy, its pinned tree and the record a host holds.

    The record and its approved set are what registering *record* stored, so it is a
    host whose last booted release shipped that copy.
    """

    def make(*, wheel, pinned, record=OLDER):
        w = build_world(
            tmp_path,
            monkeypatch,
            templates=repo_templates,
            wheel_manifest=shipped_manifest(permissions=wheel),
            permissions=pinned,
        )
        monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [])
        last_booted = shipped_manifest(permissions=record)
        write_builtin_record(manifest=last_booted)
        manager._write_approved_grants(
            NAME, manager.builtin_approved_grants(grant_source(last_booted))
        )
        _invalidate_config_cache()
        return w

    yield make
    manager.invalidate_orphan_cache()


def _approved() -> dict[str, list[str]]:
    grants = manager._read_approved_grants(NAME)
    assert grants is not None
    return {family: entries for family, entries in grants.items() if entries}


async def _staged_then_booted() -> str:
    assert await _stage() == {NAME: "staged"}
    return (await _boot())[NAME]


@pytest.mark.parametrize("path", ["booted_the_last_copy", "copy_deleted"])
async def test_a_host_that_skipped_the_last_copy_adopts_as_one_that_booted_it(
    host, monkeypatch, path
):
    """The regression: a record left by an older release, and a release with no copy.

    Both paths end with the same record and grants. Held to the older record instead,
    the deleted path would be refused at every pass.
    """
    w = host(wheel=NEWEST, pinned=NEWEST)
    if path == "booted_the_last_copy":
        copy = json.loads((w.wheel / "app.json").read_text(encoding="utf-8"))
        monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [copy])
    else:
        drop_wheel_copy(w)
    assert (await _boot())[NAME] == "not_staged"

    assert await _staged_then_booted() == "applied"
    record = _record()
    assert (record.origin, record.sourceUrl, record.sourceCommit) == (
        "registry",
        REPOSITORY,
        w.commit,
    )
    assert _approved() == NEWEST_APPROVED
    assert read_ledger(NAME).state == "adopted"


@pytest.mark.parametrize(
    ("frozen", "pinned"),
    [
        pytest.param(NEWEST, {**NEWEST, "api": [*NEWEST["api"], "/api/recent-projects"]}, id="api"),
        pytest.param(NEWEST, {**NEWEST, "bots": ["read", "wake"]}, id="explicit_only_family"),
        pytest.param(NEWEST, {**NEWEST, "workerAutoApprove": True}, id="permission_family"),
        pytest.param(NEWEST, {**NEWEST, "sessionApproval": True}, id="session_approval"),
        pytest.param(OLDER, {**OLDER, "chatSlots": True}, id="chat_slots"),
    ],
)
async def test_a_pin_that_declares_more_than_the_frozen_copy_is_held_back(host, frozen, pinned):
    """The frozen copy is the ceiling: anything past it waits, as past the wheel copy.

    The record is as wide as the pin, and is not read once the copy is gone.
    """
    w = host(wheel=frozen, pinned=pinned, record=pinned)
    drop_wheel_copy(w)
    assert await _staged_then_booted() == "blocked"
    assert read_ledger(NAME).reason == "permission_widening"
    assert manager.builtin_owns_installed(NAME), "a refusal changes nothing"
    assert await _stage() == {NAME: "blocked"}, "every pass re-checks and still refuses"


async def test_a_frozen_copy_wider_than_the_pin_grants_only_what_the_pin_declares(host):
    w = host(wheel=NEWEST, pinned=OLDER)
    drop_wheel_copy(w)
    assert await _staged_then_booted() == "applied"
    assert _approved() == {"api": OWN}


async def test_a_row_whose_copy_ships_is_held_to_the_record_not_to_a_frozen_copy(host):
    """One source of truth: a ``deletedCopy`` beside a shipped copy is never read."""
    host(wheel=OLDER, pinned=NEWEST)
    table = json.loads(pins_mod._PINS_PATH.read_text(encoding="utf-8"))
    (row,) = table["pins"]
    row[pins_mod.DELETED_COPY_KEY] = freeze_grant_inputs(shipped_manifest(permissions=NEWEST))
    pins_mod._PINS_PATH.write_text(json.dumps(table), encoding="utf-8")
    pins_mod.load_adoption_pins.cache_clear()
    pin = pins_mod.adoption_pin(NAME)
    assert pin is not None and pin.deleted_copy
    assert no_copy_baseline(pin) is None
    assert await _staged_then_booted() == "blocked"
    assert read_ledger(NAME).reason == "permission_widening"


async def test_a_row_with_neither_a_copy_nor_a_frozen_copy_vouches_for_nothing(host):
    """Fail-closed: the record a past release wrote is never the baseline."""
    w = host(wheel=OLDER, pinned=OLDER, record=NEWEST)
    drop_wheel_copy(w, freeze=False)
    pin = pins_mod.adoption_pin(NAME)
    assert pin is not None and pin.deleted_copy == ""
    baseline = no_copy_baseline(pin)
    assert baseline is not None and not any(baseline.approved.values())
    assert await _staged_then_booted() == "blocked"
    assert read_ledger(NAME).reason == "permission_widening"
    assert manager.builtin_owns_installed(NAME)


async def test_the_swap_compares_and_carries_the_baseline_it_is_given(host):
    """``update_app`` re-checks the same baseline E9 used and stores what it approves."""
    w = host(wheel=NEWEST, pinned=NEWEST)

    def swap(permissions):
        frozen = freeze_grant_inputs(shipped_manifest(permissions=permissions))
        baseline = deleted_copy_baseline(frozen)
        return manager.adopt_builtin_record(
            NAME, w.tree, repository=REPOSITORY, commit=w.commit, baseline=baseline
        )

    refused = swap(OLDER)
    assert (refused.ok, refused.error_code) == (False, "adoption_would_widen")
    assert manager.builtin_owns_installed(NAME)
    assert _approved() == {"api": OWN}, "the record's set is untouched"

    result = swap(NEWEST)
    assert result.ok, result.error
    assert _approved() == NEWEST_APPROVED
    assert _record().sourceCommit == w.commit
