"""Promoting a claimed candidate: the update approval that publishes a complete live tree
under the claim and target locks, the unattended application of a prose-only update,
the new-skill promotion, and the checked dismissal that names an authority refusal.

Each function here whose first parameter is ``self`` is a ``SkillsLoader`` method: the class
binds it by name.

Instance fields written: ``_fm_cache``.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.atomic_write import atomic_write
from kiro_crew.sel import sel
from kiro_crew.skills_auto_authority import (
    _auto_skill_authority_retire_hint,
    _auto_skill_promotion_ruled_out,
    _auto_skill_sandbox_excludes_every_promoter,
    auto_skill_promotion_disabled_reason,
)
from kiro_crew.skills_auto_model import (
    AUTO_SKILL_NAMESPACE,
    VERSIONS_DIRNAME,
    PendingDismissalRefused,
    _ClaimSnapshot,
    _emit_pending_consumed,
    _emit_update_auto_applied,
    _PinnedPrivateState,
    _ValidatedCandidateSnapshot,
    canonical_skill_text_hash,
)
from kiro_crew.skills_review import PendingApprovalRefused, _note_refusal

#: The facade's logger, so a filter or a capture on ``kiro_crew.skills`` sees these records.
logger = logging.getLogger("kiro_crew.skills")


def _approve_claimed_update_locked(
    self,
    src: Path,
    *,
    claim_fd: int,
    slug: str,
    meta: dict[str, object],
    snapshot: _ValidatedCandidateSnapshot,
    refuse_scripts: bool,
    expected_candidate_binding: str | None,
    private_state: _PinnedPrivateState,
    refusal: list[PendingApprovalRefused] | None = None,
) -> tuple[str, int] | None:
    """Promote only the whole generation authenticated at the claim rename."""
    bound_raw = snapshot.source_files.get(Path("SKILL.md"))
    if expected_candidate_binding is not None:
        if bound_raw is None:
            logger.warning("Refusing unattended promotion of %s: body is unreadable", slug)
            return None
        try:
            actual_binding = self._auto_apply_candidate_binding(
                bound_raw,
                target=meta.get("target"),
                base_version=meta.get("base_version"),
                base_content_hash=meta.get("base_content_hash"),
            )
        except (OSError, TypeError, ValueError):
            logger.warning("Refusing unattended promotion of %s: binding is unreadable", slug)
            return None
        if not secrets.compare_digest(actual_binding, expected_candidate_binding):
            logger.warning("Refusing unattended promotion of %s: candidate changed", slug)
            return None
    snapshot_has_scripts = any(
        relative.parts and relative.parts[0] == "scripts" for relative in snapshot.files
    )
    if refuse_scripts and (meta.get("has_scripts") is True or snapshot_has_scripts):
        logger.info("Refusing unattended promotion of %s: scripts require review", slug)
        return None
    target = meta.get("target")
    if not isinstance(target, str) or not target:
        return None
    target_slug = self._auto_slug_from_name(target)
    live_dir = private_state.auto.path / target_slug
    live_skill = live_dir / "SKILL.md"
    if not self._pinned_child_exists(private_state.auto, target_slug):
        logger.warning(
            "Refusing to approve update %s: target %r is not a live auto skill", slug, target
        )
        _note_refusal(refusal, PendingApprovalRefused("target_missing"))
        return None
    target_name = f"{AUTO_SKILL_NAMESPACE}/{target_slug}"
    self._fm_cache.pop(str(live_skill), None)
    live_snapshot = self._skill_tree_snapshot_child(private_state.auto, target_slug)
    if live_snapshot is None:
        logger.warning(
            "Refusing to approve update %s: live skill directory is unsafe",
            target_name,
        )
        _note_refusal(refusal, PendingApprovalRefused("invalid_layout"))
        return None
    try:
        live_prev = live_snapshot.files[Path("SKILL.md")].decode("utf-8")
    except (KeyError, UnicodeDecodeError):
        logger.warning("Refusing to approve update %s: live body is unreadable", target_name)
        return None
    if expected_candidate_binding is not None:
        expected_hash = meta.get("base_content_hash")
        if not isinstance(expected_hash, str) or not expected_hash:
            logger.warning(
                "Refusing unattended promotion of %s: live-content hash is missing",
                target_name,
            )
            return None
        actual_hash = canonical_skill_text_hash(live_prev)
        if not secrets.compare_digest(expected_hash, actual_hash):
            logger.warning(
                "Refusing unattended promotion of %s: live skill changed after staging",
                target_name,
            )
            return None
    try:
        candidate_body = snapshot.files[Path("SKILL.md")].decode("utf-8")
    except (KeyError, UnicodeDecodeError):
        return None
    before_hash = live_snapshot.generation_hash
    live_frontmatter = self._parse_frontmatter_text(live_prev)
    try:
        parsed_version = int(live_frontmatter.get("version", ""))
    except (TypeError, ValueError):
        parsed_version = 1
    current_version = parsed_version if parsed_version >= 1 else 1
    # Snapshot under a number that is guaranteed free, so an earlier snapshot
    # can never be destroyed by drifted numbering.
    versions_dir = self._versions_root(target_slug)
    snapshot_version = self._resolve_snapshot_version(versions_dir, current_version, live_snapshot)
    new_version = snapshot_version + 1
    # ``base_version`` records the live version the merge was computed
    # against. If the live skill advanced since staging, this candidate's body
    # was merged from an OLDER base, so writing it would replace whatever the
    # intervening approval added. REFUSE rather than warn.
    raw_base = meta.get("base_version")
    if isinstance(raw_base, int) and raw_base != current_version:
        logger.warning(
            "Refusing to approve stale update for %s: candidate based on v%s, live is v%d",
            target_name,
            raw_base,
            current_version,
        )
        sel().log_tool_invocation(
            session_key="skills",
            tool_name="auto_skill_update_approve",
            tool_kind="permission",
            outcome="rejected",
            metadata={
                "target": target_name,
                "base_version": raw_base,
                "live_version": current_version,
                "reason": "stale_base",
            },
        )
        _note_refusal(refusal, PendingApprovalRefused("stale_base"))
        return None
    live_created_at = live_frontmatter.get("created_at", "")
    live_pinned = str(live_frontmatter.get("pinned", "")).strip().lower() in (
        "true",
        "1",
        "yes",
    )
    live_pointer_only = (
        str(live_frontmatter.get("inject_on_trigger", "")).strip().lower() == "false"
    )
    new_live_content = self._rewrite_update_frontmatter(
        candidate_body,
        target_name=target_name,
        created_at=live_created_at,
        version=new_version,
        pinned=live_pinned,
        pointer_only=live_pointer_only,
    )

    stage, backup = self._publication_paths(src.name)
    if (
        stage.parent != private_state.private.path
        or backup.parent != private_state.live_quarantine.path
    ):
        return None
    if self._pinned_child_exists(
        private_state.private,
        stage.name,
    ) or self._pinned_child_exists(private_state.live_quarantine, backup.name):
        logger.error("Refusing to reuse skill publication artifacts for %s", target_name)
        return None
    script_modes = snapshot.modes
    script_items = [
        (relative, payload)
        for relative, payload in snapshot.files.items()
        if relative.parts and relative.parts[0] == "scripts"
    ]
    try:
        self._materialize_skill_tree_snapshot(live_snapshot, stage)
        staged_skill = stage / "SKILL.md"
        with staged_skill.open("wb") as handle:
            handle.write(new_live_content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())

        staged_versions = stage / VERSIONS_DIRNAME
        staged_versions.mkdir(parents=True, exist_ok=True)
        version_snapshot = staged_versions / f"v{snapshot_version}-SKILL.md"
        atomic_write(version_snapshot, live_prev.encode("utf-8"), fsync=True)

        if not refuse_scripts and script_items:
            staged_scripts = stage / "scripts"
            staged_scripts.mkdir(parents=True, exist_ok=True)
            for relative, payload in sorted(script_items, key=lambda item: str(item[0])):
                script_relative = relative.relative_to("scripts")
                destination = staged_scripts / script_relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                mode = script_modes.get(relative, 0o600) | 0o111
                atomic_write(destination, payload, mode=mode, fsync=True)
                platform_compat.chmod_safe(destination, mode)

        self._prune_versions(staged_versions)
        self._sync_skill_tree(stage)
        after_hash = self._skill_tree_hash_child(private_state.private, stage.name)
        if (
            after_hash is None
            or self._skill_tree_hash_child(private_state.auto, target_slug) != before_hash
        ):
            raise OSError("live or staged skill generation changed during preparation")
        if not self._prepare_claim_publication(
            claim_fd,
            self._claim_lock_path(src.name),
            src.name,
            kind="update",
            target_slug=target_slug,
            before_hash=before_hash,
            after_hash=after_hash,
            claim_snapshot=_ClaimSnapshot(
                snapshot.generation_hash,
                snapshot.source_files.get(Path(".meta.json")),
            ),
            live_backup_identity=live_snapshot.root_identity,
            snapshot_version=snapshot_version,
            new_version=new_version,
        ):
            raise OSError("claim journal failed")
        publication = self._publish_prepared_skill_tree(
            state=private_state,
            live_dir=live_dir,
            stage=stage,
            backup=backup,
            backup_identity=live_snapshot.root_identity,
            before_hash=before_hash,
            after_hash=after_hash,
        )
        if publication != "published":
            if publication == "drift":
                # The captured concurrent generation is live again and the
                # prepared after-tree never committed. Return the journal to
                # its active claim state so normal refusal recovery can put
                # the candidate back in the public review queue.
                prepared_state = self._authenticated_claim_publication(
                    claim_fd,
                    self._claim_lock_path(src.name),
                    src.name,
                )
                prepared_snapshot = (
                    self._claim_snapshot_from_fields(prepared_state)
                    if prepared_state is not None
                    else None
                )
                active_state = self._initialize_claim_lock_state(
                    claim_fd,
                    self._claim_lock_path(src.name),
                    src.name,
                ) and self._write_claim_snapshot_state(
                    claim_fd,
                    self._claim_lock_path(src.name),
                    src.name,
                    _ClaimSnapshot(
                        snapshot.generation_hash,
                        snapshot.source_files.get(Path(".meta.json")),
                        quarantine_identity=(
                            prepared_snapshot.quarantine_identity
                            if prepared_snapshot is not None
                            else None
                        ),
                    ),
                )
                if active_state:
                    self._cleanup_publication_artifacts(src.name)
                else:
                    logger.error(
                        "Retaining %s after live drift because claim rollback was not durable",
                        src,
                    )
            logger.warning(
                "Refusing to approve update %s: whole-tree publication did not complete",
                target_name,
            )
            return None
    except OSError:
        # Before the journal exists the stage is disposable. Once prepared,
        # the claim and generation artifacts are recovery state and must not
        # be guessed away here.
        if (
            self._authenticated_claim_publication(
                claim_fd, self._claim_lock_path(src.name), src.name
            )
            is None
        ):
            self._cleanup_publication_artifacts(src.name)
        logger.warning(
            "Refusing to approve update %s: could not prepare whole skill generation",
            target_name,
        )
        return None
    # (i) Audit the approved update.
    sel().log_tool_invocation(
        session_key="skills",
        tool_name="auto_skill_update_approve",
        tool_kind="permission",
        outcome="invoked",
        metadata={
            "target": target_name,
            "from_version": current_version,
            "to_version": new_version,
            "base_version": raw_base,
            "stale_base": False,
        },
    )
    self._invalidate_iter_cache()
    logger.info(
        "Approved pending update: %s (v%d -> v%d)", target_name, current_version, new_version
    )
    return target_name, new_version


def auto_apply_pending_update(
    self,
    slug: str,
    *,
    expected_candidate_binding: str,
    recovery_pending_out: list[tuple[str, int]] | None = None,
) -> tuple[str, int] | None:
    """Promote a prose-only update when approval is disabled."""
    claimed: list[bool] = []
    committed: list[tuple[str, int]] = []
    applied = self._promote_pending_update(
        slug,
        refuse_scripts=True,
        expected_candidate_binding=expected_candidate_binding,
        claimed_out=claimed,
        committed_out=committed,
    )
    if applied is None:
        if committed:
            if recovery_pending_out is not None:
                recovery_pending_out[:] = committed
            name, version = committed[0]
            _emit_update_auto_applied(
                {
                    "name": name,
                    "slug": slug,
                    "target": name,
                    "new_version": version,
                    "description": "",
                    "recovery_pending": True,
                }
            )
            return None
        if not claimed:
            # Auto-apply is the only caller that suppresses the initial
            # staged event. Capture the still-public generation once; a
            # claimed refusal already emitted from its immutable metadata.
            self.emit_pending_staged(slug)
        return None
    name, version = applied
    _emit_update_auto_applied(
        {
            "name": name,
            "slug": slug,
            "target": name,
            "new_version": version,
            # Deliberately no description: the only source would be the
            # PUBLIC pending metadata read before the claim, which an
            # attacker-writable sibling can rewrite so the notification
            # describes different bytes than were promoted. The name,
            # target and version above are computed from the claimed
            # snapshot under the target lock.
            "description": "",
        }
    )
    return applied


def _promote_pending_skill(
    self,
    slug: str,
    *,
    refusal: list[PendingApprovalRefused] | None = None,
) -> str | None:
    """Claim one NEW candidate and publish its immutable snapshot live."""
    claimed = self._claim_pending_update(slug)
    if claimed is None:
        return None
    src, claim_fd, consumed_at, claim_snapshot = claimed
    result: str | None = None
    live_published = False
    try:
        with self._promotion_lock(slug) as acquired:
            if not acquired:
                logger.warning("Could not acquire promotion lock for auto/%s", slug)
                return None
            try:
                with self._pin_private_state(
                    create=False,
                    require_sensitive=True,
                ) as private_state:
                    result = self._approve_claimed_skill_locked(
                        src,
                        claim_fd,
                        slug,
                        claim_snapshot,
                        private_state,
                        refusal=refusal,
                    )
                    if result is None:
                        return None
                    live_published = True
                    if not self._commit_claim_consumption(src, claim_fd):
                        result = None
                        return None
                    if not self._pinned_parent_matches(
                        private_state.auto
                    ) or not self._pinned_parent_matches(private_state.private):
                        result = None
                        return None
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                logger.warning("Promotion authority became unavailable for auto/%s", slug)
                return None
        _emit_pending_consumed(
            {"slug": slug, "outcome": "approved", "name": result, "consumed_at": consumed_at}
        )
        return result
    finally:
        cleanup_claim_lock = True
        try:
            if result is None and not live_published:
                cleanup_claim_lock = self._restore_failed_promotion_claim(
                    src,
                    claim_fd,
                    slug,
                    claim_snapshot,
                )
        except OSError:
            cleanup_claim_lock = False
            logger.error("Could not restore claimed skill %s", src, exc_info=True)
        finally:
            platform_compat.release_lock(claim_fd)
            os.close(claim_fd)
            if cleanup_claim_lock:
                self._cleanup_claim_lock(src.name)


def dismiss_pending_skill_checked(self, slug: str) -> bool:
    """Dismiss like :meth:`dismiss_pending_skill`, naming an authority refusal.

    Returns ``True`` once the candidate is consumed and ``False`` when it is
    absent or its dismissal failed for another reason. Raises
    ``PendingDismissalRefused`` when the candidate is still staged, by-name
    dismissal is not ruled in, and this process cannot take the claim because
    it holds no authority certificate: the case a dashboard would otherwise
    report as "not found" for a candidate the operator can still see. The
    classification runs only after the dismissal returned ``False``, so it
    can change which refusal is reported and never what is removed.
    """
    if self.dismiss_pending_skill(slug):
        return True
    if not self._is_pending_slug_safe(slug) or _auto_skill_promotion_ruled_out():
        return False
    disabled = auto_skill_promotion_disabled_reason()
    if disabled is None or not self.pending_candidate_is_staged(slug):
        return False
    hint = (
        _auto_skill_authority_retire_hint()
        if _auto_skill_sandbox_excludes_every_promoter()
        else None
    )
    raise PendingDismissalRefused("promotion_disabled", detail=disabled, hint=hint)
