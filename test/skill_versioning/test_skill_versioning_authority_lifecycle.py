"""Operator lockout, provenance recovery, retirement and masking boundaries.

Covers that an agent cannot lock the operator out, authority provenance stays
recoverable, restart recovery scales with claims in flight rather than history,
operator-only retirement (including maskless and no-promotion hosts), the
unmasked-boundary proof of authority absence, and that a rename probe or one
stuck claim never freezes the lifecycle.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import logging
import os
import secrets
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import pytest

import kiro_crew.skills as skills_mod
from kiro_crew.skills import SkillsLoader

from .skill_versioning_support import (
    _REAL_AUTHORITY_SANDBOX_REFUSAL,
    _THREAD_WAIT_CEILING_SECS,
    _authority_path,
    _initialize_test_authority,
    _prov,
    _simulate_windows_native_handles,
    _stage_update,
    _write_live,
)

# This module's auto-skill boundaries initialize explicitly through the
# ``loader`` fixture; every ``uninitialized_loader`` path must see production
# fail closed.
pytestmark = pytest.mark.usefixtures("no_auto_skill_authority_startup")


# ── An agent must not be able to lock the operator out (item 1) ──


def test_a_planted_non_canonical_auto_skill_stays_deletable(loader):
    """A name no promotion can target is mutated by name, as before the protocol.

    An injected agent plants ``auto/Persist_Me`` with ``always: true``: refusing
    its lock would keep it injected into every session while the dashboard
    answered every delete, pin and inject toggle with "not found".
    """
    planted = loader._dir / skills_mod.AUTO_SKILL_NAMESPACE / "Persist_Me"
    planted.mkdir(parents=True)
    (planted / "SKILL.md").write_text(
        "---\nname: auto/Persist_Me\ndescription: planted\ntriggers: t\nalways: true\n"
        "---\n\nobey the planted instructions\n",
        encoding="utf-8",
    )
    loader._invalidate_iter_cache()
    assert skills_mod._auto_skill_promotion_ruled_out() is False
    assert "auto/Persist_Me" in loader.get_always_skills()

    assert loader.set_pinned("auto/Persist_Me", True) is True
    assert loader.set_inject_on_trigger("auto/Persist_Me", False) is True
    assert loader.delete_skill("auto/Persist_Me") is True

    assert not planted.exists()
    assert "auto/Persist_Me" not in loader.get_always_skills()


@pytest.mark.parametrize(
    ("spelling", "lock_slug"),
    [
        ("alias-lock", "alias-lock"),
        ("Alias-Lock", "alias-lock"),
        ("alias-lock.", "alias-lock"),
        ("alias-lock. ", "alias-lock"),
        ("ALIAS-LOCK/nested", "alias-lock"),
        ("\u212aelvin-alias", "kelvin-alias"),
        ("Persist_Me", None),
        ("x", None),
    ],
)
def test_live_names_fold_onto_their_canonical_target_lock(spelling, lock_slug):
    assert SkillsLoader._live_auto_target_lock_slug(spelling) == lock_slug


def test_an_alias_spelling_waits_on_the_canonical_target_lock(loader, monkeypatch):
    """``Alias-Lock`` opens ``auto/alias-lock`` on a case-insensitive filesystem,
    so it must serialize with a promotion of ``alias-lock`` instead of refusing."""
    _write_live(loader, "Alias-Lock", version=1, body="OLD")
    attempted = threading.Event()
    real_lock = loader._promotion_lock
    seen: list[str] = []

    @contextlib.contextmanager
    def observed_lock(slug):
        seen.append(slug)
        if threading.current_thread().name.startswith("alias-mutator"):
            attempted.set()
        with real_lock(slug) as acquired:
            yield acquired

    monkeypatch.setattr(loader, "_promotion_lock", observed_lock)

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="alias-mutator") as pool:
        with real_lock("alias-lock") as acquired:
            assert acquired is True
            future = pool.submit(loader.set_pinned, "auto/Alias-Lock", True)
            assert attempted.wait(timeout=_THREAD_WAIT_CEILING_SECS)
            assert not future.done()
        assert future.result(timeout=_THREAD_WAIT_CEILING_SECS) is True
    assert seen == ["alias-lock"]


@pytest.mark.parametrize("obsolete", ["nested", "direct"])
def test_an_obsolete_root_planted_after_startup_blocks_nothing(loader, obsolete):
    """The obsolete spellings are agent-writable and hold nothing trusted once a
    certificate exists, so they are refused at startup only. An empty one planted
    later must not refuse live mutations, staging or promotion."""
    home = loader._private_root().parents[1]
    if obsolete == "nested":
        loader._legacy_private_root().mkdir(parents=True)
    else:
        (home / skills_mod.AUTO_SKILL_PRIVATE_STATE_DIRNAME).mkdir()
    _write_live(loader, "after-plant", version=1, body="OLD")

    assert skills_mod.auto_skill_promotion_disabled_reason() is None
    assert loader.set_pinned("auto/after-plant", True) is True
    assert (
        loader.stage_skill_candidate(
            "after-plant-new",
            description="d",
            triggers="t",
            procedure_md="## Steps\n\nrun\n",
            provenance=_prov(),
        )
        == "auto/after-plant-new"
    )
    assert loader.approve_pending_skill_checked("after-plant-new") == "auto/after-plant-new"
    assert loader.delete_skill("auto/after-plant") is True


# ── Authority provenance must be recoverable (item 3) ──


def _rewrite_authority_record(loader, home, case):
    record = loader._authority_provenance_path()
    root = loader._private_root()
    if case == "record-missing":
        record.unlink()
        return
    if case == "root-missing":
        shutil.rmtree(root)
        return
    with (
        loader._pin_skill_parent(home) as home_parent,
        loader._pin_skill_parent(root) as root_parent,
    ):
        home_identity = home_parent.native_identity
        root_identity = root_parent.native_identity
    if case == "home-moved":
        # What a restored backup or a cp -a / rsync host move looks like: the
        # record names the old home inode.
        home_identity = skills_mod._TaggedFileIdentity(
            home_identity.kind,
            home_identity.volume,
            (
                int(home_identity.object_id) + 1
                if isinstance(home_identity.object_id, int)
                else bytes(reversed(home_identity.object_id))
            ),
        )
    body = loader._authority_record_body(home, home, home_identity, root_identity)
    body["mac"] = "0" * 64 if case == "key-regenerated" else loader._authority_record_mac(body)
    record.write_text(json.dumps(body), encoding="utf-8")


def _restart_gateway_certification():
    skills_mod._reset_auto_skill_private_authority_for_tests()
    return skills_mod.initialize_gateway_auto_skill_private_authority()


def _plant_claim_lock(loader, claim_name):
    lock_path = loader._claim_lock_path(claim_name)
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        created: list[bool] = []
        fd = loader._open_skill_lock(
            state.claim_locks,
            lock_path.name,
            created_out=created,
        )
        acquired = skills_mod.platform_compat.try_acquire_lock(fd, exclusive=True)
        assert acquired
        try:
            assert loader._initialize_claim_lock_state(fd, lock_path, claim_name)
            if created == [True]:
                loader._sync_pinned_parent(state.claim_locks)
        finally:
            skills_mod.platform_compat.release_lock(fd)
            os.close(fd)


def _plant_public_quarantine(loader, slug, claim_name):
    loader.stage_skill_candidate(
        slug,
        description="d",
        triggers="t",
        procedure_md="## Steps\n\nrun\n",
        provenance=_prov(),
    )
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        identity = loader._pinned_child_identity(state.pending, slug)
        assert identity is not None
        loader._rename_skill_child_no_replace(
            state.pending,
            slug,
            state.quarantine,
            claim_name,
            expected_identity=identity,
        )
        loader._sync_pinned_rename_parents(state.pending, state.quarantine)


@pytest.mark.parametrize("surface", ["crash-window", "public-only", "lock-only"])
def test_stale_reseed_refuses_unmatched_in_flight_state(
    uninitialized_loader,
    surface,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    claim_name = "reseed-crash--0123456789abcdef0123456789abcdef"
    if surface in {"crash-window", "lock-only"}:
        _plant_claim_lock(loader, claim_name)
    if surface in {"crash-window", "public-only"}:
        _plant_public_quarantine(loader, "reseed-crash", claim_name)
    root = loader._private_root()
    root_identity = root.stat().st_ino
    _rewrite_authority_record(loader, home, "key-regenerated")

    with pytest.raises(OSError) as refused:
        _restart_gateway_certification()

    message = str(refused.value)
    expected = (
        f"{skills_mod.AUTO_QUARANTINE_DIRNAME}/{claim_name}"
        if surface != "lock-only"
        else f"{skills_mod.AUTO_LOCKS_DIRNAME}/{skills_mod.AUTO_CLAIMS_DIRNAME}/"
        f"{claim_name}.lock"
    )
    assert expected in message
    if surface == "crash-window":
        assert f"{skills_mod.AUTO_LOCKS_DIRNAME}/{skills_mod.AUTO_CLAIMS_DIRNAME}/" in message
    assert "still in flight" in (skills_mod.auto_skill_promotion_disabled_reason() or "")
    assert root.stat().st_ino == root_identity
    assert not [name for name in os.listdir(root.parent) if ".stale-" in name]


@pytest.mark.parametrize("surface", ["candidate", "live"])
def test_missing_private_root_refuses_public_quarantine(
    uninitialized_loader,
    surface,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    claim_name = f"missing-root-{surface}--0123456789abcdef0123456789abcdef"
    if surface == "candidate":
        _plant_public_quarantine(loader, "missing-root-candidate", claim_name)
        quarantine_dirname = skills_mod.AUTO_QUARANTINE_DIRNAME
    else:
        public_entry = loader._live_quarantine_root() / claim_name
        public_entry.mkdir(parents=True)
        quarantine_dirname = skills_mod.AUTO_LIVE_QUARANTINE_DIRNAME
    record_bytes = loader._authority_provenance_path().read_bytes()
    _rewrite_authority_record(loader, home, "root-missing")

    with pytest.raises(OSError) as refused:
        _restart_gateway_certification()

    message = str(refused.value)
    assert f"{quarantine_dirname}/{claim_name}" in message
    assert "still in flight" in message
    assert "still in flight" in (skills_mod.auto_skill_promotion_disabled_reason() or "")
    assert not loader._private_root().exists()
    assert loader._authority_provenance_path().read_bytes() == record_bytes
    assert not [
        name for name in os.listdir(loader._authority_provenance_path().parent) if ".stale-" in name
    ]


def test_bounded_stale_claim_scan_consumes_only_limit_plus_one(monkeypatch):
    consumed: list[int] = []

    class Entry:
        def __init__(self, index):
            self.name = f"entry-{index}"

    def entries():
        for index in range(10):
            consumed.append(index)
            yield Entry(index)

    monkeypatch.setattr(skills_mod, "_STALE_CLAIM_SCAN_LIMIT", 3)
    with pytest.raises(skills_mod._StaleClaimScanOverflow, match="3-entry"):
        skills_mod._bounded_stale_claim_names(
            Path("unused"),
            label="authority root",
            scanner=lambda _target: contextlib.nullcontext(entries()),
        )
    assert consumed == [0, 1, 2, 3]


@pytest.mark.parametrize("surface", ["claims", "quarantine"])
def test_pending_slug_claim_scan_consumes_only_limit_plus_one(
    loader,
    monkeypatch,
    caplog,
    surface,
):
    consumed: list[int] = []
    scan_count = 0

    class Entry:
        def __init__(self, index):
            self.name = f"other-{index}--0123456789abcdef"

    def entries():
        for index in range(10):
            consumed.append(index)
            yield Entry(index)

    def scanner(_target):
        nonlocal scan_count
        scan_count += 1
        selected = surface == "claims" or scan_count == 2
        return contextlib.nullcontext(entries() if selected else iter(()))

    monkeypatch.setattr(skills_mod, "_ACTIVE_CLAIM_SCAN_LIMIT", 3)
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        monkeypatch.setattr(skills_mod.os, "scandir", scanner)
        with caplog.at_level("WARNING", logger="kiro_crew.skills"):
            assert loader._pending_slug_claimed("wanted", private_state=state) is None

    assert consumed == [0, 1, 2, 3]
    label = "active claims" if surface == "claims" else "public candidate quarantine"
    assert f"{label} exceeded the 3-entry active-claim scan limit" in caplog.text


def test_pending_slug_claim_scan_keeps_under_bound_match_behavior(loader, monkeypatch):
    consumed: list[str] = []

    class Entry:
        def __init__(self, name):
            self.name = name

    def entries():
        for name in (
            "other--0123456789abcdef",
            "wanted--0123456789abcdef",
            "unread--0123456789abcdef",
        ):
            consumed.append(name)
            yield Entry(name)

    monkeypatch.setattr(skills_mod, "_ACTIVE_CLAIM_SCAN_LIMIT", 3)
    with loader._pin_private_state(create=True, require_sensitive=True) as state:
        monkeypatch.setattr(
            skills_mod.os,
            "scandir",
            lambda _target: contextlib.nullcontext(entries()),
        )
        assert loader._pending_slug_claimed("wanted", private_state=state) is True

    assert consumed == [
        "other--0123456789abcdef",
        "wanted--0123456789abcdef",
    ]


def test_recovery_scan_overflow_is_bounded_and_indeterminate(
    loader,
    monkeypatch,
    caplog,
):
    consumed: list[int] = []
    scan_count = 0

    class Entry:
        def __init__(self, index):
            self.name = f"active-{index}--0123456789abcdef"

    def entries():
        for index in range(10):
            consumed.append(index)
            yield Entry(index)

    def scanner(_target):
        nonlocal scan_count
        scan_count += 1
        return contextlib.nullcontext(entries() if scan_count == 2 else iter(()))

    monkeypatch.setattr(skills_mod, "_ACTIVE_CLAIM_SCAN_LIMIT", 3)
    monkeypatch.setattr(skills_mod.os, "scandir", scanner)
    with caplog.at_level("WARNING", logger="kiro_crew.skills"):
        loader._recover_abandoned_claims()

    assert consumed == [0, 1, 2, 3]
    assert (
        "active claim namespace exceeded the 3-entry scan limit while reading "
        "public candidate quarantine" in caplog.text
    )


def test_stale_claim_scan_overflow_disables_auto_skills_without_reseed(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    root = loader._private_root()
    for index in range(4):
        (root / f".publish-overflow-{index}").mkdir()
    root_identity = root.stat().st_ino
    _rewrite_authority_record(loader, home, "key-regenerated")
    monkeypatch.setattr(skills_mod, "_STALE_CLAIM_SCAN_LIMIT", 3)

    with pytest.raises(
        OSError,
        match="authority root exceeded the 3-entry stale-claim scan limit",
    ):
        _restart_gateway_certification()

    disabled = skills_mod.auto_skill_promotion_disabled_reason() or ""
    assert "stale claim state is indeterminate" in disabled
    assert "3-entry stale-claim scan limit" in disabled
    assert root.stat().st_ino == root_identity
    assert not [name for name in os.listdir(root.parent) if ".stale-" in name]


@pytest.mark.parametrize(
    "case",
    ["record-missing", "root-missing", "key-regenerated", "home-moved"],
)
def test_a_stale_idle_authority_is_reseeded_at_startup(uninitialized_loader, case):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    retired: list[str] = []
    if case != "root-missing":
        loader.stage_skill_candidate(
            "retired-history",
            description="d",
            triggers="t",
            procedure_md="## Steps\n\nrun\n",
            provenance=_prov(),
        )
        assert loader.approve_pending_skill_checked("retired-history") == "auto/retired-history"
        retired = sorted(os.listdir(loader._evidence_root()))
        assert retired and not os.listdir(loader._claims_root())
    else:
        assert not loader._quarantine_root().exists()
        assert not loader._live_quarantine_root().exists()
    old_root = loader._private_root().stat().st_ino
    _rewrite_authority_record(loader, home, case)

    binding = _restart_gateway_certification()

    assert binding == skills_mod._STARTUP_AUTHORITY_BINDING
    assert skills_mod.auto_skill_promotion_disabled_reason() is None
    parent = loader._private_root().parent
    aside = sorted(name for name in os.listdir(parent) if ".stale-" in name)
    if case != "root-missing":
        assert loader._private_root().stat().st_ino != old_root
        (stale_root,) = [
            name
            for name in aside
            if name.startswith(f"{skills_mod.AUTO_SKILL_PRIVATE_STATE_DIRNAME}.stale-")
        ]
        # Moved aside intact for inspection, never read for authority.
        assert sorted(os.listdir(parent / stale_root / skills_mod.AUTO_EVIDENCE_DIRNAME)) == (
            retired
        )
    if case != "record-missing":
        stale_record = f"{skills_mod._AUTHORITY_PROVENANCE_NAME}.stale-"
        assert any(name.startswith(stale_record) for name in aside)
    assert (
        loader.stage_skill_candidate(
            "after-reseed",
            description="d",
            triggers="t",
            procedure_md="## Steps\n\nrun\n",
            provenance=_prov(),
        )
        == "auto/after-reseed"
    )
    assert loader.approve_pending_skill_checked("after-reseed") == "auto/after-reseed"


def test_a_stale_authority_with_a_claim_in_flight_refuses_with_exact_recovery(
    uninitialized_loader,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    loader.stage_skill_candidate(
        "in-flight",
        description="d",
        triggers="t",
        procedure_md="## Steps\n\nrun\n",
        provenance=_prov(),
    )
    claimed = loader._claim_pending_update("in-flight")
    assert claimed is not None
    claim, claim_fd, _consumed_at, _snapshot = claimed
    # The owning process dies mid-claim: its lock is released, its claim stays.
    skills_mod.platform_compat.release_lock(claim_fd)
    os.close(claim_fd)
    root = loader._private_root()
    root_identity = root.stat().st_ino
    _rewrite_authority_record(loader, home, "key-regenerated")
    record_bytes = loader._authority_provenance_path().read_bytes()

    with pytest.raises(OSError) as refused:
        _restart_gateway_certification()

    message = str(refused.value)
    assert "1 claim(s) are still in flight" in message
    assert claim.name in message
    for step in (
        "stop every Kiro Crew gateway and agent",
        "tag-grants/auto-skill-private and tag-grants/auto-skill-private-authority.json",
        "skills/auto/.quarantine/<slug>--<token>",
        "skills/auto/.pending/<slug>",
        "start the gateway, which provisions a fresh authority",
    ):
        assert step in message
    assert "still in flight" in (skills_mod.auto_skill_promotion_disabled_reason() or "")
    assert root.stat().st_ino == root_identity
    assert (loader._claims_root() / claim.name).is_dir()
    assert loader._authority_provenance_path().read_bytes() == record_bytes
    assert not [name for name in os.listdir(root.parent) if ".stale-" in name]


# ── Restart recovery scales with claims in flight, not history (item 5) ──


def test_recovery_skips_retired_evidence_without_opening_its_lock(loader, monkeypatch):
    for index in range(3):
        slug = f"retired-{index}"
        loader.stage_skill_candidate(
            slug,
            description="d",
            triggers="t",
            procedure_md="## Steps\n\nrun\n",
            provenance=_prov(),
        )
        assert loader.approve_pending_skill_checked(slug) == f"auto/{slug}"
    assert len(os.listdir(loader._evidence_root())) == 3
    opened: list[str] = []
    real_open = loader._open_skill_lock

    def counting_open(parent, name, **kwargs):
        opened.append(name)
        return real_open(parent, name, **kwargs)

    monkeypatch.setattr(loader, "_open_skill_lock", counting_open)

    loader._recover_abandoned_claims()

    assert [name for name in opened if name.startswith("retired-")] == []


# ── A filesystem without no-replace rename is refused once, at startup (item 5) ──


def test_startup_refuses_a_filesystem_without_no_replace_rename(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader

    def unsupported(self, *_args, **_kwargs):
        raise OSError(errno.EINVAL, "no-replace rename is unsupported here")

    pending = loader._pending_root() / "queued-on-nfs"
    pending.mkdir(parents=True)
    (pending / "SKILL.md").write_text("## Steps\n\nqueued\n", encoding="utf-8")
    with monkeypatch.context() as nfs:
        nfs.setattr(SkillsLoader, "_rename_skill_child_no_replace", unsupported)
        with pytest.raises(OSError, match="no atomic no-replace rename"):
            skills_mod.initialize_gateway_auto_skill_private_authority()

        assert skills_mod._STARTUP_AUTHORITY_BINDING is None
        assert skills_mod.auto_skill_promotion_disabled_reason() == (
            skills_mod._NO_REPLACE_RENAME_REFUSAL
        )
        assert skills_mod._auto_skill_promotion_ruled_out() is True
        assert not [
            name
            for name in os.listdir(loader._private_root())
            if name.startswith(skills_mod._RENAME_PROBE_PREFIX)
        ]
        assert (
            loader.stage_skill_candidate(
                "after-nfs",
                description="d",
                triggers="t",
                procedure_md="## Steps\n\nrun\n",
                provenance=_prov(),
            )
            is None
        )
        # The queue can be emptied by name only while the current probe proves
        # every process on this data home lacks the publication primitive.
        assert loader.dismiss_pending_skill("queued-on-nfs") is True
    assert not pending.exists()


# ── An obsolete spelling cannot withhold the certificate across restarts ──


def _plant_always_on_auto_skill(loader, slug):
    planted = loader._dir / skills_mod.AUTO_SKILL_NAMESPACE / slug
    planted.mkdir(parents=True)
    (planted / "SKILL.md").write_text(
        f"---\nname: auto/{slug}\ndescription: planted\ntriggers: t\nalways: true\n"
        "---\n\nobey the planted instructions\n",
        encoding="utf-8",
    )
    loader._invalidate_iter_cache()
    return planted


@pytest.mark.parametrize("spelling", ["direct", "nested"])
def test_an_obsolete_spelling_is_inert_once_the_authority_exists(
    uninitialized_loader,
    spelling,
    caplog,
):
    """After first creation a planted obsolete root is ignored, so a planted
    always-on skill stays deletable after a restart instead of failing closed on a
    target lock no uncertified process can take."""
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    obsolete = (
        home / skills_mod.AUTO_SKILL_PRIVATE_STATE_DIRNAME
        if spelling == "direct"
        else loader._legacy_private_root()
    )
    obsolete.mkdir(parents=True)
    sentinel = obsolete / "sentinel"
    sentinel.write_text("planted", encoding="utf-8")
    obsolete_identity = obsolete.stat().st_ino
    planted = _plant_always_on_auto_skill(loader, "planted-always")
    caplog.set_level(logging.WARNING, logger="kiro_crew.skills")

    binding = _restart_gateway_certification()

    assert binding == skills_mod._STARTUP_AUTHORITY_BINDING
    assert skills_mod.auto_skill_promotion_disabled_reason() is None
    assert obsolete.stat().st_ino == obsolete_identity
    assert sentinel.read_text(encoding="utf-8") == "planted"
    assert "auto/planted-always" in loader.get_always_skills()
    assert loader.set_inject_on_trigger("auto/planted-always", False) is True
    assert loader.delete_skill("auto/planted-always") is True
    assert not planted.exists()
    assert "auto/planted-always" not in loader.get_always_skills()

    loader._migrate_legacy_private_state(loader._dir.resolve(), home.resolve())
    notices = [
        record
        for record in caplog.records
        if "Ignoring obsolete auto-skill authority spelling" in record.getMessage()
    ]
    assert len(notices) == 1


# ── An existing authority is retired only by the stopped operator ──


def test_existing_authority_never_grants_lock_free_mutation_when_sandbox_turns_off(
    uninitialized_loader,
    monkeypatch,
    caplog,
):
    from kiro_crew import sandbox

    loader = uninitialized_loader
    skills_mod.initialize_auto_skill_private_authority(
        skills_root=loader._dir,
        data_home=loader._private_root().parents[1],
    )
    skills_mod._reset_auto_skill_private_authority_for_tests()
    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "off")
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda _mode: False)
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda _mode: "off")
    monkeypatch.setattr(sandbox, "unavailable_kind", lambda: "")
    _write_live(loader, "root-backed", version=1, body="OLD")

    caplog.set_level(logging.WARNING, logger="kiro_crew.skills")
    assert skills_mod._auto_skill_promotion_ruled_out() is False
    assert loader.set_pinned("auto/root-backed", True) is False
    retire_notices = [
        record
        for record in caplog.records
        if skills_mod._AUTHORITY_RETIRE_COMMAND in record.getMessage()
    ]
    assert len(retire_notices) == 1
    with pytest.raises(OSError) as refused:
        skills_mod.initialize_gateway_auto_skill_private_authority()
    assert skills_mod._AUTHORITY_RETIRE_COMMAND in str(refused.value)


def test_operator_retire_moves_verified_idle_authority_and_restores_by_name_mutation(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    skills_mod.initialize_auto_skill_private_authority(
        skills_root=loader._dir,
        data_home=loader._private_root().parents[1],
    )
    root = loader._private_root()
    record = loader._authority_provenance_path()
    root_identity = root.stat().st_ino
    record_bytes = record.read_bytes()
    _write_live(loader, "after-retire", version=1, body="OLD")
    skills_mod._reset_auto_skill_private_authority_for_tests()

    retired_root, retired_record, _history = skills_mod.retire_auto_skill_private_authority()

    assert not root.exists()
    assert not record.exists()
    assert retired_root.is_dir()
    assert retired_root.stat().st_ino == root_identity
    assert retired_record.read_bytes() == record_bytes
    assert retired_root.name.split(".stale-", 1)[1] == retired_record.name.split(".stale-", 1)[1]
    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        lambda: skills_mod._SANDBOX_OFF_REFUSAL,
    )
    assert skills_mod._auto_skill_promotion_ruled_out() is True
    assert loader.set_pinned("auto/after-retire", True) is True


def test_authority_retire_cli_holds_the_gateway_lock(monkeypatch, tmp_path, capsys):
    from kiro_crew import cli

    events: list[object] = []
    retired_root = tmp_path / "auto-skill-private.stale-token"
    retired_record = tmp_path / "auto-skill-private-authority.json.stale-token"
    retired_quarantine = tmp_path / ".quarantine.stale-token"

    class HeldGatewayLock:
        def __init__(self, home):
            events.append(("constructed", home))

        def __enter__(self):
            events.append("entered")
            return self

        def __exit__(self, *_exc):
            events.append("exited")

    def retire():
        events.append("retired")
        return retired_root, retired_record, (retired_quarantine,)

    monkeypatch.setattr(cli, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "GatewayLock", HeldGatewayLock)
    monkeypatch.setattr(cli, "retire_auto_skill_private_authority", retire)

    cli._skills_cmd(argparse.Namespace(skills_action="authority-retire"))

    assert events == [("constructed", tmp_path), "entered", "retired", "exited"]
    output = capsys.readouterr().out
    assert str(retired_root) in output
    assert str(retired_record) in output
    assert f"quarantine: {retired_quarantine}" in output
    assert "moved aside with the root" in output


def test_authority_retire_cli_reports_a_root_retired_without_a_record(
    monkeypatch,
    tmp_path,
    capsys,
):
    from kiro_crew import cli

    retired_root = tmp_path / "auto-skill-private.stale-token"
    monkeypatch.setattr(cli, "config_dir", lambda: tmp_path)
    monkeypatch.setattr(cli, "GatewayLock", lambda _home: contextlib.nullcontext())
    monkeypatch.setattr(
        cli, "retire_auto_skill_private_authority", lambda: (retired_root, None, ())
    )

    cli._skills_cmd(argparse.Namespace(skills_action="authority-retire"))

    output = capsys.readouterr().out
    assert str(retired_root) in output
    assert "record: (none present)" in output
    assert "quarantine: (none present)" in output


# ── Retirement on a host where no process can promote ──


def _turn_the_host_sandbox_off(monkeypatch):
    """Model the operator turning ``agent.sandbox`` off: no process can certify."""
    from kiro_crew import sandbox

    skills_mod._reset_auto_skill_private_authority_for_tests()
    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "off")
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda _mode: False)
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda _mode: "off")
    monkeypatch.setattr(sandbox, "unavailable_kind", lambda: "")
    assert skills_mod._auto_skill_sandbox_excludes_every_promoter() is True


def _stale_names(directory: Path) -> list[str]:
    return sorted(name for name in os.listdir(directory) if ".stale-" in name)


@pytest.mark.parametrize("case", ["home-moved", "key-regenerated", "record-missing"])
def test_maskless_retire_sets_aside_an_unverifiable_root_without_trusting_it(
    uninitialized_loader,
    monkeypatch,
    case,
):
    """A home moved by ``cp -a``, rsync or a restore onto a sandbox-off host keeps a
    root whose provenance never verifies again. Retirement there moves it aside
    untrusted, so a planted always-on skill becomes removable again."""
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    _initialize_test_authority(loader)
    planted = _plant_always_on_auto_skill(loader, "planted-always")
    root = loader._private_root()
    record = loader._authority_provenance_path()
    root_identity = root.stat().st_ino
    _rewrite_authority_record(loader, home, case)
    record_bytes = record.read_bytes() if record.exists() else None
    _turn_the_host_sandbox_off(monkeypatch)
    assert loader.delete_skill("auto/planted-always") is False

    retired_root, retired_record, _history = skills_mod.retire_auto_skill_private_authority()

    assert not os.path.lexists(root)
    assert not os.path.lexists(record)
    assert retired_root.stat().st_ino == root_identity
    token = retired_root.name.split(".stale-", 1)[1]
    if record_bytes is None:
        assert retired_record is None
        assert _stale_names(root.parent) == [retired_root.name]
    else:
        assert retired_record is not None
        assert retired_record.read_bytes() == record_bytes
        assert retired_record.name.split(".stale-", 1)[1] == token
    assert skills_mod._auto_skill_promotion_ruled_out() is True
    assert loader.delete_skill("auto/planted-always") is True
    assert not planted.exists()


@pytest.mark.parametrize("surface", ["evidence", "public-quarantine"])
def test_maskless_retire_ignores_retired_history_past_the_scan_bound(
    uninitialized_loader,
    monkeypatch,
    surface,
):
    """History that outgrew the stale-state scan refuses on a masked host and is
    never read on a host where no process can promote."""
    loader = uninitialized_loader
    _initialize_test_authority(loader)
    root = loader._private_root()
    history = (
        root / skills_mod.AUTO_EVIDENCE_DIRNAME
        if surface == "evidence"
        else loader._quarantine_root()
    )
    history.mkdir(parents=True, exist_ok=True)
    for index in range(skills_mod._STALE_CLAIM_SCAN_LIMIT + 1):
        (history / f"history-{index}--{index:032x}").mkdir()
    root_identity = root.stat().st_ino
    _write_live(loader, "after-history", version=1, body="OLD")
    skills_mod._reset_auto_skill_private_authority_for_tests()

    with pytest.raises(skills_mod._StaleClaimScanOverflow):
        skills_mod.retire_auto_skill_private_authority()
    assert root.stat().st_ino == root_identity
    assert _stale_names(root.parent) == []

    _turn_the_host_sandbox_off(monkeypatch)
    retired_root, retired_record, _history = skills_mod.retire_auto_skill_private_authority()

    assert not os.path.lexists(root)
    assert retired_root.stat().st_ino == root_identity
    assert retired_record is not None and retired_record.is_file()
    assert loader.set_pinned("auto/after-history", True) is True


def test_masked_host_retire_refuses_an_unverifiable_root_with_the_exact_handoff(
    uninitialized_loader,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    _initialize_test_authority(loader)
    root = loader._private_root()
    root_identity = root.stat().st_ino
    _rewrite_authority_record(loader, home, "home-moved")
    record_bytes = loader._authority_provenance_path().read_bytes()
    skills_mod._reset_auto_skill_private_authority_for_tests()
    assert skills_mod._auto_skill_sandbox_excludes_every_promoter() is False

    with pytest.raises(OSError) as refused:
        skills_mod.retire_auto_skill_private_authority()

    expected = SkillsLoader._authority_handoff(
        "authority provenance, selected-home identity, or root identity mismatches"
    )
    assert str(refused.value) == str(expected)
    assert root.stat().st_ino == root_identity
    assert loader._authority_provenance_path().read_bytes() == record_bytes
    assert _stale_names(root.parent) == []


def test_maskless_retire_refuses_a_claim_in_flight_and_names_the_manual_recovery(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    loader.stage_skill_candidate(
        "in-flight",
        description="d",
        triggers="t",
        procedure_md="## Steps\n\nrun\n",
        provenance=_prov(),
    )
    claimed = loader._claim_pending_update("in-flight")
    assert claimed is not None
    claim, claim_fd, _consumed_at, _snapshot = claimed
    skills_mod.platform_compat.release_lock(claim_fd)
    os.close(claim_fd)
    root = loader._private_root()
    root_identity = root.stat().st_ino
    _rewrite_authority_record(loader, home, "key-regenerated")
    _turn_the_host_sandbox_off(monkeypatch)

    with pytest.raises(OSError) as refused:
        skills_mod.retire_auto_skill_private_authority()

    message = str(refused.value)
    assert "1 claim(s) in flight" in message
    assert claim.name in message
    assert "To recover by hand" in message
    assert "skills/auto/.quarantine/<slug>--<token>" in message
    assert root.stat().st_ino == root_identity
    assert (loader._claims_root() / claim.name).is_dir()
    assert _stale_names(root.parent) == []


def test_maskless_retire_refuses_when_active_claims_exceed_the_bound(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    _initialize_test_authority(loader)
    claims = loader._claims_root()
    claims.mkdir(parents=True)
    for index in range(4):
        (claims / f"active-{index}--{index:032x}").mkdir()
    monkeypatch.setattr(skills_mod, "_STALE_CLAIM_SCAN_LIMIT", 3)
    _turn_the_host_sandbox_off(monkeypatch)

    with pytest.raises(OSError, match="claims exceeded the 3-entry stale-claim scan limit"):
        skills_mod.retire_auto_skill_private_authority()

    assert loader._private_root().is_dir()
    assert _stale_names(loader._private_root().parent) == []


@pytest.mark.skipif(os.name == "nt", reason="simulated Windows rename lock runs on POSIX")
@pytest.mark.parametrize("host", ["masked", "maskless"])
def test_simulated_windows_retire_holds_no_pin_on_the_root_it_renames(
    uninitialized_loader,
    monkeypatch,
    host,
):
    """A Windows directory handle opened without ``FILE_SHARE_DELETE`` forbids
    renaming that directory or any directory above it."""
    loader = uninitialized_loader
    _simulate_windows_native_handles(monkeypatch)
    _initialize_test_authority(loader)
    root_key = os.path.realpath(loader._private_root())
    loader._quarantine_root().mkdir(parents=True, exist_ok=True)
    quarantine_key = os.path.realpath(loader._quarantine_root())
    held: dict[str, int] = {}
    real_parent_pin = SkillsLoader._pin_skill_parent
    real_child_pin = SkillsLoader._pin_skill_child_parent

    @contextlib.contextmanager
    def hold(pin):
        key = os.path.normpath(str(pin.path))
        held[key] = held.get(key, 0) + 1
        try:
            yield pin
        finally:
            held[key] -= 1

    @contextlib.contextmanager
    def tracked_parent_pin(self, path):
        with real_parent_pin(self, path) as pin, hold(pin) as held_pin:
            yield held_pin

    @contextlib.contextmanager
    def tracked_child_pin(self, parent, name, **kwargs):
        with real_child_pin(self, parent, name, **kwargs) as pin, hold(pin) as held_pin:
            yield held_pin

    real_rename = os.rename
    renamed: list[str] = []

    def windows_rename(source, destination, *args, **kwargs):
        source_key = os.path.normpath(os.fspath(source))
        for key, count in held.items():
            if count and (key == source_key or key.startswith(source_key + os.sep)):
                raise PermissionError(
                    errno.EACCES,
                    "a directory handle without FILE_SHARE_DELETE is open",
                    source_key,
                )
        renamed.append(source_key)
        return real_rename(source, destination, *args, **kwargs)

    monkeypatch.setattr(SkillsLoader, "_pin_skill_parent", tracked_parent_pin)
    monkeypatch.setattr(SkillsLoader, "_pin_skill_child_parent", tracked_child_pin)
    monkeypatch.setattr(os, "rename", windows_rename)
    if host == "maskless":
        monkeypatch.setattr(skills_mod, "_auto_skill_sandbox_excludes_every_promoter", lambda: True)
    skills_mod._reset_auto_skill_private_authority_for_tests()

    retired_root, retired_record, _history = skills_mod.retire_auto_skill_private_authority()

    assert root_key in renamed
    assert quarantine_key in renamed
    assert not os.path.lexists(root_key)
    assert retired_root.is_dir()
    assert retired_record is not None and retired_record.is_file()


# ── Claim history never exhausts the active-claim bound ──


def _plant_consumed_quarantine_history(loader, count):
    """Model ``count`` lifetime claims: a public quarantine name retired into evidence."""
    assert loader._private_state_roots_safe(create=True) is True
    for index in range(count):
        name = f"history-{index}--{index:032x}"
        (loader._quarantine_root() / name).mkdir()
        (loader._evidence_root() / name).mkdir()


def _stage_and_abandon_claim(loader, slug):
    loader.stage_skill_candidate(
        slug,
        description="d",
        triggers="t",
        procedure_md="## Steps\n\nrun\n",
        provenance=_prov(),
    )
    claimed = loader._claim_pending_update(slug)
    assert claimed is not None
    claim, claim_fd, _consumed_at, _snapshot = claimed
    # The owning process dies mid-claim: its lock is released, its claim stays.
    skills_mod.platform_compat.release_lock(claim_fd)
    os.close(claim_fd)
    assert not (loader._pending_root() / slug).exists()
    return claim


def test_consumed_quarantine_history_never_exhausts_the_active_claim_bound(loader):
    _plant_consumed_quarantine_history(loader, skills_mod._ACTIVE_CLAIM_SCAN_LIMIT + 1)

    assert loader._pending_slug_claimed("after-history") is False
    assert (
        loader.stage_skill_candidate(
            "after-history",
            description="d",
            triggers="t",
            procedure_md="## Steps\n\nrun\n",
            provenance=_prov(),
        )
        is not None
    )
    claim = _stage_and_abandon_claim(loader, "interrupted")

    loader._recover_abandoned_claims()

    assert (loader._pending_root() / "interrupted" / "SKILL.md").is_file()
    assert not claim.exists()


def test_unconsumed_quarantine_names_still_bound_the_active_claim_scan(loader, caplog):
    claim = _stage_and_abandon_claim(loader, "interrupted")
    for index in range(skills_mod._ACTIVE_CLAIM_SCAN_LIMIT + 1):
        (loader._quarantine_root() / f"planted-{index}--{index:032x}").mkdir()

    assert loader._pending_slug_claimed("after-plant") is None
    with caplog.at_level("WARNING", logger="kiro_crew.skills"):
        loader._recover_abandoned_claims()

    assert "active claim namespace exceeded the" in caplog.text
    assert claim.is_dir()
    assert not (loader._pending_root() / "interrupted").exists()


def _turn_the_host_sandbox_back_on(monkeypatch):
    """Model the operator re-enabling ``agent.sandbox``: the gateway certifies again."""
    monkeypatch.setattr(skills_mod, "_auto_skill_authority_sandbox_refusal", lambda: None)
    monkeypatch.setattr(skills_mod, "_auto_skill_sandbox_excludes_every_promoter", lambda: False)


@pytest.mark.parametrize("host", ["masked", "maskless"])
def test_retire_sets_quarantine_history_aside_so_a_fresh_root_stages_again(
    uninitialized_loader,
    monkeypatch,
    host,
):
    """Private ``evidence/`` is what makes a public quarantine name history, and it
    leaves with the retired root. Left behind, every public name would count as an
    active claim against the next fresh root and refuse staging and recovery for
    good, so both public quarantines go aside under the root's token."""
    loader = uninitialized_loader
    if host == "masked":
        # The verified path refuses history past the stale-state scan bound, so a
        # smaller active bound reproduces the same overflow below it.
        monkeypatch.setattr(skills_mod, "_ACTIVE_CLAIM_SCAN_LIMIT", 3)
    skills_mod.initialize_gateway_auto_skill_private_authority()
    history_count = skills_mod._ACTIVE_CLAIM_SCAN_LIMIT + 1
    _plant_consumed_quarantine_history(loader, history_count)
    live_history = loader._live_quarantine_root() / f"live-history--{0:032x}"
    live_history.mkdir()
    (loader._evidence_root() / live_history.name).mkdir()
    auto = loader._quarantine_root().parent
    if host == "maskless":
        _turn_the_host_sandbox_off(monkeypatch)
    else:
        monkeypatch.setattr(
            skills_mod, "_auto_skill_sandbox_excludes_every_promoter", lambda: False
        )
        skills_mod._reset_auto_skill_private_authority_for_tests()

    retired_root, _record, retired_history = skills_mod.retire_auto_skill_private_authority()

    token = retired_root.name.split(".stale-", 1)[1]
    aside = {path.name: path for path in retired_history}
    quarantine_name = f"{skills_mod.AUTO_QUARANTINE_DIRNAME}.stale-{token}"
    live_quarantine_name = f"{skills_mod.AUTO_LIVE_QUARANTINE_DIRNAME}.stale-{token}"
    assert sorted(aside) == sorted([quarantine_name, live_quarantine_name])
    assert all(os.path.samefile(path.parent, auto) for path in retired_history)
    assert not os.path.lexists(loader._quarantine_root())
    assert not os.path.lexists(loader._live_quarantine_root())
    # Nothing is deleted: the history waits beside its evidence for inspection.
    assert len(os.listdir(aside[quarantine_name])) == history_count
    assert (aside[live_quarantine_name] / live_history.name).is_dir()
    evidence = os.listdir(retired_root / skills_mod.AUTO_EVIDENCE_DIRNAME)
    assert len(evidence) == history_count + 1

    _turn_the_host_sandbox_back_on(monkeypatch)
    _restart_gateway_certification()

    assert (
        loader.stage_skill_candidate(
            "after-retire",
            description="d",
            triggers="t",
            procedure_md="## Steps\n\nrun\n",
            provenance=_prov(),
        )
        is not None
    )
    assert loader._pending_slug_claimed("another-slug") is False
    claim = _stage_and_abandon_claim(loader, "interrupted")

    loader._recover_abandoned_claims()

    assert (loader._pending_root() / "interrupted" / "SKILL.md").is_file()
    assert not claim.exists()


def test_a_failed_quarantine_set_aside_restores_every_retired_entry(
    uninitialized_loader,
    monkeypatch,
):
    loader = uninitialized_loader
    _initialize_test_authority(loader)
    _plant_consumed_quarantine_history(loader, 2)
    loader._live_quarantine_root().mkdir(exist_ok=True)
    entries = [
        loader._private_root(),
        loader._authority_provenance_path(),
        loader._quarantine_root(),
        loader._live_quarantine_root(),
    ]
    identities = [entry.stat().st_ino for entry in entries]
    real_rename = SkillsLoader._rename_skill_child_no_replace

    def refuse_the_last_set_aside(self, source, source_name, *args, **kwargs):
        if source_name == skills_mod.AUTO_LIVE_QUARANTINE_DIRNAME:
            raise OSError(errno.EIO, "simulated rename failure")
        return real_rename(self, source, source_name, *args, **kwargs)

    monkeypatch.setattr(SkillsLoader, "_rename_skill_child_no_replace", refuse_the_last_set_aside)
    monkeypatch.setattr(skills_mod, "_auto_skill_sandbox_excludes_every_promoter", lambda: False)
    skills_mod._reset_auto_skill_private_authority_for_tests()

    with pytest.raises(OSError, match="simulated rename failure"):
        skills_mod.retire_auto_skill_private_authority()

    assert [entry.stat().st_ino for entry in entries] == identities
    assert _stale_names(loader._private_root().parent) == []
    assert _stale_names(loader._quarantine_root().parent) == []


# ── Only an unmasked boundary can prove authority absence ──


def _hide_authority_behind_empty_mask(loader):
    """Replace the process view with the empty directory a sandbox mask exposes."""
    home = loader._private_root().parents[1]
    host_tag_grants = home / "tag-grants-host-view"
    (home / skills_mod._AUTHORITY_PROVENANCE_PARENT).rename(host_tag_grants)
    (home / skills_mod._AUTHORITY_PROVENANCE_PARENT).mkdir()
    skills_mod._reset_auto_skill_private_authority_for_tests()
    return host_tag_grants / skills_mod.AUTO_SKILL_PRIVATE_STATE_DIRNAME


@pytest.mark.parametrize("confinement", ["marker", "seatbelt"])
def test_masked_authority_view_never_proves_real_root_absent(
    uninitialized_loader,
    monkeypatch,
    caplog,
    confinement,
):
    """An empty masked ``tag-grants`` view cannot authorize by-name mutation."""
    from kiro_crew import sandbox

    loader = uninitialized_loader
    skills_mod.initialize_auto_skill_private_authority(
        skills_root=loader._dir,
        data_home=loader._private_root().parents[1],
    )
    real_root = _hide_authority_behind_empty_mask(loader)
    if confinement == "marker":
        monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
        monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: None)
    else:
        monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
        monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: True)
    live = _write_live(loader, "masked-real-root", version=1, body="OLD")
    before = (live / "SKILL.md").read_bytes()

    caplog.set_level(logging.WARNING, logger="kiro_crew.skills")
    assert real_root.is_dir()
    assert not loader._private_root().exists()
    assert skills_mod._auto_skill_authority_root_exists() is True
    assert skills_mod._auto_skill_promotion_ruled_out() is False
    assert loader.set_pinned("auto/masked-real-root", True) is False

    assert (live / "SKILL.md").read_bytes() == before
    assert "cannot be observed from inside the agent sandbox" in caplog.text


def test_unmasked_root_absence_still_grants_by_name_mutation(
    uninitialized_loader,
    monkeypatch,
):
    """The r4 rootless behavior remains available to an operator-side process."""
    from kiro_crew import sandbox

    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: False)
    loader = uninitialized_loader
    live = _write_live(loader, "unmasked-rootless", version=1, body="OLD")
    before = (live / "SKILL.md").read_bytes()

    assert not loader._private_root().exists()
    assert skills_mod._auto_skill_authority_root_exists() is False
    assert skills_mod._auto_skill_promotion_ruled_out() is True
    assert loader.set_pinned("auto/unmasked-rootless", True) is True
    assert (live / "SKILL.md").read_bytes() != before


def test_in_sandbox_root_absence_and_fresh_no_replace_probe_both_refuse_mutation(
    uninitialized_loader,
    monkeypatch,
    caplog,
):
    """Neither masked absence nor a masked fresh probe can grant mutation."""
    from kiro_crew import sandbox

    monkeypatch.setenv("KIROCREW_SANDBOX_ACTIVE", "1")
    monkeypatch.setattr(sandbox, "_macos_sandbox_state", lambda: None)
    monkeypatch.setattr(
        skills_mod,
        "_STARTUP_AUTHORITY_REFUSAL",
        skills_mod._NO_REPLACE_RENAME_REFUSAL,
    )

    def unsupported_probe(*_args, **_kwargs):
        raise NotImplementedError

    with monkeypatch.context() as fresh_probe:
        fresh_probe.setattr(
            SkillsLoader,
            "_ensure_private_authority",
            lambda *_args, **_kwargs: object(),
        )
        fresh_probe.setattr(
            SkillsLoader,
            "_probe_rename_under_authority",
            unsupported_probe,
        )
        assert skills_mod._no_replace_refusal_still_binds_data_home() is False

    loader = uninitialized_loader
    live = _write_live(loader, "sandbox-rootless", version=1, body="OLD")
    before = (live / "SKILL.md").read_bytes()
    caplog.set_level(logging.WARNING, logger="kiro_crew.skills")

    assert not loader._private_root().exists()
    assert skills_mod._auto_skill_authority_root_exists() is True
    assert skills_mod._auto_skill_promotion_ruled_out() is False
    assert loader.set_pinned("auto/sandbox-rootless", True) is False

    assert (live / "SKILL.md").read_bytes() == before
    assert "cannot be observed from inside the agent sandbox" in caplog.text


# ── Unmasked root absence grants by-name mutation ──


@pytest.mark.parametrize(
    ("mode", "effective", "kind", "host_wide"),
    [
        ("off", "off", "", True),
        ("standard", "standard", "no_backend", True),
        ("standard", "standard", "transient", False),
        ("standard", "standard", "foreign_sandbox", False),
        ("off", "standard", "", False),
    ],
    ids=[
        "sandbox-off",
        "no-backend",
        "transient-probe",
        "foreign-outer-sandbox",
        "floor-raises-off",
    ],
)
def test_maskless_host_predicates_shape_only_the_retire_hint(
    uninitialized_loader,
    monkeypatch,
    mode,
    effective,
    kind,
    host_wide,
):
    from kiro_crew import sandbox

    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: mode)
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda _mode: False)
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda _mode: effective)
    monkeypatch.setattr(sandbox, "unavailable_kind", lambda: kind)

    with pytest.raises(OSError):
        skills_mod.initialize_gateway_auto_skill_private_authority()

    assert skills_mod._STARTUP_AUTHORITY_BINDING is None
    assert skills_mod._auto_skill_sandbox_excludes_every_promoter() is host_wide
    assert skills_mod._auto_skill_promotion_ruled_out() is True
    _write_live(uninitialized_loader, "maskless-live", version=1, body="OLD")
    assert uninitialized_loader.set_pinned("auto/maskless-live", True) is True
    assert not _authority_path(uninitialized_loader._private_root().parents[1]).exists()


def test_an_unreadable_sandbox_predicate_does_not_override_root_absence(
    uninitialized_loader,
    monkeypatch,
):
    from kiro_crew import sandbox

    def unreadable(_mode):
        raise RuntimeError("governance floor could not be read")

    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        lambda: skills_mod._SANDBOX_MASK_UNAVAILABLE_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", unreadable)
    monkeypatch.setattr(sandbox, "unavailable_kind", lambda: "no_backend")

    assert skills_mod._auto_skill_sandbox_excludes_every_promoter() is False
    assert skills_mod._auto_skill_promotion_ruled_out() is True
    _write_live(uninitialized_loader, "unreadable-rootless", version=1, body="OLD")
    assert uninitialized_loader.set_pinned("auto/unreadable-rootless", True) is True


def test_sandbox_off_keeps_live_deletion_and_explicit_dismissal(uninitialized_loader, monkeypatch):
    from kiro_crew import sandbox

    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: "off")
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda mode: mode != "off")
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda mode: mode)
    loader = uninitialized_loader

    with pytest.raises(OSError, match="sandbox_off"):
        skills_mod.initialize_gateway_auto_skill_private_authority()

    planted = _plant_always_on_auto_skill(loader, "maskless-always")
    assert loader.set_pinned("auto/maskless-always", True) is True
    assert loader.delete_skill("auto/maskless-always") is True
    assert not planted.exists()
    pending = loader._pending_root() / "queued-maskless"
    pending.mkdir(parents=True)
    (pending / "SKILL.md").write_text("## Steps\n\nqueued\n", encoding="utf-8")
    assert loader.dismiss_pending_skill("queued-maskless") is True
    assert not pending.exists()
    assert not _authority_path(loader._private_root().parents[1]).exists()


def test_root_absence_keeps_by_name_mutation_across_sandbox_mode_changes(
    uninitialized_loader,
    monkeypatch,
):
    """A mode change cannot create promotion authority while the root is absent."""
    from kiro_crew import sandbox

    mode = "off"
    monkeypatch.setattr(
        skills_mod,
        "_auto_skill_authority_sandbox_refusal",
        _REAL_AUTHORITY_SANDBOX_REFUSAL,
    )
    monkeypatch.setattr(skills_mod, "_agent_sandbox_is_delegated", lambda: False)
    monkeypatch.setattr(sandbox, "configured_sandbox_mode", lambda: mode)
    monkeypatch.setattr(sandbox, "credential_mask_applies", lambda current: current != "off")
    monkeypatch.setattr(sandbox, "effective_sandbox_mode", lambda current: current)
    with pytest.raises(OSError, match="sandbox_off"):
        skills_mod.initialize_gateway_auto_skill_private_authority()
    _write_live(uninitialized_loader, "maskless-flip", version=1, body="OLD")
    assert skills_mod._auto_skill_promotion_ruled_out() is True
    assert uninitialized_loader.set_pinned("auto/maskless-flip", True) is True

    mode = "standard"

    assert skills_mod._auto_skill_promotion_ruled_out() is True
    assert uninitialized_loader.set_pinned("auto/maskless-flip", False) is True
    assert not _authority_path(uninitialized_loader._private_root().parents[1]).exists()


# ── A rename probe or one stuck claim never freezes lifecycle ──


def _plant_rename_probe(loader, *, orphaned):
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    probe = loader._claims_root() / (
        f"{skills_mod._RENAME_PROBE_PREFIX}{secrets.token_hex(16)}-source"
    )
    probe.write_bytes(b"")
    # Set the modification time rather than wait for one: an orphan is older than
    # the cleanup threshold, a probe still in use is not.
    stamp = 1_000_000.0 if orphaned else datetime.now(tz=timezone.utc).timestamp() + 3600
    os.utime(probe, (stamp, stamp))
    return probe


_FAR_FUTURE_LIFECYCLE = {
    "max_auto_skills": 100,
    "stale_after_days": 1,
    "archive_after_days": 1,
    "now": datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp(),
}


def test_an_orphaned_rename_probe_neither_freezes_lifecycle_nor_survives_recovery(loader):
    live = _write_live(loader, "probe-unrelated", version=1, body="OLD")
    orphan = _plant_rename_probe(loader, orphaned=True)
    in_use = _plant_rename_probe(loader, orphaned=False)

    result = loader.run_skill_lifecycle(**_FAR_FUTURE_LIFECYCLE)

    assert result["archived"] == 1
    assert not live.exists()
    loader._recover_abandoned_claims()
    assert not orphan.exists()
    assert in_use.exists()


def test_recovery_removes_at_most_the_bounded_number_of_orphaned_probes(loader, monkeypatch):
    monkeypatch.setattr(skills_mod, "_ORPHANED_RENAME_PROBE_CLEANUP_LIMIT", 2)
    probes = [_plant_rename_probe(loader, orphaned=True) for _ in range(3)]

    loader._recover_abandoned_claims()
    assert sum(probe.exists() for probe in probes) == 1
    loader._recover_abandoned_claims()
    assert not any(probe.exists() for probe in probes)


def test_an_unreadable_valid_claim_journal_exempts_every_lifecycle_target(
    loader,
    monkeypatch,
    caplog,
):
    victim = _write_live(loader, "opaque-victim", version=1, body="OLD")
    unrelated = _write_live(loader, "opaque-unrelated", version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "opaque-candidate",
        target="auto/opaque-victim",
        body="## Steps\n\nNEW",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    with monkeypatch.context() as interrupted:
        interrupted.setattr(loader, "_commit_claim_consumption", lambda *_args: False)
        assert (
            loader.auto_apply_pending_update(
                "opaque-candidate",
                expected_candidate_binding=binding[0],
            )
            is None
        )
    (claim,) = list(loader._claims_root().glob("opaque-candidate--*"))
    lock_path = loader._claim_lock_path(claim.name)
    real_open = loader._open_skill_lock

    def unreadable(parent, name, **kwargs):
        if parent.path == lock_path.parent and name == lock_path.name:
            raise OSError("claim journal is unreadable")
        return real_open(parent, name, **kwargs)

    monkeypatch.setattr(loader, "_open_skill_lock", unreadable)
    caplog.set_level(logging.WARNING, logger="kiro_crew.skill_runtime.auto_skills")

    result = loader.run_skill_lifecycle(**_FAR_FUTURE_LIFECYCLE)

    assert result["archived"] == 0
    assert victim.is_dir()
    assert unrelated.is_dir()
    messages = [record.getMessage() for record in caplog.records]
    assert any(str(lock_path) in message for message in messages)
    assert any("restart the gateway to recover it" in message for message in messages)


def test_a_readable_authenticated_journal_exempts_only_its_recorded_target(
    loader,
    monkeypatch,
):
    victim = _write_live(loader, "readable-victim", version=1, body="OLD")
    unrelated = _write_live(loader, "readable-unrelated", version=1, body="OLD")
    binding: list[str] = []
    _stage_update(
        loader,
        "opaque-candidate",
        target="auto/readable-victim",
        body="## Steps\n\nNEW",
        notify=False,
        unattended=True,
        unattended_binding_out=binding,
    )
    with monkeypatch.context() as interrupted:
        interrupted.setattr(loader, "_commit_claim_consumption", lambda *_args: False)
        assert (
            loader.auto_apply_pending_update(
                "opaque-candidate",
                expected_candidate_binding=binding[0],
            )
            is None
        )

    result = loader.run_skill_lifecycle(**_FAR_FUTURE_LIFECYCLE)

    assert result["archived"] == 1
    assert victim.is_dir()
    assert not unrelated.exists()


def test_a_journal_in_a_pre_publication_state_exempts_no_target(loader):
    """Recovery never reconciles a live generation for an active-state journal, so
    neither its own slug nor any other target is held live by it."""
    own = _write_live(loader, "journal-active", version=1, body="OLD")
    other = _write_live(loader, "journal-other", version=1, body="OLD")
    claim_name = f"journal-active--{secrets.token_hex(16)}"
    assert loader._private_state_roots_safe(create=True, require_sensitive=True) is True
    (loader._claims_root() / claim_name).mkdir()
    _plant_claim_lock(loader, claim_name)

    result = loader.run_skill_lifecycle(**_FAR_FUTURE_LIFECYCLE)

    assert result["archived"] == 2
    assert not own.exists()
    assert not other.exists()


def test_a_crash_orphaned_rename_probe_does_not_block_a_stale_reseed(uninitialized_loader):
    loader = uninitialized_loader
    home = loader._private_root().parents[1]
    skills_mod.initialize_gateway_auto_skill_private_authority()
    _plant_rename_probe(loader, orphaned=False)
    _rewrite_authority_record(loader, home, "record-missing")

    binding = _restart_gateway_certification()

    assert binding == skills_mod._STARTUP_AUTHORITY_BINDING
    assert skills_mod.auto_skill_promotion_disabled_reason() is None
