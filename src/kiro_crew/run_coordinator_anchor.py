"""Stable filesystem identity for the durable run-coordinator ledger."""

from __future__ import annotations

import hashlib
import os
import stat
import threading
from pathlib import Path

from kiro_crew.config.paths import (
    RUN_COORDINATOR_DIR_NAME,
    _resolve_default_home,
    _valid_override_home,
    data_home,
)
from kiro_crew.platform_compat import (
    is_link_or_junction,
    make_owner_only_dir,
    restrict_dir_to_owner,
    restrict_to_owner,
)

RUN_COORDINATOR_ANCHOR_DIR_NAME = ".kirocrew.run-coordinator"

_anchor_lock = threading.Lock()
_anchor_cache: dict[tuple[str, str], Path] = {}


def _anchor_home() -> Path:
    return Path.home()


def _anchor_key() -> tuple[str, str]:
    raw_override = os.environ.get("KIROCREW_HOME")
    if raw_override:
        identity = os.path.normcase(os.path.abspath(os.path.expanduser(raw_override)))
    else:
        identity = "default-v1"
    record_name = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return (os.path.normcase(os.path.abspath(_anchor_home())), record_name)


def run_coordinator_anchor_dir() -> Path:
    """Return the agent-denied directory holding canonical ledger anchors."""
    return _anchor_home() / RUN_COORDINATOR_ANCHOR_DIR_NAME


def prepare_run_coordinator_anchor_dir() -> Path:
    """Create and fail-loud owner-tighten the agent-denied anchor directory."""
    anchor_dir = run_coordinator_anchor_dir()
    if is_link_or_junction(anchor_dir):
        raise OSError("run coordinator anchor directory cannot be a link")
    make_owner_only_dir(anchor_dir)
    if is_link_or_junction(anchor_dir):
        raise OSError("run coordinator anchor directory cannot be a link")
    restrict_dir_to_owner(anchor_dir)
    return anchor_dir


def _configured_data_home() -> Path:
    """Return the selected data home without erasing a valid override's spelling."""
    raw_override = os.environ.get("KIROCREW_HOME")
    if not raw_override:
        return data_home()
    if _valid_override_home() is None:
        # Resolve the configured fallback directly. A memoized override may
        # still name its prior target after the live link becomes unsafe.
        return _resolve_default_home()
    return Path(os.path.abspath(Path(raw_override).expanduser()))


def configured_run_coordinator_dir() -> Path:
    """Return the selected ledger path without resolving a valid home's alias."""
    return _configured_data_home() / RUN_COORDINATOR_DIR_NAME


def _read_anchor(path: Path) -> Path:
    if is_link_or_junction(path):
        raise OSError("run coordinator anchor cannot be a link")
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise OSError("run coordinator anchor must be a regular file")
    restrict_to_owner(path)
    raw = path.read_text(encoding="utf-8")
    if not raw.endswith("\n") or "\n" in raw[:-1] or "\x00" in raw:
        raise OSError("run coordinator anchor is malformed")
    anchored = Path(raw[:-1])
    if not anchored.is_absolute() or anchored.name != RUN_COORDINATOR_DIR_NAME:
        raise OSError("run coordinator anchor is malformed")
    return anchored


def _create_anchor(path: Path, anchored: Path) -> None:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags, 0o600)
    try:
        try:
            # The final name is uniquely ours by O_EXCL, so apply the Windows
            # DACL before the first payload byte without sacrificing create-once
            # semantics to a replacing atomic rename.
            restrict_to_owner(path)
            payload = (str(anchored) + "\n").encode("utf-8")
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("could not write run coordinator anchor")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def canonical_run_coordinator_dir() -> Path:
    """Return the durable canonical ledger path shared by storage and sandboxes.

    An ordinary directory keeps the ledger at its configured path. A data-home
    link instead gets an owner-only record outside that link, so replacing the
    link cannot redirect a later gateway process to attacker-prepared state.
    """
    key = _anchor_key()
    cached = _anchor_cache.get(key)
    if cached is not None:
        return cached
    with _anchor_lock:
        cached = _anchor_cache.get(key)
        if cached is not None:
            return cached

        configured = configured_run_coordinator_dir()
        lexical = os.path.normcase(os.path.abspath(configured))
        resolved = configured.resolve(strict=False)

        # A lexical home that used to be a link can later be replaced by an
        # ordinary directory. Its spelling still selects the same persisted
        # identity, so consult an existing record before the direct-path fast
        # path or a restart would silently abandon the original ledger.
        anchor_dir = run_coordinator_anchor_dir()
        record = anchor_dir / key[1]
        try:
            os.lstat(anchor_dir)
        except FileNotFoundError:
            pass
        else:
            prepare_run_coordinator_anchor_dir()
            try:
                anchored = _read_anchor(record)
            except FileNotFoundError:
                pass
            else:
                if os.path.normcase(os.path.abspath(anchored)) != os.path.normcase(
                    os.path.abspath(resolved)
                ):
                    raise OSError("run coordinator anchor does not match the configured data home")
                _anchor_cache[key] = anchored
                return anchored

        if os.path.normcase(os.path.abspath(resolved)) == lexical:
            _anchor_cache[key] = configured
            return configured

        anchor_dir = prepare_run_coordinator_anchor_dir()
        record = anchor_dir / key[1]
        try:
            anchored = _read_anchor(record)
        except FileNotFoundError:
            if is_link_or_junction(configured):
                raise OSError("run coordinator directory cannot be a link")
            anchored = resolved
            try:
                _create_anchor(record, anchored)
            except FileExistsError:
                anchored = _read_anchor(record)
                if os.path.normcase(os.path.abspath(anchored)) != os.path.normcase(
                    os.path.abspath(resolved)
                ):
                    raise OSError("run coordinator anchor does not match the configured data home")
        _anchor_cache[key] = anchored
        return anchored


def run_coordinator_anchor_matches_current_home() -> bool:
    """Return whether the canonical ledger is under the currently resolved home.

    A persisted anchor deliberately survives retargeting the configured data-home
    link. The old target is no longer covered by hook-layer path matching after
    such a retarget, so callers may use this proof before allowing execution
    without an OS sandbox.
    """
    configured = _configured_data_home() / RUN_COORDINATOR_DIR_NAME
    try:
        anchored = canonical_run_coordinator_dir()
        resolved = configured.resolve(strict=False)
    except OSError:
        return False
    return os.path.normcase(os.path.abspath(anchored)) == os.path.normcase(
        os.path.abspath(resolved)
    )


def _clear_run_coordinator_anchor_cache() -> None:
    """Clear process-local state for isolated tests."""
    with _anchor_lock:
        _anchor_cache.clear()
