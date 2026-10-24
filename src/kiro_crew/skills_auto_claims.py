"""The claim transaction a promotion, dismissal or unattended apply runs on: renaming a
pending candidate into its claim, retaining or discarding the claimed generation,
publishing a prepared tree with no-replace renames, and the restart recovery that
consumes, re-queues or retains each abandoned claim by exact digest and identity.

Each function here whose first parameter is ``self`` is a ``SkillsLoader`` method: the class
binds it by name.
"""

from __future__ import annotations

import logging
import os
import secrets
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from kiro_crew import platform_compat
from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.skills_auto_model import (
    _ACTIVE_CLAIM_SCAN_LIMIT,
    _ORPHANED_RENAME_PROBE_CLEANUP_LIMIT,
    _ORPHANED_RENAME_PROBE_MIN_AGE_S,
    _RENAME_PROBE_PREFIX,
    VERSIONS_DIRNAME,
    _ActiveClaimScanOverflow,
    _ClaimSnapshot,
    _PinnedPrivateState,
    _PinnedSkillParent,
    _SkillTreeSnapshot,
    _TaggedFileIdentity,
)
from kiro_crew.skills_auto_pinning import _DIR_FD_SUPPORTED

#: The facade's logger, so a filter or a capture on ``kiro_crew.skills`` sees these records.
logger = logging.getLogger("kiro_crew.skills")


def _claim_pending_update(self, slug: str) -> tuple[Path, int, str, _ClaimSnapshot] | None:
    """Atomically quarantine a public inode and materialize its trusted claim."""
    if not self._is_pending_slug_safe(slug):
        return None
    if not self._private_state_roots_safe(create=True, require_sensitive=True):
        logger.error(
            "Refusing to claim pending skill %s: private state is unsafe",
            slug,
        )
        return None
    # Product writers recover every abandoned prepared publication before
    # claiming more work, so a later update cannot hide whether the prior
    # atomic live replace committed. Recovery acquires its own pinned
    # hierarchy rather than inheriting this admission verdict.
    self._recover_abandoned_claims(roots_authenticated=True)
    # Restoration and publication require atomic no-replace renames. Prove
    # that primitive before removing the visible pending name.
    if not self._probe_no_replace_rename():
        return None
    claim_name = f"{slug}--{secrets.token_hex(16)}"
    claim = self._claims_root() / claim_name
    quarantine = self._quarantine_root() / claim_name
    lock_path = self._claim_lock_path(claim_name)
    fd: int | None = None
    claim_lock_acquired = False
    try:
        with self._pin_private_state(
            create=True,
            require_sensitive=True,
        ) as private_state:
            created_lock: list[bool] = []
            fd = cast(
                int,
                self._open_skill_lock(
                    private_state.claim_locks,
                    lock_path.name,
                    created_out=created_lock,
                ),
            )
            if not platform_compat.try_acquire_lock(fd, exclusive=True):
                raise BlockingIOError("claim lock is held")
            claim_lock_acquired = True
            if not self._initialize_claim_lock_state(fd, lock_path, claim_name):
                raise OSError("claim lock initialization failed")
            if created_lock == [True]:
                # File fsync makes the payload durable; parent fsync makes
                # this fresh journal NAME durable before public mutation.
                self._sync_pinned_parent(private_state.claim_locks)
    except (OSError, RuntimeError, ValueError):
        if claim_lock_acquired and fd is not None:
            platform_compat.release_lock(fd)
        if fd is not None:
            os.close(fd)
        self._cleanup_claim_lock(claim_name)
        return None
    assert fd is not None
    consumed_at = datetime.now(tz=timezone.utc).isoformat()
    claim_snapshot: _ClaimSnapshot | None = None
    quarantined = False
    try:
        lock_state: list[_PinnedPrivateState] = []
        with self._file_lock("pending.lock", state_out=lock_state) as acquired:
            if not acquired or not lock_state:
                raise OSError("pending namespace lock unavailable")
            private_state = lock_state[0]
            pending_parent = private_state.pending
            candidate_info = self._stat_pinned_child(pending_parent, slug)
            if self._pinned_parent_matches(pending_parent):
                linked_candidate = (
                    stat.S_ISLNK(candidate_info.st_mode)
                    or bool(getattr(candidate_info, "st_reparse_tag", 0))
                    or not stat.S_ISDIR(candidate_info.st_mode)
                )
                initial_candidate_identity = self._pinned_child_identity(
                    pending_parent,
                    slug,
                )
                if initial_candidate_identity is None:
                    raise OSError("pending candidate identity is unavailable before admission")
                marker_present = False
                captured_root_identity: _TaggedFileIdentity | None = initial_candidate_identity
                captured_tree: _SkillTreeSnapshot | None = None
                if not linked_candidate:
                    with self._pin_skill_child_parent(
                        pending_parent,
                        slug,
                        create=False,
                    ) as candidate_parent:
                        admitted_info = self._stat_pinned_child(pending_parent, slug)
                        if not platform_compat.IS_WINDOWS and not os.path.samestat(
                            candidate_info, admitted_info
                        ):
                            raise OSError("pending candidate changed before capture")
                        if not platform_compat.IS_WINDOWS and candidate_parent.identity != (
                            admitted_info.st_dev,
                            admitted_info.st_ino,
                        ):
                            raise OSError("pending candidate changed before capture")
                        if platform_compat.IS_WINDOWS and (
                            candidate_parent.native_identity != initial_candidate_identity
                            or not self._opened_path_matches(
                                candidate_parent.fd,
                                candidate_parent.path,
                                admitted_info,
                            )
                        ):
                            raise OSError("pending candidate changed before capture")
                        candidate_info = admitted_info
                        captured_root_identity = self._tag_opened_identity(candidate_parent.fd)
                        if captured_root_identity is None:
                            raise OSError("pending candidate identity is unavailable")
                        try:
                            self._stat_pinned_child(candidate_parent, ".promoted")
                        except FileNotFoundError:
                            pass
                        else:
                            marker_present = True
                        captured_tree = self._skill_tree_snapshot_child(
                            pending_parent,
                            slug,
                        )
                        if (
                            captured_tree is not None
                            and captured_tree.root_identity != captured_root_identity
                        ):
                            raise OSError("pending candidate changed during capture")
                        captured_info = self._stat_pinned_child(pending_parent, slug)
                        if not platform_compat.IS_WINDOWS and not os.path.samestat(
                            candidate_info, captured_info
                        ):
                            raise OSError("pending candidate changed during capture")
                        if platform_compat.IS_WINDOWS and (
                            self._pinned_child_identity(pending_parent, slug)
                            != captured_root_identity
                            or not self._opened_path_matches(
                                candidate_parent.fd,
                                candidate_parent.path,
                                captured_info,
                            )
                        ):
                            raise OSError("pending candidate changed during capture")
                        candidate_info = captured_info
                claim_snapshot = _ClaimSnapshot(
                    captured_tree.generation_hash if captured_tree is not None else None,
                    (
                        captured_tree.files.get(Path(".meta.json"))
                        if captured_tree is not None
                        else None
                    ),
                    tree=captured_tree,
                    quarantine_identity=captured_root_identity,
                )
                snapshot_generation = claim_snapshot.generation_hash
                snapshot_tree = claim_snapshot.tree
                has_snapshot = snapshot_generation is not None and snapshot_tree is not None
                # Linked candidates are never publishable, but dismissal and
                # refusal still atomically remove their public pending name.
                if not has_snapshot and not (linked_candidate or marker_present):
                    raise OSError("pending candidate snapshot is unreadable")
                if has_snapshot and not self._write_claim_snapshot_state(
                    fd,
                    lock_path,
                    claim_name,
                    claim_snapshot,
                ):
                    raise OSError("claim generation witness failed")
                if linked_candidate:
                    assert captured_root_identity is not None
                    self._rename_untrusted_link_no_replace(
                        pending_parent,
                        slug,
                        private_state.quarantine,
                        claim_name,
                        expected_identity=captured_root_identity,
                    )
                else:
                    self._rename_skill_child_no_replace(
                        pending_parent,
                        slug,
                        private_state.quarantine,
                        claim_name,
                        expected_identity=captured_root_identity,
                    )
                quarantined = True
                if captured_root_identity is not None:
                    if linked_candidate:
                        quarantine_root_identity = self._pinned_child_identity(
                            private_state.quarantine,
                            claim_name,
                        )
                    else:
                        with self._pin_skill_child_parent(
                            private_state.quarantine,
                            claim_name,
                            create=False,
                        ) as quarantine_parent:
                            quarantine_root_identity = self._tag_opened_identity(
                                quarantine_parent.fd
                            )
                    if quarantine_root_identity != captured_root_identity:
                        raise OSError("quarantined candidate differs from captured candidate")
                self._sync_pinned_rename_parents(
                    pending_parent,
                    private_state.quarantine,
                )
                if has_snapshot:
                    assert snapshot_generation is not None
                    assert snapshot_tree is not None
                    # The public inode can still have retained writers. It
                    # never enters the private state; only these captured bytes do.
                    if not self._pinned_parent_matches(private_state.claims):
                        raise OSError("trusted claims parent changed before materialization")
                    materialization = self._claim_materialization_path(claim_name)
                    if os.path.lexists(materialization):
                        raise OSError("trusted claim materialization stage already exists")
                    self._materialize_skill_tree_snapshot(
                        snapshot_tree,
                        materialization,
                    )
                    self._sync_skill_tree(materialization)
                    self._rename_skill_child_no_replace(
                        private_state.private,
                        materialization.name,
                        private_state.claims,
                        claim_name,
                    )
                    if not self._pinned_parent_matches(private_state.claims):
                        raise OSError("trusted claims parent changed during materialization")
                    materialized = self._stat_pinned_child(
                        private_state.claims,
                        claim_name,
                    )
                    if not stat.S_ISDIR(materialized.st_mode) or is_link_or_junction(claim):
                        raise OSError("trusted claim materialization is unsafe")
                    self._sync_skill_tree(claim)
                    # The claims destination is authority; the source
                    # materialization is disposable and may safely reappear.
                    self._sync_pinned_parent(private_state.claims)
                    trusted_snapshot = self._capture_claim_snapshot(claim)
                    if (
                        trusted_snapshot.generation_hash is None
                        or trusted_snapshot.tree is None
                        or not secrets.compare_digest(
                            snapshot_generation,
                            trusted_snapshot.generation_hash,
                        )
                    ):
                        raise OSError("trusted claim materialization changed")
                    claim_snapshot = _ClaimSnapshot(
                        trusted_snapshot.generation_hash,
                        trusted_snapshot.metadata_bytes,
                        tree=trusted_snapshot.tree,
                        quarantine_identity=claim_snapshot.quarantine_identity,
                    )
    except OSError:
        if quarantined and claim_snapshot is not None:
            self._cleanup_publication_artifacts(claim_name)
            self._restore_claimed_update(claim, fd, slug, claim_snapshot)
        platform_compat.release_lock(fd)
        os.close(fd)
        if (
            not os.path.lexists(claim)
            and not os.path.lexists(quarantine)
            and not os.path.lexists(self._evidence_root() / claim_name)
        ):
            self._cleanup_claim_lock(claim_name)
        return None
    if claim_snapshot is None:
        platform_compat.release_lock(fd)
        os.close(fd)
        self._cleanup_claim_lock(claim_name)
        return None
    if not is_link_or_junction(quarantine) and self._completion_marker_present(quarantine):
        logger.warning("Refusing pending skill %s: reserved completion marker exists", slug)
        try:
            if self._remove_untrusted_completion_marker(quarantine):
                self._restore_claimed_update(claim, fd, slug, claim_snapshot)
        except OSError:
            logger.error("Could not restore marker-bearing claim %s", quarantine, exc_info=True)
        finally:
            platform_compat.release_lock(fd)
            os.close(fd)
            if (
                not os.path.lexists(claim)
                and not os.path.lexists(quarantine)
                and not os.path.lexists(self._evidence_root() / claim_name)
            ):
                self._cleanup_claim_lock(claim_name)
        return None
    return claim, fd, consumed_at, claim_snapshot


def _retain_claim_evidence(self, claim: Path, claim_fd: int) -> bool:
    """Retain private evidence while both public identities remain exact."""
    lock_path = self._claim_lock_path(claim.name)
    if not self._commit_claim_evidence_state(claim_fd, lock_path, claim.name):
        logger.error("Could not commit private evidence state for %s", claim)
        return False
    evidence_state = self._authenticated_claim_evidence_state(
        claim_fd,
        lock_path,
        claim.name,
    )
    if evidence_state is None:
        return False
    try:
        with self._pin_private_state(
            create=False,
            require_sensitive=True,
        ) as private_state:
            if not self._public_claim_evidence_matches(
                private_state,
                evidence_state,
                claim.name,
            ):
                return False
            try:
                source = self._stat_pinned_child(private_state.claims, claim.name)
            except FileNotFoundError:
                try:
                    retained = self._stat_pinned_child(
                        private_state.evidence,
                        claim.name,
                    )
                except OSError:
                    return False
                retained_ok = stat.S_ISDIR(retained.st_mode) and not is_link_or_junction(
                    self._evidence_root() / claim.name
                )
            else:
                if not stat.S_ISDIR(source.st_mode) or is_link_or_junction(claim):
                    return False
                try:
                    self._stat_pinned_child(private_state.evidence, claim.name)
                except FileNotFoundError:
                    pass
                except OSError:
                    return False
                else:
                    return False
                self._rename_skill_child_no_replace(
                    private_state.claims,
                    claim.name,
                    private_state.evidence,
                    claim.name,
                )
                self._sync_pinned_rename_parents(
                    private_state.claims,
                    private_state.evidence,
                )
                retained = self._stat_pinned_child(private_state.evidence, claim.name)
                retained_ok = stat.S_ISDIR(retained.st_mode) and not is_link_or_junction(
                    self._evidence_root() / claim.name
                )
            if not retained_ok:
                return False
            # Linearize completion only after the private move and a fresh
            # check of BOTH descriptor-contaminated public generations.
            return self._public_claim_evidence_matches(
                private_state,
                evidence_state,
                claim.name,
            )
    except (OSError, RuntimeError, ValueError):
        logger.error("Could not retain trusted claim evidence %s", claim, exc_info=True)
        return False


def _discard_trusted_claim(self, claim: Path) -> bool:
    """Remove only the fresh private materialization for one restored claim."""
    try:
        with self._pin_private_state(
            create=False,
            require_sensitive=True,
        ) as private_state:
            try:
                claim_info = self._stat_pinned_child(private_state.claims, claim.name)
            except FileNotFoundError:
                return True
            if not stat.S_ISDIR(claim_info.st_mode) or is_link_or_junction(claim):
                return False
            claim_identity = self._pinned_child_identity(
                private_state.claims,
                claim.name,
            )
            if claim_identity is None:
                return False
            return self._remove_private_tree(
                claim,
                what="restored trusted skill claim",
                expected_identity=claim_identity,
            )
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return False


def _publish_prepared_skill_tree(
    self,
    *,
    state: _PinnedPrivateState,
    live_dir: Path,
    stage: Path,
    backup: Path | None,
    backup_identity: _TaggedFileIdentity | None,
    before_hash: str | None,
    after_hash: str,
) -> str:
    """Publish a prepared generation beneath retained authority parents.

    ``published`` means the exact authenticated before-generation was moved
    aside and the exact prepared after-generation is live. ``drift`` means a
    different live generation reached the mutation boundary and was restored
    byte-for-byte. ``incomplete`` retains the prepared journal and artifacts
    for recovery because the filesystem state cannot be classified safely.
    """
    if (
        live_dir.parent != state.auto.path
        or stage.parent != state.private.path
        or (backup is not None and backup.parent != state.live_quarantine.path)
        or not self._pinned_parent_matches(state.auto)
        or not self._pinned_parent_matches(state.private)
        or not self._pinned_parent_matches(state.live_quarantine)
    ):
        return "incomplete"
    live_name = live_dir.name
    stage_name = stage.name
    backup_name = backup.name if backup is not None else None
    captured_hash: str | None = None
    moved_live = False
    try:
        if backup_name is not None:
            if before_hash is None or backup_identity is None:
                return "incomplete"
            self._rename_skill_child_no_replace(
                state.auto,
                live_name,
                state.live_quarantine,
                backup_name,
                expected_identity=backup_identity,
            )
            moved_live = True
            self._sync_pinned_rename_parents(state.auto, state.live_quarantine)
            current_backup_identity = self._pinned_child_identity(
                state.live_quarantine,
                backup_name,
            )
            if current_backup_identity != backup_identity:
                return "incomplete"
            captured_hash = self._skill_tree_hash_child(
                state.live_quarantine,
                backup_name,
            )
            if captured_hash != before_hash:
                if self._pinned_child_exists(state.auto, live_name):
                    return "incomplete"
                self._rename_skill_child_no_replace(
                    state.live_quarantine,
                    backup_name,
                    state.auto,
                    live_name,
                    expected_identity=backup_identity,
                )
                self._sync_pinned_rename_parents(state.live_quarantine, state.auto)
                moved_live = False
                restored_identity = self._pinned_child_identity(state.auto, live_name)
                restored_hash = self._skill_tree_hash_child(state.auto, live_name)
                return (
                    "drift"
                    if (
                        restored_identity == backup_identity
                        and captured_hash is not None
                        and restored_hash == captured_hash
                    )
                    else "incomplete"
                )
        elif before_hash is not None:
            return "incomplete"

        self._rename_skill_child_no_replace(
            state.private,
            stage_name,
            state.auto,
            live_name,
            compensate_mismatch_to_source=False,
        )
        self._sync_pinned_rename_parents(state.private, state.auto)
        if backup_name is not None:
            current_backup_identity = self._pinned_child_identity(
                state.live_quarantine,
                backup_name,
            )
            if current_backup_identity != backup_identity:
                return "incomplete"
            if self._skill_tree_hash_child(state.live_quarantine, backup_name) != before_hash:
                # The detached pre-publication inode can still have a retained
                # writer. Its drift does not authorize moving the current live
                # name into a disposable stage: that name may already hold a
                # later by-name generation. An exact after-tree is committed and
                # the changed public backup remains evidence; every other live
                # state is ambiguous and all artifacts stay in place.
                live_hash = self._skill_tree_hash_child(state.auto, live_name)
                return "published" if live_hash == after_hash else "incomplete"
    except OSError:
        if backup_name is not None and moved_live:
            try:
                backup_info = self._stat_pinned_child(
                    state.live_quarantine,
                    backup_name,
                )
                current_backup_identity = self._pinned_child_identity(
                    state.live_quarantine,
                    backup_name,
                )
                if (
                    not self._pinned_child_exists(state.auto, live_name)
                    and stat.S_ISDIR(backup_info.st_mode)
                    and current_backup_identity == backup_identity
                    and not is_link_or_junction(state.live_quarantine.path / backup_name)
                ):
                    self._rename_skill_child_no_replace(
                        state.live_quarantine,
                        backup_name,
                        state.auto,
                        live_name,
                        expected_identity=backup_identity,
                    )
                    self._sync_pinned_rename_parents(
                        state.live_quarantine,
                        state.auto,
                    )
            except OSError:
                logger.error(
                    "Could not roll back interrupted skill generation swap for %s",
                    live_dir,
                )
        return "incomplete"
    return (
        "published"
        if self._skill_tree_hash_child(state.auto, live_name) == after_hash
        else "incomplete"
    )


def _reconcile_prepared_claim(
    self,
    claim: Path,
    claim_fd: int,
    lock_path: Path,
    *,
    private_state: _PinnedPrivateState | None = None,
) -> bool | None:
    """Return True if fully published, False if rolled back, else None."""
    journal = self._authenticated_claim_publication(claim_fd, lock_path, claim.name)
    if journal is None:
        return None
    target_slug = str(journal["target"])
    with self._promotion_lock(target_slug) as acquired:
        if not acquired:
            return None
        if private_state is not None:
            if (
                not self._pinned_parent_matches(private_state.auto)
                or not self._pinned_parent_matches(private_state.private)
                or not self._pinned_parent_matches(private_state.live_quarantine)
            ):
                return None
            return self._reconcile_prepared_claim_pinned(
                journal,
                private_state,
                claim.name,
            )
        try:
            with self._pin_private_state(
                create=False,
                require_sensitive=True,
            ) as state:
                return self._reconcile_prepared_claim_pinned(
                    journal,
                    state,
                    claim.name,
                )
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            return None


def _reconcile_prepared_claim_pinned(
    self,
    journal: dict[str, object],
    state: _PinnedPrivateState,
    claim_name: str,
) -> bool | None:
    """Classify one journal while every authority parent remains retained."""
    target_slug = str(journal["target"])
    kind = journal["kind"]
    before_hash = journal.get("before")
    after_hash = str(journal["after"])
    journal_format = journal.get("format", 1)
    live_exists = self._pinned_child_exists(state.auto, target_slug)

    if journal_format == 1:
        if kind == "new":
            return False if not live_exists else None
        if not live_exists:
            return None
        try:
            with self._pin_skill_child_parent(
                state.auto,
                target_slug,
                create=False,
            ) as live_parent:
                current_hash = self._lone_regular_file_hash_at(live_parent, "SKILL.md")
                if current_hash != before_hash:
                    return None
                snapshot_value = journal.get("snapshot")
                if not isinstance(snapshot_value, int):
                    return None
                try:
                    with self._pin_skill_child_parent(
                        live_parent,
                        VERSIONS_DIRNAME,
                        create=False,
                    ) as versions_parent:
                        orphan_name = f"v{snapshot_value}-SKILL.md"
                        orphan_exists = self._pinned_child_exists(
                            versions_parent,
                            orphan_name,
                        )
                        orphan_hash = (
                            self._lone_regular_file_hash_at(
                                versions_parent,
                                orphan_name,
                            )
                            if orphan_exists
                            else None
                        )
                        if orphan_hash == before_hash:
                            orphan_info = self._stat_pinned_child(
                                versions_parent,
                                orphan_name,
                            )
                            orphan_identity = self._pinned_child_identity(
                                versions_parent,
                                orphan_name,
                            )
                            if not self._unlink_skill_child(
                                versions_parent,
                                orphan_name,
                                expected=orphan_info,
                                expected_identity=orphan_identity,
                            ):
                                return None
                            self._sync_pinned_parent(versions_parent)
                        elif orphan_exists:
                            return None
                except FileNotFoundError:
                    pass
                return False
        except (FileNotFoundError, OSError):
            return None

    stage, backup = self._publication_paths(claim_name)
    stage_exists = self._pinned_child_exists(state.private, stage.name)
    backup_exists = self._pinned_child_exists(state.live_quarantine, backup.name)
    expected_backup_identity = self._publication_live_backup_identity(journal)
    current_backup_identity = (
        self._pinned_child_identity(state.live_quarantine, backup.name) if backup_exists else None
    )
    backup_matches = (
        expected_backup_identity is not None and current_backup_identity == expected_backup_identity
    )
    live_hash = self._skill_tree_hash_child(state.auto, target_slug) if live_exists else None
    stage_hash = self._skill_tree_hash_child(state.private, stage.name) if stage_exists else None
    backup_hash = (
        self._skill_tree_hash_child(state.live_quarantine, backup.name) if backup_matches else None
    )

    if kind == "new":
        if live_hash == after_hash and not backup_exists:
            return True
        if not live_exists and stage_exists and stage_hash == after_hash and not backup_exists:
            return False
        return None
    if not isinstance(before_hash, str) or expected_backup_identity is None:
        return None
    if live_hash == after_hash:
        return True if backup_matches else None

    if live_hash == before_hash:
        if stage_exists and stage_hash != after_hash:
            return None
        if backup_exists:
            return None
        return False

    if (
        backup_matches
        and backup_hash == before_hash
        and stage_exists
        and stage_hash == after_hash
        and not live_exists
    ):
        try:
            self._rename_skill_child_no_replace(
                state.live_quarantine,
                backup.name,
                state.auto,
                target_slug,
                expected_identity=expected_backup_identity,
            )
            self._sync_pinned_rename_parents(state.live_quarantine, state.auto)
        except OSError:
            return None
        return (
            False
            if (
                self._pinned_child_identity(state.auto, target_slug) == expected_backup_identity
                and self._skill_tree_hash_child(state.auto, target_slug) == before_hash
            )
            else None
        )
    return None


def _restore_failed_promotion_claim(
    self,
    claim: Path,
    claim_fd: int,
    slug: str,
    claim_snapshot: _ClaimSnapshot,
) -> bool:
    """Restore a proven rollback and report whether its lock may be removed."""
    quarantine = self._quarantine_root() / claim.name
    if not (
        os.path.lexists(claim)
        or os.path.lexists(quarantine)
        or is_link_or_junction(claim)
        or is_link_or_junction(quarantine)
    ):
        return True
    lock_path = self._claim_lock_path(claim.name)
    journal = self._authenticated_claim_publication(claim_fd, lock_path, claim.name)
    if journal is None:
        if not self._cleanup_publication_artifacts(claim.name):
            logger.error(
                "Could not clean failed skill publication artifacts; retaining %s",
                claim,
            )
            return False
        self._restore_claimed_update(claim, claim_fd, slug, claim_snapshot)
        return (
            self._authenticated_claim_evidence_state(
                claim_fd,
                lock_path,
                claim.name,
            )
            is None
            and not os.path.lexists(claim)
            and not os.path.lexists(quarantine)
        )
    published = self._reconcile_prepared_claim(claim, claim_fd, lock_path)
    if published is False:
        if not self._cleanup_publication_artifacts(claim.name):
            logger.error(
                "Prepared skill publication rolled back but artifact cleanup failed; "
                "retaining %s",
                claim,
            )
            return False
        if self._completion_marker_present(claim):
            if not self._remove_untrusted_completion_marker(claim):
                return False
        self._restore_claimed_update(claim, claim_fd, slug, claim_snapshot)
        return (
            self._authenticated_claim_evidence_state(
                claim_fd,
                lock_path,
                claim.name,
            )
            is None
            and not os.path.lexists(claim)
            and not os.path.lexists(quarantine)
        )
    if published is None:
        logger.error(
            "Prepared skill claim has ambiguous publication state; retaining %s",
            claim,
        )
        return False
    logger.info(
        "Prepared skill generation is fully live; retaining %s for restart commit",
        claim,
    )
    return False


def _recover_recorded_claim_restore(
    self,
    claim: Path,
    claim_fd: int,
    restore_state: tuple[str, _TaggedFileIdentity],
) -> bool:
    """Restore to the exact journaled pending slot or remain unresolved."""
    restore_slug, restore_identity = restore_state
    lock_state: list[_PinnedPrivateState] = []
    restored = False
    with self._file_lock("pending.lock", state_out=lock_state) as acquired:
        if not acquired or not lock_state:
            return False
        try:
            private_state = lock_state[0]
            pending_parent = private_state.pending
            if not self._pinned_parent_matches(pending_parent):
                return False
            try:
                current = self._stat_pinned_child(
                    private_state.quarantine,
                    claim.name,
                )
            except FileNotFoundError:
                pending_info = self._stat_pinned_child(
                    pending_parent,
                    restore_slug,
                )
                restored = (
                    stat.S_ISDIR(pending_info.st_mode)
                    and self._pinned_child_identity(pending_parent, restore_slug)
                    == restore_identity
                )
            else:
                current_identity = self._pinned_child_identity(
                    private_state.quarantine,
                    claim.name,
                )
                if not stat.S_ISDIR(current.st_mode) or current_identity != restore_identity:
                    return False
                try:
                    self._stat_pinned_child(pending_parent, restore_slug)
                except FileNotFoundError:
                    pass
                else:
                    return False
                self._rename_skill_child_no_replace(
                    private_state.quarantine,
                    claim.name,
                    pending_parent,
                    restore_slug,
                    expected_identity=restore_identity,
                )
                self._sync_pinned_rename_parents(
                    private_state.quarantine,
                    pending_parent,
                )
                pending_info = self._stat_pinned_child(
                    pending_parent,
                    restore_slug,
                )
                restored = (
                    stat.S_ISDIR(pending_info.st_mode)
                    and self._pinned_child_identity(pending_parent, restore_slug)
                    == restore_identity
                )
        except (FileNotFoundError, OSError, RuntimeError, ValueError):
            return False
    return restored and self._discard_trusted_claim(claim)


def _recover_abandoned_claims(self, *, roots_authenticated: bool = False) -> None:
    """Restore or retire claims whose owning process exited mid-transaction."""
    del roots_authenticated  # Each recovery pass acquires its own mutation authority.
    try:
        self._preflight_private_state(require_sensitive=True)
    except (OSError, RuntimeError, ValueError):
        return
    if not os.path.lexists(self._private_root()):
        return
    try:
        private_state = self._pin_private_state(
            create=True,
            require_sensitive=True,
        )
        state = private_state.__enter__()
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return
    root = self._claims_root()
    try:
        claim_names: set[str] = set()
        orphan_probes: list[str] = []
        consumed = 0

        def retain_active_names(
            parent: _PinnedSkillParent,
            *,
            label: str,
            retire_against_evidence: bool,
        ) -> None:
            nonlocal consumed
            if not _DIR_FD_SUPPORTED and not self._pinned_parent_matches(parent):
                raise OSError(f"{label} parent changed during recovery inspection")
            target: int | Path = parent.fd if _DIR_FD_SUPPORTED else parent.path
            with os.scandir(target) as entries:
                for entry in entries:
                    claim_name = entry.name
                    if retire_against_evidence and claim_name not in claim_names:
                        try:
                            self._stat_pinned_child(state.evidence, claim_name)
                        except FileNotFoundError:
                            pass
                        else:
                            # Retention moves private evidence last. A retired
                            # public name is history that only grows, so it is
                            # excluded before it counts toward the bound on the
                            # active namespace.
                            continue
                    if consumed >= _ACTIVE_CLAIM_SCAN_LIMIT:
                        raise _ActiveClaimScanOverflow(
                            "active claim namespace exceeded the "
                            f"{_ACTIVE_CLAIM_SCAN_LIMIT}-entry scan limit while reading "
                            f"{label}"
                        )
                    consumed += 1
                    if parent is state.claims and claim_name.startswith(_RENAME_PROBE_PREFIX):
                        # A probe's scratch file is never a claim. Collect a
                        # bounded number for removal once it is provably old.
                        if len(orphan_probes) < _ORPHANED_RENAME_PROBE_CLEANUP_LIMIT:
                            orphan_probes.append(claim_name)
                        continue
                    claim_names.add(claim_name)

        def remove_orphaned_probes() -> None:
            """Unlink rename probes a crashed process left under ``claims/``.

            A probe younger than ``_ORPHANED_RENAME_PROBE_MIN_AGE_S`` may belong
            to a claim in progress in another process and is left alone; the
            identity captured here must still hold at the unlink.
            """
            now = time.time()
            for probe_name in orphan_probes:
                try:
                    info = self._stat_pinned_child(state.claims, probe_name)
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_nlink != 1
                        or now - info.st_mtime < _ORPHANED_RENAME_PROBE_MIN_AGE_S
                    ):
                        continue
                    self._unlink_skill_child(
                        state.claims,
                        probe_name,
                        expected=info,
                        expected_identity=self._pinned_child_identity(
                            state.claims,
                            probe_name,
                        ),
                    )
                except OSError:
                    logger.debug("Could not remove orphaned rename probe", exc_info=True)

        try:
            retain_active_names(
                state.claims,
                label="active claims",
                retire_against_evidence=False,
            )
            retain_active_names(
                state.quarantine,
                label="public candidate quarantine",
                retire_against_evidence=True,
            )
            retain_active_names(
                state.live_quarantine,
                label="public live quarantine",
                retire_against_evidence=True,
            )
        except _ActiveClaimScanOverflow as exc:
            logger.warning("Could not recover abandoned skill claims: %s", exc)
            return
        except (OSError, RuntimeError, ValueError):
            logger.warning(
                "Could not recover abandoned skill claims: active claim namespace "
                "inspection was indeterminate",
                exc_info=True,
            )
            return
        finally:
            remove_orphaned_probes()
        for claim_name in sorted(claim_names):
            try:
                claim_info = self._stat_pinned_child(state.claims, claim_name)
            except FileNotFoundError:
                claim_info = None
            except OSError:
                continue
            try:
                quarantine_info = self._stat_pinned_child(
                    state.quarantine,
                    claim_name,
                )
            except FileNotFoundError:
                quarantine_info = None
            except OSError:
                continue
            try:
                live_quarantine_info = self._stat_pinned_child(
                    state.live_quarantine,
                    claim_name,
                )
            except FileNotFoundError:
                live_quarantine_info = None
            except OSError:
                continue
            if claim_info is None and quarantine_info is None and live_quarantine_info is None:
                continue
            claim = root / claim_name
            claim_linked = claim_info is None or (
                stat.S_ISLNK(claim_info.st_mode)
                or bool(getattr(claim_info, "st_reparse_tag", 0))
                or not stat.S_ISDIR(claim_info.st_mode)
            )
            quarantine_linked = quarantine_info is not None and (
                stat.S_ISLNK(quarantine_info.st_mode)
                or bool(getattr(quarantine_info, "st_reparse_tag", 0))
            )
            if quarantine_info is not None and (
                not quarantine_linked and not stat.S_ISDIR(quarantine_info.st_mode)
            ):
                continue
            if "--" not in claim_name:
                continue
            slug, _token = claim_name.rsplit("--", 1)
            if not self._is_pending_slug_safe(slug):
                continue
            lock_path = self._claim_lock_path(claim_name)
            try:
                lock_info = self._stat_pinned_child(
                    state.claim_locks,
                    lock_path.name,
                )
                if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
                    continue
                fd = self._open_skill_lock(
                    state.claim_locks,
                    lock_path.name,
                )
            except OSError:
                continue
            acquired = platform_compat.try_acquire_lock(fd, exclusive=True)
            cleanup_claim_lock = False
            try:
                if not acquired:
                    continue
                if (
                    self._authenticated_claim_evidence_state(
                        fd,
                        lock_path,
                        claim_name,
                    )
                    is not None
                ):
                    if not self._retain_claim_evidence(claim, fd):
                        logger.error("Could not retain recovered claim evidence %s", claim)
                    continue
                restore_state = self._authenticated_claim_restore_state(
                    fd,
                    lock_path,
                    claim_name,
                )
                if restore_state is not None:
                    restored = self._recover_recorded_claim_restore(
                        claim,
                        fd,
                        restore_state,
                    )
                    if not restored:
                        logger.error("Could not complete recorded claim restore %s", claim)
                    cleanup_claim_lock = (
                        restored
                        and not os.path.lexists(claim)
                        and not os.path.lexists(self._quarantine_root() / claim_name)
                    )
                    continue
                publication = (
                    None
                    if claim_linked
                    else self._authenticated_claim_publication(fd, lock_path, claim_name)
                )
                claim_snapshot = self._authenticated_claim_snapshot_state(
                    fd,
                    lock_path,
                    claim_name,
                )
                if claim_snapshot is None and publication is not None:
                    claim_snapshot = self._claim_snapshot_from_fields(publication)
                if claim_snapshot is None:
                    claim_snapshot = _ClaimSnapshot(None, None)
                retained_update = (
                    publication is not None
                    and publication.get("format") == 2
                    and publication.get("kind") == "update"
                )
                lock_completed = self._authenticated_claim_lock_state(
                    fd, lock_path, claim_name, completed=True
                )
                marker_completed = not claim_linked and self._authenticated_completion_marker(claim)
                if not claim_linked and (lock_completed or marker_completed):
                    retained = (
                        self._retain_published_update_evidence(claim, fd)
                        if retained_update
                        else self._retain_claim_evidence(claim, fd)
                    )
                    if not retained:
                        logger.error(
                            "Could not retain completed claim evidence %s",
                            claim,
                        )
                    continue

                prepared = publication is not None
                published = (
                    self._reconcile_prepared_claim(
                        claim,
                        fd,
                        lock_path,
                        private_state=state,
                    )
                    if prepared
                    else False
                )
                if published is True:
                    retained = (
                        self._retain_published_update_evidence(claim, fd)
                        if retained_update
                        else self._retain_claim_evidence(claim, fd)
                    )
                    if not retained:
                        logger.error(
                            "Could not retain recovered publication evidence %s",
                            claim,
                        )
                    continue
                if published is None:
                    logger.error(
                        "Prepared skill claim has ambiguous publication state; retaining %s",
                        claim,
                    )
                    continue
                if not self._cleanup_publication_artifacts(claim_name):
                    logger.error(
                        "Could not clean rolled-back publication artifacts; retaining %s",
                        claim,
                    )
                    continue
                if not claim_linked and self._completion_marker_present(claim):
                    if not self._remove_untrusted_completion_marker(claim):
                        continue
                self._restore_claimed_update(claim, fd, slug, claim_snapshot)
                cleanup_claim_lock = (
                    self._authenticated_claim_evidence_state(
                        fd,
                        lock_path,
                        claim_name,
                    )
                    is None
                    and not os.path.lexists(claim)
                    and not os.path.lexists(self._quarantine_root() / claim_name)
                )
            finally:
                if acquired:
                    platform_compat.release_lock(fd)
                os.close(fd)
                if acquired and cleanup_claim_lock:
                    self._cleanup_claim_lock(claim_name)
    finally:
        private_state.__exit__(None, None, None)
