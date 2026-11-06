"""APPLY of built-in adoption: the swap, the grant, retries, crash windows, audit.

Matrix rows 5, 7, 8, 9, 10, 11, 12, 15, 16, 17 and 24 of the adoption contract.
Offline and deterministic: the git fetch clones a local bare repository, the
archive is served from memory, and every clock is injected.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from builtin_adoption_helpers import (
    NAME,
    REPOSITORY,
    agent_section,
    build_world,
    requires_git,
    seed_agent,
    write_builtin_record,
)

from kiro_crew.apps import builtin_adoption as adoption
from kiro_crew.apps import builtin_adoption_apply as apply_mod
from kiro_crew.apps import manager
from kiro_crew.apps.builtin_adoption_state import read_adoption_markers, read_ledger
from kiro_crew.config.loader import _invalidate_config_cache, config_local_path
from kiro_crew.testing.ids import UNALLOCATABLE_PID

pytestmark = [requires_git, pytest.mark.asyncio]

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


class _HardKill(BaseException):
    """Stands in for a hard kill: no ``except Exception`` on the APPLY path catches it."""


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
def world(tmp_path, monkeypatch, audit):
    w = build_world(tmp_path, monkeypatch)
    write_builtin_record()
    _invalidate_config_cache()
    return w


async def _stage(now=T0):
    return await adoption.run_staging_pass(now=lambda: now)


async def _boot():
    return await adoption.apply_builtin_adoptions()


def _record():
    record = manager._read_installed(NAME)
    assert record is not None
    return record


async def test_stage_then_apply_adopts_the_record_in_place(world, audit):
    assert await _stage() == {NAME: "staged"}
    assert manager.builtin_owns_installed(NAME), "STAGE never mutates a record"
    assert await _boot() == {NAME: "applied"}
    record = _record()
    assert (record.origin, record.lifecycle, record.resources) == ("registry", "gateway", "gateway")
    assert record.source == f"registry:{NAME}"
    assert (record.sourceUrl, record.sourceCommit, record.sourceRegistry) == (
        REPOSITORY,
        world.commit,
        "",
    )
    assert record.enabled is True and not record.sessionApprovalConsentPending
    assert record.migratedTo == ""
    assert adoption.builtin_released(NAME)
    marker = read_adoption_markers()[NAME]
    assert marker["state"] == "adopted" and marker["commit"] == world.commit
    ledger = read_ledger(NAME)
    assert ledger is not None and ledger.state == "adopted"
    assert audit.outcomes() == ["staged", "applied"]


async def test_apply_records_the_legacy_nav_beside_the_marker(world, tmp_path, monkeypatch):
    from builtin_adoption_helpers import install_pins

    await _stage()
    assert await _boot() == {NAME: "applied"}
    agent = agent_section(world.home)
    pinned = adoption.adoption_pin(NAME)
    nav = {"route": pinned.legacy_route, "icon": pinned.legacy_icon}
    assert agent["apps_builtin_adoption_nav"] == {NAME: nav}
    assert set(agent["apps_builtin_adoption"][NAME]) == {
        "state",
        "repository",
        "commit",
        "addedName",
        "setRepository",
        "removedLocal",
    }
    # The release that drops the pin row still answers the same nav.
    install_pins(monkeypatch, tmp_path / "unpinned.json", [])
    assert adoption.adoption_row(NAME, _record()) == {"state": "adopted", "legacyNav": nav}


def _drop_recorded_nav(home) -> None:
    path = home / "config.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["agent"].pop("apps_builtin_adoption_nav")
    path.write_text(json.dumps(raw), encoding="utf-8")
    _invalidate_config_cache()


async def test_a_boot_records_the_nav_of_an_app_adopted_without_one(world):
    await _stage()
    assert await _boot() == {NAME: "applied"}
    recorded = agent_section(world.home)["apps_builtin_adoption_nav"]
    _drop_recorded_nav(world.home)
    assert await _boot() == {}
    assert agent_section(world.home)["apps_builtin_adoption_nav"] == recorded
    stamp = (world.home / "config.json").read_bytes()
    assert await _boot() == {}
    assert (world.home / "config.json").read_bytes() == stamp, "a recorded nav is not rewritten"


async def test_no_nav_is_recorded_for_a_record_from_another_repository(world):
    await _stage()
    assert await _boot() == {NAME: "applied"}
    _drop_recorded_nav(world.home)
    record = _record()
    record.sourceUrl = "https://github.com/other/fork"
    manager._write_installed(NAME, record)
    assert await _boot() == {}
    assert "apps_builtin_adoption_nav" not in agent_section(world.home)


async def test_a_fresh_install_registers_the_built_in_then_adopts_like_an_upgrade(
    tmp_path, monkeypatch, audit
):
    """Row 18: a host that never had the app gets the built-in, then the same move."""
    world = build_world(tmp_path, monkeypatch)
    _invalidate_config_cache()
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [_shipped_definition(world)])
    manager.register_builtin_apps()
    assert manager.builtin_owns_installed(NAME)
    assert not adoption.builtin_released(NAME)
    assert await _stage() == {NAME: "staged"}
    assert await _boot() == {NAME: "applied"}
    record = _record()
    assert (record.origin, record.sourceUrl) == ("registry", REPOSITORY)
    assert adoption.builtin_released(NAME)
    assert audit.outcomes() == ["staged", "applied"]


# --------------------------------------------------------------------------- #
# Row 15: preservation; row 9: permission parity
# --------------------------------------------------------------------------- #


async def test_adoption_preserves_data_secret_grants_and_the_enabled_flag(world):
    write_builtin_record(enabled=False, data="user data", secret=b"keep-this")
    approved = [f"/api/apps/{NAME}", f"/api/apps/{NAME}/*"]
    manager._write_approved_grants(NAME, {"api": approved, "events": []})
    await _stage()
    assert await _boot() == {NAME: "applied"}
    dest = manager.app_dir(NAME)
    assert (dest / "data" / "store.txt").read_text(encoding="utf-8") == "user data"
    assert (dest / ".app_secret").read_bytes() == b"keep-this"
    assert (dest / "ui" / "dist" / "index.mjs").is_file()
    assert _record().enabled is False
    grants = manager._read_approved_grants(NAME)
    assert grants is not None and sorted(grants["api"]) == sorted(approved)
    from kiro_crew.apps.point_grants import grant_source

    held = manager._held_back_entries(grants, grant_source(manager.get_app_manifest(NAME)))
    assert not any(held.values())


async def test_an_approved_set_narrower_than_the_pin_is_widening(world):
    """A grant the owner had not approved would be held back, so adoption refuses."""
    manager._write_approved_grants(NAME, {"api": [f"/api/apps/{NAME}"], "events": []})
    await _stage()
    assert await _boot() == {NAME: "blocked"}
    assert read_ledger(NAME).reason == "permission_widening"
    assert manager.builtin_owns_installed(NAME)


@pytest.mark.parametrize(
    "overrides",
    [
        {"permissions": {"api": [f"/api/apps/{NAME}", "/api/sessions"], "storage": True}},
        {
            "permissions": {
                "api": [f"/api/apps/{NAME}", f"/api/apps/{NAME}/*"],
                "storage": True,
                "sessionApproval": True,
            }
        },
    ],
)
async def test_a_widening_manifest_is_refused_with_nothing_mutated(
    tmp_path, monkeypatch, audit, overrides
):
    build_world(tmp_path, monkeypatch, **overrides)
    write_builtin_record()
    home = manager.config_dir()
    await _stage()
    before = (manager.app_dir(NAME) / "installed.json").read_bytes()
    assert await _boot() == {NAME: "blocked"}
    assert (manager.app_dir(NAME) / "installed.json").read_bytes() == before
    assert NAME not in agent_section(home).get("apps_trusted", [])
    assert NAME not in read_adoption_markers()
    ledger = read_ledger(NAME)
    assert ledger is not None and (ledger.state, ledger.reason) == (
        "blocked",
        "permission_widening",
    )


# --------------------------------------------------------------------------- #
# Row 10: the grant mutation shape
# --------------------------------------------------------------------------- #


async def test_the_grant_writes_three_keys_and_the_marker_in_one_write(world, monkeypatch):
    seed_agent(world.home, {"apps_trusted_local": [NAME, "other"]})
    from kiro_crew.apps import agent_config_writer

    trust_writes = []
    real = agent_config_writer.mutate_trust_settings

    async def counting(mutate):
        trust_writes.append(mutate)
        await real(mutate)

    monkeypatch.setattr(agent_config_writer, "mutate_trust_settings", counting)
    await _stage()
    assert await _boot() == {NAME: "applied"}
    agent = agent_section(world.home)
    assert NAME in agent["apps_trusted"]
    assert agent["apps_trusted_repositories"][NAME] == REPOSITORY
    assert agent["apps_trusted_local"] == ["other"]
    assert len(trust_writes) == 1
    marker = agent["apps_builtin_adoption"][NAME]
    assert (marker["addedName"], marker["setRepository"], marker["removedLocal"]) == (
        True,
        True,
        True,
    )


async def test_a_grant_to_another_repository_is_never_overwritten(world):
    other = "https://github.com/someone/else"
    seed_agent(world.home, {"apps_trusted": [NAME], "apps_trusted_repositories": {NAME: other}})
    await _stage()
    assert await _boot() == {NAME: "blocked"}
    assert agent_section(world.home)["apps_trusted_repositories"][NAME] == other
    assert manager.builtin_owns_installed(NAME)
    assert read_ledger(NAME).reason == "trust_conflict"


async def test_a_same_repository_grant_is_reused_without_a_trust_write(world, monkeypatch):
    seed_agent(
        world.home, {"apps_trusted": [NAME], "apps_trusted_repositories": {NAME: REPOSITORY}}
    )
    from kiro_crew.apps import agent_config_writer

    async def refuse(_mutate):
        raise AssertionError("no trust write expected")

    monkeypatch.setattr(agent_config_writer, "mutate_trust_settings", refuse)
    await _stage()
    assert await _boot() == {NAME: "applied"}
    marker = agent_section(world.home)["apps_builtin_adoption"][NAME]
    assert not (marker["addedName"] or marker["setRepository"] or marker["removedLocal"])


async def test_a_failed_swap_rolls_back_exactly_what_the_grant_added(world, monkeypatch, audit):
    seed_agent(world.home, {"apps_trusted": ["keep"], "apps_trusted_local": [NAME]})
    await _stage()

    def failing(*_a, **_k):
        return manager.AppResult(ok=False, name=NAME, error="disk", error_code="")

    monkeypatch.setattr(manager, "adopt_builtin_record", failing)
    assert await _boot() == {NAME: "apply_failed"}
    agent = agent_section(world.home)
    assert agent["apps_trusted"] == ["keep"]
    assert NAME not in agent.get("apps_trusted_repositories", {})
    assert agent["apps_trusted_local"] == [NAME]
    assert NAME not in agent.get("apps_builtin_adoption", {})
    assert manager.builtin_owns_installed(NAME)
    assert read_ledger(NAME).state == "staged", "a local failure re-applies without a fetch"
    assert audit.outcomes()[-2:] == ["rolled_back", "apply_failed"]


# --------------------------------------------------------------------------- #
# Rows 7 and 8: transient and deterministic failures
# --------------------------------------------------------------------------- #


async def test_offline_backs_off_deterministically_and_leaves_the_built_in(world, audit):
    import urllib.error

    world.git_outcome = "network"
    world.archive_error = urllib.error.URLError(OSError("offline"))
    expected = [5, 10, 20, 40, 80, 160, 320, 640, 1280, 1440, 1440]
    now = T0
    for minutes in expected:
        assert await _stage(now) == {NAME: "pending"}
        ledger = read_ledger(NAME)
        assert ledger is not None and ledger.reason == "network_unreachable"
        due = datetime.fromisoformat(ledger.next_attempt_at)
        assert due - now == timedelta(minutes=minutes)
        calls = (len(world.git_calls), len(world.archive_calls))
        assert await _stage(due - timedelta(seconds=1)) == {NAME: "skipped"}
        assert (len(world.git_calls), len(world.archive_calls)) == calls
        now = due
    assert manager.builtin_owns_installed(NAME)
    assert not adoption.builtin_released(NAME)
    assert await _boot() == {NAME: "not_staged"}
    assert audit.outcomes() == ["stage_failed"], "one row per transition, not per retry"


async def test_a_retry_failing_for_a_new_reason_records_that_transition(world, audit):
    import urllib.error

    world.git_outcome = "network"
    world.archive_error = urllib.error.URLError(OSError("offline"))
    assert await _stage(T0) == {NAME: "pending"}
    due = datetime.fromisoformat(read_ledger(NAME).next_attempt_at)
    world.archive_error = urllib.error.HTTPError("https://x.invalid/a", 503, "busy", None, None)
    assert await _stage(due) == {NAME: "pending"}
    assert read_ledger(NAME).reason == "http_status"
    assert audit.outcomes() == ["stage_failed", "stage_failed"]


async def test_a_restart_before_the_next_attempt_makes_no_fetch(world):
    import urllib.error

    world.git_outcome = "network"
    world.archive_error = urllib.error.URLError(OSError("offline"))
    await _stage(T0)
    calls = (len(world.git_calls), len(world.archive_calls))
    assert await _stage(T0 + timedelta(minutes=4)) == {NAME: "skipped"}
    assert (len(world.git_calls), len(world.archive_calls)) == calls


@pytest.mark.parametrize(
    "policy",
    [
        {"mode": "enforce", "require_signature": True},
        {"mode": "enforce", "approved": ["other-app"]},
    ],
)
async def test_an_admission_policy_blocks_visibly_then_adopts_once_relaxed(world, policy):
    policy_path = world.home / "app_admission.json"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    assert await _stage() == {NAME: "staged"}
    assert await _boot() == {NAME: "blocked"}
    assert read_ledger(NAME).reason == "admission_denied"
    assert manager.builtin_owns_installed(NAME)
    row = adoption.adoption_row(NAME, _record())
    assert row == {"state": "blocked", "reason": "admission_denied"}
    calls = (len(world.git_calls), len(world.archive_calls))
    assert await _stage(T0 + timedelta(days=3)) == {NAME: "blocked"}, "re-checked, not fetched"

    def no_copy(*_a, **_k):
        raise AssertionError("a boot copied and hashed a tree it had already refused")

    with pytest.MonkeyPatch.context() as m:
        m.setattr(apply_mod, "_prepare_tree", no_copy)
        assert await _boot() == {NAME: "blocked"}
    assert (len(world.git_calls), len(world.archive_calls)) == calls
    policy_path.unlink()
    assert await _boot() == {NAME: "blocked"}, "only STAGE re-checks a blocked pin"
    assert await _stage(T0 + timedelta(days=3)) == {NAME: "staged"}
    assert (len(world.git_calls), len(world.archive_calls)) == calls
    assert await _boot() == {NAME: "applied"}


async def test_a_transient_config_failure_at_apply_is_retried_at_the_next_boot(world, monkeypatch):
    from kiro_crew.dashboard.handlers.security_audit import ConfigCorruptError

    real = apply_mod.write_grant_and_marker
    calls = []

    async def unreadable_once(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ConfigCorruptError("config.json unreadable")
        return await real(*args, **kwargs)

    monkeypatch.setattr(apply_mod, "write_grant_and_marker", unreadable_once)
    await _stage()
    assert await _boot() == {NAME: "blocked"}
    assert read_ledger(NAME).reason == "config_unreadable"
    assert manager.builtin_owns_installed(NAME)
    assert await _stage() == {NAME: "staged"}
    assert await _boot() == {NAME: "applied"}
    assert len(calls) == 2


async def test_an_overlay_owned_trust_key_blocks_then_adopts_once_removed(world):
    config_local_path().write_text(json.dumps({"agent": {"apps_trusted": []}}), encoding="utf-8")
    _invalidate_config_cache()
    await _stage()
    assert await _boot() == {NAME: "blocked"}
    assert read_ledger(NAME).reason == "trust_overlay_owned"
    assert NAME not in read_adoption_markers()
    assert await _stage() == {NAME: "blocked"}, "the re-check sees the overlay too"
    assert read_ledger(NAME).reason == "trust_overlay_owned"
    config_local_path().unlink()
    _invalidate_config_cache()
    assert await _stage() == {NAME: "staged"}
    assert await _boot() == {NAME: "applied"}


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"name": "someone-else"}, "manifest_identity_mismatch"),
        ({"version": "0.2.0"}, "version_mismatch"),
        ({"defaultEnabled": True}, "pinned_feature_unsupported"),
        ({"mcpServers": {"x": {"command": "x"}}}, "pinned_feature_unsupported"),
    ],
)
async def test_a_deterministic_tree_refusal_is_never_retried(
    tmp_path, monkeypatch, audit, overrides, code
):
    world = build_world(tmp_path, monkeypatch, **overrides)
    write_builtin_record()
    assert await _stage() == {NAME: "blocked"}
    assert read_ledger(NAME).reason == code
    calls = (len(world.git_calls), len(world.archive_calls))
    assert await _stage(T0 + timedelta(days=30)) == {NAME: "skipped"}
    assert await _boot() == {NAME: "blocked"}
    assert (len(world.git_calls), len(world.archive_calls)) == calls
    assert manager.builtin_owns_installed(NAME)
    assert audit.outcomes() == ["blocked"]


async def test_a_new_pin_commit_resets_the_ledger(world, monkeypatch, tmp_path):
    import urllib.error

    world.git_outcome = "network"
    world.archive_error = urllib.error.URLError(OSError("offline"))
    await _stage(T0)
    from builtin_adoption_helpers import install_pins, pin_row

    install_pins(monkeypatch, tmp_path / "pins2.json", [pin_row("e" * 40, "f" * 64, "0" * 64)])
    assert await _stage(T0) == {NAME: "pending"}
    assert read_ledger(NAME).commit == "e" * 40 and read_ledger(NAME).attempts == 1


# --------------------------------------------------------------------------- #
# Row 17: tampering between STAGE and APPLY
# --------------------------------------------------------------------------- #


async def test_a_staged_tree_tampered_before_apply_is_never_swapped_in(world):
    await _stage()
    checkout = manager.config_dir() / read_ledger(NAME).staged["path"]
    (checkout / "backend" / "routes.py").write_text("raise SystemExit\n", encoding="utf-8")
    before = (manager.app_dir(NAME) / "installed.json").read_bytes()
    assert await _boot() == {NAME: "blocked"}
    assert read_ledger(NAME).reason == "tree_digest_mismatch"
    assert (manager.app_dir(NAME) / "installed.json").read_bytes() == before
    assert NAME not in read_adoption_markers()


async def test_apply_reads_the_artifact_where_the_pin_puts_it_never_where_the_ledger_says(world):
    from kiro_crew.apps.builtin_adoption_state import write_ledger

    await _stage()
    ledger = read_ledger(NAME)
    ledger.staged["path"] = "elsewhere"
    write_ledger(ledger)
    assert await _boot() == {NAME: "applied"}


@pytest.mark.skipif(os.name != "posix", reason="a plain symlink needs no privilege on POSIX")
async def test_a_link_at_the_staged_location_is_never_copied(world):
    await _stage()
    checkout = manager.config_dir() / read_ledger(NAME).staged["path"]
    moved = manager.config_dir() / "moved-checkout"
    checkout.rename(moved)
    checkout.symlink_to(moved, target_is_directory=True)
    assert await _boot() == {NAME: "not_staged"}
    assert read_ledger(NAME).state == "pending"
    assert manager.builtin_owns_installed(NAME)


async def test_a_host_below_the_pin_seam_level_fetches_and_copies_nothing(world, monkeypatch):
    from builtin_adoption_helpers import host_with_seams

    assert await _stage() == {NAME: "staged"}
    fetched = (len(world.git_calls), len(world.archive_calls))
    host_with_seams(monkeypatch, 0)
    assert await _stage(T0 + timedelta(days=3)) == {NAME: "held"}
    assert (len(world.git_calls), len(world.archive_calls)) == fetched

    def no_copy(*_a, **_k):
        raise AssertionError("APPLY materialized a tree this build can never run")

    with monkeypatch.context() as m:
        m.setattr(apply_mod, "_prepare_tree", no_copy)
        assert await _boot() == {NAME: "blocked"}
        assert await _boot() == {NAME: "blocked"}
    ledger = read_ledger(NAME)
    assert (ledger.state, ledger.reason) == ("blocked", "host_sdk_too_old")
    assert ledger.staged, "the staged artifact is kept for a build that can run it"
    assert manager.builtin_owns_installed(NAME)
    host_with_seams(monkeypatch, 1)
    assert await _stage(T0 + timedelta(days=3)) == {NAME: "staged"}
    assert (len(world.git_calls), len(world.archive_calls)) == fetched
    assert await _boot() == {NAME: "applied"}


async def test_a_fresh_host_below_the_pin_seam_level_never_fetches(world, monkeypatch):
    from builtin_adoption_helpers import host_with_seams

    host_with_seams(monkeypatch, 0)
    assert await _stage() == {NAME: "held"}
    assert (world.git_calls, world.archive_calls) == ([], [])
    assert read_ledger(NAME) is None
    assert await _boot() == {}, "a pin this build can never run reports nothing at boot"


# --------------------------------------------------------------------------- #
# Row 11: crash windows
# --------------------------------------------------------------------------- #


async def _crash_after_the_grant(monkeypatch) -> None:
    original = manager.adopt_builtin_record

    def killed(*_a, **_k):
        raise _HardKill

    monkeypatch.setattr(manager, "adopt_builtin_record", killed)
    with pytest.raises(_HardKill):
        await _boot()
    monkeypatch.setattr(manager, "adopt_builtin_record", original)
    assert read_adoption_markers()[NAME]["state"] == "granting"


@pytest.mark.parametrize("refusal", ["tampered", "below_seam_level", "blocked"])
async def test_a_crash_surviving_grant_is_rolled_back_by_a_refusal_before_the_swap(
    world, monkeypatch, refusal
):
    from builtin_adoption_helpers import host_with_seams

    from kiro_crew.apps.builtin_adoption_state import write_ledger

    await _stage()
    await _crash_after_the_grant(monkeypatch)
    if refusal == "tampered":
        checkout = manager.config_dir() / read_ledger(NAME).staged["path"]
        (checkout / "backend" / "routes.py").write_text("raise SystemExit\n", encoding="utf-8")
    elif refusal == "below_seam_level":
        host_with_seams(monkeypatch, 0)
    else:
        ledger = read_ledger(NAME)
        ledger.state, ledger.reason = "blocked", "admission_denied"
        write_ledger(ledger)
    assert await _boot() == {NAME: "blocked"}
    assert NAME not in read_adoption_markers()
    assert NAME not in agent_section(world.home).get("apps_trusted", [])
    assert manager.builtin_owns_installed(NAME)


@pytest.mark.parametrize("refusal", ["blocked", "swap"])
async def test_a_binding_the_operator_re_pointed_after_the_crash_survives_the_rollback(
    world, monkeypatch, refusal
):
    """The undo is decided from the file: a binding to another repository is the operator's."""
    from kiro_crew.apps.builtin_adoption_state import write_ledger

    await _stage()
    await _crash_after_the_grant(monkeypatch)
    other = "https://github.com/someone/else"
    seed_agent(world.home, {"apps_trusted_repositories": {NAME: other}})
    if refusal == "blocked":
        ledger = read_ledger(NAME)
        ledger.state, ledger.reason = "blocked", "admission_denied"
        write_ledger(ledger)
    # "swap": the update refuses a pin whose repository differs from the binding.
    assert await _boot() == {NAME: "blocked" if refusal == "blocked" else "apply_failed"}
    agent = agent_section(world.home)
    assert agent["apps_trusted_repositories"][NAME] == other
    assert NAME in agent["apps_trusted"]
    assert NAME not in agent.get("apps_builtin_adoption", {})
    assert manager.builtin_owns_installed(NAME)
    if refusal == "swap":
        assert await _boot() == {NAME: "blocked"}
        assert read_ledger(NAME).reason == "trust_conflict"
        assert agent_section(world.home)["apps_trusted_repositories"][NAME] == other


async def test_a_grant_the_operator_withdrew_after_the_crash_is_not_restored(world, monkeypatch):
    """With the binding gone, the rollback never puts back the local marker it removed."""
    from kiro_crew.apps.builtin_adoption_state import write_ledger

    seed_agent(world.home, {"apps_trusted_local": [NAME]})
    await _stage()
    await _crash_after_the_grant(monkeypatch)
    seed_agent(world.home, {"apps_trusted_repositories": {}, "apps_trusted_local": []})
    ledger = read_ledger(NAME)
    ledger.state, ledger.reason = "blocked", "admission_denied"
    write_ledger(ledger)
    assert await _boot() == {NAME: "blocked"}
    agent = agent_section(world.home)
    assert NAME not in agent["apps_trusted"]
    assert agent["apps_trusted_local"] == []
    assert NAME not in agent.get("apps_builtin_adoption", {})


async def test_a_crash_after_the_grant_reuses_it_on_the_next_boot(world, monkeypatch):
    await _stage()
    original = manager.adopt_builtin_record

    def killed(*_a, **_k):
        raise _HardKill

    monkeypatch.setattr(manager, "adopt_builtin_record", killed)
    with pytest.raises(_HardKill):
        await _boot()
    marker = read_adoption_markers()[NAME]
    assert marker["state"] == "granting" and marker["addedName"]
    assert manager.builtin_owns_installed(NAME)
    monkeypatch.setattr(manager, "adopt_builtin_record", original)
    assert await _boot() == {NAME: "applied"}
    final = agent_section(world.home)["apps_builtin_adoption"][NAME]
    assert final["state"] == "adopted" and final["addedName"], "the original flags are kept"


async def test_a_crash_after_the_grant_rolls_back_on_a_refusal(world, monkeypatch):
    await _stage()

    original = manager.adopt_builtin_record

    def killed(*_a, **_k):
        raise _HardKill

    monkeypatch.setattr(manager, "adopt_builtin_record", killed)
    with pytest.raises(_HardKill):
        await _boot()
    monkeypatch.setattr(manager, "adopt_builtin_record", original)
    (world.home / "app_admission.json").write_text(
        json.dumps({"mode": "enforce", "require_signature": True}), encoding="utf-8"
    )
    assert await _boot() == {NAME: "blocked"}
    agent = agent_section(world.home)
    assert NAME not in agent.get("apps_trusted", [])
    assert NAME not in agent.get("apps_builtin_adoption", {})


async def test_a_crash_after_the_swap_finalizes_without_a_second_swap(world, monkeypatch, audit):
    await _stage()
    from kiro_crew.apps import builtin_adoption_state as state_mod

    async def killed(*_a, **_k):
        raise _HardKill

    real_finalize = state_mod.finalize_marker
    monkeypatch.setattr(apply_mod, "finalize_marker", killed)
    with pytest.raises(_HardKill):
        await _boot()
    assert read_adoption_markers()[NAME]["state"] == "granting"
    assert _record().origin == "registry"
    monkeypatch.setattr(apply_mod, "finalize_marker", real_finalize)
    swaps = []
    monkeypatch.setattr(manager, "adopt_builtin_record", lambda *a, **k: swaps.append(a))
    assert await _boot() == {NAME: "finalized"}
    assert swaps == []
    assert read_adoption_markers()[NAME]["state"] == "adopted"


async def test_a_kill_before_the_retired_tree_goes_finalizes_and_is_swept_after(world, monkeypatch):
    """The record is durable, the old tree and the update marker are not yet gone."""
    from kiro_crew.apps import manager_adopt, manager_records

    await _stage()
    real_remove = manager._remove_any_shape

    def killed_at_the_retired_tree(path):
        if "-update-old-" in path.name:
            raise _HardKill
        real_remove(path)

    with monkeypatch.context() as kill:
        kill.setattr(manager, "_remove_any_shape", killed_at_the_retired_tree)
        kill.setattr(manager_records._UpdateMarker, "__exit__", lambda *_a: None)
        with pytest.raises(_HardKill):
            await _boot()
    # This process stands in for the killed one: give its leftovers a pid that is gone.
    root = manager.apps_dir()
    for leftover in [*root.iterdir(), *manager._update_marker_dir(NAME).iterdir()]:
        if f"{os.getpid()}-" in leftover.name:
            leftover.rename(
                leftover.with_name(
                    leftover.name.replace(f"{os.getpid()}-", f"{UNALLOCATABLE_PID}-")
                )
            )
    monkeypatch.setattr(manager_adopt, "pid_exists", lambda pid: pid != UNALLOCATABLE_PID)
    assert [row["name"] for row in manager.list_apps()] == [NAME], "one row, not two"
    assert read_adoption_markers()[NAME]["state"] == "granting"

    assert await _boot() == {NAME: "finalized"}
    assert manager._retired_trees(NAME), "no retired tree is deleted before the listener"
    await manager.sweep_interrupted_updates()
    assert sorted(p.name for p in root.iterdir() if p.name.startswith(f".{NAME}-")) == []
    assert (manager.app_dir(NAME) / "data" / "store.txt").read_text(encoding="utf-8") == "kept"
    assert manager.uninstall_app(NAME).ok
    assert adoption.builtin_released(NAME), "an uninstalled adopted app stays released"
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [_shipped_definition(world)])
    manager.register_builtin_apps()
    assert manager._read_installed(NAME) is None


async def test_an_uninstall_after_a_killed_update_is_released_at_the_next_boot(world, monkeypatch):
    """Only the marker is left, and no record: APPLY's own pass clears it first."""
    from kiro_crew.apps import manager_adopt

    await _stage()
    assert await _boot() == {NAME: "applied"}
    assert manager.uninstall_app(NAME).ok
    manager._update_marker_dir(NAME).mkdir()
    (manager._update_marker_dir(NAME) / f"{UNALLOCATABLE_PID}-ab12cd34").touch()
    monkeypatch.setattr(manager_adopt, "pid_exists", lambda pid: pid != UNALLOCATABLE_PID)
    assert not adoption.builtin_released(NAME), "the marker reads as a swap in flight"

    def listed():
        raise AssertionError("APPLY listed the apps directory")

    monkeypatch.setattr(manager, "swap_leftover_names", listed)
    await _boot()
    assert not manager._update_in_progress(NAME)
    assert adoption.builtin_released(NAME)


def _simulate_kill_mid_swap(world, *, partial: bool) -> None:
    """The state ``update_app`` leaves when killed after moving the old tree aside."""
    dest = manager.app_dir(NAME)
    retired = dest.parent / f"{manager._update_retired_tree_prefix(NAME)}999-deadbeef"
    tmp_data = dest.parent / f".{NAME}-data-tmp"
    (dest / "data").rename(tmp_data)
    (dest / ".app_secret").rename(dest.parent / f".{NAME}-secret-tmp")
    dest.rename(retired)
    manager._update_marker_dir(NAME).mkdir()
    (manager._update_marker_dir(NAME) / "999-deadbeef").touch()
    if partial:
        dest.mkdir()
        (dest / "app.json").write_text("{}", encoding="utf-8")


@pytest.mark.parametrize("partial", [False, True])
async def test_a_kill_mid_swap_is_recovered_then_adopted(world, partial):
    await _stage()
    from kiro_crew.apps.builtin_adoption_pins import adoption_pin
    from kiro_crew.apps.builtin_adoption_state import GrantPlan, write_grant_and_marker

    await write_grant_and_marker(NAME, adoption_pin(NAME), plan=GrantPlan(write=True))
    _simulate_kill_mid_swap(world, partial=partial)
    assert not adoption.builtin_released(NAME), "mid-swap keeps today's behaviour"
    assert await _boot() == {NAME: "applied"}
    dest = manager.app_dir(NAME)
    assert (dest / "data" / "store.txt").read_text(encoding="utf-8") == "kept"
    assert (dest / ".app_secret").read_bytes() == b"s3cret"
    assert not manager._update_marker_dir(NAME).exists()
    assert _record().origin == "registry"


async def test_an_ambiguous_mid_swap_state_is_left_untouched_and_nothing_registers_over_it(
    world, monkeypatch
):
    await _stage()
    from kiro_crew.apps.builtin_adoption_pins import adoption_pin
    from kiro_crew.apps.builtin_adoption_state import GrantPlan, write_grant_and_marker

    await write_grant_and_marker(NAME, adoption_pin(NAME), plan=GrantPlan(write=True))
    _simulate_kill_mid_swap(world, partial=False)
    dest = manager.app_dir(NAME)
    second = dest.parent / f"{manager._update_retired_tree_prefix(NAME)}998-deadbeef"
    second.mkdir()
    (second / "installed.json").write_text("{}", encoding="utf-8")
    assert await _boot() == {}
    assert not dest.exists()
    assert not adoption.builtin_released(NAME)
    assert manager._update_marker_dir(NAME).exists()
    # The boot goes on to register built-ins: a fresh record here would be a reset
    # app, and the next adoption's stale-copy cleanup would delete the moved data.
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [_shipped_definition(world)])
    manager.register_builtin_apps()
    assert not (dest / "installed.json").exists(), "nothing registers over the files set aside"
    tmp_data = dest.parent / f".{NAME}-data-tmp"
    assert await _boot() == {}
    assert (tmp_data / "store.txt").read_text(encoding="utf-8") == "kept"
    assert (dest.parent / f".{NAME}-secret-tmp").read_bytes() == b"s3cret"


async def test_data_in_two_places_is_never_adopted(world):
    """The swap's stale-copy cleanup would delete ``.<name>-data-tmp``: refuse instead."""
    await _stage()
    dest = manager.app_dir(NAME)
    tmp_data = dest.parent / f".{NAME}-data-tmp"
    tmp_data.mkdir()
    (tmp_data / "store.txt").write_text("preserved by a killed swap", encoding="utf-8")
    assert await _boot() == {}
    assert manager.builtin_owns_installed(NAME)
    assert (tmp_data / "store.txt").read_text(encoding="utf-8") == "preserved by a killed swap"
    assert (dest / "data" / "store.txt").read_text(encoding="utf-8") == "kept"


# --------------------------------------------------------------------------- #
# Rows 5, 12 and 16: uninstall sticks, shadowing apps, idempotence
# --------------------------------------------------------------------------- #


async def test_uninstall_after_adoption_sticks_across_a_reboot(world, monkeypatch):
    await _stage()
    assert await _boot() == {NAME: "applied"}
    result = manager.uninstall_app(NAME)
    assert result.ok, result.error
    assert manager._read_installed(NAME) is None
    assert read_adoption_markers()[NAME]["state"] == "adopted"
    from kiro_crew.apps import agent_config_writer

    async def no_trust(_mutate):
        raise AssertionError("no re-grant after uninstall")

    monkeypatch.setattr(agent_config_writer, "mutate_trust_settings", no_trust)
    calls = (len(world.git_calls), len(world.archive_calls))
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [_shipped_definition(world)])
    manager.register_builtin_apps()
    assert manager._read_installed(NAME) is None
    assert not manager.app_dir(NAME).joinpath("installed.json").exists()
    assert await _stage(T0 + timedelta(days=1)) == {NAME: "skipped"}
    assert await _boot() == {}
    assert (len(world.git_calls), len(world.archive_calls)) == calls
    assert adoption.builtin_released(NAME)


def _shipped_definition(world):
    data = json.loads((world.wheel / "app.json").read_text(encoding="utf-8"))
    return data


async def test_a_user_app_shadowing_the_name_is_never_adopted(world):
    record = _record()
    record.source, record.origin, record.lifecycle = "/home/u/my-app", "local", "gateway"
    manager._write_installed(NAME, record)
    assert await _stage() == {NAME: "skipped"}
    assert await _boot() == {}
    assert NAME not in agent_section(world.home).get("apps_trusted", [])
    assert _record().source == "/home/u/my-app"
    assert adoption.adoption_row(NAME, _record()) is None


async def test_apply_twice_swaps_once_and_audits_once(world, audit, monkeypatch):
    await _stage()
    assert await _boot() == {NAME: "applied"}
    assert await _boot() == {}
    assert audit.outcomes().count("applied") == 1
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [_shipped_definition(world)])
    stamped = (manager.app_dir(NAME) / "installed.json").read_bytes()
    manager.register_builtin_apps()
    assert (manager.app_dir(NAME) / "installed.json").read_bytes() == stamped


async def test_apply_scans_no_app_config_keys_and_registration_publishes_them(world, monkeypatch):
    from kiro_crew.apps import manager_records

    scans: list[object] = []
    real_scan = manager_records._scan_installed_app_config_keys

    def counting_scan(root):
        scans.append(root)
        return real_scan(root)

    monkeypatch.setattr(manager_records, "_scan_installed_app_config_keys", counting_scan)
    await _stage()
    scans.clear()
    assert await _boot() == {NAME: "applied"}
    assert scans == [], "APPLY is on the boot path: registration publishes the keys"
    monkeypatch.setattr(manager, "discover_builtin_apps", lambda: [_shipped_definition(world)])
    manager.register_builtin_apps()
    assert len(scans) == 1


async def test_nothing_happens_with_an_empty_pin_table(tmp_path, monkeypatch, audit):
    from builtin_adoption_helpers import install_pins

    install_pins(monkeypatch, tmp_path / "empty.json", [])
    write_builtin_record()
    assert await _boot() == {}
    assert await adoption.run_staging_pass(now=lambda: T0) == {}
    assert audit.rows == []
    assert manager.builtin_owns_installed(NAME)


# --------------------------------------------------------------------------- #
# Row 24: audit shape
# --------------------------------------------------------------------------- #


async def test_every_audit_row_has_the_exact_shape_and_no_url(world, audit):
    await _stage()
    await _boot()
    rows = [r for r in audit.rows if r.get("operation") == "app_builtin_adoption"]
    assert rows
    for row in rows:
        assert set(row) == {"caller", "operation", "outcome", "resources"}
        assert row["caller"] == "app_adoption"
        assert row["resources"].startswith(f"name={NAME!r} commit={world.commit[:12]} path=")
        assert "://" not in row["resources"] and "github" not in row["resources"]
    assert [r["resources"].split(" path=")[1] for r in rows] == ["git code=-", "git code=-"]


async def test_the_staging_loop_sleeps_until_the_next_attempt_then_exits(world):
    import urllib.error

    world.git_outcome = "network"
    world.archive_error = urllib.error.URLError(OSError("offline"))
    clock = [T0]
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] = clock[0] + timedelta(seconds=seconds)
        if len(sleeps) == 2:
            world.git_outcome = ""
            world.archive_error = None

    await adoption.stage_builtin_adoptions(now=lambda: clock[0], sleep=fake_sleep)
    assert sleeps == [300.0, 600.0]
    assert read_ledger(NAME).state == "staged"


async def test_a_pass_that_raises_waits_the_base_backoff_not_a_second(world, monkeypatch):
    """A raising pass advances no ledger, so its past-due retry must not spin."""
    import urllib.error

    world.git_outcome = "network"
    world.archive_error = urllib.error.URLError(OSError("offline"))
    await _stage(T0)
    clock = [T0 + timedelta(hours=1)]
    sleeps: list[float] = []

    real_stage_pin = apply_mod.stage_pin

    async def disk_error_twice(*args, **kwargs):
        if len(sleeps) < 2:
            raise OSError("EIO while copying the checkout")
        return await real_stage_pin(*args, **kwargs)

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] = clock[0] + timedelta(seconds=seconds)

    monkeypatch.setattr(apply_mod, "stage_pin", disk_error_twice)
    world.git_outcome = ""
    world.archive_error = None
    await adoption.stage_builtin_adoptions(now=lambda: clock[0], sleep=fake_sleep)
    assert sleeps == [300.0, 300.0]
    assert read_ledger(NAME).state == "staged"


async def test_a_first_pass_that_raises_with_no_ledger_still_retries(world, monkeypatch):
    """No ledger is written when the very first pass raises, so the loop must not exit."""
    assert read_ledger(NAME) is None
    clock = [T0]
    sleeps: list[float] = []
    real_stage_pin = apply_mod.stage_pin

    async def disk_error_once(*args, **kwargs):
        if not sleeps:
            raise OSError("ENOSPC while creating the verify dir")
        return await real_stage_pin(*args, **kwargs)

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        clock[0] = clock[0] + timedelta(seconds=seconds)

    monkeypatch.setattr(apply_mod, "stage_pin", disk_error_once)
    await adoption.stage_builtin_adoptions(now=lambda: clock[0], sleep=fake_sleep)
    assert sleeps == [300.0]
    assert read_ledger(NAME).state == "staged"
