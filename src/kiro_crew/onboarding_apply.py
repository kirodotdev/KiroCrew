"""Transactional destination writes, conflict strategies, and the import ledger.

The file-backed half of ``apply_import`` (see
docs/system-specs/modules/onboarding-import.md, "Conflict strategy" and
"Idempotency and deduplication"):

* the ledger -- ``imports/foreign-agent-imports.json``, loaded, recorded into
  and rewritten whole;
* the conflict strategies (``skip`` / ``rename`` / ``overwrite``), their rename
  derivation, and the restore copies an overwrite writes first;
* the writers whose destination is a file Kiro Crew already reads:
  workspaces and settings through ``config.json`` (one locked
  read-modify-write each), MCP servers through ``mcp.json`` under the
  dashboard's MCP lock, and skill packages staged in a sibling directory and
  swapped in atomically, with every path re-checked for a symlink component
  immediately before the write.

The writers into Kiro Crew's own stores -- lessons, memories and schedules --
and the per-item dispatch loop stay in :mod:`kiro_crew.onboarding_import`, next
to the persistence and cron ratchets that pin them there.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# The module, not its names: the patch seams read from it here (``_is_link_like``)
# live only in that module, so each is read off it at call time and a patch on
# ``onboarding_import.<name>`` reaches this module too.
from kiro_crew import onboarding_scan
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import ConfigReadError, update_config_locked
from kiro_crew.mcp_utils import mcp_server_alias
from kiro_crew.onboarding_plan import _SAFE_NAME_RE, _merge_missing
from kiro_crew.onboarding_scan import _Item
from kiro_crew.security import is_sensitive_path

# The facade's logger, not ``__name__``: operators and tests filter import
# warnings on ``kiro_crew.onboarding_import``, whichever owner emits them.
logger = logging.getLogger("kiro_crew.onboarding_import")


# Conflict strategies. ``skip`` is the default and the only non-destructive one;
# the other two require an explicit user choice per apply request. See
# docs/system-specs/modules/onboarding-import.md -> "Conflict strategy".
STRATEGY_SKIP = "skip"


STRATEGY_RENAME = "rename"


STRATEGY_OVERWRITE = "overwrite"


CONFLICT_STRATEGIES = (STRATEGY_SKIP, STRATEGY_RENAME, STRATEGY_OVERWRITE)


# Categories whose destination collisions a strategy can actually resolve. The
# rest are merge-only (instructions, memories, denied_commands, settings) or
# semantically deduplicated (schedules), so a strategy has nothing to act on.
STRATEGY_CATEGORIES = frozenset({"skills", "mcp_servers", "workspaces"})


# Categories whose destination holds exactly ONE item per identity and can be
# REPLACED after a first import (via ``overwrite``). Their ledger records cannot
# be trusted as a fast path, because the destination may have moved on since.
_REPLACEABLE_CATEGORIES = frozenset({"skills", "mcp_servers"})


_LEDGER_VERSION = 1


_LEDGER_RELATIVE_PATH = Path("imports") / "foreign-agent-imports.json"


# ``overwrite`` never destroys without a restore copy. One dir per apply run so a
# user can find everything a single import replaced together.
_REPLACED_RELATIVE_DIR = Path("imports") / "replaced"


@dataclass(frozen=True)
class _WriteOutcome:
    """One writer's result plus the details the API must report back.

    ``status`` is the four-value writer vocabulary (imported/existing/conflict/
    rejected). ``renamed_to`` and ``restored_to`` are set only when a strategy
    actually took effect, so a plain ``skip`` apply reports exactly what it did
    before this existed.
    """

    status: str
    renamed_to: str = ""
    restored_to: str = ""
    # The destination identity this item now occupies (currently only an MCP
    # server name). Lets the ledger keep one record per single-occupancy
    # destination instead of one per source.
    destination_key: str = ""


def _load_json_dict(path: Path, *, fail_closed: bool = False) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except OSError:
        if fail_closed:
            raise
        return {}
    except (UnicodeError, json.JSONDecodeError) as exc:
        if fail_closed:
            raise ValueError("invalid destination JSON") from exc
        return {}
    if isinstance(data, dict):
        return data
    if fail_closed:
        raise ValueError("destination JSON must contain an object")
    return {}


def _write_json(path: Path, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2, sort_keys=True) + "\n")


def _load_ledger(path: Path) -> dict[str, Any]:
    data = _load_json_dict(path)
    if data.get("version") != _LEDGER_VERSION or not isinstance(data.get("records"), dict):
        return {"version": _LEDGER_VERSION, "records": {}}
    return data


def _record_ledger(
    ledger: dict[str, Any],
    item: _Item,
    *,
    destination_key: str = "",
) -> None:
    records = ledger.setdefault("records", {})
    if destination_key:
        # A destination that holds exactly ONE item per key (an MCP server name)
        # can only be described by one ledger record. Without this, two sources
        # overwriting the same name leave two live fingerprints: when the first
        # source's definition later changes, its new fingerprint overwrites the
        # destination while the SECOND source's stale fingerprint still
        # deduplicates — so the definition the user selected silently vanishes.
        stale = [
            fingerprint
            for fingerprint, existing in records.items()
            if isinstance(existing, dict)
            and existing.get("category_id") == item.category
            and existing.get("destination_key") == destination_key
            and fingerprint != item.fingerprint
        ]
        for fingerprint in stale:
            del records[fingerprint]
    record = {
        "source_id": item.source_id,
        "category_id": item.category,
        "imported_at": datetime.now(timezone.utc).isoformat(),
    }
    if destination_key:
        record["destination_key"] = destination_key
    records[item.fingerprint] = record


def _normalize_strategy(value: Any) -> str:
    """Coerce a client-supplied strategy to a known one, defaulting to skip."""

    candidate = str(value or "").strip().lower()
    return candidate if candidate in CONFLICT_STRATEGIES else STRATEGY_SKIP


def _rename_candidates(base: str, item: _Item) -> list[str]:
    """Derived non-colliding names, most readable first.

    A user who renames wants to recognize the result, so the source-suffixed
    form is tried before the digest-suffixed fallback.
    """

    suffixed = f"{base}-{item.source_id}"
    digest = f"{base}-{item.fingerprint[:8]}"
    return [name for name in (suffixed, digest) if name != base]


def _restore_dir(data_home: Path, run_stamp: str, category: str) -> Path:
    return data_home / _REPLACED_RELATIVE_DIR / run_stamp / category


def _preserve_replaced_tree(source: Path, destination: Path) -> str:
    """Copy a directory aside before it is replaced. Returns the restore path.

    Raises so the caller can refuse to overwrite: losing the restore copy is the
    one failure that makes ``overwrite`` unrecoverable.

    The run stamp has one-second resolution, so two overwrites of the same item
    inside one second would collide. Suffix on collision rather than refusing (a
    refusal here reads to the user as an unresolvable conflict) and never
    overwrite an existing restore copy, which would defeat the point of keeping
    one.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    target = destination
    for attempt in range(1, 100):
        if not target.exists():
            break
        target = destination.with_name(f"{destination.name}-{attempt}")
    shutil.copytree(source, target, symlinks=True, dirs_exist_ok=False)
    return str(target)


def _preserve_replaced_json(payload: Any, destination: Path) -> str:
    """Write a replaced JSON fragment aside. Returns the restore path.

    Suffixes on collision for the same reason as ``_preserve_replaced_tree``: the
    run stamp is second-resolution, and clobbering an earlier restore copy would
    defeat the point of keeping one.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    target = destination
    for attempt in range(1, 100):
        if not target.exists():
            break
        target = destination.with_name(f"{destination.stem}-{attempt}{destination.suffix}")
    _write_json(target, payload)
    return str(target)


def _write_workspace(
    item: _Item,
    data_home: Path,
    *,
    strategy: str = STRATEGY_SKIP,
) -> _WriteOutcome:
    workspace = Path(str(item.payload))
    try:
        workspace = workspace.resolve(strict=True)
    except OSError:
        # A configured workspace that is missing from disk is a normal skip, not a
        # write failure.
        return _WriteOutcome("rejected")
    destination = data_home.resolve()
    if (
        not workspace.is_dir()
        or is_sensitive_path(str(workspace))
        or workspace == destination
        or destination in workspace.parents
    ):
        return _WriteOutcome("rejected")

    path = data_home / "config.json"
    # ONE locked read-modify-write: a raw _load_json_dict + _write_json pair
    # takes no advisory lock, so a concurrent locked writer (CLI, dashboard)
    # landing between the read and the atomic write is silently reverted by this
    # import's whole-document publish.
    outcome: _WriteOutcome | None = None

    def _mutate(data: dict) -> dict | None:
        nonlocal outcome
        workspaces = data.get("workspaces")
        if workspaces is None:
            workspaces = {}
            data["workspaces"] = workspaces
        if not isinstance(workspaces, dict):
            outcome = _WriteOutcome("conflict")
            return None

        canonical = str(workspace)
        for existing in workspaces.values():
            if isinstance(existing, dict):
                existing_dir = existing.get("dir")
            elif isinstance(existing, str):
                existing_dir = existing
            else:
                existing_dir = None
            if not isinstance(existing_dir, str):
                continue
            try:
                if str(Path(existing_dir).expanduser().resolve()) == canonical:
                    outcome = _WriteOutcome("existing")
                    return None
            except (OSError, RuntimeError):
                continue

        base_name = _SAFE_NAME_RE.sub("-", workspace.name).strip("-._").lower()
        base_name = base_name[:64] or f"imported-{item.source_id}"
        if base_name not in workspaces:
            workspaces[base_name] = {"dir": canonical}
            outcome = _WriteOutcome("imported")
            return data

        # The name is taken by a DIFFERENT directory. Deriving a suffixed name
        # is a rename, so it now requires the user to have asked for one; a
        # plain skip reports the collision instead of quietly inventing a name.
        if strategy != STRATEGY_RENAME:
            outcome = _WriteOutcome("conflict")
            return None
        for candidate in (
            f"{base_name}-{item.source_id}"[:64],
            f"{base_name[:55]}-{item.fingerprint[:8]}",
        ):
            if candidate not in workspaces:
                workspaces[candidate] = {"dir": canonical}
                outcome = _WriteOutcome("imported", renamed_to=candidate)
                return data
        outcome = _WriteOutcome("conflict")
        return None

    # stamp_meta=False: this import is merge-only and must not alter any byte
    # it did not add; a ConfigReadError keeps the old fail_closed contract
    # (ValueError), which the apply loop maps to a rejected outcome.
    try:
        update_config_locked(path, mutate=_mutate, stamp_meta=False)
    except ConfigReadError as exc:
        raise ValueError("invalid destination JSON") from exc
    assert outcome is not None  # every _mutate path sets it
    return outcome


@contextmanager
def _mcp_lock(_path: Path) -> Iterator[None]:
    """Coordinate with dashboard and app writers of the Kiro Crew MCP file."""
    # The dashboard's MCP handlers write the same data-home file while holding
    # the global Kiro MCP sidecar lock. Reuse that lock here so import cannot
    # race a manual enable/edit operation. This is imported lazily because the
    # dashboard handler imports this module during gateway startup.
    from kiro_crew.dashboard.handlers.mcp import _get_mcp_lock_sync

    with _get_mcp_lock_sync():
        yield


def _write_mcp(
    item: _Item,
    data_home: Path,
    user_home: Path,
    *,
    strategy: str = STRATEGY_SKIP,
    run_stamp: str = "",
) -> _WriteOutcome:
    path = data_home / "mcp.json"
    with _mcp_lock(path):
        data = _load_json_dict(path, fail_closed=True)
        if "mcpServers" not in data:
            servers: dict[str, Any] = {}
            data["mcpServers"] = servers
        else:
            servers = data["mcpServers"]
            if not isinstance(servers, dict):
                return _WriteOutcome("conflict")
        name = str(item.payload["name"])
        spec = item.payload["spec"]
        from kiro_crew.mcp_discovery import configured_mcp_aliases

        reserved = configured_mcp_aliases(data_home=data_home, user_home=user_home)

        def _install(target_name: str) -> None:
            servers[target_name] = spec
            _write_json(path, data)

        if name not in servers:
            # An alias collision means some OTHER effective MCP source already
            # owns this name, so writing it here would shadow a server the user
            # did not import. A rename is a legitimate way out.
            if mcp_server_alias(name) not in reserved:
                _install(name)
                return _WriteOutcome("imported", destination_key=name)
        elif servers[name] == spec:
            return _WriteOutcome("existing", destination_key=name)

        if strategy == STRATEGY_RENAME:
            for candidate in _rename_candidates(name, item):
                if candidate in servers:
                    if servers[candidate] == spec:
                        return _WriteOutcome(
                            "existing",
                            renamed_to=candidate,
                            destination_key=candidate,
                        )
                    continue
                if mcp_server_alias(candidate) in reserved:
                    continue
                _install(candidate)
                return _WriteOutcome(
                    "imported",
                    renamed_to=candidate,
                    destination_key=candidate,
                )
            return _WriteOutcome("conflict")

        if strategy == STRATEGY_OVERWRITE:
            # Only an entry WE can see in this file is replaceable; a name
            # reserved by another source is not ours to overwrite.
            if name not in servers:
                return _WriteOutcome("conflict")
            try:
                restored = _preserve_replaced_json(
                    {name: servers[name]},
                    _restore_dir(data_home, run_stamp, "mcp_servers")
                    # Scope by source + fingerprint: two selected sources can
                    # define the SAME server name, and a bare-name file would let
                    # the second overwrite clobber the user's only restore copy.
                    / item.source_id
                    / f"{_SAFE_NAME_RE.sub('-', name)}-{item.fingerprint[:8]}.json",
                )
            except OSError:
                logger.warning(
                    "Could not preserve the MCP server being replaced; refusing to overwrite",
                    exc_info=True,
                )
                return _WriteOutcome("conflict")
            _install(name)
            return _WriteOutcome(
                "imported",
                restored_to=restored,
                destination_key=name,
            )

        return _WriteOutcome("conflict")


def _has_symlink_component(path: Path, anchor: Path) -> bool:
    try:
        relative = path.relative_to(anchor)
    except ValueError:
        return True
    current = anchor
    for part in relative.parts:
        current = current / part
        try:
            component_stat = current.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return True
        if onboarding_scan._is_link_like(current, component_stat):
            return True
    return False


def _skill_destination_key(source_id: str, name: str) -> str:
    """The single-occupancy identity a skill package occupies.

    A skill dir holds exactly ONE package per (source, name), so the ledger must
    keep one record for it. Without this, importing V1, overwriting with V2, then
    reverting the source to V1 leaves V1's stale fingerprint deduplicating the
    revert while V2 stays installed.
    """

    return f"skills:{source_id}/{name}"


def _skill_files_are_valid(files: Any) -> bool:
    if not isinstance(files, dict) or "SKILL.md" not in files:
        return False
    for relative, content in files.items():
        if not isinstance(relative, str) or not isinstance(content, str):
            return False
        relative_path = Path(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            return False
    return True


def _skill_tree_state(
    destination: Path,
    files: dict[str, str],
    data_home: Path,
) -> str:
    """Classify a candidate skill destination: absent / existing / conflict."""

    present = 0
    for relative, content in files.items():
        target = destination / Path(relative)
        if _has_symlink_component(target, data_home):
            return "rejected"
        if not target.exists():
            continue
        present += 1
        try:
            if target.read_bytes() != content.encode("utf-8"):
                return "conflict"
        except OSError:
            return "conflict"
    if present == len(files):
        # Every file we carry is present and identical -- but the destination may
        # also hold files we DON'T carry, i.e. ones the upstream source deleted.
        # Reporting "existing" there would leave the stale file installed forever,
        # so treat an extra file as a conflict the user resolves (overwrite
        # replaces the whole tree, which removes it).
        try:
            installed = {
                path.relative_to(destination).as_posix()
                for path in destination.rglob("*")
                if path.is_file()
            }
        except OSError:
            return "conflict"
        expected = {Path(relative).as_posix() for relative in files}
        return "existing" if installed == expected else "conflict"
    if present:
        return "conflict"
    # A destination dir that exists but holds none of our files is still occupied.
    if destination.exists() or onboarding_scan._is_link_like(destination):
        return "conflict"
    return "absent"


def _install_skill_tree(destination: Path, files: dict[str, str], data_home: Path) -> str:
    """Stage the package in a sibling temp dir and move it into place atomically."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    if _has_symlink_component(destination, data_home):
        return "rejected"
    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{destination.name}.import-",
            dir=str(destination.parent),
        )
    )
    try:
        for relative, content in files.items():
            target = staging / Path(relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content.encode("utf-8"))
        # Re-check immediately before the move: the plan-time check is a TOCTOU
        # window, and this is the last moment we can still refuse.
        if _has_symlink_component(destination, data_home):
            return "rejected"
        if destination.exists() or onboarding_scan._is_link_like(destination):
            return "conflict"
        os.replace(staging, destination)
        return "imported"
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _write_skill(
    item: _Item,
    data_home: Path,
    *,
    strategy: str = STRATEGY_SKIP,
    run_stamp: str = "",
) -> _WriteOutcome:
    files = item.payload.get("files")
    if not _skill_files_are_valid(files):
        return _WriteOutcome("rejected")
    name = str(item.payload["name"])
    root = data_home / "skills" / "imported" / item.source_id
    destination = root / name
    if _has_symlink_component(destination, data_home):
        return _WriteOutcome("rejected")

    state = _skill_tree_state(destination, files, data_home)
    if state == "rejected":
        return _WriteOutcome(state)
    if state == "existing":
        return _WriteOutcome(
            state,
            destination_key=_skill_destination_key(item.source_id, name),
        )
    if state == "absent":
        return _WriteOutcome(
            _install_skill_tree(destination, files, data_home),
            destination_key=_skill_destination_key(item.source_id, name),
        )

    # state == "conflict": a different package already occupies this name.
    if strategy == STRATEGY_RENAME:
        for candidate in _rename_candidates(name, item):
            alternate = root / candidate
            if _has_symlink_component(alternate, data_home):
                continue
            alternate_state = _skill_tree_state(alternate, files, data_home)
            if alternate_state == "existing":
                # The renamed copy is already installed and identical.
                return _WriteOutcome(
                    "existing",
                    renamed_to=candidate,
                    destination_key=_skill_destination_key(item.source_id, candidate),
                )
            if alternate_state == "absent":
                status = _install_skill_tree(alternate, files, data_home)
                return _WriteOutcome(
                    status,
                    renamed_to=candidate if status == "imported" else "",
                    destination_key=(
                        _skill_destination_key(item.source_id, candidate)
                        if status == "imported"
                        else ""
                    ),
                )
        return _WriteOutcome("conflict")

    if strategy == STRATEGY_OVERWRITE:
        # The restore copy is written FIRST and its failure aborts the
        # overwrite: an unrecoverable replace is worse than a reported conflict.
        try:
            restored = _preserve_replaced_tree(
                destination,
                _restore_dir(data_home, run_stamp, "skills") / item.source_id / name,
            )
        except (OSError, shutil.Error):
            logger.warning(
                "Could not preserve the skill being replaced; refusing to overwrite",
                exc_info=True,
            )
            return _WriteOutcome("conflict")
        # MOVE the old tree aside rather than deleting it in place. A partial
        # delete (a locked file on Windows) would otherwise leave the installed
        # skill mangled AND the install failing, so the user ends up with
        # neither version. Renaming is atomic: it either frees the name
        # completely or fails with the old tree still whole.
        # Pick an UNUSED retired path rather than clearing one. A leftover
        # retired tree from an interrupted overwrite is the only surviving copy of
        # that earlier version, so deleting it to make room would destroy exactly
        # what the move-aside exists to preserve. (Same reasoning as the
        # suffix-on-collision restore paths above.)
        base = destination.with_name(f".{destination.name}.replaced-{item.fingerprint[:8]}")
        retired = base
        for attempt in range(1, 100):
            if not (retired.exists() or onboarding_scan._is_link_like(retired)):
                break
            retired = base.with_name(f"{base.name}-{attempt}")
        try:
            if retired.exists() or onboarding_scan._is_link_like(retired):
                # 99 leftovers means something is badly wrong; refuse rather than
                # delete someone else's copy.
                return _WriteOutcome("conflict")
            os.replace(destination, retired)
        except OSError:
            logger.warning(
                "Could not move the skill being replaced out of the way; " "refusing to overwrite",
                exc_info=True,
            )
            return _WriteOutcome("conflict")

        def _restore_retired() -> None:
            # Put the original back so a failed replace is a no-op, not data loss.
            with contextlib.suppress(OSError):
                if not destination.exists():
                    os.replace(retired, destination)

        try:
            status = _install_skill_tree(destination, files, data_home)
        except BaseException:
            # A RAISE (disk full, permissions, cancellation) must restore too --
            # handling only the non-"imported" return left the original stranded
            # under its retired name with nothing installed.
            _restore_retired()
            raise
        if status != "imported":
            _restore_retired()
            return _WriteOutcome(status)
        shutil.rmtree(retired, ignore_errors=True)
        return _WriteOutcome(
            status,
            restored_to=restored,
            destination_key=_skill_destination_key(item.source_id, name),
        )

    return _WriteOutcome("conflict")


def _write_settings(item: _Item, data_home: Path) -> _WriteOutcome:
    path = data_home / "config.json"
    # ONE locked read-modify-write -- see the workspace importer above for why a
    # raw read+write pair loses concurrent updates.
    changed = False

    def _mutate(data: dict) -> dict | None:
        nonlocal changed
        changed = _merge_missing(data, item.payload)
        return data if changed else None

    try:
        update_config_locked(path, mutate=_mutate, stamp_meta=False)
    except ConfigReadError as exc:
        raise ValueError("invalid destination JSON") from exc
    return _WriteOutcome("imported" if changed else "existing")
