"""Whole-file content provenance for the sandbox-sealed keystone config files.

The Linux namespace sandbox seals ``config.json``, ``config.local.json``,
``denied_commands.json`` and ``computer_use.json`` read-only with a per-FILE
read-only bind. Every legitimate writer publishes through ``atomic_write`` (temp
file + ``rename``), and the kernel detaches a file bind whose target is renamed
over from another mount namespace. So the seal on one of these files holds from a
sandbox's spawn only until the gateway's NEXT save of that file, after which a
prompt-injected agent that lands a direct ``open(..., "w")`` in that window can
rewrite it -- turning ``agent.sandbox`` off, clearing the denied-command deny-all,
auto-approving tools, enabling computer use -- and the gateway then reads the
agent's own file as if the operator had written it.

This module closes that WITHOUT depending on the bind holding and WITHOUT a list of
keys to keep current: every gateway write stores the exact bytes it is about to publish
as the ENDORSED COPY before the rename (:func:`endorsing`), and every read -- the load
AND the read half of a read-modify-write -- serves the endorsed copy whenever the
on-disk bytes differ from it, so a later unrelated save cannot re-endorse a tampered
file. Every key is covered, tightening and loosening alike; there is
no key table. A file that differs is pending, and an ordinary save of it is refused
(:func:`refuse_save_if_pending`) until the owner applies or discards the change, so the
change is neither endorsed nor silently overwritten.

**Where it applies.** The per-file bind, and the rename-detach that lapses it, exist
ONLY in the Linux namespace sandbox, so the check runs on a Linux host that can build
that sandbox and nowhere else (:func:`enforcement_active`). Off Linux, and on a Linux
host without user namespaces (a container), nothing seals these files and the loaders
serve the on-disk file unchanged. The decision reads HOST FACTS only (platform, the
sandbox launch marker, one cached userns probe), never the file it is protecting, so
writing ``sandbox: off`` cannot switch it off. The gateway probes once at start, off the
event loop (:func:`prime_host_verdict`); until that answer exists the check is ON, and
inside a sandbox it is always ON, where the endorsed copies stay readable through the
directory bind below.

**A hand edit is PENDING, never silently dropped.** Where it applies, an on-disk file
that differs from its endorsed copy keeps loading as the endorsed copy, and is reported
as pending (:func:`pending_changes`: the leaf, a signature of the on-disk bytes, and a
plain changed-key diff with secret-bearing values withheld). The owner applies or
discards it from Settings (``dashboard/handlers/config_pending.py``, owner-gated and
CSRF-guarded like every Settings write, never reachable by the agent or an MCP tool);
both go through the leaf's normal endorsing writer. Users may hand-edit; nothing
requires it, and nothing an in-sandbox process writes takes effect on its own. A file
that is deleted or made unreadable is the same case: every reader serves the endorsed
copy (:func:`reconciled_bytes`) and the leaf is pending as removed until the owner
discards the removal, which puts the file back.

The endorsed copies live under the ``config-provenance`` directory of the data home.
That directory is a read-only DIRECTORY bind in every sandbox
(``sandbox_leaves._CREW_READONLY_LEAVES``), and a directory bind does NOT suffer the
rename-detach: the gateway's writes land on files INSIDE it and leave the directory's
own inode -- what the bind pins -- unchanged. So no sandboxed process can write or
replace an endorsed copy. Each copy gets the same file-tool gate as its original, and
all are written owner-only (0o600): a copy of ``config.json`` can hold an inline channel
token and stays readable like ``config.json`` (neither is on the read-gate floor), while
the copies of ``denied_commands.json`` and ``computer_use.json`` are on the read+write
floor beside their originals (``security.paths_lists``). The directory bind keeps all of
them readable to in-sandbox loaders, which read them directly in :func:`reconcile`.

``docs/system-specs/modules/security.md`` and ``config.md`` describe the seal and
this content check together.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import hashlib
import json
import logging
import math
import os
import stat
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from kiro_crew import sandbox_launch_mark
from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.paths import config_dir, peek_data_home
from kiro_crew.user_json import strict_json_equal

logger = logging.getLogger("kiro_crew.config.loader")

#: The sealed data-home directory the endorsed copies live under -- shared with
#: ``write_provenance`` (a read-only, no-follow, pre-created DIRECTORY bind in every
#: sandbox).
RECORD_DIR_LEAF = "config-provenance"
#: The sub-directory holding one endorsed copy per protected leaf.
ENDORSED_SUBDIR = "endorsed"
#: Owner-only on both directory levels and the copies: a copy can carry an inline
#: credential, exactly like the file it mirrors, and the data home leaves nothing
#: readable by other local users (test_owner_only_boot).
_DIR_MODE = 0o700
_FILE_MODE = 0o600

LEAF_CONFIG = "config.json"
LEAF_CONFIG_LOCAL = "config.local.json"
LEAF_DENIED_COMMANDS = "denied_commands.json"
LEAF_COMPUTER_USE = "computer_use.json"

PROTECTED_LEAVES: tuple[str, ...] = (
    LEAF_CONFIG,
    LEAF_CONFIG_LOCAL,
    LEAF_DENIED_COMMANDS,
    LEAF_COMPUTER_USE,
)

#: Leaves whose on-disk file has already been reported as differing from the endorsed
#: copy, so the substitution is logged once per leaf per process. A lost race only
#: duplicates a log line.
_reported: set[str] = set()

#: ``(leaf, pending signature)`` pairs whose refused migration write-back was already
#: logged, so a pending file logs once per version per process, not on every load.
_writeback_refusals_logged: set[tuple[str, str]] = set()

#: Endorsed copies a rollback could not put back, keyed by the copy's path:
#: ``(what the copy file still holds, the copy it should hold)``. While the file still
#: holds the first, :func:`endorsed_bytes` answers the second, so a failed publish whose
#: restore also failed leaves this process where a good restore would have.
#: shortcut: in-process only; a restart or another process (the CLI, an in-sandbox
#: loader) reads the stale file and shows the leaf as pending until the next save
#: endorses it. Persist it beside the copies if that ever matters.
_unrestored: dict[str, tuple[bytes | None, bytes | None]] = {}


#: This process's answer to "can the namespace sandbox be built here", set once by
#: :func:`prime_host_verdict`. ``None`` until then.
_host_verdict: bool | None = None
_host_verdict_lock = threading.Lock()


def enforcement_active() -> bool:
    """True only where the Linux per-file read-only bind can actually detach.

    Off Linux (Windows, macOS), and on a Linux host whose probe says the namespace
    sandbox cannot be built (no user namespaces), there is no per-file bind to lapse, so
    the loaders serve the on-disk file unchanged. HOST FACTS only, so it never reads the
    file it protects. Every config read calls this, on any thread, so it never probes on
    an event loop and never once per read:

    * inside a sandbox (:func:`sandbox_launch_mark.launched_in_sandbox`) it is ON without
      looking further: the sandbox's seccomp filter refuses the probe's namespace calls,
      which would read as "no sandbox here";
    * with the probe's answer cached (the gateway primes it at start) it is that answer;
    * with no answer yet on a running event loop, it is ON (fail closed: a hand edit waits
      for review rather than slipping through) and nothing is probed or cached;
    * with no answer yet off a loop (the CLI, pod tooling, an MCP server), it probes once
      for this process and caches the answer, so those agree with the gateway.
    """
    if sys.platform != "linux":
        return False
    if sandbox_launch_mark.launched_in_sandbox():
        return True
    verdict = _host_verdict
    if verdict is not None:
        return verdict
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return prime_host_verdict()
    return True


def prime_host_verdict() -> bool:
    """Probe this host once for this process and cache the answer; return it.

    Blocking (one short-lived probe child), so call it off the event loop: the gateway
    runs it in a worker thread at start, before any session or sandbox.
    A later call returns the cached answer.
    """
    global _host_verdict
    with _host_verdict_lock:
        if _host_verdict is None:
            _host_verdict = _probe_host()
            if not _host_verdict:
                # A load before the answer read through the copy; drop it so the next
                # load serves the file as written.
                from kiro_crew.config.loader_document import _invalidate_config_cache

                _invalidate_config_cache()
        return _host_verdict


def _probe_host() -> bool:
    """One quiet namespace probe: ``False`` only when it proved userns is unavailable.

    Calls the probe itself rather than ``sandbox.userns_available``: that entry logs
    each failure as a warning and records it as the sandbox's last failure, which the
    spawn refusal and its remedy text report. A config read must change neither. A probe
    that raises or fails transiently counts as sandbox-capable (ON), like the sandbox,
    which keeps trying to spawn after a transient probe.
    shortcut: a transient answer is cached for the process like any other; re-probe on a
    timer if a host that recovers userns without a restart ever matters.
    """
    try:
        from kiro_crew.sandbox_probe import _probe_unshare_once

        ok, transient, _reason, _remedy = _probe_unshare_once()
    except Exception:
        logger.debug("loosening provenance: userns probe failed; enforcing", exc_info=True)
        return True
    return ok or transient


def _not_regular(path: Path) -> OSError:
    return OSError(errno.EINVAL, "not a regular file", str(path))


def _read_regular(path: Path) -> bytes:
    """The bytes of *path*, refusing anything but a regular file with ``OSError``.

    An in-sandbox process that can rewrite a protected name can also point it (or a
    symlink it follows) at a FIFO or ``/dev/zero``; a bare read of that blocks or never
    ends. Opened non-blocking so a FIFO with no writer returns at once, then judged by
    ``fstat`` on the open file, so the file checked is the file read.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise _not_regular(path)
        return handle.read()


def _refuse_special(path: Path) -> None:
    """Raise ``OSError`` when *path* exists and is not a regular file (a missing one is
    left to the reader).

    shortcut: stat-then-open race for custom readers; upgrade by passing the fd.
    """
    try:
        mode = path.stat().st_mode
    except FileNotFoundError:
        return
    if not stat.S_ISREG(mode):
        raise _not_regular(path)


def _endorsed_dir() -> Path:
    return config_dir() / RECORD_DIR_LEAF / ENDORSED_SUBDIR


def _endorsed_path(leaf: str) -> Path:
    return _endorsed_dir() / leaf


def leaf_for(path: Path) -> str | None:
    """The protected leaf *path* is, or ``None`` for any other file.

    Matched by RESOLVED identity against each protected name under the data home -- the
    same rule as ``write_provenance.staging_hook`` -- so a symlinked ``config.json``
    written through to its target (``crew-settings.json`` in a dotfiles repo) is still
    endorsed as ``config.json``, and a file elsewhere that merely shares a protected
    basename is never mistaken for one.

    Locates the home through ``peek_data_home``, never ``config_dir``: this only asks
    where the home is, and ``spec_build._load_json`` calls it at import time, so a
    resolver that creates the home would turn "import the package" into "mkdir it".

    A protected leaf named by its own path is matched before anything resolves, so a
    looping symlink at that leaf still reads as its endorsed copy; a loop anywhere else
    (``Path.resolve`` raises ``RuntimeError`` for one on Python 3.12) only drops that one
    candidate instead of failing an unrelated read.
    """
    try:
        home = peek_data_home()
    except OSError:
        return None
    given = path.absolute()
    logical = next((leaf for leaf in PROTECTED_LEAVES if (home / leaf).absolute() == given), None)
    if logical is not None:
        return logical
    try:
        target = path.resolve()
    except (OSError, RuntimeError):
        return None
    for leaf in PROTECTED_LEAVES:
        try:
            if (home / leaf).resolve() == target:
                return leaf
        except (OSError, RuntimeError):
            continue
    return None


def record(leaf: str, content: bytes, *, strict: bool = False) -> None:
    """Store *content* as the gateway-endorsed copy of *leaf*.

    A failed write leaves the PREVIOUS copy in place (``atomic_write`` never leaves it
    half-written): removing it would hand the next load the on-disk bytes, i.e. drop
    the content check exactly when a disk is full. *strict* re-raises the failure, for a
    writer that must not publish what it could not endorse (:func:`endorsing`).
    """
    if leaf not in PROTECTED_LEAVES:
        return
    try:
        # Owner-only on BOTH levels: parents=True would create config-provenance with
        # the umask default, which test_owner_only_boot refuses.
        (config_dir() / RECORD_DIR_LEAF).mkdir(mode=_DIR_MODE, exist_ok=True)
        _endorsed_dir().mkdir(mode=_DIR_MODE, exist_ok=True)
        atomic_write(_endorsed_path(leaf), content, mode=_FILE_MODE)
        _unrestored.pop(str(_endorsed_path(leaf)), None)
    except Exception:
        if strict:
            raise
        logger.warning(
            "loosening provenance: could not record the endorsed copy for %s; "
            "the previous copy stays in effect",
            leaf,
            exc_info=True,
        )


#: The refusal every ordinary save of a pending file gets, CLI and dashboard alike. Plain
#: words, no terminal step: the dashboard shows its own translation of the same sentence
#: (keyed by :attr:`PendingEditBlocksSave.code`) with a link to the pending card.
PENDING_SAVE_REFUSED = (
    "This settings file was changed outside Kiro Crew, so this save was not made. "
    "Review the change in Settings > Security first (Apply or Discard), then try again."
)


def leaf_pending(leaf: str) -> bool:
    """Whether *leaf* is pending: :func:`pending_changes` would list it.

    Raises ``OSError`` when the endorsed copy exists but cannot be read, as
    :func:`endorsed_bytes` does.
    """
    if not enforcement_active():
        return False
    endorsed = endorsed_bytes(leaf)
    return endorsed is not None and endorsed != _disk_bytes(leaf)


def refuse_save_if_pending(leaf: str | None) -> None:
    """Raise ``PendingEditBlocksSave`` when *leaf*, in this data home, is pending.

    An ordinary save reads the endorsed copy, so publishing it would replace the
    pending on-disk edit with nothing keeping it. Only the owner's Apply or Discard
    (``resolving_pending=True`` on the leaf's writer) may write a pending leaf. Every
    other leaf saves normally. A no-op where nothing is ever pending.
    shortcut: an edit landing between this check and the rename is still replaced, as
    before; closing that needs a lock every outside editor honours.
    """
    if leaf is None or leaf not in PROTECTED_LEAVES or not leaf_pending(leaf):
        return
    # Function-local: the class subclasses ConfigWriteRefused, so every caller's
    # existing refusal path catches it, and loader_document imports this module.
    from kiro_crew.config.loader_document import PendingEditBlocksSave

    raise PendingEditBlocksSave(leaf)


@contextlib.contextmanager
def endorsing(
    leaf: str | None,
    content: bytes,
    *,
    resolving_pending: bool = False,
) -> Iterator[None]:
    """Endorse *content* for *leaf* BEFORE the ``with`` body publishes it.

    A save to a leaf that is pending is refused before anything is endorsed or
    published (:func:`refuse_save_if_pending`), unless the caller is the owner's Apply
    or Discard (*resolving_pending*).

    *content* is the writer's own bytes, never a re-read of the destination, so nothing
    an in-sandbox process lands on the name around the rename can be endorsed. Where the
    sandbox seals these files (:func:`enforcement_active`) the endorsement is required:
    a copy that cannot be read or written aborts the write before anything is
    published, so the gateway never publishes a protected file it has not endorsed; a
    publish that then fails puts the previous copy back. Elsewhere it is best-effort and
    never blocks a save. ``leaf=None`` (not a protected file) is a no-op.
    """
    if leaf is None or leaf not in PROTECTED_LEAVES:
        yield
        return
    if not resolving_pending:
        refuse_save_if_pending(leaf)
    enforcing = enforcement_active()
    # raises under enforcement when the copy is unreadable
    prior = endorsed_bytes(leaf)
    record(leaf, content, strict=enforcing)
    try:
        yield
    except BaseException:
        if enforcing:
            _restore_endorsed(leaf, prior, "after a failed write")
            # A load inside the window served the attempted copy and cached it under
            # the unchanged on-disk fingerprint; drop it as a successful write does.
            # Function-local: loader_document imports this module.
            from kiro_crew.config.loader_document import _invalidate_config_cache

            _invalidate_config_cache()
        raise


def endorsed_bytes(leaf: str) -> bytes | None:
    """The gateway-endorsed copy of *leaf*, or ``None`` when there is none.

    Where the sandbox seals these files, a copy that EXISTS but cannot be read raises
    (``OSError``) rather than reading as "none": "none" serves the on-disk bytes, so a
    read error would silently drop the content check. Every reader already maps an
    ``OSError`` to its unreadable-file path (defaults, refuse the write, disabled).
    """
    path = _endorsed_path(leaf)
    try:
        held: bytes | None = path.read_bytes()
    except FileNotFoundError:
        held = None
    except OSError:
        if enforcement_active():
            raise
        logger.debug("loosening provenance: endorsed copy for %s unreadable", leaf, exc_info=True)
        return None
    stuck = _unrestored.get(str(path))
    if stuck is None:
        return held
    left, intended = stuck
    if held == left:
        return intended
    # Something has written the copy since; it holds the current endorsement.
    _unrestored.pop(str(path), None)
    return held


def _restore_endorsed(leaf: str, prior: bytes | None, when: str) -> None:
    """Put *prior* back as the endorsed copy of *leaf* (``None``: no copy).

    When that write fails too, the copy file is left holding bytes that were never
    published, and the next save would read the leaf as edited outside Kiro Crew. So
    :func:`endorsed_bytes` is told to answer *prior* for as long as the file still holds
    those bytes.
    """
    path = _endorsed_path(leaf)
    try:
        if prior is None:
            path.unlink(missing_ok=True)
            _unrestored.pop(str(path), None)
        else:
            record(leaf, prior, strict=True)
        return
    except Exception:
        logger.warning(
            "loosening provenance: could not restore the endorsed copy for %s %s; "
            "this process keeps using the previous copy",
            leaf,
            when,
            exc_info=True,
        )
    try:
        left: bytes | None = path.read_bytes()
    except FileNotFoundError:
        left = None
    except OSError:
        logger.debug("loosening provenance: endorsed copy for %s unreadable", leaf, exc_info=True)
        return
    if left != prior:
        _unrestored[str(path)] = (left, prior)


def reconcile(leaf: str, disk_bytes: bytes) -> bytes:
    """Return the bytes the loader should parse for *leaf*.

    Where the sandbox seals these files (:func:`enforcement_active`): when the on-disk
    bytes differ from the gateway-endorsed copy, the endorsed copy is returned and the
    substitution is logged once -- so a direct in-sandbox rewrite of ANY key is ignored.
    Elsewhere, or when there is no endorsed copy yet (a file the gateway has not saved
    since this check shipped), the on-disk bytes are returned unchanged. Every gateway
    publish of these files endorses first (:func:`endorsing`), so the gateway's own
    rename never unseals a file with no copy. An existing but unreadable copy raises
    ``OSError`` (:func:`endorsed_bytes`).
    shortcut: a host-side rename by another program (an editor saving by rename) also
    detaches the bind, and a file with no copy then loads an in-sandbox write as
    written; endorse at that point if such a writer ever needs covering.
    """
    if not enforcement_active():
        return disk_bytes
    endorsed = endorsed_bytes(leaf)
    if endorsed is None or endorsed == disk_bytes:
        return disk_bytes
    _report_once(leaf, "is not the gateway's own write")
    return endorsed


def reconciled_bytes(
    leaf: str, path: Path, read: Callable[[Path], bytes] = _read_regular
) -> bytes | None:
    """The bytes every reader of *leaf* at *path* parses, or ``None`` for no file at all.

    The one entry the loaders and the read half of each read-modify-write call. It reads
    *path* with *read* and :func:`reconcile`\\ s the result. Where the sandbox seals these
    files and an endorsed copy exists, a file that is MISSING or cannot be read serves the
    endorsed copy too: an in-sandbox process that can rewrite the name past a detached bind
    can also delete or ``chmod`` it, and the "no file" path of every reader is its safe
    default (no owner deny rules, default config), not the owner's settings. Such a leaf
    is pending as removed (:func:`pending_changes`). Elsewhere, or with no endorsed copy,
    a missing file returns ``None`` and any other read error is raised, as before. A
    *path* that is not a regular file (a FIFO, a device) cannot be read
    (:func:`_read_regular`), whichever *read* the caller passes.
    """
    try:
        _refuse_special(path)
        content = read(path)
    except (OSError, UnicodeDecodeError) as exc:
        # A text reader that cannot decode the file has read nothing usable either.
        endorsed = endorsed_bytes(leaf) if enforcement_active() else None
        if endorsed is not None:
            _report_once(leaf, "is missing or unreadable")
            return endorsed
        if isinstance(exc, FileNotFoundError):
            return None
        raise
    return reconcile(leaf, content)


def _report_once(leaf: str, what: str) -> None:
    if leaf not in _reported:
        _reported.add(leaf)
        logger.warning(
            "%s on disk %s; using the gateway-endorsed copy until the change is applied "
            "or discarded in Settings",
            leaf,
            what,
        )


def endorse_installed(path: Path, content: bytes) -> None:
    """Endorse *content*, the bytes an owner restore just installed at *path*.

    For the snapshot restore and import paths, which install ``config.json`` /
    ``config.local.json`` by copy rather than through ``atomic_write``: a restore is an
    owner action, so what it installs takes effect instead of being held as pending.
    *content* is the SOURCE file's bytes, never a re-read of the destination. A no-op for
    a path that is not a protected leaf of this data home. Raises where the sandbox seals
    these files and the copy cannot be written, so the restore's own rollback runs.
    """
    leaf = leaf_for(path)
    if leaf is not None:
        record(leaf, content, strict=enforcement_active())


@contextlib.contextmanager
def endorsements_restored_on_error() -> Iterator[None]:
    """Put every endorsed copy back as it was when the ``with`` body raises.

    For a multi-file restore whose rollback puts the previous files back: the copies the
    body endorsed (:func:`endorse_installed`) must go back with them, or the restored-over
    files would read as pending. Raises before the body when a copy cannot be read.
    """
    prior = {leaf: endorsed_bytes(leaf) for leaf in PROTECTED_LEAVES}
    try:
        yield
    except BaseException:
        for leaf, content in prior.items():
            _restore_endorsed(leaf, content, "after a failed restore")
        raise


# --- Pending changes ----------------------------------------------------------
#
# A protected file whose on-disk bytes differ from an EXISTING endorsed copy is pending:
# the loaders keep serving the endorsed copy (reconcile), and Settings shows the change
# so the owner can apply or discard it. Read-only here; the writes live with each
# leaf's own endorsing writer (dashboard/handlers/config_pending.py).

#: Most changed keys reported per leaf; the rest are summarized by ``truncated``.
_MAX_CHANGES = 100
#: Most characters of one listed key path (its segments together), cut with ``…``.
_MAX_SHOWN_PATH = 200
#: Per leaf, the keys whose change can loosen confinement: listed before every other
#: changed key, so the cap never drops one. The same keys as ``SECURITY_RULES`` in
#: ``website/src/pages/settings/SecurityPanelConfigPending.tsx``, which words them.
_CONFIG_SECURITY_KEYS = (
    "agent.sandbox",
    "agent.sandbox_allow_unsandboxed_exec",
    "agent.apps_allow_third_party",
    "hooks.auto_approve_tools",
    # The standing approve-every-tool grant, in each spelling the loader reads.
    "agent.dangerously_skip_permissions",
    "agent.dangerouslySkipPermissions",
    "agent.yolo",
    "agent.env",
)
_SECURITY_KEYS: dict[str, tuple[str, ...]] = {
    LEAF_CONFIG: _CONFIG_SECURITY_KEYS,
    LEAF_CONFIG_LOCAL: _CONFIG_SECURITY_KEYS,
    LEAF_DENIED_COMMANDS: ("disable_all", "disabled_ids", "user_added"),
    LEAF_COMPUTER_USE: ("enabled",),
}
_MAX_SHOWN_STR = 200
_MAX_SHOWN_LIST = 50
#: A key path segment that names a credential, or a section whose keys are arbitrary
#: env/header names whose values can be credentials: values under it are withheld
#: (:func:`_secret_segment`). Matched anywhere in the case-folded segment:
_SECRET_WORDS = (
    "token",
    "secret",
    "passw",
    "credential",
    "apikey",
    "api_key",
    "api-key",
    "private",
    "auth",
    "cookie",
    "bearer",
    "signing",
)
#: Matched at the end of the case-folded segment; ``key`` also matches at its start.
_SECRET_SUFFIXES = ("_key", "-key", "env", "header", "headers")
#: The non-ASCII letters a case-insensitive match equates with an ASCII letter that
#: ``str.lower`` leaves apart (dotted and dotless I, long s); the Kelvin sign lowers
#: to ``k`` by itself.
_ASCII_FOLD = str.maketrans({"\u0130": "i", "\u0131": "i", "\u017f": "s"})
_ABSENT = object()


def disk_signature(content: bytes) -> str:
    """Identity of one on-disk version: what the owner reviewed must be what is applied."""
    return hashlib.sha256(content).hexdigest()


def _parse_object(content: bytes) -> dict | None:
    """*content* as a JSON object, or ``None`` when it is not one a config reader can walk.

    A document nested past ``MAX_DOCUMENT_NESTING`` is not one: the decoder can raise
    ``RecursionError`` on it, and the diff and every config reader walk it recursively.
    Nor is one holding a number JSON has no spelling for (``NaN``, ``Infinity``, or a
    literal like ``1e400`` that overflows to infinity): the pending route would send it
    as ``Infinity``, which the browser cannot parse, so the card could not be shown.
    """
    from kiro_crew.user_json import exceeds_nesting, strip_utf8_bom

    try:
        doc = json.loads(
            strip_utf8_bom(content.decode("utf-8")),
            parse_constant=_refuse_constant,
            parse_float=_finite_float,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(doc, dict) or exceeds_nesting(doc):
        return None
    return doc


def _refuse_constant(name: str) -> Any:
    raise ValueError(f"{name} is not a JSON number")


def _finite_float(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"{text} is out of range")
    return value


def _secret_segment(segment: str) -> bool:
    """Whether one key path segment names a credential or a credential-bearing section."""
    folded = segment.translate(_ASCII_FOLD).lower()
    # One trailing newline does not hide a suffix: "env\n" is withheld like "env".
    tail = folded[:-1] if folded.endswith("\n") else folded
    return (
        any(word in folded for word in _SECRET_WORDS)
        or folded.startswith("key")
        or tail.endswith(_SECRET_SUFFIXES)
    )


def _secret_path(path: list[str]) -> bool:
    return any(_secret_segment(segment) for segment in path)


def _shown(path: list[str], value: Any) -> tuple[bool, Any]:
    """``(True, value)`` when *value* may be shown in the diff, else ``(False, None)``."""
    if _secret_path(path):
        return False, None
    if value is None or isinstance(value, (bool, int, float)):
        return True, value
    if isinstance(value, str):
        if len(value) > _MAX_SHOWN_STR:
            return False, None
        from kiro_crew.security.redaction import redact_credentials

        if redact_credentials(value)[0] != value:
            return False, None
        return True, value
    if isinstance(value, list) and len(value) <= _MAX_SHOWN_LIST:
        if all(not isinstance(i, (list, dict)) and _shown(path, i)[0] for i in value):
            return True, value
    return False, None


def _security_key(path: list[str], keys: tuple[str, ...]) -> str | None:
    """The first of *keys* a change at *path* touches: the key, a key under it, or a
    section holding it (the same match as the Settings card's ``securityHints``)."""
    dotted = ".".join(path)
    return next(
        (
            key
            for key in keys
            if dotted == key or dotted.startswith(key + ".") or key.startswith(dotted + ".")
        ),
        None,
    )


def _shown_path(path: list[str]) -> list[str]:
    """*path* for display: at most :data:`_MAX_SHOWN_PATH` characters, cut with ``…``."""
    shown: list[str] = []
    room = _MAX_SHOWN_PATH
    for segment in path:
        if len(segment) > room:
            shown.append(segment[: max(room - 1, 0)] + "\u2026")
            break
        shown.append(segment)
        room -= len(segment)
    return shown


class _Changes:
    """The diff rows of one leaf: the ones touching a :data:`_SECURITY_KEYS` key first,
    each such key keeping its own equal share of :data:`_MAX_CHANGES`, so neither a
    flood of other keys nor a flood under one security key (``agent.env.*``) can push
    another security key's row out of the listed rows or grow the response."""

    def __init__(self, leaf: str) -> None:
        self.keys = _SECURITY_KEYS.get(leaf, ())
        self.share = _MAX_CHANGES // max(len(self.keys), 1)
        self.per_key: dict[str, int] = {}
        self.first: list[dict] = []
        self.rest: list[dict] = []
        self.total = 0

    def add(self, path: list[str], kind: str, before: Any, after: Any) -> None:
        self.total += 1
        key = _security_key(path, self.keys)
        if key is None:
            if len(self.rest) < _MAX_CHANGES:
                self.rest.append(_change(path, kind, before, after))
        elif self.per_key.get(key, 0) < self.share:
            self.per_key[key] = self.per_key.get(key, 0) + 1
            self.first.append(_change(path, kind, before, after))

    def rows(self) -> list[dict]:
        return (self.first + self.rest)[:_MAX_CHANGES]


def _diff_keys(before: Any, after: Any, path: list[str], out: _Changes) -> None:
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after), key=str):
            if key not in after:
                out.add(path + [str(key)], "removed", before[key], _ABSENT)
            elif key not in before:
                out.add(path + [str(key)], "added", _ABSENT, after[key])
            elif not strict_json_equal(before[key], after[key]):
                _diff_keys(before[key], after[key], path + [str(key)], out)
        return
    out.add(path, "changed", before, after)


def _change(path: list[str], kind: str, before: Any, after: Any) -> dict:
    # Redaction judges the full path; only the displayed copy is cut.
    entry: dict[str, Any] = {"path": _shown_path(path), "change": kind}
    redacted = False
    for name, value in (("before", before), ("after", after)):
        if value is _ABSENT:
            continue
        ok, view = _shown(path, value)
        if ok:
            entry[name] = view
        elif _secret_path(path) or isinstance(value, str):
            redacted = True
    if redacted:
        entry["redacted"] = True
    return entry


#: The signature of a pending leaf whose file is missing or unreadable: no on-disk
#: bytes to hash, and never equal to a SHA-256 hex digest.
REMOVED_SIGNATURE = "removed"


def _disk_bytes(leaf: str) -> bytes | None:
    """The on-disk bytes of *leaf*, or ``None`` when it is missing or cannot be read."""
    try:
        return _read_regular(config_dir() / leaf)
    except OSError:
        return None


def pending_changes() -> list[dict]:
    """Each protected leaf whose on-disk bytes differ from its endorsed copy.

    ``[]`` wherever the content check does not apply (:func:`enforcement_active`).
    A leaf with no endorsed copy is not pending (it is served as written until the
    gateway's first save of it endorses it), and a leaf
    whose copy cannot be read is skipped (its loaders already fail closed). A file that
    is missing or unreadable while its copy exists is pending as ``removed`` (signature
    :data:`REMOVED_SIGNATURE`): every reader serves the copy (:func:`reconciled_bytes`),
    and Discard puts the file back. Each entry is
    ``{leaf, signature, disk_valid, removed, changes, truncated}``; ``changes`` lists
    ``{path, change: added|removed|changed, before?, after?, redacted?}``, with a value
    present only when it cannot carry a credential. Rows touching a
    :data:`_SECURITY_KEYS` key come first, so ``truncated`` never hides one.
    """
    if not enforcement_active():
        return []
    pending: list[dict] = []
    for leaf in PROTECTED_LEAVES:
        try:
            endorsed = endorsed_bytes(leaf)
        except OSError:
            continue
        disk = _disk_bytes(leaf)
        if endorsed is None or endorsed == disk:
            continue
        if disk is None:
            pending.append(
                {
                    "leaf": leaf,
                    "signature": REMOVED_SIGNATURE,
                    "disk_valid": False,
                    "removed": True,
                    "changes": [],
                    "truncated": False,
                }
            )
            continue
        after = _parse_object(disk)
        changes = _Changes(leaf)
        if after is not None:
            _diff_keys(_parse_object(endorsed) or {}, after, [], changes)
        pending.append(
            {
                "leaf": leaf,
                "signature": disk_signature(disk),
                "disk_valid": after is not None,
                "removed": False,
                "changes": changes.rows(),
                "truncated": changes.total > len(changes.rows()),
            }
        )
    return pending


def check_pending(leaf: str, signature: str) -> bytes | None:
    """The on-disk bytes of pending *leaf* (``None``: removed), if still the version reviewed.

    All Discard needs: it restores the endorsed copy whatever the edit holds, so an
    unparseable or removed file can always be discarded. Raises :class:`PendingChanged`
    when the leaf is not pending or its current version is not *signature* (edited again
    after the owner looked).
    """
    disk = _disk_bytes(leaf)
    endorsed = endorsed_bytes(leaf)
    if endorsed is None or endorsed == disk or _signature_of(disk) != signature:
        raise PendingChanged(leaf)
    return disk


def _signature_of(disk: bytes | None) -> str:
    """The signature :func:`pending_changes` reports for on-disk bytes *disk*."""
    return REMOVED_SIGNATURE if disk is None else disk_signature(disk)


def log_writeback_refused_once(leaf: str, message: str) -> None:
    """Log a migration write-back refused because *leaf* is pending, once per version.

    Every config load retries the write-back and is refused again while the file is
    pending; a new edit (a new signature) is logged again.
    """
    key = (leaf, _signature_of(_disk_bytes(leaf)))
    if key in _writeback_refusals_logged:
        return
    _writeback_refusals_logged.add(key)
    logger.warning("Config write-back failed: %s", message)


def read_pending_disk(leaf: str, signature: str) -> dict:
    """The on-disk document of pending *leaf*, for Apply, if still the version reviewed.

    Raises :class:`PendingChanged` as :func:`check_pending` does, :class:`PendingRemoved`
    when the file is missing or unreadable (there is nothing to apply; Discard puts it
    back), and :class:`PendingInvalid` when the on-disk file is not a JSON object.
    """
    disk = check_pending(leaf, signature)
    if disk is None:
        raise PendingRemoved(leaf)
    doc = _parse_object(disk)
    if doc is None:
        raise PendingInvalid(leaf)
    return doc


class PendingChanged(Exception):
    """The leaf is not pending, or not at the version the owner reviewed."""


class PendingInvalid(Exception):
    """The pending on-disk file is not a JSON object, so it cannot be applied."""


class PendingRemoved(Exception):
    """The pending file is missing or unreadable, so there is no edit to apply."""
