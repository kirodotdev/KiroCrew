"""S4-02: transactional lifecycle for *data-only custom* Mode plug-ins.

This is deliberately not an executable extension loader or a public REST API.
Never modifies shipped Tiga/Dyna JSON or KiroCrew; existing Policy Pack APIs
continue to hot-read the installed JSON. A one-step verified rollback is kept
for each managed custom Mode. Manual edits are allowed but block rollback when
its checksum no longer matches the committed build.
"""
from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Iterator

from .mode_registry import MODE_PLUGIN_API_VERSION
from .policy_packs import (
    PolicyPack, PolicyPackError, _require_id, parse_policy_pack, policy_pack_dir,
)


class ModeLifecycleError(PolicyPackError):
    """Unsafe or incompatible Mode lifecycle transition."""


def _sha(data: bytes) -> str:
    return sha256(data).hexdigest()


def _path(mode_id: str) -> Path:
    mid = _require_id(mode_id, 'id')
    if mid in {"tiga", "dyna"}:
        raise ModeLifecycleError(f"{mid} is a reserved built-in Mode; its lifecycle is not user-managed")
    return policy_pack_dir() / f"{mid}.json"


def _assert_regular(path: Path) -> None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ModeLifecycleError(f"unsafe Mode path: {path}")


def _bytes_if_any(path: Path) -> bytes | None:
    _assert_regular(path)
    return path.read_bytes() if path.exists() else None


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _assert_regular(path)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(payload)
            file.flush()
            os.fsync(file.fileno())
        os.replace(tmp_name, path)
        # Best effort directory fsync; not supported by all platforms.
        if os.name == "posix":
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


@contextmanager
def _mode_lock(mode_id: str) -> Iterator[None]:
    """Per-Mode cross-process advisory lock on Windows and POSIX."""
    path = policy_pack_dir()
    path.mkdir(parents=True, exist_ok=True)
    lock_path = path / f".{mode_id}.lck"
    _assert_regular(lock_path)
    with lock_path.open("a+b") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() == 0:
            f.write(b"0")
            f.flush()
        if os.name == "nt":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def _parse_existing(payload: bytes, mode_id: str) -> tuple[dict[str, Any], PolicyPack]:
    try:
        raw = json.loads(payload.decode("utf-8-sig"))
        pack = parse_policy_pack(raw, source="custom")
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ModeLifecycleError(f"installed Mode {mode_id!r} is invalid; refusing to overwrite it: {exc}") from exc
    if pack.id != mode_id:
        raise ModeLifecycleError("installed Mode ID does not match its filename")
    return raw, pack


def _serialize(raw: dict[str, Any]) -> bytes:
    return (json.dumps(raw, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _history(mode_id: str) -> tuple[Path, Path]:
    history = policy_pack_dir() / ".history"
    base = history / mode_id
    if history.is_symlink() or base.is_symlink():
        raise ModeLifecycleError("unsafe Mode history directory")
    return base / "previous.json", base / "rollback.json"


def _commit(mode_id: str, before: bytes | None, after: bytes) -> PolicyPack:
    """Journal previous snapshot *before* publishing the new Mode file."""
    target = _path(mode_id)
    prev_path, record_path = _history(mode_id)
    _assert_regular(prev_path)
    _assert_regular(record_path)
    _, pack = _parse_existing(after, mode_id)
    previous_snapshot = _bytes_if_any(prev_path)
    previous_record = _bytes_if_any(record_path)
    journal = {
        "format": 1, "mode_id": mode_id,
        "before_sha256": _sha(before) if before is not None else None,
        "after_sha256": _sha(after), "before_exists": before is not None,
    }
    try:
        if before is not None:
            _atomic_write(prev_path, before)
        elif prev_path.exists():
            prev_path.unlink()
        _atomic_write(record_path, _serialize(journal))
        _atomic_write(target, after)
    except OSError:
        # A failure can happen before *or after* os.replace. Only restore the
        # previous journal when the active JSON is still the old version. If
        # replacement did succeed, preserve the new rollback evidence.
        active = _bytes_if_any(target)
        if active == before:
            try:
                for path, old in ((prev_path, previous_snapshot), (record_path, previous_record)):
                    if old is None:
                        if path.exists():
                            path.unlink()
                    else:
                        _atomic_write(path, old)
            except OSError:
                # Preserve the original failure; a later audited repair must
                # check the on-disk journal before any further transition.
                pass
        raise
    return pack


def install_or_update_mode(raw: dict[str, Any], *, expected_sha256: str | None = None) -> PolicyPack:
    """Install/update a user Mode, requiring monotonic explicit semantic version.

    Compare-and-swap `expected_sha256` is optional for managed clients and
    strongly recommended when submitting a previously edited JSON document.
    Reinstalling a disabled Mode never silently re-enables it.
    """
    if not isinstance(raw, dict):
        raise ModeLifecycleError("Mode package must be a JSON object")
    mid = _require_id(raw.get("id"), "id")
    target = _path(mid)
    if "plugin_version" not in raw:
        raise ModeLifecycleError("managed Mode updates require an explicit plugin_version")
    candidate = parse_policy_pack(raw, source="custom")
    # Guard alternate or future reserved core identifiers.
    with _mode_lock(mid):
        old = _bytes_if_any(target)
        if expected_sha256 is not None and expected_sha256 != (_sha(old) if old is not None else None):
            raise ModeLifecycleError("Mode changed on disk; update refused (checksum mismatch)")
        if old is not None:
            _, previous = _parse_existing(old, mid)
            if tuple(int(x) for x in candidate.plugin_version.split(".")) <= tuple(int(x) for x in previous.plugin_version.split(".")):
                raise ModeLifecycleError("new plugin_version must be greater than installed version")
            if not previous.enabled and candidate.enabled:
                raise ModeLifecycleError("updating a disabled Mode must not implicitly enable it")
            # Active Crew sessions and scheduled tasks can retain a Mode/Type
            # wire ID across turns.  Updating an enabled Mode must not silently
            # remove a Type that those sessions may still reference.  An
            # intentional breaking migration must first disable the Mode and
            # make the interruption explicit to its operator.
            if previous.enabled and candidate.enabled:
                removed = {p.id for p in previous.policies} - {p.id for p in candidate.policies}
                if removed:
                    raise ModeLifecycleError(
                        "cannot remove existing Type IDs while Mode is enabled: "
                        + ", ".join(sorted(removed))
                        + "; disable the Mode before a breaking Type migration"
                    )
        return _commit(mid, old, _serialize(raw))


def set_custom_mode_enabled(mode_id: str, enabled: bool, *, expected_sha256: str | None = None) -> PolicyPack:
    """Enable or disable custom Mode; no implicit enable for built-in previews."""
    if not isinstance(enabled, bool):
        raise ModeLifecycleError("enabled must be boolean")
    mid = _require_id(mode_id, 'id')
    target = _path(mid)
    with _mode_lock(mid):
        before = _bytes_if_any(target)
        if before is None:
            raise ModeLifecycleError(f"custom Mode {mid!r} is not installed")
        if expected_sha256 is not None and expected_sha256 != _sha(before):
            raise ModeLifecycleError("Mode changed on disk; transition refused (checksum mismatch)")
        raw, previous = _parse_existing(before, mid)
        if previous.enabled == enabled:
            return previous
        raw["enabled"] = enabled
        return _commit(mid, before, _serialize(raw))


def rollback_custom_mode(mode_id: str) -> PolicyPack | None:
    """One-step rollback; refuses manual edits after an update.

    Returns None when rolling back a newly installed custom Mode (uninstall).
    Does not touch any Crew or Provider configuration.
    """
    mid = _require_id(mode_id, 'id')
    target = _path(mid)
    with _mode_lock(mid):
        prev_path, record_path = _history(mid)
        journal_bytes = _bytes_if_any(record_path)
        if journal_bytes is None:
            raise ModeLifecycleError("no managed rollback snapshot exists")
        try:
            journal = json.loads(journal_bytes)
        except (ValueError, TypeError) as exc:
            raise ModeLifecycleError("invalid rollback metadata") from exc
        if not isinstance(journal, dict) or journal.get("format") != 1 or journal.get("mode_id") != mid:
            raise ModeLifecycleError("rollback metadata is incompatible")
        current = _bytes_if_any(target)
        previous = _bytes_if_any(prev_path) if journal.get("before_exists") is True else None
        if journal.get("before_exists") is True and (previous is None or _sha(previous) != journal.get("before_sha256")):
            raise ModeLifecycleError("rollback snapshot is missing or corrupted")
        if previous is not None:
            _parse_existing(previous, mid)
        if current is None or _sha(current) != journal.get("after_sha256"):
            raise ModeLifecycleError("Mode was modified after update; refusing to overwrite manual changes")
        if previous is None:
            target.unlink()
            restored = None
        else:
            _atomic_write(target, previous)
            _, restored = _parse_existing(previous, mid)
        record_path.unlink()
        if prev_path.exists():
            prev_path.unlink()
        return restored


def mode_lifecycle_status(mode_id: str) -> dict[str, Any]:
    """Return non-sensitive identity/status/checksum for diagnostics."""
    mid = _require_id(mode_id, 'id')
    target = _path(mid)
    with _mode_lock(mid):
        current = _bytes_if_any(target)
        if current is None:
            return {"id": mid, "installed": False, "rollback_available": False}
        _, pack = _parse_existing(current, mid)
        _, record_path = _history(mid)
        record = _bytes_if_any(record_path)
        valid_rollback = False
        if record is not None:
            try:
                journal = json.loads(record)
                if (isinstance(journal, dict) and journal.get("format") == 1
                        and journal.get("mode_id") == mid
                        and journal.get("after_sha256") == _sha(current)
                        and isinstance(journal.get("before_exists"), bool)):
                    previous, _ = _history(mid)
                    previous_bytes = _bytes_if_any(previous)
                    if journal["before_exists"]:
                        if (previous_bytes is not None
                                and _sha(previous_bytes) == journal.get("before_sha256")):
                            # Rollback must not be offered if the snapshot is
                            # present but cannot be parsed as the same Mode.
                            _parse_existing(previous_bytes, mid)
                            valid_rollback = True
                    else:
                        # The first-install rollback is a removal; it must not
                        # be confused by an orphaned previous snapshot.
                        valid_rollback = (previous_bytes is None and
                                          journal.get("before_sha256") is None)
            except (ValueError, TypeError, UnicodeError, PolicyPackError):
                pass
        return {"id": mid, "installed": True, "enabled": pack.enabled,
                "plugin_version": pack.plugin_version, "registry_api": MODE_PLUGIN_API_VERSION,
                "sha256": _sha(current), "rollback_available": valid_rollback,
                "rollback_conflicted": record is not None and not valid_rollback}
