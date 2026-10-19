"""The Notes backend's own state on disk: where it lives, and how it is read and written.

The data home and the files under it -- ``vaults.json``, ``settings.json`` and the stored
GitHub PAT -- each resolved per call, ``_HOME`` being the test override; the readers and
writers of those files, every state write staged in the masked staging directory; and
the settings defaults, the auto-sync bounds and ``synced_cleanly``. ``logger`` is the logger ``server`` and its owners share,
named for ``server``. The locks that serialize a read-modify-write of these files stay in
``server``, the facade this module is reached through.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from kiro_crew.atomic_write import refuse_linked_parent, replace_with_retry
from kiro_crew.config.paths import config_dir
from kiro_crew.platform_compat import restrict_to_owner

#: The logger ``server`` and its owners share, named for ``server``. A literal rather than
#: ``__name__``: the gateway runs ``server`` as ``__main__``, and the mirror needs the
#: facade and its owners to hold one object under that name.
logger = logging.getLogger("kiro_crew.apps.builtins.md_notebook.server")


APP_NAME = os.environ.get("KIROCREW_APP_NAME", "md-notebook")

#: The state writers' staging directory, a TOP-LEVEL leaf in the crew data home. Must stay
#: byte-identical to ``sandbox._MD_NOTEBOOK_STAGING_LEAF``, which is what masks it from the
#: agent and hands it back to this backend's own spawn — a mismatch would silently stage
#: PAT bytes outside the mask. Spelled here rather than imported so this app backend does
#: not pull the sandbox module into its own process; a test pins the two together.
_STAGING_LEAF = "md-notebook-staging"


# Data-home paths are resolved LAZILY, never at import time: the
# active Kiro Crew home depends on KIROCREW_HOME, which a pod, the legacy-home
# migration, or the test-isolation fixture can change AFTER this module is
# imported. `_HOME` is the override hook (None = live-resolve); tests do
# `monkeypatch.setattr(server, "_HOME", tmp)`. See config/paths.py.
_HOME: Optional[Path] = None


def _default_home() -> Path:
    """The app's data root under the active Kiro Crew home.

    ``config_dir()`` follows ``KIROCREW_HOME`` (default ``~/.kiro/crew``), so an
    isolated dev instance (``KIROCREW_HOME=.kirocrew-dev``) keeps its own vaults
    instead of loading — and editing — the production notes. ``MD_NOTEBOOK_HOME``
    still wins when set. Falls back to the default root when run outside the
    package. Mirrors ``code_review_sage``'s ``crew_home()``.
    """
    env = os.environ.get("MD_NOTEBOOK_HOME")
    if env:
        return Path(env)
    return _crew_data_home()


def _crew_data_home() -> Path:
    """The app's data root under the Kiro Crew data home, IGNORING
    ``MD_NOTEBOOK_HOME``.

    The PAT is stored here (never under ``MD_NOTEBOOK_HOME``) so it always sits
    behind ``is_sensitive_path()``'s floor, which protects
    ``<crew-home>/workspace/md-notebook/pat``. A user pointing
    ``MD_NOTEBOOK_HOME`` at an unprotected directory must not be able to relocate
    the live GitHub credential out from behind that floor.
    """
    # config_dir is imported at module scope but CALLED here (lazily) — issue
    # #874 forbids resolving the data home at import time, not importing the
    # resolver. It honors KIROCREW_HOME on every call.
    try:
        base = config_dir()
    except Exception:
        override = os.environ.get("KIROCREW_HOME")
        base = Path(override) if override else Path.home() / ".kiro" / "crew"
    return base / "workspace" / APP_NAME


def _home() -> Path:
    return _HOME if _HOME is not None else _default_home()


def _vaults_json() -> Path:
    # The vault REGISTRY carries each vault's remoteUrl/gitDir, which the
    # unattended auto-sync loop trusts as the `git push` target and its anti-tamper
    # pins. Like the PAT and the sync settings it therefore MUST live under the
    # protected crew data home, never under MD_NOTEBOOK_HOME — otherwise an
    # operator's (or a prompt-injected agent's) MD_NOTEBOOK_HOME could relocate the
    # registry outside is_sensitive_path()'s fence and repoint the push. The clone
    # DATA (`_clone_root`) stays under _home(): it is bulk per-instance content, not
    # an authorization surface. The _HOME test hook still applies for tmp isolation.
    base = _HOME if _HOME is not None else _crew_data_home()
    return base / "vaults.json"


def _settings_json() -> Path:
    # Like the PAT, the sync settings MUST live under the protected crew data
    # home, never under MD_NOTEBOOK_HOME, so the `autoSync` bit — which authorizes
    # the background loop's unattended `git push` — always sits behind
    # is_sensitive_path()'s floor. Resolving via `_home()` would let an operator's
    # MD_NOTEBOOK_HOME relocate it outside the fence, where an agent could flip it.
    # The _HOME test hook still applies so tests keep their tmp isolation.
    base = _HOME if _HOME is not None else _crew_data_home()
    return base / "settings.json"


def _pat_file() -> Path:
    # The PAT MUST live under the protected crew data home, never under
    # MD_NOTEBOOK_HOME, so it stays behind is_sensitive_path()'s floor. The
    # _HOME test hook still applies so tests keep their tmp isolation.
    base = _HOME if _HOME is not None else _crew_data_home()
    return base / "pat"


def _clone_root() -> Path:
    return _home() / "vaults"


# Auto-sync interval bounds, in minutes. These mirror DEFAULT_AUTO_SYNC_MINS /
# MIN_AUTO_SYNC_MINS / MAX_AUTO_SYNC_MINS in
# website/src/apps/md-notebook/constants.ts: the picker's range and the
# scheduler's clamp must agree, or the UI would offer a value the backend then
# silently rewrites.
DEFAULT_AUTO_SYNC_MINS = 10
# The floor is load-bearing rather than cosmetic: 0 would make the scheduler a
# busy loop, pushing to the remote continuously.
MIN_AUTO_SYNC_MINS = 1
MAX_AUTO_SYNC_MINS = 1440


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _read_vaults_sync() -> list[dict[str, Any]]:
    try:
        with open(_vaults_json(), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _staging_dir() -> Path:
    """The masked write-staging directory every STATE writer publishes through.

    Every state writer (vaults/settings/PAT) stages its temp file HERE and renames onto
    its target. A temp staged beside the target — the previous shape — carries the real PAT
    bytes under a name the OS sandbox's three leaf masks do not cover, and a SIGKILL
    between write and rename left that unmasked sibling readable by a same-uid sandboxed
    agent forever.

    A TOP-LEVEL directory in the crew data home (``sandbox._MD_NOTEBOOK_STAGING_LEAF``),
    NOT a child of the state directory, for the reason the sibling ``aws-control-staging``
    leaf records: a mask covers the leaf, not its ancestors, so a staging dir under the
    agent-writable ``workspace/md-notebook`` could be renamed out from under its own mask
    and a later PAT write would publish through the replacement, unmasked, into a live
    agent's view. It stays on the same filesystem as the targets, so the publish rename is
    still atomic. NOTE saves are deliberately untouched — they stage beside the note inside
    the vault, which may be a different filesystem, and hold no secret.
    """
    # Mirrors ``_crew_data_home``'s resolution, minus the app subdirectory: the staging
    # directory is a sibling of ``workspace``, not of the state files. Under the ``_HOME``
    # test hook it sits beside that hook's directory, which keeps it on the same
    # filesystem as the targets — the only property the rename depends on.
    if _HOME is not None:
        return _HOME.parent / _STAGING_LEAF
    try:
        base = config_dir()
    except Exception:
        override = os.environ.get("KIROCREW_HOME")
        base = Path(override) if override else Path.home() / ".kiro" / "crew"
    return base / _STAGING_LEAF


def _write_state_staged_sync(target: Path, content: str, *, fsync_file: bool = False) -> None:
    """Stage *content* in the masked staging dir, then rename onto *target*.

    ``mkstemp`` opens the temp 0600 on POSIX before any payload byte;
    ``restrict_to_owner`` adds the owner-only DACL on Windows (chmod is a no-op
    there), warn-not-fail per the original PAT policy — losing the credential
    write is worse than a permissions warning. On failure the temp is removed;
    a removal that itself fails leaves the orphan inside the mask, not beside
    the target.
    """
    staging = _staging_dir()
    # The planted-link refusal atomic_write enforces:
    # atomic_write(restrict_to_owner=True)
    # refuses a secret write whose parent chain passes through a planted
    # symlink/junction — otherwise mkdir(parents=True), mkstemp and the rename
    # all follow the link and the token lands OUTSIDE the sensitive-path fence
    # while the caller sees success. Moving the staging off atomic_write must
    # not shed that guard. Both chains this write walks, checked BEFORE the
    # mkdirs (mkdir walks THROUGH a planted link and would build the tree
    # under its target); the probe name is never created, only its chain is
    # judged.
    refuse_linked_parent(target)
    refuse_linked_parent(staging / ".chain-probe")
    staging.mkdir(parents=True, exist_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(staging), suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        try:
            restrict_to_owner(tmp)
        except OSError:
            logger.warning("could not restrict a staged state temp to owner-only", exc_info=True)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fd = -1
            fh.write(content)
            if fsync_file:
                fh.flush()
                os.fsync(fh.fileno())
        replace_with_retry(tmp, target)
    except BaseException:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def _write_vaults_sync(vaults: list[dict[str, Any]]) -> None:
    """Replace the vault registry, retrying the Windows rename window.

    Same reason as the note writer in ``server_paths``: this file is read back by every
    later request, so a handle can be open on it when the rename lands.
    Staged in the masked staging dir — see :func:`_staging_dir`.
    """
    _write_state_staged_sync(_vaults_json(), json.dumps(vaults, indent=2))


def _read_pat_sync() -> Optional[str]:
    try:
        pat = open(_pat_file(), encoding="utf-8").read().strip() or None
    except OSError:
        return None
    if pat is not None:
        # Re-assert owner-only access on load: a PAT file left with a permissive
        # ACL (e.g. created before this lockdown, or by another principal on
        # Windows) would otherwise stay readable by other OS accounts. Warn but
        # do not fail the read — losing auth entirely is worse than a warning.
        try:
            restrict_to_owner(_pat_file())
        except OSError:
            logger.warning("could not restrict Notes PAT file to owner-only", exc_info=True)
    return pat


def _write_pat_sync(pat: str) -> None:
    # Stage in the MASKED staging dir, fsync, then atomically replace. A direct
    # O_TRUNC open would empty the existing token before the new bytes land, so
    # a failure partway (a full disk is the realistic one) would lose a valid
    # credential — and a temp staged BESIDE the target would hold the real PAT
    # bytes at a name the sandbox's leaf masks do not cover (see _staging_dir).
    # The temp is 0600 from mkstemp on POSIX and owner-only-DACL'd on Windows
    # before any payload byte, warn-not-fail; written 0600 and never echoed
    # back (only a boolean).
    _write_state_staged_sync(_pat_file(), pat, fsync_file=True)


async def read_vaults() -> list[dict[str, Any]]:
    return await asyncio.to_thread(_read_vaults_sync)


async def write_vaults(vaults: list[dict[str, Any]]) -> None:
    await asyncio.to_thread(_write_vaults_sync, vaults)


async def read_pat() -> Optional[str]:
    return await asyncio.to_thread(_read_pat_sync)


# ---------------------------------------------------------------------------
# App settings (auto-sync)
# ---------------------------------------------------------------------------


def _default_settings() -> dict[str, Any]:
    """A fresh defaults dict.

    ``autoSync`` defaults to False and that default is the app's authorization
    boundary, not a taste call: enabling it is what lets this backend run
    ``git push`` to a remote with nobody watching.
    """
    return {
        "autoSync": False,
        "autoSyncMins": DEFAULT_AUTO_SYNC_MINS,
        "lastSync": {},
    }


def _coerce_auto_sync_mins(value: Any) -> Optional[int]:
    """Clamp *value* into MIN..MAX minutes, or None if it is not an interval.

    Out-of-range is CLAMPED rather than refused because the intent is
    unambiguous — 0 means "as often as possible", 99999 means "rarely" — while a
    non-integer carries no interval to clamp at all. Returning None rather than a
    default lets each caller pick the right response to that: a read falls back
    to the default (it has nobody to hand an error to), a write returns 400.

    ``bool`` is excluded explicitly because ``isinstance(True, int)`` is True, and
    a client sending ``true`` for a minute count is a bug to surface, not a
    request for a one-minute interval.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return max(MIN_AUTO_SYNC_MINS, min(MAX_AUTO_SYNC_MINS, value))


def _read_settings_sync() -> dict[str, Any]:
    """Load settings, treating the file's SHAPE and its FIELDS as untrusted.

    ``settings.json`` sits in the app's data directory, which the agent and the
    user can both edit, so the document may be any JSON at all. A non-dict
    document, a wrong-typed field, or an out-of-range interval degrades to that
    field's default: this is the single read chokepoint, so no caller has to
    re-check a type, and a corrupt settings file must not stop the app from
    opening notes.

    ``autoSync`` is the strictest of the three — only a real boolean enables it,
    never a truthy string — because a garbled file must fail toward NOT pushing.
    """
    out = _default_settings()
    try:
        with open(_settings_json(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        return out
    except (OSError, json.JSONDecodeError):
        logger.warning("md-notebook: unreadable settings, using defaults")
        return out
    if not isinstance(raw, dict):
        return out
    if isinstance(raw.get("autoSync"), bool):
        out["autoSync"] = raw["autoSync"]
    mins = _coerce_auto_sync_mins(raw.get("autoSyncMins"))
    if mins is not None:
        out["autoSyncMins"] = mins
    stored_last = raw.get("lastSync")
    if isinstance(stored_last, dict):
        # Per-entry filtering, not whole-map rejection: one mangled timestamp
        # must not cost the user every other vault's sync time. A bool passes
        # isinstance(int) so it is excluded, and a negative epoch is not a time.
        out["lastSync"] = {
            str(vault_id): stamp
            for vault_id, stamp in stored_last.items()
            if isinstance(stamp, int) and not isinstance(stamp, bool) and stamp >= 0
        }
    return out


def _write_settings_sync(settings: dict[str, Any]) -> None:
    # Staged in the masked staging dir like the other state writers (see
    # _staging_dir); the helper creates both the staging dir and the target's
    # own parent, which diverges from _home() when the _HOME test hook is unset.
    # fsync preserved from the previous path (_stage_note_text_sync fsync'd):
    # settings carry the autoSync authorization bit and the lastSync stamp, and
    # a rename published from an unflushed page cache can discard an
    # acknowledged toggle on power loss.
    _write_state_staged_sync(_settings_json(), json.dumps(settings, indent=2), fsync_file=True)


async def read_settings() -> dict[str, Any]:
    return await asyncio.to_thread(_read_settings_sync)


def synced_cleanly(result: dict[str, Any]) -> bool:
    """Whether a ``git_ops.sync`` result has earned a ``lastSync`` stamp.

    A conflicted run aborted its merge and pushed nothing, so stamping it would
    tell the user their notes are backed up when they are still only local.
    """
    return not result.get("conflicts")
