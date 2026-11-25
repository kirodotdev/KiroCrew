"""Startup modules: an app's ``ui.startup`` component, mounted in every dashboard tab.

The contract is ``ui.startup`` in ``docs/app-kit/manifest-reference.md``. The dashboard
mounts an app's ``ui.startup`` module on every load only while ``GET /api/apps``
reports its startup state as ``admitted``. That state is computed on every read,
never stored, so a later disable, trust change or update applies on the next load:

* ``denied``: the app is disabled, its execution ceiling is closed, it declares
  something outside the browser (:func:`eligibility_refusal`), or the request is
  not the owner's dashboard;
* ``needs-approval``: the owner has not approved this app's startup entry at its
  current version and code (:func:`ui_matches`);
* ``admitted``: everything above passed.

The browser fetches the module, and every chunk it imports, after that read, so the
static route checks every file it serves from an approved app's ``ui/`` against the
per-file approval map (:func:`approved_module`) and sends only the bytes it checked.
Startup mounts use ``ui/startup:<digest>/`` so relative imports also require that
approval to exist and still name the same digest.

The owner approves with ``kirocrew app startup <name>``. The record is
``member-records/app-startup/<name>.json`` (``apps.protected_app_state``), which the
sandbox hides in every mode, beside the app's other owner decisions, and the verb
refuses a process that shows agent confinement, so a sandboxed agent cannot
approve an app on the owner's behalf. This is a guardrail, not a boundary,
against code running in the dashboard origin: an open app page runs there with
the owner's session and can reach owner routes, so it is not a CLI route only to
keep that reach off the approval.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, TypedDict

from kiro_crew.apps import protected_app_state, protected_app_ui
from kiro_crew.apps.manager import (
    APP_MANIFEST_FILENAME,
    _check_path_safety,
    _read_installed,
    app_dir,
    apps_dir,
    hold_app_record_lock,
    holds_app_record_locks,
)
from kiro_crew.pinned_fs import (
    PinnedPathRefusal,
    fd_real_path,
    is_reparse_point,
    open_in_pinned_parent,
    supports_pinned_walk,
)

logger = logging.getLogger(__name__)

#: Top-level ``app.json`` keys that start no process in the gateway, a backend or a
#: schedule. (``contributes.commands`` can seed an agent turn, but only on the user's
#: own click, as from any app page.)
#: Anything else -- ``backend``, ``setup``, ``crons``, ``agents``, ``skills``, ``sops``,
#: ``mcpServers``, ``dependencies``, ``openCommand``, ``publishProvider``,
#: ``agentTools``, or a key added after this list -- makes the app ineligible. A key
#: joins only in a change that says why it starts no process.
BROWSER_ONLY_KEYS = frozenset(
    {
        "name",
        "version",
        "displayName",
        "description",
        "author",
        "license",
        "minKiroCrewVersion",
        "signer",
        "signature",
        "tags",
        "jobFamilies",
        "platform",
        "permissions",
        "notifications",
        "config_keys",
        "credentialMasks",
        "l10n",
        "iconPath",
        "links",
        "repo",
        "screenshots",
        # Store-page text and art: the registry copies them into the listing
        # (``registry_pipeline/manifests.py``); nothing executes them.
        "highlights",
        "useCases",
        "configuration",
        "screenshotsDark",
        "heroImage",
        "heroImageDark",
        "heroImageDetail",
        "heroImageDetailDark",
        "iconPathDark",
        "icon",
        "ui",
        "desktop",
        "contributes",
    }
)

#: ``contributes`` points whose entries are browser UI. ``fileMenuItems`` and
#: ``sessionCreateItems`` are not here: each names an app route the host calls.
BROWSER_ONLY_CONTRIBUTIONS = frozenset(
    {
        "commands",
        "sessionControls",
        "panelTabs",
        "linkPreviews",
        "messageActions",
        "linkOpen",
        "composerActions",
        "selectionActions",
        "widgets",
    }
)

#: A file larger than the static route serves (``routes_static._UI_MAX_BYTES``) is
#: never hashed, so it can never match an approval.
_MAX_FILE_BYTES = 8 * 1024 * 1024

#: Allow large split bundles while bounding map validation and app-list file reads.
#: Inventory stops on the first entry past this cap, before opening any file.
_MAX_UI_FILES = 4096


def eligibility_refusal(raw: dict[str, Any], resources: str) -> str | None:
    """Why this app may not run a startup module, or ``None`` when all its code is in the browser.

    *raw* is the ``app.json`` object as written, not the parsed manifest: the parser
    keeps only the keys it knows, so a key it drops would otherwise pass unseen.
    """
    if resources == "app":
        return "the app manages its own resources"
    for key in sorted(raw):
        if key not in BROWSER_ONLY_KEYS:
            return f"it declares {key!r}, which runs outside the browser"
    contributes = raw.get("contributes") or {}
    if not isinstance(contributes, dict):
        return "its 'contributes' is not an object"
    for point in sorted(contributes):
        if point not in BROWSER_ONLY_CONTRIBUTIONS:
            return f"it declares 'contributes.{point}', which runs outside the browser"
    ui = raw.get("ui") or {}
    if isinstance(ui, dict) and ui.get("overlays"):
        return "it declares 'ui.overlays'"
    return None


#: A startup entry the browser cannot rewrite into another path: plain segments of
#: letters, digits, ``.``, ``_`` and ``-``, none led by a dot. That refuses ``..`` in
#: every spelling the URL parser accepts (``%2e%2e``, ``.%2E``, a tab between dots),
#: and ``%``, whitespace, ``?``, ``#`` and backslashes outright.
_ENTRY_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*")


#: The suffixes allowed for the startup entry; serving checks every UI file type.
MODULE_SUFFIXES = (".mjs", ".js")


class OversizeFile(OSError):
    """A file under ``ui/`` exceeds the static route's size cap, so it cannot be hashed."""


class FileCountExceeded(OSError):
    """The UI tree or stored approval exceeds the file-count budget."""


class ApprovalMismatch(Exception):
    """The requested UI file is absent from its approval or its bytes do not match."""


class StartupApproval(TypedDict):
    version: str
    entry: str
    digest: str
    files: dict[str, str]


def entry_refusal(entry: str) -> str | None:
    """Why *entry* cannot be a startup module, or ``None``.

    Checked at install, at approval and on every read, because the installed
    ``app.json`` can be edited after install-time validation, and a path the
    browser normalizes could load another app's ``ui/``, outside this app's digest.
    """
    if not _ENTRY_RE.fullmatch(entry):
        return f"its ui.startup {entry!r} is not a plain path inside ui/"
    if not entry.lower().endswith(MODULE_SUFFIXES):
        return f"its ui.startup {entry!r} is not a .mjs or .js module"
    return None


def _read_capped(fd: int, rel: str) -> bytes:
    """Everything *fd* holds from its current offset; :class:`OversizeFile` past the cap."""
    from kiro_crew.bounded_read import bounded_read_fd

    data = bounded_read_fd(fd, _MAX_FILE_BYTES + 1)
    if len(data) > _MAX_FILE_BYTES:
        raise OversizeFile(f"{rel} grew past the size cap")
    return data


def _check_file_count(count: int) -> None:
    if count > _MAX_UI_FILES:
        raise FileCountExceeded(f"found {count} files; startup approval cap is {_MAX_UI_FILES}")


def _ui_paths(name: str) -> dict[str, tuple[Path, Path, Path]]:
    """Inventory regular files up to the count cap, each with its root and that root's anchor.

    ``ui_roots`` may first capture scripts into the protected copy, and returns the
    containment anchor for the script root (the copy root when the protected copy is
    on, the gateway-owned install dir otherwise, or the operator's granted dev root).
    Non-script files are anchored to the gateway-owned install dir. The root and its
    own anchor are kept per file so :func:`_file_hash` can prove containment the way the
    serving route does, rather than opening a walked path directly.
    """
    ui = app_dir(name) / "ui"
    roots = protected_app_ui.ui_roots(apps_dir(), name, script=True)
    if roots is None:
        raise FileNotFoundError(f"app {name!r} has no served UI scripts")
    scripts, script_anchor, _pinned = roots
    ui_anchor = Path(os.path.realpath(apps_dir())) / name
    anchors = {ui: ui_anchor, scripts: script_anchor}
    paths: dict[str, tuple[Path, Path, Path]] = {}
    for root in dict.fromkeys((ui, scripts)):
        for path in root.rglob("*"):
            source = scripts if path.suffix.lower() in protected_app_ui.SCRIPT_EXTENSIONS else ui
            if root == source and stat.S_ISREG(path.lstat().st_mode):
                paths[path.relative_to(root).as_posix()] = (path, root, anchors[source])
                _check_file_count(len(paths))
    return paths


def _open_contained(name: str, path: Path, root: Path, anchor: Path, rel: str) -> int:
    """Open *path* read-only with its parent chain pinned under the resolved *root*.

    The same containment the UI serving route runs (``routes_static._open_ui_file``):
    ``rglob`` enumerates a swapped ``ui/`` root's real contents and ``O_NOFOLLOW``
    guards only the final component, so a ``ui -> credential-dir`` swap would otherwise
    be read by the unsandboxed gateway. The root is resolved THROUGH (dev mode links
    ``ui/`` legitimately), but a resolved root outside the gateway-owned install
    *anchor* is refused unless it is exactly the operator's dev-mode granted root; then
    the file's resolved parent must stay under the root, and the leaf opens relative to
    the pinned parent. Raises ``OSError`` on any escape, missing file, or link at the
    final name, which fails admission closed.
    """
    from kiro_crew.apps.dev_mode import dev_mode_granted_root

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    resolved_root = Path(os.path.realpath(root))
    try:
        resolved_root.relative_to(anchor)
    except ValueError as exc:
        if str(resolved_root) != dev_mode_granted_root(name):
            raise OSError(f"{rel!r} root resolves outside the install") from exc
    resolved_parent = Path(os.path.realpath(path.parent))
    try:
        resolved_parent.relative_to(resolved_root)
    except ValueError as exc:
        raise OSError(f"{rel!r} resolves outside the approved ui root") from exc
    if supports_pinned_walk():
        try:
            return open_in_pinned_parent(
                str(resolved_parent), path.name, flags=flags, mode=0o600, what="startup ui file"
            )
        except (PinnedPathRefusal, ValueError) as exc:
            raise OSError(f"{rel!r} could not be opened under the approved root") from exc
    # Windows: no O_NOFOLLOW/descriptor-relative open, so mirror the route's
    # name-based reparse probe plus a post-open descriptor-path check.
    if any(
        is_reparse_point(p)
        for p in (path, *path.parents)
        if p == resolved_root or resolved_root in p.parents or p == path
    ):
        raise OSError(f"{rel!r} is behind a reparse point")
    fd = os.open(path, flags)
    fd_real = fd_real_path(fd)
    if fd_real is None or not Path(fd_real).is_relative_to(resolved_root):
        os.close(fd)
        raise OSError(f"{rel!r} opened outside the approved root")
    return fd


def _file_hash(name: str, path: Path, root: Path, anchor: Path, rel: str) -> str:
    fd = _open_contained(name, path, root, anchor, rel)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"{rel!r} is not a regular file")
        # A hard link shares its target's inode, so O_NOFOLLOW and the parent
        # pin never see it; st_nlink > 1 is the only signal. The serving route
        # refuses it the same way (routes_static), so an app cannot alias a file
        # outside the install into its hashed inventory.
        if st.st_nlink != 1:
            raise OSError(f"{rel!r} is a hard link")
        if st.st_size > _MAX_FILE_BYTES:
            raise OversizeFile(f"{rel!r} is over the size cap")
        return hashlib.sha256(_read_capped(fd, rel)).hexdigest()
    finally:
        os.close(fd)


def ui_files(name: str) -> dict[str, str]:
    """Hash the bounded regular-file inventory, skipping links and refusing oversize files."""
    paths = _ui_paths(name)
    return {rel: _file_hash(name, *paths[rel], rel) for rel in sorted(paths)}


def ui_matches(name: str, files: dict[str, str]) -> bool:
    """Compare one inventory to the approval, stopping at the first different file.

    Check the complete path set before reading bytes: an added or deleted file
    withdraws admission without opening any UI file. Blocking: run off the loop.
    """
    paths = _ui_paths(name)
    if paths.keys() != files.keys():
        return False
    for rel in sorted(paths):
        if _file_hash(name, *paths[rel], rel) != files[rel]:
            return False
    return True


def _files_digest(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode("utf-8") + b"\0")
        digest.update(bytes.fromhex(files[rel]))
    return digest.hexdigest()


def ui_digest(name: str) -> str:
    """SHA-256 over the sorted relative paths and raw file hashes of :func:`ui_files`."""
    return _files_digest(ui_files(name))


def approved_module(
    name: str, path: str, fd: int | None, *, digest: str | None = None
) -> tuple[bytes, str] | None:
    """The bytes the static route may send, read once from its pinned descriptor.

    Without *digest*, ``None`` when the owner holds no approval: serve as usual.
    With *digest*, the current approval must exist and name that digest. The file's
    SHA-256 must match its entry in the approval map. An unknown path, an unavailable
    descriptor (``fd=None``), a mismatch or any fault raises. No other UI file is read.
    A stale approval is checked too: the browser mounted from an earlier list read,
    and ``app.json`` is agent-writable. Blocking: run off the event loop.
    """
    text = protected_app_state.read_startup_text(name)
    if text is None:
        if digest is not None:
            raise ApprovalMismatch(f"app {name!r}: startup approval is absent")
        return None
    approval = _parse_approval(text)
    if digest is not None and digest != approval["digest"]:
        raise ApprovalMismatch(f"app {name!r}: startup approval digest changed")
    ui = app_dir(name) / "ui"
    rel = (ui / path).relative_to(ui).as_posix()
    expected = approval["files"].get(rel)
    if expected is None or fd is None:
        raise ApprovalMismatch(f"app {name!r}: ui/{rel} is not an available approved file")
    data = _read_capped(fd, rel)
    if hashlib.sha256(data).hexdigest() != expected:
        raise ApprovalMismatch(f"app {name!r}: ui/{rel} changed since the startup approval")
    return data, approval["digest"]


def _parse_approval(text: str) -> StartupApproval:
    """Validate the record and its map's digest once at the storage boundary."""
    raw = json.loads(text)
    keys = ("version", "entry", "digest")
    if not isinstance(raw, dict) or not all(isinstance(raw.get(key), str) for key in keys):
        raise ValueError("startup approval must hold version, entry and digest strings")
    files = raw.get("files")
    if not isinstance(files, dict):
        raise ValueError("startup approval must hold a file hash map; re-approve this app")
    _check_file_count(len(files))
    for rel, digest in files.items():
        path = PurePosixPath(rel)
        if (
            not rel
            or path.is_absolute()
            or path.as_posix() != rel
            or ".." in path.parts
            or "\x00" in rel
            or "\\" in rel
            or not isinstance(digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        ):
            raise ValueError(f"invalid startup approval entry for {rel!r}: file path or hash")
    if _files_digest(files) != raw["digest"]:
        raise ValueError("startup approval digest does not match its file map")
    return {
        "version": raw["version"],
        "entry": raw["entry"],
        "digest": raw["digest"],
        "files": files,
    }


def read_approval(name: str) -> StartupApproval | None:
    """The version, entry, digest and file hashes the owner approved for *name*, or ``None``.

    Absent, unreadable and malformed records all read as no approval for the app list.
    """
    try:
        text = protected_app_state.read_startup_text(name)
        return _parse_approval(text) if text is not None else None
    except Exception:  # noqa: BLE001 -- an unreadable record approves nothing
        logger.debug("startup approval for %r unreadable; reading as none", name, exc_info=True)
        return None


def startup_state(
    *,
    entry: str,
    version: str,
    digest: Callable[[], str],
    enabled: bool,
    ceiling_reason: str | None,
    eligibility: str | None,
    approval: StartupApproval | None,
) -> dict[str, str]:
    """The startup state of one app that declares ``ui.startup``.

    *digest* is called only once every cheaper check has passed and an approval
    exists, so a disabled or ineligible app's ``ui/`` is never read.
    """
    if not enabled:
        return {"state": "denied", "reason": "the app is disabled"}
    if ceiling_reason:
        return {"state": "denied", "reason": ceiling_reason}
    if eligibility:
        return {"state": "denied", "reason": eligibility}
    unapproved = {"state": "needs-approval", "reason": "the owner has not approved this code"}
    if approval is None or approval["version"] != version or approval["entry"] != entry:
        return unapproved
    try:
        if approval["digest"] != digest():
            return unapproved
    except (OSError, UnicodeError, ValueError) as exc:
        return {"state": "needs-approval", "reason": f"could not verify ui/: {exc}"}
    return {"state": "admitted", "reason": "", "digest": approval["digest"]}


def _read_raw_manifest(name: str) -> dict[str, Any]:
    try:
        raw = json.loads((app_dir(name) / APP_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _declared_entry(raw: dict[str, Any]) -> str:
    ui = raw.get("ui")
    entry = ui.get("startup") if isinstance(ui, dict) else None
    return entry if isinstance(entry, str) else ""


def _app_state(name: str) -> dict[str, Any] | None:
    """The installed app's startup row, with a digest only when admitted, or ``None``."""
    from kiro_crew.apps.execution import third_party_ceiling_closed

    raw = _read_raw_manifest(name)
    entry = _declared_entry(raw)
    if not entry:
        return None
    record = _read_installed(name)
    if record is None:
        return None
    approval = read_approval(name)
    state = startup_state(
        entry=entry,
        version=str(raw.get("version", "")),
        digest=lambda: (
            approval["digest"]
            if approval is not None and ui_matches(name, approval["files"])
            else ""
        ),
        enabled=record.enabled,
        ceiling_reason=third_party_ceiling_closed(name),
        eligibility=entry_refusal(entry) or eligibility_refusal(raw, record.resources),
        approval=approval,
    )
    return {"entry": entry, **state}


def stamp_startup(apps: list[dict[str, Any]], *, owner: bool) -> None:
    """Add each listed app's ``startup`` row in place. Blocking: run off the event loop.

    Only the owner's dashboard mounts startup modules, so every other caller is told
    ``denied`` rather than ``admitted``.
    """
    for app in apps:
        try:
            row = _app_state(str(app.get("name", "")))
        except Exception:  # noqa: BLE001 -- one bad app must not fail the list
            logger.warning("startup state for app %r failed", app.get("name"), exc_info=True)
            row = None
        if row is None:
            continue
        if not owner and row["state"] == "admitted":
            row = {
                "entry": row["entry"],
                "state": "denied",
                "reason": "only the owner's dashboard runs it",
            }
        app["startup"] = row


def _store(name: str, entry: StartupApproval | None) -> None:
    """Write *name*'s approval, or with ``None`` remove it. Uninstall drops it with the
    app's other owner records (``protected_app_state.forget_grants``)."""
    if entry is not None and _read_installed(name) is None:
        # The app was uninstalled while this approval was being prepared.
        raise FileNotFoundError(f"app {name!r} is not installed")
    text = None if entry is None else json.dumps(entry, indent=2, sort_keys=True) + "\n"
    protected_app_state.write_startup_text(name, text)


def _operator_refusal() -> str | None:
    """Why this process may not write the record: it shows agent confinement."""
    from kiro_crew.sandbox import agent_confinement_evidence

    return agent_confinement_evidence()


def _audit(approve: bool, name: str, outcome: str, error: str = "", *, caller: str = "cli") -> None:
    try:
        from kiro_crew.sel import sel

        sel().log_api_access(
            caller=caller,
            operation="app_startup_approve" if approve else "app_startup_revoke",
            outcome=outcome,
            source="cli" if caller == "cli" else "app",
            resources=name,
            error=error,
        )
    except Exception:  # noqa: BLE001 -- the audit must not change the outcome
        logger.debug("SEL audit for startup approval failed", exc_info=True)


def withdrawal_notice(name: str, action: str) -> str:
    """Audit a committed lifecycle withdrawal and disclose unchecked serving to its owner."""
    reason = f"{action} dropped ui.startup"
    _audit(False, name, "completed", reason, caller=f"app_{action}")
    return (
        f" Startup approval withdrawn because {reason}. "
        "UI files are now served without startup approval checks."
    )


def set_approval(name: str, approve: bool) -> dict[str, Any]:
    """Approve or revoke *name*'s startup entry at its current version and code.

    Returns ``{"error": ...}`` or the app's new ``{"entry", "state", "reason"}``.
    Every refusal and failure is audited, not only a completed change.
    """
    result = _set_approval(name, approve)
    if "error" in result:
        _audit(
            approve,
            name,
            "denied" if result["error"].startswith("refused") else "failed",
            result["error"],
        )
    else:
        _audit(approve, name, "completed")
    return result


@holds_app_record_locks
def _set_approval(name: str, approve: bool) -> dict[str, Any]:
    refusal = _operator_refusal()
    if refusal:
        return {"error": f"refused: {refusal}"}
    # Reject an unsafe name before it reaches app_record_lock, which would
    # otherwise create a lock file for it; the other locked writers (uninstall,
    # enable, dev-mode) check the same way before taking the lock.
    if not _check_path_safety(name):
        return {"error": f"unsafe app name: {name!r}"}
    # Hold the record lock across this decision's read, hash and write, as
    # install_app and update_app do, so a lifecycle rollback cannot restore the
    # approval this revoke removed (or clobber the one it writes).
    hold_app_record_lock(name)
    entry: StartupApproval | None = None
    if approve:
        # A revoke still runs for an app that is gone, so a stale entry can be cleared.
        if _read_installed(name) is None:
            return {"error": f"app {name!r} is not installed"}
        raw = _read_raw_manifest(name)
        declared = _declared_entry(raw)
        if not declared:
            return {"error": f"app {name!r} declares no ui.startup"}
        bad_entry = entry_refusal(declared)
        if bad_entry:
            return {"error": f"app {name!r}: {bad_entry}"}
        try:
            files = ui_files(name)
            digest = _files_digest(files)
        except (OSError, UnicodeError) as exc:
            return {"error": f"app {name!r}: could not hash its ui/ ({exc})"}
        entry = {
            "version": str(raw.get("version", "")),
            "entry": declared,
            "digest": digest,
            "files": files,
        }
    try:
        if entry is not None:
            entry = _parse_approval(json.dumps(entry))
        _store(name, entry)
    except FileNotFoundError as exc:
        return {"error": str(exc)}
    except (OSError, ValueError) as exc:
        return {"error": f"could not write the startup approval record: {exc}"}
    return _app_state(name) or {"entry": "", "state": "", "reason": ""}
