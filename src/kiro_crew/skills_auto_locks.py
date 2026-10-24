"""Auto-skill locking and the claim-lock journal: the lock files in their one documented
order (:data:`kiro_crew.skills_auto_model.AUTO_SKILL_LOCK_ORDER`), the per-target lock
every live mutation takes and the refusal it reports when that lock is refused, the
active-claim scan staging consults, and the authenticated state each claim lock records
while its transaction runs: the captured generation, the prepared publication, the
restore target, the retained evidence and the completion marker.

Each function here whose first parameter is ``self`` is a ``SkillsLoader`` method: the class
binds it by name.

Instance fields read: ``_dir``.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import os
import re
import stat
import time
import unicodedata
from contextlib import suppress
from pathlib import Path, PurePosixPath
from typing import Iterator, cast

from kiro_crew import platform_compat
from kiro_crew.platform_compat import is_link_or_junction
from kiro_crew.sel import sel
from kiro_crew.skills_auto_authority import (
    _auto_skill_authority_masked_view_reason,
    _auto_skill_authority_retire_hint,
    _auto_skill_authority_root_exists,
    _auto_skill_promotion_ruled_out,
    _auto_skill_sandbox_excludes_every_promoter,
    auto_skill_promotion_disabled_reason,
)
from kiro_crew.skills_auto_model import (
    _ACTIVE_CLAIM_SCAN_LIMIT,
    _AUTO_NAME_PATTERN,
    _CLAIM_LOCK_MAX_STATE_BYTES,
    _PROMOTE_LOCK_POLL_S,
    _PROMOTE_LOCK_TIMEOUT_S,
    AUTO_SKILL_NAMESPACE,
    LiveSkillMutationRefused,
    _ActiveClaimScanOverflow,
    _ClaimSnapshot,
    _PinnedPrivateState,
    _PinnedSkillParent,
    _TaggedFileIdentity,
)
from kiro_crew.skills_auto_pinning import (
    _DIR_FD_SUPPORTED,
    _identity_from_payload,
    _identity_payload,
)

#: The facade's logger, so a filter or a capture on ``kiro_crew.skills`` sees these records.
logger = logging.getLogger("kiro_crew.skills")


def _canonical_skill_name(name: str) -> str:
    """The spelling of a safe skill name with its ``.`` components collapsed.

    ``PurePosixPath`` drops ``.`` components, so ``./auto`` reads ``auto`` and
    ``auto/./x`` reads ``auto/x``: the directory ``loader._dir / name`` opens under
    either spelling. Every reserved-namespace decision classifies this spelling,
    never the raw one. Only called after ``_safe_name`` accepted *name*, so ``..``
    and rooted spellings never reach it.
    """
    return PurePosixPath(name).as_posix()


def delete_skill_checked(self, name: str) -> bool:
    """Delete like :meth:`delete_skill`, naming an authority refusal.

    Returns ``True`` once the skill is removed and ``False`` when it is absent
    or its deletion failed for another reason. Raises
    ``LiveSkillMutationRefused`` when :meth:`_live_auto_mutation_refusal`
    names why the target lock was refused: the case a dashboard would
    otherwise report as "not found" for a skill the operator can still see.
    """
    if self.delete_skill(name):
        return True
    refusal = self._live_auto_mutation_refusal(name)
    if refusal is not None:
        raise refusal
    return False


def _live_auto_mutation_refusal(self, name: str) -> LiveSkillMutationRefused | None:
    """The authority refusal behind a failed live ``auto/`` mutation, or ``None``.

    Read only after the mutation returned ``False``, so it can change which
    refusal is reported and never what is mutated. It follows the lock
    decision in :meth:`_live_auto_mutation_lock`: only an ``auto/`` name with
    a canonical promotion target takes the authority-backed target lock, and
    that lock is refused for want of a certificate only while by-name
    mutation is not ruled in and this process reports a disabled reason. A
    skill that is gone stays ``None``, so it still reads as not found.
    """
    canonical = _canonical_skill_name(name)
    if not self.is_auto_generated(canonical):
        return None
    target_slug = self._auto_slug_from_name(canonical)
    if (
        not self._is_pending_slug_safe(target_slug.split("/", 1)[0])
        or self._live_auto_target_lock_slug(target_slug) is None
        or _auto_skill_promotion_ruled_out()
    ):
        return None
    disabled = auto_skill_promotion_disabled_reason()
    if disabled is None or not (self._dir / name).is_dir():
        return None
    hint = (
        _auto_skill_authority_retire_hint()
        if _auto_skill_sandbox_excludes_every_promoter()
        else None
    )
    return LiveSkillMutationRefused("promotion_disabled", detail=disabled, hint=hint)


def set_pinned_checked(self, name: str, pinned: bool) -> bool:
    """Pin like :meth:`set_pinned`, raising ``LiveSkillMutationRefused`` the way
    :meth:`delete_skill_checked` does instead of returning ``False``."""
    if self.set_pinned(name, pinned):
        return True
    refusal = self._live_auto_mutation_refusal(name)
    if refusal is not None:
        raise refusal
    return False


@contextlib.contextmanager
def _file_lock(
    self,
    name: str,
    *,
    state_out: list[_PinnedPrivateState] | None = None,
) -> Iterator[bool]:
    """Yield whether a bounded cross-process advisory lock was acquired.

    ``state_out`` is an operation-local handoff of the exact hierarchy this
    lock keeps pinned. It is populated only while the context is active; a
    caller that needs a public namespace parent must perform all work before
    leaving the ``with`` block and must never retain the object afterward.
    """
    try:
        private_state = self._pin_private_state(
            create=True,
            require_sensitive=True,
        )
        state = private_state.__enter__()
    except (OSError, RuntimeError, ValueError):
        logger.warning("Could not authenticate skill lock parent for %s", name)
        yield False
        return
    fd: int | None = None
    acquired = False
    try:
        try:
            fd = cast(int, self._open_skill_lock(state.locks, name))
        except OSError:
            logger.warning("Could not open skill lock %s", state.locks.path / name)
            yield False
            return
        deadline = time.monotonic() + _PROMOTE_LOCK_TIMEOUT_S
        while True:
            if platform_compat.try_acquire_lock(fd, exclusive=True):
                acquired = True
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(_PROMOTE_LOCK_POLL_S)
        if acquired and state_out is not None:
            state_out[:] = [state]
        yield acquired
    finally:
        if acquired and fd is not None:
            platform_compat.release_lock(fd)
        if fd is not None:
            with suppress(OSError):
                os.close(fd)
        private_state.__exit__(None, None, None)


@contextlib.contextmanager
def _promotion_lock(self, target_slug: str) -> Iterator[bool]:
    """Serialize promotions to one live auto-skill across processes.

    Refuses a non-canonical slug instead of locking it: the lock file is
    NAMED by the slug while the live directory is RESOLVED by the
    filesystem, and the two disagree on aliases. On a case-insensitive
    filesystem ``Foo`` opens ``auto/foo`` but locks ``target-Foo.lock``;
    Win32 strips trailing dots/spaces from path components, so ``foo.``
    opens ``auto/foo`` while locking ``target-foo..lock``. Either way two
    writers hold different locks over one directory and updates are lost.
    Every product-created slug already matches ``_AUTO_NAME_PATTERN`` (all
    creation paths enforce it), so canonical callers are unaffected and an
    alias fails closed here. A live mutation never reaches this refusal with
    an alias: :meth:`_live_auto_mutation_lock` folds an alias onto its
    canonical slug first, and mutates a name no promotion can target by name.
    """
    if not _AUTO_NAME_PATTERN.fullmatch(target_slug):
        logger.warning("Refusing promotion lock for non-canonical slug: %r", target_slug)
        yield False
        return
    with self._file_lock(f"target-{target_slug}.lock") as acquired:
        yield acquired


def _audit_reserved_auto_mutation_denial(name: str) -> None:
    """Record a reserved-namespace mutation refusal without changing its verdict."""
    try:
        sel().log_tool_invocation(
            session_key="skills",
            tool_name="skill_mutation",
            tool_kind="permission",
            outcome="denied",
            metadata={
                "target": name,
                "reason": "reserved_auto_namespace",
            },
        )
    except Exception:  # noqa: BLE001 — audit failure cannot allow the mutation
        logger.warning("Could not audit reserved auto-skill mutation denial", exc_info=True)


def _live_auto_target_lock_slug(slug: str) -> str | None:
    """The canonical promotion target a live ``auto/`` name must serialize with.

    A promotion only ever targets a slug matching ``_AUTO_NAME_PATTERN``, and
    its lock is NAMED by that slug. A live name can still reach that
    directory under another spelling: a case-insensitive or case-folding
    filesystem resolves ``Foo`` to ``foo``, Win32 strips trailing dots and
    spaces so ``foo.`` opens ``foo``, and a nested ``foo/bar`` lives inside
    ``foo``. Each of those takes the canonical slug's lock. The fold (NFKC,
    then case folding, then trailing dots and spaces) is wider than any one
    filesystem's alias rule, which can only add serialization, never remove it.

    A name with no canonical fold is one promotion can never reach under any
    spelling, so ``None`` is returned and the caller mutates it by name as
    before the claim protocol: nothing can race it. Refusing it instead would
    let an agent plant a skill the operator cannot delete or disable (an
    injected ``auto/Persist_Me`` carrying ``always: true``).
    """
    head = slug.split("/", 1)[0]
    folded = unicodedata.normalize("NFKC", head).casefold().rstrip(" .")
    return folded if _AUTO_NAME_PATTERN.fullmatch(folded) else None


@contextlib.contextmanager
def _live_auto_mutation_lock(self, name: str) -> Iterator[bool]:
    """Serialize a live auto-skill mutation with candidate promotion.

    A name promotion can target, under any alias spelling, takes that
    target's canonical lock; any other ``auto/`` name is mutated by name
    (:meth:`_live_auto_target_lock_slug`). The bare namespace and its
    dot-prefixed internals (``.pending``, the quarantines) are refused: they
    are never a loadable skill, and a by-name mutation there would bypass the
    claim protocol.
    """
    # ``_safe_name`` accepts ``./auto`` and ``./auto/.pending``: pathlib drops
    # a ``.`` component on join, so both open the directories their canonical
    # spellings name, while the literal ``auto/`` prefix check and the
    # namespace read below would see ``.``. Classify the canonical spelling.
    canonical = _canonical_skill_name(name)
    if not self.is_auto_generated(canonical):
        namespace, _separator, _slug = canonical.partition("/")
        # Case-insensitive filesystems resolve e.g. ``AUTO/x`` to the same
        # directory as ``auto/x``, and Win32 strips trailing dots/spaces
        # from path components, so ``auto.`` (or ``auto ``) opens the
        # ``auto`` directory too.  The bare reserved namespace — in any of
        # those alias spellings — would otherwise take the lock-free
        # manual-skill branch and let a delete remove every live, pending,
        # and private auto-skill entry.
        if namespace.rstrip(" .").casefold() == AUTO_SKILL_NAMESPACE.casefold():
            self._audit_reserved_auto_mutation_denial(name)
            logger.warning("Refusing reserved auto-skill path: %s", name)
            yield False
            return
        yield True
        return
    target_slug = self._auto_slug_from_name(canonical)
    head = target_slug.split("/", 1)[0]
    if not self._is_pending_slug_safe(head):
        yield False
        return
    lock_slug = self._live_auto_target_lock_slug(target_slug)
    if lock_slug is None:
        yield True
        return
    with self._promotion_lock(lock_slug) as acquired:
        if not acquired and _auto_skill_promotion_ruled_out():
            # Root absence or the re-proven no-replace refusal means no
            # authority-backed promotion can race this by-name mutation.
            yield True
            return
        if not acquired:
            masked_view = _auto_skill_authority_masked_view_reason()
            if masked_view is not None:
                logger.warning(
                    "Auto-skill mutation of %s requires the authority target lock, " "but %s",
                    name,
                    masked_view,
                )
            elif _auto_skill_authority_root_exists():
                disabled = auto_skill_promotion_disabled_reason()
                if disabled is not None and _auto_skill_sandbox_excludes_every_promoter():
                    logger.warning(
                        "Auto-skill mutation of %s requires the authority target lock, "
                        "but this process cannot authenticate it (%s). %s",
                        name,
                        disabled,
                        _auto_skill_authority_retire_hint(),
                    )
        yield acquired


def _quarantine_is_consumed_evidence(
    self,
    state: _PinnedPrivateState,
    claim_name: str,
) -> bool:
    """Whether one public quarantine has complete authenticated evidence."""
    try:
        # Evidence-state publication precedes the private claim-to-evidence
        # rename. While the trusted claim still exists, the lifecycle is
        # active or interrupted even if a colliding evidence name exists.
        try:
            self._stat_pinned_child(state.claims, claim_name)
        except FileNotFoundError:
            pass
        else:
            return False

        evidence_info = self._stat_pinned_child(state.evidence, claim_name)
        quarantine_info = self._stat_pinned_child(state.quarantine, claim_name)
        lock_name = self._claim_lock_path(claim_name).name
        lock_info = self._stat_pinned_child(state.claim_locks, lock_name)
        if (
            not stat.S_ISDIR(evidence_info.st_mode)
            or not stat.S_ISDIR(quarantine_info.st_mode)
            or not stat.S_ISREG(lock_info.st_mode)
            or lock_info.st_nlink != 1
            or is_link_or_junction(self._evidence_root() / claim_name)
            or is_link_or_junction(self._quarantine_root() / claim_name)
        ):
            return False
        fd = self._open_skill_lock(state.claim_locks, lock_name)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return False
    acquired = False
    try:
        acquired = platform_compat.try_acquire_lock(fd, exclusive=True)
        if not acquired:
            return False
        evidence_state = self._authenticated_claim_evidence_state(
            fd,
            self._claim_lock_path(claim_name),
            claim_name,
        )
        return evidence_state is not None and self._public_claim_evidence_matches(
            state,
            evidence_state,
            claim_name,
        )
    finally:
        if acquired:
            platform_compat.release_lock(fd)
        os.close(fd)


def _pending_slug_claimed(
    self,
    slug: str,
    *,
    private_state: _PinnedPrivateState | None = None,
) -> bool | None:
    """Whether an active claim owns *slug*, or ``None`` if inspection failed.

    ``None`` is deliberately not false: a transient I/O failure cannot prove
    the slug free. Staging aborts and retries rather than reusing a live
    claim's destination. Each directory scan counts at most
    ``_ACTIVE_CLAIM_SCAN_LIMIT`` unconsumed names and retains only the current
    slug-matching name. A public quarantine name whose claim already retired
    into private ``evidence/`` is history: every promotion, dismissal and TTL
    prune leaves one forever, so it is skipped before it is counted. Only a
    certified promoter writes ``evidence/``, so the uncounted names are
    bounded by real claim history, never by what an agent plants.
    """

    def retired_history(state: _PinnedPrivateState, claim_name: str) -> bool:
        try:
            self._stat_pinned_child(state.claims, claim_name)
        except FileNotFoundError:
            pass
        else:
            return False
        try:
            self._stat_pinned_child(state.evidence, claim_name)
        except FileNotFoundError:
            return False
        return True

    def matching_claim(
        state: _PinnedPrivateState,
        parent: _PinnedSkillParent,
        *,
        label: str,
        require_unconsumed_quarantine: bool = False,
    ) -> bool:
        if not _DIR_FD_SUPPORTED and not self._pinned_parent_matches(parent):
            raise OSError(f"{label} parent changed during active-claim inspection")
        target: int | Path = parent.fd if _DIR_FD_SUPPORTED else parent.path
        counted = 0
        with os.scandir(target) as entries:
            for entry in entries:
                claim_name = entry.name
                if not (require_unconsumed_quarantine and retired_history(state, claim_name)):
                    if counted >= _ACTIVE_CLAIM_SCAN_LIMIT:
                        raise _ActiveClaimScanOverflow(
                            f"{label} exceeded the {_ACTIVE_CLAIM_SCAN_LIMIT}-entry "
                            "active-claim scan limit"
                        )
                    counted += 1
                if claim_name.rsplit("--", 1)[0] != slug:
                    continue
                if require_unconsumed_quarantine and self._quarantine_is_consumed_evidence(
                    state,
                    claim_name,
                ):
                    continue
                return True
        return False

    try:
        state_context = (
            contextlib.nullcontext(private_state)
            if private_state is not None
            else self._pin_private_state(create=False)
        )
        with state_context as state:
            if matching_claim(state, state.claims, label="active claims"):
                return True
            if matching_claim(
                state,
                state.quarantine,
                label="public candidate quarantine",
                require_unconsumed_quarantine=True,
            ):
                return True
            return False
    except FileNotFoundError:
        return False
    except _ActiveClaimScanOverflow as exc:
        logger.warning(
            "Could not determine whether pending slug %s has an active claim: %s",
            slug,
            exc,
        )
        return None
    except (OSError, RuntimeError, ValueError):
        logger.warning("Could not determine whether pending slug %s has an active claim", slug)
        return None


def _claim_lock_path(self, claim_name: str) -> Path:
    return self._locks_root() / "claims" / f"{claim_name}.lock"


def _claim_lock_state_payload(claim_name: str, *, completed: bool) -> bytes:
    return (("C" if completed else "A") + claim_name + "\n").encode("utf-8")


def _read_authenticated_claim_lock_payload(
    cls, fd: int, lock_path: Path, claim_name: str
) -> bytes | None:
    """Read a bounded payload only from the held lock file's authenticated inode."""
    if is_link_or_junction(lock_path):
        return None
    try:
        linked = os.lstat(lock_path)
        opened = os.fstat(fd)
        if (
            not stat.S_ISREG(linked.st_mode)
            or not stat.S_ISREG(opened.st_mode)
            or linked.st_nlink != 1
            or opened.st_nlink != 1
            or not cls._opened_path_matches(fd, lock_path, linked)
            or opened.st_size > _CLAIM_LOCK_MAX_STATE_BYTES
        ):
            return None
        os.lseek(fd, 0, os.SEEK_SET)
        payload = os.read(fd, _CLAIM_LOCK_MAX_STATE_BYTES + 1)
    except OSError:
        return None
    if len(payload) != opened.st_size:
        return None
    return payload


def _authenticated_claim_lock_state(
    cls,
    fd: int,
    lock_path: Path,
    claim_name: str,
    *,
    completed: bool,
) -> bool:
    """Authenticate a fixed-size active/completed record in a held claim lock."""
    payload = cls._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
    return payload == cls._claim_lock_state_payload(claim_name, completed=completed)


def _write_claim_lock_payload(
    cls,
    fd: int,
    lock_path: Path,
    claim_name: str,
    payload: bytes,
) -> bool:
    """Durably replace the held claim lock's authenticated bounded payload."""
    if not payload or len(payload) > _CLAIM_LOCK_MAX_STATE_BYTES:
        return False
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        remaining = memoryview(payload)
        while remaining:
            written = os.write(fd, remaining)
            if written <= 0:
                raise OSError("short claim-lock state write")
            remaining = remaining[written:]
        os.ftruncate(fd, len(payload))
        os.fsync(fd)
    except OSError:
        logger.warning("Could not write claim lock state %s", lock_path)
        return False
    return cls._read_authenticated_claim_lock_payload(fd, lock_path, claim_name) == payload


def _initialize_claim_lock_state(self, fd: int, lock_path: Path, claim_name: str) -> bool:
    """Durably bind a fresh claim lock to its active claim before claiming."""
    return self._write_claim_lock_payload(
        fd,
        lock_path,
        claim_name,
        self._claim_lock_state_payload(claim_name, completed=False),
    )


def _claim_snapshot_fields(snapshot: _ClaimSnapshot) -> dict[str, object]:
    return {
        "claim_generation": snapshot.generation_hash,
        "claim_metadata": (
            base64.b64encode(snapshot.metadata_bytes).decode("ascii")
            if snapshot.metadata_bytes is not None
            else None
        ),
        "quarantine_identity": (
            _identity_payload(snapshot.quarantine_identity)
            if snapshot.quarantine_identity is not None
            else None
        ),
    }


def _claim_snapshot_from_fields(data: dict[str, object]) -> _ClaimSnapshot | None:
    generation = data.get("claim_generation")
    encoded_metadata = data.get("claim_metadata")
    if not isinstance(generation, str) or re.fullmatch(r"[0-9a-f]{64}", generation) is None:
        return None
    if encoded_metadata is None:
        metadata_bytes = None
    elif isinstance(encoded_metadata, str):
        try:
            metadata_bytes = base64.b64decode(encoded_metadata, validate=True)
        except (ValueError, binascii.Error):
            return None
    else:
        return None
    encoded_identity = data.get("quarantine_identity")
    quarantine_identity = _identity_from_payload(encoded_identity)
    if quarantine_identity is None:
        return None
    return _ClaimSnapshot(
        generation,
        metadata_bytes,
        quarantine_identity=quarantine_identity,
    )


def _write_claim_snapshot_state(
    self,
    fd: int,
    lock_path: Path,
    claim_name: str,
    snapshot: _ClaimSnapshot,
) -> bool:
    """Durably bind recovery to exact pre-rename generation and metadata."""
    if not self._authenticated_claim_lock_state(
        fd,
        lock_path,
        claim_name,
        completed=False,
    ):
        return False
    payload = json.dumps(
        {
            "state": "claimed",
            "format": 1,
            "claim": claim_name,
            **self._claim_snapshot_fields(snapshot),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return self._write_claim_lock_payload(fd, lock_path, claim_name, payload)


def _authenticated_claim_snapshot_state(
    self,
    fd: int,
    lock_path: Path,
    claim_name: str,
) -> _ClaimSnapshot | None:
    """Return the durable pre-rename claim snapshot, if authenticated."""
    payload = self._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
    if payload is None:
        return None
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("state") != "claimed" or data.get("format") != 1 or data.get("claim") != claim_name:
        return None
    return self._claim_snapshot_from_fields(data)


def _prepare_claim_publication(
    self,
    fd: int,
    lock_path: Path,
    claim_name: str,
    *,
    kind: str,
    target_slug: str,
    before_hash: str | None,
    after_hash: str,
    claim_snapshot: _ClaimSnapshot,
    live_backup_identity: _TaggedFileIdentity | None,
    snapshot_version: int | None,
    new_version: int | None,
) -> bool:
    """Durably journal the exact publication recovery must reconcile."""
    if (
        not self._authenticated_claim_lock_state(
            fd,
            lock_path,
            claim_name,
            completed=False,
        )
        and self._authenticated_claim_snapshot_state(fd, lock_path, claim_name) is None
    ):
        return False
    active_snapshot = self._authenticated_claim_snapshot_state(
        fd,
        lock_path,
        claim_name,
    )
    if claim_snapshot.quarantine_identity is None and active_snapshot is not None:
        claim_snapshot = _ClaimSnapshot(
            claim_snapshot.generation_hash,
            claim_snapshot.metadata_bytes,
            tree=claim_snapshot.tree,
            quarantine_identity=active_snapshot.quarantine_identity,
        )
    payload = json.dumps(
        {
            "state": "prepared",
            "format": 2,
            "claim": claim_name,
            "kind": kind,
            "target": target_slug,
            "before": before_hash,
            "after": after_hash,
            "live_backup_identity": (
                self._identity_payload(live_backup_identity)
                if live_backup_identity is not None
                else None
            ),
            **self._claim_snapshot_fields(claim_snapshot),
            "snapshot": snapshot_version,
            "version": new_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return self._write_claim_lock_payload(fd, lock_path, claim_name, payload)


def _authenticated_claim_publication(
    self, fd: int, lock_path: Path, claim_name: str
) -> dict[str, object] | None:
    """Parse a prepared publication only after authenticating its lock inode."""
    payload = self._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
    if payload is None:
        return None
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("state") != "prepared" or data.get("claim") != claim_name:
        return None
    journal_format = data.get("format", 1)
    if journal_format not in (1, 2):
        return None
    kind = data.get("kind")
    target = data.get("target")
    before_hash = data.get("before")
    after_hash = data.get("after")
    claim_generation = data.get("claim_generation")
    claim_metadata = data.get("claim_metadata")
    snapshot_version = data.get("snapshot")
    new_version = data.get("version")
    live_backup_identity = self._identity_from_payload(data.get("live_backup_identity"))
    if kind not in ("new", "update"):
        return None
    if not isinstance(target, str) or not self._is_pending_slug_safe(target):
        return None
    if before_hash is not None and (
        not isinstance(before_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", before_hash)
    ):
        return None
    if not isinstance(after_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", after_hash):
        return None
    if claim_generation is not None and (
        not isinstance(claim_generation, str) or not re.fullmatch(r"[0-9a-f]{64}", claim_generation)
    ):
        return None
    if (claim_generation is not None or claim_metadata is not None) and (
        self._claim_snapshot_from_fields(data) is None
    ):
        return None
    if snapshot_version is not None and (
        not isinstance(snapshot_version, int) or snapshot_version < 1
    ):
        return None
    if new_version is not None and (not isinstance(new_version, int) or new_version < 1):
        return None
    if kind == "update" and (
        before_hash is None
        or snapshot_version is None
        or new_version is None
        or (journal_format == 2 and live_backup_identity is None)
    ):
        return None
    if kind == "new" and journal_format == 2 and data.get("live_backup_identity") is not None:
        return None
    return data


def _publication_live_backup_identity(
    self,
    publication: dict[str, object],
) -> _TaggedFileIdentity | None:
    return self._identity_from_payload(publication.get("live_backup_identity"))


def _authenticated_claim_restore_state(
    self,
    fd: int,
    lock_path: Path,
    claim_name: str,
) -> tuple[str, _TaggedFileIdentity] | None:
    """Return the durable pending destination and quarantined native identity."""
    payload = self._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
    if payload is None:
        return None
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(data, dict)
        or data.get("state") not in {"claimed", "prepared", "evidence"}
        or data.get("claim") != claim_name
        or self._claim_snapshot_from_fields(data) is None
    ):
        return None
    restore_slug = data.get("restore_slug")
    restore_identity = self._identity_from_payload(data.get("restore_identity"))
    if (
        not isinstance(restore_slug, str)
        or not self._is_pending_slug_safe(restore_slug)
        or restore_identity is None
    ):
        return None
    return restore_slug, restore_identity


def _write_claim_restore_state(
    self,
    fd: int,
    lock_path: Path,
    claim_name: str,
    *,
    restore_slug: str,
    restore_identity: _TaggedFileIdentity,
) -> bool:
    """Durably bind a no-replace restore of the exact public inode."""
    payload = self._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
    if payload is None:
        return False
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return False
    if (
        not isinstance(data, dict)
        or data.get("state") not in {"claimed", "prepared"}
        or data.get("claim") != claim_name
        or self._claim_snapshot_from_fields(data) is None
        or not self._is_pending_slug_safe(restore_slug)
    ):
        return False
    data["restore_slug"] = restore_slug
    data["restore_identity"] = self._identity_payload(restore_identity)
    restore_payload = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return self._write_claim_lock_payload(fd, lock_path, claim_name, restore_payload)


def _clear_claim_restore_state(
    self,
    fd: int,
    lock_path: Path,
    claim_name: str,
) -> bool:
    """Clear a refused in-process destination before trying another slot."""
    payload = self._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
    if payload is None:
        return False
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return False
    if (
        not isinstance(data, dict)
        or data.get("state") not in {"claimed", "prepared"}
        or data.get("claim") != claim_name
    ):
        return False
    data.pop("restore_slug", None)
    data.pop("restore_identity", None)
    data.pop("restore_generation", None)
    cleared_payload = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return self._write_claim_lock_payload(fd, lock_path, claim_name, cleared_payload)


def _authenticated_claim_evidence_state(
    self,
    fd: int,
    lock_path: Path,
    claim_name: str,
) -> dict[str, object] | None:
    """Return a durable private-evidence record bound to this claim lock."""
    payload = self._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
    if payload is None:
        return None
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if (
        data.get("state") != "evidence"
        or data.get("claim") != claim_name
        or data.get("evidence") != claim_name
        or self._claim_snapshot_from_fields(data) is None
    ):
        return None
    return data


def _public_claim_evidence_matches(
    self,
    state: _PinnedPrivateState,
    evidence_state: dict[str, object],
    claim_name: str,
) -> bool:
    """Validate every public identity required by one evidence journal."""
    snapshot = self._claim_snapshot_from_fields(evidence_state)
    if snapshot is None or snapshot.quarantine_identity is None:
        return False
    try:
        candidate = self._stat_pinned_child(state.quarantine, claim_name)
        candidate_identity = self._pinned_child_identity(state.quarantine, claim_name)
    except OSError:
        return False
    if (
        not stat.S_ISDIR(candidate.st_mode)
        or candidate_identity != snapshot.quarantine_identity
        or is_link_or_junction(state.quarantine.path / claim_name)
    ):
        return False
    if evidence_state.get("format") == 2 and evidence_state.get("kind") == "update":
        old_live_identity = self._publication_live_backup_identity(evidence_state)
        if old_live_identity is None:
            return False
        try:
            old_live = self._stat_pinned_child(state.live_quarantine, claim_name)
            current_old_live_identity = self._pinned_child_identity(
                state.live_quarantine,
                claim_name,
            )
        except OSError:
            return False
        if (
            not stat.S_ISDIR(old_live.st_mode)
            or current_old_live_identity != old_live_identity
            or is_link_or_junction(state.live_quarantine.path / claim_name)
        ):
            return False
    return True


def _commit_claim_evidence_state(
    self,
    fd: int,
    lock_path: Path,
    claim_name: str,
) -> bool:
    """Forbid requeue before moving only the trusted claim to evidence."""
    existing = self._authenticated_claim_evidence_state(fd, lock_path, claim_name)
    if existing is not None:
        return True
    payload = self._read_authenticated_claim_lock_payload(fd, lock_path, claim_name)
    if payload is None:
        return False
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return False
    if (
        not isinstance(data, dict)
        or data.get("state") not in {"claimed", "prepared"}
        or data.get("claim") != claim_name
        or self._claim_snapshot_from_fields(data) is None
    ):
        return False
    data["state"] = "evidence"
    data["evidence"] = claim_name
    evidence_payload = json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return self._write_claim_lock_payload(fd, lock_path, claim_name, evidence_payload)


def _commit_claim_lock_state(self, fd: int, lock_path: Path, claim_name: str) -> bool:
    """Durably transition a held active/prepared claim to completed."""
    if self._authenticated_claim_lock_state(fd, lock_path, claim_name, completed=True):
        return True
    payload = self._claim_lock_state_payload(claim_name, completed=True)
    return self._write_claim_lock_payload(fd, lock_path, claim_name, payload)


def _published_update_evidence_matches(self, claim: Path, claim_fd: int) -> bool:
    """Whether mandatory public old-live evidence still matches its journal."""
    lock_path = self._claim_lock_path(claim.name)
    publication = self._authenticated_claim_publication(
        claim_fd,
        lock_path,
        claim.name,
    )
    if publication is None or publication.get("format") != 2 or publication.get("kind") != "update":
        return False
    expected_identity = self._publication_live_backup_identity(publication)
    if expected_identity is None:
        return False
    _stage, backup = self._publication_paths(claim.name)
    try:
        with self._pin_private_state(
            create=False,
            require_sensitive=True,
        ) as private_state:
            retained = self._stat_pinned_child(
                private_state.live_quarantine,
                backup.name,
            )
            retained_identity = self._pinned_child_identity(
                private_state.live_quarantine,
                backup.name,
            )
            return (
                stat.S_ISDIR(retained.st_mode)
                and retained_identity == expected_identity
                and not is_link_or_junction(backup)
            )
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        return False


def _retain_published_update_evidence(self, claim: Path, claim_fd: int) -> bool:
    """Retain the exact public old-live inode beside private claim evidence.

    A writer may keep a directory or file descriptor across the live-to-public-
    quarantine rename and mutate that inode indefinitely. Its identity remains
    journal-bound, but its mutable bytes are evidence only and never publication
    authority. It is never moved under or deleted from private authority.
    """
    lock_path = self._claim_lock_path(claim.name)
    publication = self._authenticated_claim_publication(
        claim_fd,
        lock_path,
        claim.name,
    )
    if publication is None or publication.get("format") != 2 or publication.get("kind") != "update":
        return False
    expected_identity = self._publication_live_backup_identity(publication)
    if expected_identity is None:
        return False
    _stage, backup = self._publication_paths(claim.name)
    try:
        with self._pin_private_state(
            create=False,
            require_sensitive=True,
        ) as private_state:
            retained = self._stat_pinned_child(
                private_state.live_quarantine,
                backup.name,
            )
            retained_identity = self._pinned_child_identity(
                private_state.live_quarantine,
                backup.name,
            )
            if (
                not stat.S_ISDIR(retained.st_mode)
                or retained_identity != expected_identity
                or is_link_or_junction(backup)
            ):
                return False
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        logger.error(
            "Could not retain published skill backup as public evidence %s",
            claim,
            exc_info=True,
        )
        return False
    return self._retain_claim_evidence(claim, claim_fd)


def _commit_claim_consumption(self, claim: Path, claim_fd: int) -> bool:
    """Make a published claim durably recoverable before reporting success.

    The in-tree marker and the external lock state are independent records.
    Write the marker first. If an update committed, retain its pre-publication
    inode and prepared journal as private evidence instead of collapsing the
    journal to a completed bit and deleting a generation a retained writer can
    still mutate. If marker publication fails, the exact prepared journal is
    already durable and restart recovery can classify the live generation.
    """
    lock_path = self._claim_lock_path(claim.name)
    publication = self._authenticated_claim_publication(
        claim_fd,
        lock_path,
        claim.name,
    )
    retain_update = (
        publication is not None
        and publication.get("format") == 2
        and publication.get("kind") == "update"
    )
    if retain_update and not self._published_update_evidence_matches(claim, claim_fd):
        logger.error(
            "Published update lacks mandatory identity-bound old-live evidence: %s",
            claim,
        )
        return False
    marker_completed = self._write_completion_marker(claim)
    if not marker_completed:
        if publication is not None:
            logger.warning(
                "Completion marker write failed for %s; prepared journal retained",
                claim,
            )
        else:
            logger.error("Published claim has no durable consumption record: %s", claim)
        return False
    retained = (
        self._retain_published_update_evidence(claim, claim_fd)
        if retain_update
        else self._retain_claim_evidence(claim, claim_fd)
    )
    if not retained:
        if retain_update:
            logger.error(
                "Published update evidence retention failed for %s; " "marker and journal retained",
                claim,
            )
            return False
        logger.warning(
            "Published claim evidence retention deferred for %s; "
            "authenticated marker and journal retained",
            claim,
        )
        return True
    return True


def _cleanup_claim_lock(self, claim_name: str) -> None:
    """Remove a completed claim lock under the pinned private hierarchy."""
    try:
        with self._pin_private_state(
            create=False,
            require_sensitive=True,
        ) as private_state:
            try:
                self._stat_pinned_child(private_state.quarantine, claim_name)
            except FileNotFoundError:
                pass
            except OSError:
                return
            else:
                return
            try:
                self._stat_pinned_child(private_state.live_quarantine, claim_name)
            except FileNotFoundError:
                pass
            except OSError:
                return
            else:
                return
            try:
                self._stat_pinned_child(private_state.claims, claim_name)
            except FileNotFoundError:
                pass
            except OSError:
                return
            else:
                return
            try:
                self._stat_pinned_child(private_state.evidence, claim_name)
            except FileNotFoundError:
                pass
            except OSError:
                return
            else:
                return
            lock_name = self._claim_lock_path(claim_name).name
            try:
                lock_info = self._stat_pinned_child(
                    private_state.claim_locks,
                    lock_name,
                )
            except FileNotFoundError:
                return
            if not stat.S_ISREG(lock_info.st_mode) or lock_info.st_nlink != 1:
                return
            lock_identity = self._pinned_child_identity(
                private_state.claim_locks,
                lock_name,
            )
            if not self._unlink_skill_child(
                private_state.claim_locks,
                lock_name,
                expected=lock_info,
                expected_identity=lock_identity,
            ):
                logger.debug("Could not remove completed claim lock %s", claim_name)
    except (FileNotFoundError, OSError, RuntimeError, ValueError):
        logger.debug("Could not pin completed claim lock %s", claim_name, exc_info=True)


def _completion_marker_present(
    self,
    claim: Path,
    expected_claim_identity: _TaggedFileIdentity | None = None,
) -> bool:
    """Check the reserved marker through a pinned claim directory."""
    try:
        with self._pin_skill_parent(claim) as parent:
            if (
                expected_claim_identity is not None
                and parent.native_identity != expected_claim_identity
            ):
                return False
            self._stat_pinned_child(parent, ".promoted")
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _remove_untrusted_completion_marker(
    self,
    claim: Path,
    expected_claim_identity: _TaggedFileIdentity | None = None,
) -> bool:
    """Remove only one reserved marker entry, never a linked/replaced tree."""
    try:
        with self._pin_skill_parent(claim) as parent:
            if (
                expected_claim_identity is not None
                and parent.native_identity != expected_claim_identity
            ):
                return False
            try:
                marker = self._stat_pinned_child(parent, ".promoted")
            except FileNotFoundError:
                return True
            marker_identity = self._pinned_child_identity(parent, ".promoted")
            if stat.S_ISDIR(marker.st_mode):
                # Never recurse through an untrusted marker name. An empty
                # directory can be removed exactly; a non-empty one leaves
                # the claim private for later inspection.
                return self._unlink_skill_child(
                    parent,
                    ".promoted",
                    expected=marker,
                    expected_identity=marker_identity,
                    directory=True,
                )
            return self._unlink_skill_child(
                parent,
                ".promoted",
                expected=marker,
                expected_identity=marker_identity,
            )
    except OSError:
        logger.warning("Could not remove untrusted completion marker from %s", claim)
        return False


def _authenticated_completion_marker(
    self,
    claim: Path,
    expected_claim_identity: _TaggedFileIdentity | None = None,
) -> bool:
    """Recognize only this claim's regular, single-link completion marker."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    try:
        with self._pin_skill_parent(claim) as parent:
            if (
                expected_claim_identity is not None
                and parent.native_identity != expected_claim_identity
            ):
                return False
            marker = self._stat_pinned_child(parent, ".promoted")
            if not stat.S_ISREG(marker.st_mode) or marker.st_nlink != 1:
                return False
            if os.name == "nt":
                fd = platform_compat.open_file_no_reparse(claim / ".promoted")
            else:
                fd = os.open(".promoted", flags, dir_fd=parent.fd)
            opened = os.fstat(fd)
            if (
                not self._opened_path_matches(fd, claim / ".promoted", marker)
                or opened.st_size > 256
            ):
                return False
            payload = os.read(fd, 257)
            return payload == (claim.name + "\n").encode("utf-8")
    except OSError:
        return False
    finally:
        if fd is not None:
            os.close(fd)


def _write_completion_marker(
    self,
    claim: Path,
    expected_claim_identity: _TaggedFileIdentity | None = None,
) -> bool:
    """Exclusively commit and durably link a claim outcome under its directory."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd: int | None = None
    created: os.stat_result | None = None
    created_identity: _TaggedFileIdentity | None = None
    wrote = False
    try:
        with self._pin_skill_parent(claim) as parent:
            if (
                expected_claim_identity is not None
                and parent.native_identity != expected_claim_identity
            ):
                return False
            if os.name == "nt":
                fd = os.open(str(claim / ".promoted"), flags, 0o600)
            else:
                fd = os.open(".promoted", flags, 0o600, dir_fd=parent.fd)
            created = os.fstat(fd)
            created_identity = self._tag_opened_identity(fd)
            if created_identity is None:
                raise OSError("completion-marker identity is unavailable")
            remaining = memoryview((claim.name + "\n").encode("utf-8"))
            while remaining:
                written = os.write(fd, remaining)
                if written <= 0:
                    raise OSError("short completion-marker write")
                remaining = remaining[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            # A file fsync does not make its new directory entry durable.
            # Sync the authenticated descriptor before any journal state may
            # depend on this independent completion record.
            self._sync_pinned_parent(parent)
            wrote = True
    except OSError:
        logger.warning("Could not commit claim completion marker in %s", claim)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                wrote = False
    if wrote and self._authenticated_completion_marker(
        claim,
        expected_claim_identity,
    ):
        return True
    if created is not None:
        try:
            with self._pin_skill_parent(claim) as parent:
                if (
                    expected_claim_identity is not None
                    and parent.native_identity != expected_claim_identity
                ):
                    return False
                removed = self._unlink_skill_child(
                    parent,
                    ".promoted",
                    expected=created,
                    expected_identity=created_identity,
                )
                if removed:
                    self._sync_pinned_parent(parent)
        except OSError:
            pass
    return False


def _cleanup_completed_claim(self, claim: Path, claim_fd: int) -> bool:
    """Delete one completed claim without following a replacement name.

    The held per-claim lock is the durable completion record. The claim
    directory identity is captured before any cleanup and is required by
    recursive removal plus every marker fallback, so a parent/name swap can
    only defer cleanup; it cannot redirect deletion or marker publication.
    """
    try:
        with self._pin_skill_parent(claim) as claim_parent:
            claim_identity = claim_parent.native_identity
    except OSError:
        logger.error("Refusing to clean replaced/linked claim %s", claim)
        return False
    lock_path = self._claim_lock_path(claim.name)
    lock_completed = self._authenticated_claim_lock_state(
        claim_fd, lock_path, claim.name, completed=True
    )
    marker_completed = self._authenticated_completion_marker(
        claim,
        claim_identity,
    )
    if not lock_completed and marker_completed:
        lock_completed = self._commit_claim_lock_state(claim_fd, lock_path, claim.name)
    if not lock_completed:
        if marker_completed:
            logger.warning(
                "Deferred completed-claim cleanup until lock outcome is durable: %s",
                claim,
            )
            return True
        logger.error("No authenticated committed outcome for claim %s", claim)
        return False
    if not self._cleanup_publication_artifacts(claim.name):
        logger.warning(
            "Deferred completed-claim cleanup until generation artifacts are removed: %s",
            claim,
        )
        return True
    if not self._remove_private_tree(
        claim,
        what="completed skill claim",
        expected_identity=claim_identity,
    ):
        logger.warning("Deferred completed-claim cleanup for %s", claim)
    if not os.path.lexists(claim):
        return True
    try:
        with self._pin_skill_parent(claim) as current_claim:
            if current_claim.identity != claim_identity:
                logger.error("Completed claim changed identity during cleanup: %s", claim)
                return False
    except OSError:
        logger.error("Completed claim became unreadable during cleanup: %s", claim)
        return False
    if self._authenticated_completion_marker(claim, claim_identity):
        return True
    if self._completion_marker_present(
        claim,
        claim_identity,
    ) and not self._remove_untrusted_completion_marker(
        claim,
        claim_identity,
    ):
        return False
    if self._write_completion_marker(claim, claim_identity):
        return True
    if self._authenticated_claim_lock_state(claim_fd, lock_path, claim.name, completed=True):
        logger.warning(
            "Claim marker could not be repaired; authenticated lock outcome retained for %s",
            claim,
        )
        return True
    logger.error("Could not preserve committed outcome for claim %s", claim)
    return False
