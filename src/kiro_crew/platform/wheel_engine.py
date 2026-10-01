"""Shadow-venv update engine for the ``cli.sh`` managed-venv install shape.

The managed venv (``${KIROCREW_HOME}-venv``) is the one install whose bytes the
gateway process is itself executing, so an in-place ``pip install --upgrade``
would overwrite a live runtime — the torn-runtime hazard
``docs/request-for-change/rfc-update-architecture.md`` §3 exists to prevent.
This module implements the versioned-trees-with-atomic-promotion design that
RFC's §3 reduced to invariants:

* **A new version is built into a FRESH sibling tree** (``crew-venv-<version>``)
  while the old gateway keeps serving from its own tree, untouched.
* **Promotion is a symlink replaced via ``os.replace``** — sibling symlink +
  ``rename(2)``, never ``ln -sfn`` (unlink+create has a missing-path window).
* **The stable path is a NEW NAME** (``crew-venv-current``) that has always
  been a symlink. The legacy fixed directory ``crew-venv`` is never renamed,
  moved, or converted: renaming it would break its own absolute shebangs while
  a gateway may still be running from it. It remains a functional fallback.
* **No tree a live process might be using is ever moved or deleted.** Every
  process started from an engine-built tree holds a shared lock on it
  (:mod:`kiro_crew.platform.tree_liveness`); pruning, after a promotion, deletes
  a superseded tree only while it holds that lock exclusively, and always keeps
  the stable link's target, the previous tree, the tree serving this process,
  and the legacy directory.

Authenticity comes from the same offline trust root ``cli.sh`` pins: the
channel feed serves an RSA-signed manifest, the signature is verified against
the public key embedded below (byte-identical to ``cli.sh``'s copy — a drift
test reads both), and the wheel's SHA-256 must match the SIGNED digest. The
feed alone is never trusted for anything actionable.

What this engine deliberately does NOT change, stated for parity rather than
guarded against: the shadow ``pip install`` resolves the wheel's dependencies
from the index exactly as ``cli.sh``'s own venv branch does today, so the
provenance story covers the Kiro Crew wheel itself, not the dependency set.
Raising that bar (hash-pinned constraints inside the signed payload) is worth
doing for the installer and this engine together, not here alone.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from kiro_crew.config.paths import data_home
from kiro_crew.platform.tree_liveness import LIVENESS_LOCK, TREE_MARKER
from kiro_crew.platform_compat import (
    IS_POSIX,
    kill_popen_tree,
    make_owner_only_dir,
    open_create_or_existing,
    release_lock,
    trusted_system_bin,
    try_acquire_lock,
    try_acquire_lock_or_raise,
)
from kiro_crew.subprocess_utf8 import utf8_stdout

logger = logging.getLogger(__name__)

# ── Offline trust root ──────────────────────────────────────────────────────
# MUST stay byte-identical to the constants in cli.sh at the repo root; the
# drift test in test/test_wheel_engine.py reads both and fails the build when
# they disagree. Rotation follows packaging/signing/README.md's dual-trust
# sequence — never an in-place edit.
CLI_MANIFEST_KEY_ID = "sha256:d3a83f0c1ff84a2cbee6bd34d889d8725af34358148a6c18ed3ecbbbcceec06b"
CLI_MANIFEST_PUBLIC_KEY_B64 = (
    "LS0tLS1CRUdJTiBQVUJMSUMgS0VZLS0tLS0KTUlJQm9qQU5CZ2txaGtpRzl3MEJBUUVGQUFPQ0FZOEFN"
    "SUlCaWdLQ0FZRUF0MnR0NnZ3ZFZ4Z0tWbTRGQVdkeApwZjZFckx3Y2ljUHlHUGh2SXdXRTRqNmg1Yjlw"
    "MzFiaktMaWlEakxvK3VpQUJPL21vUjdJUUtoaUNSaXY0d0dTCk1mYnd2ZnNhLy8xNlVBbkNURkRDb1pI"
    "d0IwVm93cTRYWjZ1NHBrdTFqNlBlRXBMNjVqRXZvcjd1a29HS2xiOVMKQlBva01aN0VtYlpWbmJiSWJB"
    "VXYrZ0NWajRCWDRpam5GWkJEMmNPcmtkQWdGR3UraU9jRHVlRDNqTExicXVhUwp0K0tLWXltQ2VxaitP"
    "azZ0OFBMQ2VRZmYrWVc4YS9wRU03Wm1tMTJ0Y3BRdEF0OHVCSVdkZE9qaTN1c3BhVlA3CkZJUlhzNnJI"
    "ajIwTDd0dE9kMGpmKzRWQ0ZtV09FWE4rNWc0YS8rNkcrc3lxeDk4VlR2RVF5cDZVdWZnb0FoQkMKLzFV"
    "NG5XajdmMVRFQkV4dXBSRXFUK1lmUmp6aFJUR2NGN0czRUp3MmZjUU1taElIdFpVanM3endVY3NmblhD"
    "MwpGQzJBR3pBZnExSGV0WHU5amFOQWZSdjdLZXYxT2hvVmMzYUlONEd3UkpZRDNPNUFSQk5SRGpQUVFW"
    "UHBaVW5rCjB1WVdpZExSVDVRUVZMYnlSLzJFKytqTWFyRXBkVXRkZGY1anlwZW5pbFhUQWdNQkFBRT0K"
    "LS0tLS1FTkQgUFVCTElDIEtFWS0tLS0tCg=="
)

#: Same schema string cli.sh and the dashboard check enforce.
_CLI_MANIFEST_SCHEMA = "kirocrew-cli-artifact-manifest-v1"

#: Same ceiling cli.sh applies to the manifest with ``--max-filesize``.
_MANIFEST_MAX_BYTES = 65536

#: Received-bytes ceiling for the wheel download. The wheel is tens of MB
#: today; the cap exists so a misbehaving origin cannot fill the disk, not as
#: a tight bound. Enforced against received bytes, never Content-Length.
_WHEEL_MAX_BYTES = 500 * 1024 * 1024

#: Free-space floor on the venv parent before a shadow build is attempted.
#: A shadow tree is a full second install; failing before pip half-fills the
#: disk beats an ENOSPC mid-build. Sized to a current install (~350 MiB with
#: dependencies) plus headroom.
_SHADOW_MIN_FREE_BYTES = 1 * 1024 * 1024 * 1024

_FETCH_TIMEOUT_SECS = 30
#: Per-read socket timeout for the wheel download.
_WHEEL_FETCH_TIMEOUT_SECS = 300
#: Wall-clock bound on the whole wheel download. The per-read timeout alone lets
#: an origin that drips one byte every few minutes hold the apply indefinitely.
_WHEEL_FETCH_TOTAL_SECS = 900
_OPENSSL_TIMEOUT_SECS = 30
_VENV_CREATE_TIMEOUT_SECS = 120
#: pip resolves and downloads the full dependency set into a tree that has
#: never seen it — the same bound dep_sync uses for a cold reinstall, doubled
#: for a slow index on a cold cache, and for the compile fallback a host can
#: opt back into below.
_PIP_INSTALL_TIMEOUT_SECS = 900
_PROBE_TIMEOUT_SECS = 60
#: Dependencies are installed from prebuilt wheels only (the same policy as
#: cli.sh's `PIP_BINARY_ONLY`). Without it pip builds any dependency that has
#: no wheel for this host from its sdist, which needs a C toolchain the gateway
#: host was never required to have; with it, such a host fails fast with pip's
#: "No matching distribution found" naming the dependency.
_PIP_BINARY_ONLY = "--only-binary=:all:"
#: cli.sh's opt-in for a host that has the toolchain and wants the compile
#: fallback back; read from this (gateway) process's environment, so a service
#: unit has to carry it for an update to honour it.
_ALLOW_SOURCE_BUILDS_ENV = "KIROCREW_ALLOW_SOURCE_BUILDS"


def _pip_binary_policy() -> list[str]:
    """The pip flags that keep dependency resolution binary-only (see above)."""
    if os.environ.get(_ALLOW_SOURCE_BUILDS_ENV, "0") == "1":
        return []
    return [_PIP_BINARY_ONLY]


# Signed-but-optional, mirroring cli.sh: a breaking release adds a fleet
# floor (`min_version`). The signature still covers it (it stays in the
# canonical payload), so the set check tolerates exactly this key and
# nothing else. The floor is metadata for RUNNING installs; this engine
# always installs the signed version itself, so format is all it checks.
_MANIFEST_OPTIONAL_FIELDS = frozenset({"min_version"})
_MIN_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")

_MANIFEST_EXPECTED_FIELDS = {
    "algorithm",
    "channel",
    "key_id",
    "pub_date",
    "python_requires",
    "schema",
    "sha256",
    "signature",
    "version",
    "wheel_url",
}

_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_PUB_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class WheelUpdateError(Exception):
    """A wheel update step failed; the message is operator-facing.

    Every raise site leaves the install exactly as it found it: the stable
    link is only ever touched by :func:`promote`, which is the last step and
    is atomic.
    """


class WheelUpdateCancelled(WheelUpdateError):
    """The caller withdrew the apply before :func:`promote` ran.

    Raised only where nothing has been promoted, so the stable link still names
    the tree it named before the call. ``reason`` is the cancel's
    (:attr:`ApplyCancel.reason`).
    """

    def __init__(self, message: str, *, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class WheelUpdateBusy(WheelUpdateError):
    """Another writer holds this layout's update lock; nothing was touched."""


class WheelUpdateNotReady(WheelUpdateError):
    """A precondition is still settling (memory is being prepared); retry soon."""


class WheelUpdateSnapshotFailed(WheelUpdateError):
    """The pre-update memory copy did not land, so nothing is promoted.

    Never answered with the installer re-run, which would perform exactly the
    un-copied update this refusal stops.
    """


class WheelUpdateIncompatible(WheelUpdateError):
    """The SIGNED release metadata excludes this host; retrying will not help.

    Decided from the verified manifest before anything is downloaded (today:
    ``python_requires`` against the build interpreter), on every attempt.
    ``version``/``sha256`` name the release.
    """

    def __init__(self, message: str, *, version: str, sha256: str) -> None:
        super().__init__(message)
        self.version = version
        self.sha256 = sha256


#: Cancel reasons after which the process keeps running, so a partial tree is
#: removed at once. Any other reason (a shutdown, an exec, a hard exit) means the
#: process is ending: the tree is only renamed aside, and the next sweep deletes it.
_REMOVE_PARTIAL_TREE_REASONS = frozenset({"deadline", "cancel"})


class ApplyCancel:
    """A cancel flag whose :meth:`set` also kills the build child in flight.

    The apply runs on a worker thread its caller cannot stop. Setting this flag
    from any thread stops the apply at its next step boundary, and the hooks a
    running step registered (:meth:`on_set`) run synchronously inside ``set()``:
    a build child's process group is killed, a blocked download socket is shut
    down. So a caller that sets it and then exits leaves nothing writing behind.
    The first ``set`` decides :attr:`reason`.
    """

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._hooks: list[Callable[[], None]] = []
        self.reason = ""

    def set(self, reason: str = "cancel") -> None:
        with self._lock:
            if self._event.is_set():
                return
            self.reason = reason
            self._event.set()
            hooks = list(self._hooks)
        for hook in hooks:
            with contextlib.suppress(Exception):
                hook()

    def is_set(self) -> bool:
        return self._event.is_set()

    def on_set(self, hook: Callable[[], None]) -> Callable[[], None]:
        """Run *hook* when the flag is set (now, if it already is); return the undo."""
        with self._lock:
            fire_now = self._event.is_set()
            if not fire_now:
                self._hooks.append(hook)
        if fire_now:
            hook()

        def _remove() -> None:
            with self._lock:
                if hook in self._hooks:
                    self._hooks.remove(hook)

        return _remove


def _raise_if_cancelled(cancel: ApplyCancel | None) -> None:
    if cancel is not None and cancel.is_set():
        raise WheelUpdateCancelled(
            "the update was cancelled before promotion; the current install is unchanged",
            reason=cancel.reason,
        )


@dataclass(frozen=True)
class _BuildContext:
    """What every child of one locked apply shares."""

    cancel: ApplyCancel | None = None
    #: The update lock's descriptor, handed to EVERY child so the lock is held
    #: for as long as a child that could still write the tree is alive, even past
    #: this process (``flock`` locks belong to the open file description, which
    #: the child shares).
    lock_fd: int | None = None
    #: ``True`` for a child started inside the gateway: the trusted system PATH
    #: and no loader-injection variables (see :func:`_build_child_env`). ``False``
    #: (``kirocrew update`` in the operator's shell) passes the full environment,
    #: so a toolchain on the operator's PATH reaches a source build.
    trusted_env: bool = True


_NO_CONTEXT = _BuildContext()


def check_release_version(version: str) -> None:
    """Raise :class:`WheelUpdateError` unless *version* is in the release grammar.

    A version reaches the update paths from the unsigned feed (and the armed
    approval that records it) before the signed manifest is read, so each path
    refuses a malformed one as an update failure instead of a raw ``ValueError``
    from building its tree's name.
    """
    if not _VERSION_RE.fullmatch(version):
        raise WheelUpdateError(f"release version {version[:64]!r} fails validation")


@dataclass(frozen=True)
class ManagedVenvLayout:
    """Where the managed install's trees live for this data home."""

    #: The legacy fixed venv cli.sh installs into (``crew-venv``). Never
    #: written by this engine; functional fallback per the first-migration
    #: protocol.
    legacy: Path
    #: The stable symlink (``crew-venv-current``) every launch path should
    #: resolve through. May not exist yet on an unmigrated install.
    stable_link: Path

    def versioned_tree(self, version: str) -> Path:
        """The sibling tree a given version installs into.

        Raises :class:`WheelUpdateError` for a version outside the release grammar
        (:func:`check_release_version`).
        """
        check_release_version(version)
        return self.stable_link.with_name(f"{self.legacy.name}-{version}")

    def is_managed_tree(self, path: Path) -> bool:
        """Is *path* inside one of this layout's trees (legacy or versioned)?"""
        try:
            resolved = path.resolve()
        except OSError:
            return False
        prefix = f"{self.legacy.name}-"
        # Identity trust extends ONLY to the legacy root — the one path this
        # layout owns unconditionally. Every versioned sibling (a genuinely
        # promoted tree included) goes through the convention-plus-artifacts
        # branch below, which every real install satisfies (bin/kirocrew ships
        # with every completed build, the sentinel with every in-flight one).
        # The stable link's TARGET is deliberately NOT an identity root: the
        # respawn guard uses this predicate to detect a link repointed OUTSIDE
        # the layout, and trusting the target by identity would make that
        # check vacuously true.
        try:
            legacy_resolved = self.legacy.resolve()
            if resolved == legacy_resolved or resolved.is_relative_to(legacy_resolved):
                return True
        except OSError:
            pass
        # A tree created after this snapshot: matched by naming convention in
        # the same parent — but POSITIVELY identified, never by prefix alone.
        # `KIROCREW_VENV=/srv/crew` makes the prefix "crew-", which a sibling
        # like /srv/crew-dev (someone's unrelated venv) satisfies by name; a
        # restart would then resolve through /srv/crew-current and switch an
        # unmanaged process's runtime. A managed versioned tree additionally
        # carries this engine's own artifacts: the kirocrew console script
        # (every completed install ships it) or the build sentinel (every
        # in-flight build does).
        parent = self.legacy.parent
        try:
            if resolved.is_relative_to(parent.resolve()):
                rel = resolved.relative_to(parent.resolve())
                head = rel.parts[0] if rel.parts else ""
                if head == self.legacy.name:
                    return True
                if not head.startswith(prefix):
                    return False
                candidate = parent / head
                return (candidate / "bin" / "kirocrew").exists() or (
                    candidate / _SHADOW_SENTINEL
                ).exists()
        except OSError:
            pass
        return False


def managed_venv_layout() -> ManagedVenvLayout:
    """Resolve the managed-venv layout for this install.

    Mirrors cli.sh's path rule exactly: ``KIROCREW_VENV`` wins, else the venv
    lives BESIDE the data home (``${KIROCREW_HOME%/}-venv``) — never inside
    it, for the blast-radius reason cli.sh documents.
    """
    override = os.environ.get("KIROCREW_VENV", "").strip()
    if override:
        legacy = Path(os.path.abspath(override))
    else:
        legacy = Path(f"{str(data_home()).rstrip('/')}-venv")
    return ManagedVenvLayout(
        legacy=legacy,
        stable_link=legacy.with_name(f"{legacy.name}-current"),
    )


def running_from_managed_venv(layout: ManagedVenvLayout | None = None) -> bool:
    """Is THIS process served by the managed venv (legacy or versioned tree)?

    The dispatch predicate for the shadow path: a pipx install, a system
    Python, or a dev venv must never take it — their bytes are owned by
    something else, and building a sibling tree beside a data home they do not
    use would be litter at best. POSIX-only by construction: cli.sh is a POSIX
    installer, so on Windows there is no managed venv to detect.

    Identity comes from the interpreter's ``bin/`` directory, not the file:
    ``python -m venv`` symlinks ``bin/python3`` to the base interpreter, so
    resolving that file leaves the venv before the layout can recognize it.
    """
    if not IS_POSIX:
        return False
    if layout is None:
        layout = managed_venv_layout()
    return layout.is_managed_tree(Path(sys.executable).parent)


#: Name of the metadata file pipx writes at the root of every environment it
#: manages (``$PIPX_HOME/venvs/<env>/pipx_metadata.json``). pipx owns it,
#: rewrites it atomically after each operation, and reads it to drive its own
#: ``upgrade``/``reinstall``/``uninstall`` — so its presence beside the running
#: interpreter is an authoritative, version-stable "this venv is pipx's" signal
#: that needs no path-guessing against ``$PIPX_HOME``.
_PIPX_METADATA_NAME = "pipx_metadata.json"


def running_from_pipx() -> bool:
    """Is THIS process served by a pipx-managed environment?

    Distinguishes the one non-managed shape whose installer re-run is
    legitimate — ``cli.sh`` installs into a pipx venv when pipx is present, and
    a re-run upgrades that same pipx venv in place — from a user's own plain
    ``pip install`` into a venv they manage, where a re-run would build a
    SECOND copy beside the one actually serving the user.

    Identity is the metadata file pipx keeps at the environment root. A pipx
    venv's root is ``sys.prefix`` for a process launched from it (the console
    script runs the venv's own interpreter), so the marker sits one directory
    up from ``bin/``. Reading ``sys.prefix`` rather than resolving
    ``sys.executable`` keeps the answer stable across the ``bin/python3 ->``
    base-interpreter symlink that ``python -m venv`` writes.
    """
    try:
        return (Path(sys.prefix) / _PIPX_METADATA_NAME).is_file()
    except OSError:
        return False


def _legacy_nested_venv() -> Path:
    """The venv an earlier ``cli.sh`` created INSIDE the data home.

    Mirrors cli.sh's ``_OLD_VENV`` (``<data home>/venv``) exactly. The current
    installer retires it: a re-run that lands a working tree beside the data
    home repoints the stable link and the launcher at that tree, then
    ``rm -rf``s this one. An installer re-run while a gateway still serves
    from this venv deletes it underneath the running process.
    """
    return data_home() / "venv"


def _respawn_tree_is_managed(layout: ManagedVenvLayout) -> bool:
    """Is the tree serving THIS process one a restart may re-route?

    Identity is read from the interpreter's ``bin/`` directory, not from the
    interpreter file: ``python -m venv`` writes ``bin/python3`` as a symlink
    to the base interpreter, so resolving the file lands outside every venv
    and would answer "not managed" for every real install. The directory
    resolves through the layout's own links (stable link -> versioned tree)
    and stops there.

    Two identities qualify. A tree of this layout (legacy or versioned), and
    the retired in-data-home venv — our own environment from an earlier
    installer, which the same cli.sh re-run that lands the new tree deletes
    from under the running gateway; without the stable link that process has
    no interpreter left to exec. This is the respawn identity only: the
    dispatch predicate for the shadow-build path is
    :func:`running_from_managed_venv`, which keeps its own rule.
    """
    bin_dir = Path(sys.executable).parent
    if layout.is_managed_tree(bin_dir):
        return True
    try:
        resolved = bin_dir.resolve()
        nested = _legacy_nested_venv().resolve()
    except OSError:
        return False
    return resolved == nested or resolved.is_relative_to(nested)


def _interpreter_in(tree: Path) -> str | None:
    """The usable interpreter inside *tree*'s ``bin/``, or ``None``.

    Prefers this process's own interpreter name so a restart keeps the version
    it was launched under, and falls back to ``python3``, which every venv
    ships.
    """
    for name in (os.path.basename(sys.executable), "python3"):
        candidate = tree / "bin" / name
        try:
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
        except OSError:
            continue
    return None


def _respawn_fallback(layout: ManagedVenvLayout) -> str:
    """The answer when the stable link cannot carry the restart.

    ``sys.executable`` whenever it still exists — a broken, absent, or
    outside-the-layout link must never take the restart path away from a
    process whose own interpreter is fine.

    When it does NOT exist, the cached path names nothing and the exec would
    raise ENOENT. That is reachable: the installer re-run that retires the
    in-data-home venv deletes it on the NEW tree's own import check, which is
    independent of the stable-link repoint — the repoint is skipped when a real
    directory sits at the stable name, and its failure is non-fatal. So the
    nested venv is gone while the link is unusable. The legacy fixed tree is
    the interpreter that re-run just built and import-verified, and this engine
    never prunes it, so it is the honest last resort. Validated the same way as
    the stable link's: inside the layout, present, executable.
    """
    if os.path.exists(sys.executable):
        return sys.executable
    if layout.is_managed_tree(layout.legacy):
        candidate = _interpreter_in(layout.legacy)
        if candidate is not None:
            return candidate
    return sys.executable


def respawn_executable() -> str:
    """The interpreter a gateway restart should exec.

    ``sys.executable`` is the answer for every install shape EXCEPT a managed
    venv that has been promoted past the tree this process started from: there
    the cached path resolves into the OLD versioned tree (still on disk, so
    the exec would succeed — and silently resurrect the old version). Routing
    through the stable link is what makes a restart pick up a promotion, and
    it is one of the four persisted launch paths RFC §3 requires to resolve
    through the stable name.

    Falls back to ``sys.executable`` whenever the stable link does not exist
    or does not carry a usable interpreter, so a broken or absent link can
    never take the restart path away — except when ``sys.executable`` itself is
    already gone, where :func:`_respawn_fallback` reaches the legacy tree
    instead of handing back a path that does not exist.

    One more shape routes here: a process still served by the retired
    in-data-home venv (:func:`_legacy_nested_venv`). The installer re-run that
    migrates it deletes that venv after repointing the stable link, so
    ``sys.executable`` is gone and the stable link is the only interpreter
    left; before that re-run there is no stable link and the fallback keeps
    the restart on the nested venv, unchanged.

    The answer names the link's RESOLVED tree (``crew-venv-<version>/bin/…``),
    never ``crew-venv-current/bin/…``. An interpreter started through the link
    takes its ``sys.prefix``, and so every later import, its own package data,
    its installed metadata and its ``sys.executable``, from the link, and the
    next promotion would swap all of that under the running process. Only the
    directory is resolved: ``bin/python3`` is itself a symlink to the base
    interpreter, and resolving it would leave the venv entirely.
    """
    if not IS_POSIX:
        return sys.executable
    layout = managed_venv_layout()
    if not _respawn_tree_is_managed(layout):
        return sys.executable
    # The link's TARGET must resolve inside this layout's own trees before it
    # is trusted with an exec: a stable link repointed outside the managed
    # parent answers sys.executable instead. Scope honestly stated: this pins
    # WHERE the interpreter may live, not WHO wrote it — an actor with shell
    # as the gateway's user can rewrite the trees themselves (sys.executable's
    # own tree included), which is the RFC's accepted local-code-execution gap
    # and is not widened by the link.
    if not layout.is_managed_tree(layout.stable_link):
        return _respawn_fallback(layout)
    try:
        target = Path(os.path.realpath(layout.stable_link))
    except OSError:
        return _respawn_fallback(layout)
    candidate = _interpreter_in(target)
    if candidate is not None:
        return candidate
    return _respawn_fallback(layout)


def runs_through_stable_link(path: str | None = None) -> bool:
    """Does *path* (``sys.prefix`` by default) name its tree through the stable link?

    Read lexically, the way the interpreter itself keeps it: a process started as
    ``crew-venv-current/bin/python3`` keeps that spelling in ``sys.prefix``, so
    every module it imports later resolves through the link, and the next
    promotion swaps them under it. Only each component's parent is resolved, so
    a symlinked home still matches; the link itself is never followed.
    """
    if not IS_POSIX:
        return False
    layout = managed_venv_layout()
    link_parent = _realpath(layout.stable_link.parent)
    if link_parent is None:
        return False
    link = link_parent / layout.stable_link.name
    lexical = Path(os.path.abspath(sys.prefix if path is None else path))
    for candidate in (lexical, *lexical.parents):
        parent = _realpath(candidate.parent)
        if candidate.name and parent is not None and parent / candidate.name == link:
            return True
    return False


def stable_launch_path(path: str) -> str:
    """*path* rewritten through the stable link when it lies in a versioned tree.

    For a value that is PERSISTED and later executed outside the writing
    process's lifetime (a service ``ExecStart``, a launchd launcher, the
    ``~/.local/bin`` shim): a path inside
    ``crew-venv-<version>/`` would keep naming that tree after the next update
    promoted another one, and the prune may delete it. Through
    ``crew-venv-current/`` it follows every promotion. That holds whichever
    tree the link targets now: a path in an older tree is rewritten too, since
    that tree is the one a later prune removes. Returned unchanged when *path*
    is not inside an engine-built tree of this layout, or when the link is
    dangling or its tree has no file at the same relative path.
    """
    if not IS_POSIX or not path:
        return path
    layout = managed_venv_layout()
    try:
        resolved = Path(os.path.realpath(path))
        parent = Path(os.path.realpath(layout.legacy.parent))
        rel = resolved.relative_to(parent)
    except (OSError, ValueError):
        return path
    if len(rel.parts) < 2 or not _is_owned_tree(layout, parent / rel.parts[0]):
        return path
    through = layout.stable_link.joinpath(*rel.parts[1:])
    try:
        if os.path.exists(through):
            return str(through)
    except OSError:
        pass
    return path


# ── Manifest fetch and verification ─────────────────────────────────────────


def _staging_dir() -> Path:
    """Per-run staging area under the data home's ``trust/`` keystone directory.

    The verified wheel sits on disk between its SHA-256 check and the pip
    install, and a TMPDIR staging file is writable by the agent (same uid, and
    TMPDIR is deliberately agent-reachable scratch) — a swap in that window
    would promote unverified code. ``trust/`` is the existing whole-directory
    keystone leaf: the agent's file gate and every bash form refuse it, while
    this engine (the gateway or the operator's CLI) writes directly. Same
    accepted residual as every keystone leaf: arbitrary local code as the
    user is out of scope, the AGENT's tool surface is what is fenced.
    """
    root = data_home() / "trust" / "update-staging"
    make_owner_only_dir(root)
    return root


def _response_socket(resp: object) -> object | None:
    """The socket under a urllib response, when the stdlib layout exposes it."""
    raw = getattr(getattr(resp, "fp", None), "raw", None)
    return getattr(raw, "_sock", None)


class _SinkWriteError(Exception):
    """A :func:`_read_bounded` sink's own write failed (a full or failing staging disk).

    Not a :class:`WheelUpdateError` and not an :class:`OSError`, so it crosses
    the fetch's own error handling untouched: the fetch would otherwise report
    the caller's write failure as ``could not fetch <url>``. The caller that
    owns the destination converts it, naming that destination.
    """

    def __init__(self, error: OSError) -> None:
        super().__init__(str(error))
        self.error = error


def _read_bounded(
    url: str,
    *,
    cap: int,
    read_timeout: float,
    total_secs: float,
    cancel: ApplyCancel | None,
    sink: Callable[[bytes], None],
) -> None:
    """Stream *url* into *sink*, bounded per read, in total, in size and by *cancel*.

    ``read1`` returns whatever has arrived rather than looping until a full
    chunk does, so the deadline and the cancel are checked after every read even
    against an origin that drips one byte at a time. Each read's socket timeout
    is the smaller of *read_timeout* and what is left of *total_secs*, and
    setting *cancel* shuts the socket down, so neither waits on a silent origin.
    Enforced against received bytes, never Content-Length. An ``OSError`` from
    *sink* itself is raised as :class:`_SinkWriteError`, never as a fetch failure.
    """
    if not url.startswith("https://"):
        raise WheelUpdateError(f"refusing non-HTTPS URL: {url}")
    _raise_if_cancelled(cancel)
    deadline = time.monotonic() + total_secs
    req = urllib.request.Request(url, headers={"User-Agent": "kirocrew-update/1"})
    received = 0
    undo: Callable[[], None] | None = None
    try:
        with urllib.request.urlopen(  # nosemgrep: dynamic-urllib-use-detected
            req, timeout=min(read_timeout, total_secs)
        ) as resp:
            sock = _response_socket(resp)
            if cancel is not None and sock is not None:

                def _shut() -> None:
                    with contextlib.suppress(OSError):
                        sock.shutdown(socket.SHUT_RDWR)  # type: ignore[attr-defined]

                undo = cancel.on_set(_shut)
            while True:
                _raise_if_cancelled(cancel)
                left = deadline - time.monotonic()
                if left <= 0:
                    raise WheelUpdateError(f"{url} did not finish within {total_secs:.0f}s")
                if sock is not None:
                    sock.settimeout(min(read_timeout, left))  # type: ignore[attr-defined]
                chunk = resp.read1(65536)
                # Before EOF is read as "complete": a cancel shuts the socket,
                # which ends the read the same way a finished body does.
                _raise_if_cancelled(cancel)
                if not chunk:
                    return
                received += len(chunk)
                if received > cap:
                    raise WheelUpdateError(f"{url} exceeded the {cap}-byte ceiling")
                try:
                    sink(chunk)
                except OSError as exc:
                    raise _SinkWriteError(exc) from exc
    except (WheelUpdateError, _SinkWriteError):
        raise
    except (urllib.error.URLError, OSError, ValueError) as exc:
        # A socket shut down by the cancel surfaces here as a read error.
        _raise_if_cancelled(cancel)
        if time.monotonic() >= deadline:
            raise WheelUpdateError(f"{url} did not finish within {total_secs:.0f}s") from exc
        raise WheelUpdateError(f"could not fetch {url}: {exc}") from exc
    finally:
        if undo is not None:
            undo()


def _download_to_file(
    url: str,
    dest: Path,
    cap: int,
    timeout: float,
    expected_sha: str,
    *,
    cancel: ApplyCancel | None = None,
    total_secs: float = _WHEEL_FETCH_TOTAL_SECS,
) -> None:
    """Stream *url* into *dest*, enforcing *cap* and the digest INCREMENTALLY.

    Chunked rather than buffered: the wheel ceiling is 500 MiB, and one
    ``resp.read()`` of that size doubles as a memory spike on the very gateway
    the update is trying to keep alive. The hash is folded in as chunks land,
    so the verify step never re-reads the file it just wrote — and the digest
    therefore covers the exact bytes on disk. Bounded as :func:`_read_bounded`
    describes.
    """
    digest = hashlib.sha256()
    try:
        with open(dest, "wb") as out:

            def _sink(chunk: bytes) -> None:
                digest.update(chunk)
                out.write(chunk)

            _read_bounded(
                url,
                cap=cap,
                read_timeout=timeout,
                total_secs=total_secs,
                cancel=cancel,
                sink=_sink,
            )
    except (OSError, _SinkWriteError) as exc:
        # The open itself, or a write mid-stream (surfaced distinctly by
        # ``_read_bounded`` so it is not reported as a fetch failure).
        cause = exc.error if isinstance(exc, _SinkWriteError) else exc
        with contextlib.suppress(OSError):
            dest.unlink(missing_ok=True)
        raise WheelUpdateError(f"could not write {dest}: {cause}") from cause
    except BaseException:
        with contextlib.suppress(OSError):
            dest.unlink(missing_ok=True)
        raise
    got = digest.hexdigest()
    if got != expected_sha:
        with contextlib.suppress(OSError):
            dest.unlink(missing_ok=True)
        raise WheelUpdateError(
            f"wheel SHA-256 mismatch (expected {expected_sha}, got {got}) — refusing to install"
        )


def _fetch_bytes(url: str, cap: int, timeout: float, *, cancel: ApplyCancel | None = None) -> bytes:
    """GET *url*, refusing more than *cap* received bytes or *timeout* in total.

    The single small-document network seam of this module, so tests stub one
    function. HTTPS is asserted as defence in depth — the callers validate the
    CDN base before composing the URL, but this function must hold on its own.
    """
    parts: list[bytes] = []
    _read_bounded(
        url, cap=cap, read_timeout=timeout, total_secs=timeout, cancel=cancel, sink=parts.append
    )
    return b"".join(parts)


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def _openssl_bin() -> str:
    openssl = trusted_system_bin("openssl")
    if openssl is None:
        raise WheelUpdateError(
            "openssl is required to verify the signed manifest and was not "
            "found in a trusted system directory"
        )
    return openssl


def _check_key_fingerprint(openssl: str, *, ctx: _BuildContext = _NO_CONTEXT) -> bytes:
    """Return the embedded public key PEM after self-checking its fingerprint.

    The PEM goes to ``openssl pkey`` on STDIN and the DER comes back on STDOUT —
    no file is written by name, so there is no name for a concurrent same-uid
    process to pre-plant as a symlink and have the gateway follow. The DER's
    SHA-256 must equal :data:`CLI_MANIFEST_KEY_ID`, so an accidental edit to
    either constant fails closed before any signature is considered.
    """
    try:
        pem = base64.b64decode(CLI_MANIFEST_PUBLIC_KEY_B64, validate=True)
    except ValueError as exc:
        raise WheelUpdateError("embedded manifest public key is malformed") from exc
    rc, der, _err = _spawn_build_child(
        [openssl, "pkey", "-pubin", "-outform", "DER"],
        _OPENSSL_TIMEOUT_SECS,
        "reading the pinned manifest key with openssl",
        ctx=ctx,
        want_stdout=True,
        stdin_data=pem,
    )
    if rc != 0:
        raise WheelUpdateError("embedded manifest public key is invalid")
    fingerprint = "sha256:" + hashlib.sha256(der).hexdigest()
    if fingerprint != CLI_MANIFEST_KEY_ID:
        raise WheelUpdateError("embedded manifest public key fingerprint mismatch")
    return pem


def _verify_signature(
    canonical: bytes, signature: bytes, workdir: Path, *, ctx: _BuildContext = _NO_CONTEXT
) -> None:
    """Verify *signature* over *canonical* against the pinned trust root.

    Delegates the RSA math to the ``openssl`` binary — the same verifier
    cli.sh uses, resolved through :func:`trusted_system_bin` so a planted
    PATH shim cannot stand in for it. The pinned key's fingerprint is
    self-checked first. Each openssl child runs through
    :func:`_spawn_build_child`, so only a genuine refusal reads "signature
    verification failed"; an openssl that cannot run or times out says so in
    its own words.

    No verification INPUT is ever written to a predictable, shared, or
    agent-reachable path. The gateway runs OUTSIDE the sandbox, so writing the
    PEM/signature/payload to a named file in an agent-writable directory (the
    SEL ``trust`` keystone is sandbox read-write) would let a concurrent
    same-uid agent pre-plant that name as a symlink to an operator file and have
    this process clobber it through the write. So on POSIX the key and signature
    are handed to openssl over anonymous pipe FDs (``/dev/fd/N``) the gateway
    created, and the payload over stdin — no file exists to plant against.
    ``workdir`` is used only as a Windows fallback (no ``/dev/fd`` there), and
    then every file is opened ``O_CREAT|O_EXCL|O_NOFOLLOW`` so a pre-planted
    symlink is refused rather than followed.
    """
    openssl = _openssl_bin()
    pem = _check_key_fingerprint(openssl, ctx=ctx)

    devfd = IS_POSIX and os.path.isdir("/dev/fd")
    if devfd:
        _verify_over_fds(openssl, pem, signature, canonical, ctx=ctx)
    else:
        _verify_over_nofollow_files(openssl, pem, signature, canonical, workdir, ctx=ctx)


def _verify_over_fds(
    openssl: str,
    pem: bytes,
    signature: bytes,
    canonical: bytes,
    *,
    ctx: _BuildContext = _NO_CONTEXT,
) -> None:
    """POSIX path: key + signature over anonymous pipe FDs, payload over stdin.

    Nothing is written to any directory, so there is no attacker-plantable name.
    """
    key_r, key_w = os.pipe()
    sig_r, sig_w = os.pipe()
    try:
        try:
            # Write both inputs fully before the child reads them. They are small
            # (a 2048-bit key, a 256-byte signature), well under a pipe's buffer,
            # so a single write cannot block against a reader that has not started.
            os.write(key_w, pem)
            os.write(sig_w, signature)
        finally:
            os.close(key_w)
            os.close(sig_w)
        rc, _out, _err = _spawn_build_child(
            [
                openssl,
                "dgst",
                "-sha256",
                "-verify",
                f"/dev/fd/{key_r}",
                "-signature",
                f"/dev/fd/{sig_r}",
                "/dev/stdin",
            ],
            _OPENSSL_TIMEOUT_SECS,
            "checking the manifest signature with openssl",
            ctx=ctx,
            stdin_data=canonical,
            extra_fds=(key_r, sig_r),
        )
    finally:
        for fd in (key_r, sig_r):
            try:
                os.close(fd)
            except OSError:
                pass
    if rc != 0:
        raise WheelUpdateError("manifest signature verification failed — refusing to install")


def _create_exclusive_nofollow(path: Path, data: bytes) -> None:
    """Write *data* to *path*, refusing to follow or clobber anything there.

    ``O_CREAT|O_EXCL`` fails if the name already exists (a pre-planted file or
    symlink), and ``O_NOFOLLOW`` refuses a symlink at the final component — so
    the gateway never follows an agent-planted link out of the staging dir.

    ``O_BINARY`` (Windows only; 0 elsewhere) keeps the write byte-exact: in text
    mode Windows translates the canonical payload's trailing ``\\n`` to ``\\r\\n``,
    which changes the signed bytes and makes verification fail closed.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _verify_over_nofollow_files(
    openssl: str,
    pem: bytes,
    signature: bytes,
    canonical: bytes,
    workdir: Path,
    *,
    ctx: _BuildContext = _NO_CONTEXT,
) -> None:
    """Fallback (no ``/dev/fd``, e.g. Windows): stage in *workdir*, but create
    every file ``O_CREAT|O_EXCL|O_NOFOLLOW`` so a pre-planted symlink or file is
    refused rather than followed, and read none of them back by name after."""
    pem_path = workdir / "cli-manifest-public.pem"
    payload_path = workdir / "signed-payload.json"
    sig_path = workdir / "manifest-signature.bin"
    try:
        _create_exclusive_nofollow(pem_path, pem)
        _create_exclusive_nofollow(payload_path, canonical)
        _create_exclusive_nofollow(sig_path, signature)
    except OSError as exc:
        raise WheelUpdateError(f"could not stage verification inputs: {exc}") from exc
    rc, _out, _err = _spawn_build_child(
        [
            openssl,
            "dgst",
            "-sha256",
            "-verify",
            str(pem_path),
            "-signature",
            str(sig_path),
            str(payload_path),
        ],
        _OPENSSL_TIMEOUT_SECS,
        "checking the manifest signature with openssl",
        ctx=ctx,
        cwd=str(workdir),
    )
    if rc != 0:
        raise WheelUpdateError("manifest signature verification failed — refusing to install")


def parse_and_validate_manifest(
    raw: bytes,
    *,
    channel: str,
    artifact_base: str,
) -> tuple[dict[str, str], bytes, bytes]:
    """Structural validation + canonicalization, PURE (no I/O).

    Returns ``(payload, canonical_bytes, signature)`` for
    :func:`_verify_signature`. A byte-for-byte port of the two validation
    heredocs in cli.sh: duplicate keys, extra/missing fields, non-string
    values, oversized payloads, and a wheel URL that is not the ONE canonical
    URL implied by the byte host + channel + signed version are all refused.
    Nothing in the payload is acted on until the signature over the canonical
    bytes verifies.
    """
    if len(raw) > _MANIFEST_MAX_BYTES:
        raise WheelUpdateError("manifest exceeds the size ceiling")
    try:
        manifest = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError) as exc:
        raise WheelUpdateError(f"manifest is not valid JSON: {exc}") from exc
    if (
        not isinstance(manifest, dict)
        or not _MANIFEST_EXPECTED_FIELDS <= set(manifest)
        or set(manifest) - _MANIFEST_EXPECTED_FIELDS - _MANIFEST_OPTIONAL_FIELDS
    ):
        raise WheelUpdateError("manifest carries unexpected fields")
    if not all(isinstance(v, str) and v for v in manifest.values()):
        raise WheelUpdateError("manifest carries an invalid field type")
    if "min_version" in manifest and not _MIN_VERSION_RE.match(manifest["min_version"]):
        raise WheelUpdateError("manifest min_version fails validation")
    if manifest["schema"] != _CLI_MANIFEST_SCHEMA:
        raise WheelUpdateError("unsupported manifest schema")
    if manifest["algorithm"] != "RSASSA_PKCS1_V1_5_SHA_256":
        raise WheelUpdateError("unsupported signature algorithm")
    if manifest["key_id"] != CLI_MANIFEST_KEY_ID:
        raise WheelUpdateError("manifest signed by an untrusted key")
    if manifest["channel"] != channel:
        raise WheelUpdateError(
            f"manifest channel {manifest['channel']!r} does not match {channel!r}"
        )
    version = manifest["version"]
    check_release_version(version)
    if not _SHA256_RE.match(manifest["sha256"]):
        raise WheelUpdateError("manifest sha256 fails validation")
    if not _PUB_DATE_RE.match(manifest["pub_date"]):
        raise WheelUpdateError("manifest pub_date fails validation")
    requires = manifest["python_requires"]
    if len(requires) > 128 or any(not (0x20 <= ord(c) <= 0x7E) for c in requires):
        raise WheelUpdateError("manifest python_requires fails validation")
    wheel_name = f"kirocrew-{version}-py3-none-any.whl"
    expected_url = f"{artifact_base}/cli/{channel}/{version}/{wheel_name}"
    if manifest["wheel_url"] != expected_url:
        raise WheelUpdateError("manifest wheel_url is not the canonical artifact URL")

    try:
        signature = base64.b64decode(manifest["signature"], validate=True)
    except ValueError as exc:
        raise WheelUpdateError("manifest signature is not valid base64") from exc
    if not signature or len(signature) > 1024:
        raise WheelUpdateError("manifest signature size is invalid")

    unsigned = {k: v for k, v in manifest.items() if k != "signature"}
    canonical = (
        json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n"
    ).encode("ascii")
    if len(canonical) > 16384:
        raise WheelUpdateError("canonical manifest payload is oversized")
    return {k: str(v) for k, v in unsigned.items()}, canonical, signature


def fetch_verified_manifest(
    *,
    channel: str,
    feed_base: str,
    artifact_base: str,
    workdir: Path,
    ctx: _BuildContext = _NO_CONTEXT,
) -> dict[str, str]:
    """Fetch the channel manifest and return its AUTHENTICATED payload."""
    url = f"{feed_base}/feed/{channel}/latest-cli.json"
    raw = _fetch_bytes(url, _MANIFEST_MAX_BYTES, _FETCH_TIMEOUT_SECS, cancel=ctx.cancel)
    payload, canonical, signature = parse_and_validate_manifest(
        raw, channel=channel, artifact_base=artifact_base
    )
    _raise_if_cancelled(ctx.cancel)
    _verify_signature(canonical, signature, workdir, ctx=ctx)
    return payload


def download_verified_wheel(
    payload: dict[str, str], dest_dir: Path, *, cancel: ApplyCancel | None = None
) -> Path:
    """Download the wheel the SIGNED payload names and verify its SHA-256."""
    url = payload["wheel_url"]
    expected_sha = payload["sha256"]
    wheel_path = dest_dir / f"kirocrew-{payload['version']}-py3-none-any.whl"
    _download_to_file(
        url,
        wheel_path,
        _WHEEL_MAX_BYTES,
        _WHEEL_FETCH_TIMEOUT_SECS,
        expected_sha,
        cancel=cancel,
    )
    return wheel_path


# ── Shadow build, verification, promotion ───────────────────────────────────


#: How much of a child's stderr is kept: its TAIL, where pip and venv put the
#: error. Written to an anonymous file rather than a pipe, so a chatty child can
#: neither fill the caller's memory nor block on a full pipe.
_STDERR_TAIL_BYTES = 64 * 1024
#: How much of a child's stdout is read when it is wanted (a version string, a
#: ``pip check`` report).
_STDOUT_MAX_BYTES = 64 * 1024
#: How much of a failed build step's redacted stderr goes into its message.
_ERROR_DETAIL_CHARS = 2000
#: Ceiling on failure text an operator is shown in one line (a dashboard step, an
#: ERROR log line, an audit record, a probe's detail). Shared with
#: :mod:`kiro_crew.platform.wheel_apply`.
FAILURE_TEXT_CHARS = 500
#: How often a running child is polled for exit. Its own kill needs no poll:
#: a timeout or a cancel kills the group at once; this bounds only how soon the
#: kill (or an ordinary exit) is noticed.
_REAP_POLL_SECS = 0.05

#: Loader variables the gateway's own process may need its children to keep:
#: a base interpreter built with a shared libpython finds it through them. The
#: INJECTION variables (``LD_PRELOAD``, ``DYLD_INSERT_LIBRARIES``) stay removed.
_KEPT_LOADER_PATHS = ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FALLBACK_LIBRARY_PATH")


def _build_child_env(trusted: bool) -> dict[str, str]:
    """The environment a child of the apply runs with.

    *trusted* (a child started inside the gateway): the update commands'
    scrubbed environment (``PATH`` narrowed to the trusted system directories,
    the interpreter and loader injection variables and exported shell functions
    removed), keeping only the library search paths the gateway itself was
    started with (:data:`_KEPT_LOADER_PATHS`). Otherwise (``kirocrew update`` in
    the operator's shell) the full environment, so the operator's toolchain and
    credential helpers reach pip. Interpreter children run ``-I`` either way, so
    an inherited ``PYTHONPATH`` never reaches pip's dependency resolution.
    """
    if not trusted:
        return dict(os.environ)
    from kiro_crew.platform.update_provider import _trusted_path_env

    env = _trusted_path_env()
    if env is None:
        raise WheelUpdateError("no trusted system PATH to run the update's build steps with")
    for name in _KEPT_LOADER_PATHS:
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _spawn_build_child(
    argv: list[str],
    timeout: float,
    step: str,
    *,
    ctx: _BuildContext = _NO_CONTEXT,
    cwd: str = "/",
    want_stdout: bool = False,
    umask: int = -1,
    stdin_data: bytes | None = None,
    extra_fds: tuple[int, ...] = (),
) -> tuple[int, bytes, bytes]:
    """Run one child of the apply to completion; return ``(rc, stdout, stderr tail)``.

    Every child of the apply comes through here. It runs in its own session, so
    its whole process group can be killed (``python -m venv`` runs ``ensurepip``
    as a grandchild), with the environment :func:`_build_child_env` gives it,
    with the update lock's descriptor (:attr:`_BuildContext.lock_fd`), and with
    no pipes: stdout and stderr go to anonymous files. *stdin_data*, when
    given, reaches the child through an anonymous file too, and *extra_fds*
    are inherited beside the lock's descriptor (openssl reads its key and
    signature from them as ``/dev/fd/N``).

    The group is killed when *timeout* passes, when the cancel is set
    (synchronously, inside :meth:`ApplyCancel.set`), and when this call is
    interrupted, and the child is reaped before this returns or raises. A
    killed child never reports a status. The exit is polled under the same lock
    the kill takes, so a kill is only ever signalled while the leader is
    unreaped: its pid, which names the group, cannot have been reused.

    Raises :class:`WheelUpdateError` naming *step* when the child cannot start
    or times out, and :class:`WheelUpdateCancelled` on cancel.
    """
    _raise_if_cancelled(ctx.cancel)
    env = _build_child_env(ctx.trusted_env)
    pass_fds = ((ctx.lock_fd,) if ctx.lock_fd is not None else ()) + tuple(extra_fds)
    with (
        tempfile.TemporaryFile() as infile,
        tempfile.TemporaryFile() as outfile,
        tempfile.TemporaryFile() as errfile,
    ):
        if stdin_data is not None:
            infile.write(stdin_data)
            infile.seek(0)
        try:
            proc = subprocess.Popen(
                argv,
                stdin=infile if stdin_data is not None else subprocess.DEVNULL,
                stdout=outfile if want_stdout else subprocess.DEVNULL,
                stderr=errfile,
                cwd=cwd,
                env=env,
                umask=umask,
                start_new_session=IS_POSIX,
                pass_fds=pass_fds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise WheelUpdateError(f"{step} could not run: {exc}") from exc
        guard = threading.Lock()
        killed_for: list[str] = []

        def _kill(reason: str) -> None:
            with guard:
                if proc.returncode is None:
                    killed_for.append(reason)
                    kill_popen_tree(proc)

        undo: Callable[[], None] | None = None
        try:
            if ctx.cancel is not None:
                undo = ctx.cancel.on_set(lambda: _kill("cancel"))
            deadline = time.monotonic() + timeout
            while True:
                with guard:
                    if proc.poll() is not None:
                        break
                if time.monotonic() >= deadline:
                    _kill("timeout")
                time.sleep(_REAP_POLL_SECS)
        except BaseException as exc:
            _kill("interrupted")
            proc.wait()
            if isinstance(exc, (OSError, subprocess.SubprocessError)):
                raise WheelUpdateError(f"{step} failed while running: {exc}") from exc
            raise
        finally:
            if undo is not None:
                undo()
        if killed_for and killed_for[0] == "cancel":
            _raise_if_cancelled(ctx.cancel)
        if killed_for:
            raise WheelUpdateError(f"{step} timed out after {timeout:.0f}s")
        outfile.seek(0)
        out = outfile.read(_STDOUT_MAX_BYTES)
        errfile.seek(0, os.SEEK_END)
        errfile.seek(max(0, errfile.tell() - _STDERR_TAIL_BYTES))
        return proc.returncode, out, errfile.read()


def _redacted_detail(raw: bytes, limit: int = _ERROR_DETAIL_CHARS) -> str:
    """A child's output for an error message: redacted IN FULL, then cut to its tail.

    The order is the point. Cutting first can split a credential, and half a
    token does not match the redactors' patterns, so the surviving fragment
    would reach the log and the dashboard verbatim.
    """
    from kiro_crew.platform.context import redact_log_via_context

    return redact_log_via_context(utf8_stdout(raw).strip())[-limit:]


def _run(argv: list[str], timeout: float, step: str, *, ctx: _BuildContext = _NO_CONTEXT) -> None:
    """Run one build step; raise :class:`WheelUpdateError` with its redacted tail on failure.

    Build steps write into the shadow tree, so they run under the owner-only
    build umask (see :data:`_BUILD_UMASK`).
    """
    rc, _out, err = _spawn_build_child(argv, timeout, step, ctx=ctx, umask=_BUILD_UMASK)
    if rc != 0:
        detail = _redacted_detail(err)
        raise WheelUpdateError(f"{step} exited {rc}" + (f": {detail}" if detail else ""))


#: Owner-only umask for the venv/pip build children (POSIX; -1 = leave unchanged).
#: ``kirocrew service install`` refuses to attach the AppArmor unprivileged-userns
#: profile to a launcher that is group- or world-writable (service/apparmor.py), and
#: ``python -m venv``/pip create ``bin/`` under the process umask — so a permissive
#: umask (``002``, common on shared dev hosts) would otherwise birth the tree ``0775``
#: and the profile install would refuse, recurring on every update. ``subprocess``
#: applies ``umask`` in the child between fork and exec (thread-safe, unlike a
#: ``preexec_fn`` in this threaded gateway), so bin/ and the launcher are born
#: owner-only with no window and the profile attaches.
_BUILD_UMASK = 0o077 if IS_POSIX else -1


#: Build sentinel: written into a shadow directory the moment this engine
#: creates it, removed only after the tree is promoted. Its PRESENCE says the
#: build never completed; together with :data:`TREE_MARKER` it is what makes a
#: directory deletable debris, whatever else the directory does or does not hold
#: (an interrupted build can leave nothing but these two files).
_SHADOW_SENTINEL = ".kirocrew-shadow-incomplete"

#: Infix of a tree being deleted (``.crew-venv-<v>.deleting-<pid>``). A tree is
#: renamed to a tombstone first and removed after: an interrupted removal of the
#: tree in place would leave a directory under the versioned name that is not a
#: whole venv. The tombstone is deleted with its ownership proofs last
#: (:func:`_remove_tombstone`), so an interrupted removal stays sweepable.
_TOMBSTONE_INFIX = ".deleting-"


def _versioned_name(layout: ManagedVenvLayout) -> re.Pattern[str]:
    """The one spelling of a versioned sibling's name: ``<legacy>-<version>``."""
    return re.compile(re.escape(f"{layout.legacy.name}-") + _VERSION_RE.pattern[1:])


def _realpath(path: Path) -> Path | None:
    try:
        return Path(os.path.realpath(path))
    except (OSError, RuntimeError):
        return None


def _is_stable_target(layout: ManagedVenvLayout, tree: Path) -> bool:
    """Is *tree* what the stable link resolves to? The one form of this check."""
    target, real = _realpath(layout.stable_link), _realpath(tree)
    return target is not None and real is not None and target == real


def _is_owned_tree(layout: ManagedVenvLayout, tree: Path) -> bool:
    """Did THIS layout's engine build *tree*? Never raises; unreadable is "no".

    Positively identified, so another install that shares the parent directory
    (``KIROCREW_VENV=/srv/crew`` beside ``/srv/crew-beta``) is never touched: the
    exact versioned name (a tombstone of one counts), AND either the ownership
    marker naming this layout or, for a build an earlier engine left unfinished,
    no marker at all beside the build sentinel.
    """
    name = tree.name
    if name.startswith("."):
        head, sep, _pid = name[1:].rpartition(_TOMBSTONE_INFIX)
        if not sep:
            return False
        name = head
    if not _versioned_name(layout).fullmatch(name):
        return False
    try:
        if tree.is_symlink() or not tree.is_dir():
            return False
        marker = tree / TREE_MARKER
        if marker.exists():
            return marker.read_text(encoding="utf-8").strip() == str(layout.legacy)
        return (tree / _SHADOW_SENTINEL).exists()
    except (OSError, UnicodeDecodeError):
        return False


def _owned_siblings(layout: ManagedVenvLayout) -> list[Path]:
    """Every engine-built tree (and tombstone) of this layout beside it."""
    try:
        entries = list(layout.legacy.parent.iterdir())
    except OSError:
        return []
    return [entry for entry in entries if _is_owned_tree(layout, entry)]


def _is_unpromoted_debris(layout: ManagedVenvLayout, tree: Path) -> bool:
    """An owned tree whose build never completed.

    Shared by the sweep and a failed apply's cleanup, which both remove it only
    through :func:`_discard_if_unheld`, so a tree a process holds is kept. Never
    the stable link's target (a promoted tree keeps its sentinel for a moment
    after the flip, and for good if that unlink failed).
    """
    try:
        unfinished = (tree / _SHADOW_SENTINEL).exists()
    except OSError:
        return False
    return unfinished and _is_owned_tree(layout, tree) and not _is_stable_target(layout, tree)


def _remove_tombstone(tombstone: Path) -> None:
    """Delete *tombstone*, its ownership proofs last. Best effort; never raises.

    :func:`_is_owned_tree` recognises a tombstone by :data:`TREE_MARKER` (or, for
    an earlier engine's unfinished build, :data:`_SHADOW_SENTINEL`), and the build
    writes the marker first, so a plain ``rmtree`` unlinks it first too. An
    interrupted removal would then leave a tree nothing can identify as ours,
    which no sweep ever deletes.
    """
    proofs = (_SHADOW_SENTINEL, TREE_MARKER)
    try:
        entries = [entry for entry in tombstone.iterdir() if entry.name not in proofs]
    except OSError:
        return
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                entry.unlink()
    for name in proofs:
        with contextlib.suppress(OSError):
            (tombstone / name).unlink()
    with contextlib.suppress(OSError):
        tombstone.rmdir()


def _discard_tree(tree: Path, *, remove: bool = True) -> None:
    """Rename *tree* to a tombstone, then (when *remove*) delete the tombstone.

    The rename is atomic, so the versioned name is free the moment it returns.
    A removal that is interrupted leaves only the tombstone, still carrying its
    ownership marker, which the next locked apply's sweep
    (:func:`_sweep_layout_debris`) deletes.
    """
    tombstone = tree.with_name(f".{tree.name}{_TOMBSTONE_INFIX}{os.getpid()}")
    try:
        os.replace(str(tree), str(tombstone))
    except OSError as exc:
        raise WheelUpdateError(f"could not clear {tree}: {exc}") from exc
    if remove:
        _remove_tombstone(tombstone)


def _release_takes_liveness_lock(tree: Path) -> bool:
    """Does the release installed in *tree* hold its liveness lock while it runs?

    The engine stamps its ownership marker on every tree it builds, whatever
    release goes inside, and an older release (an approved channel downgrade)
    never takes the lock -- so a free lock proves nothing about such a tree.
    Read off the installed package: the lock arrived with
    ``kiro_crew/platform/tree_liveness.py``. ``False`` when that cannot be read.
    """
    try:
        return any(tree.glob("lib/python*/site-packages/kiro_crew/platform/tree_liveness.py"))
    except OSError:
        return False


def _dir_identity(path: Path) -> tuple[int, int] | None:
    """``(st_dev, st_ino)`` of the directory at *path*, or ``None`` when absent."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino)


def _may_run_without_the_lock(tree: Path) -> bool:
    """Could a process be running from *tree* without holding its liveness lock?

    True when a release is installed there (its ``kiro_crew`` package exists)
    and that release predates the lock. A leftover build sentinel does not
    prove such a tree never ran: an apply killed between its promotion and the
    sentinel's removal leaves both. A build that stopped before the install
    leaves no package, and is still debris.
    """
    try:
        installed = any(tree.glob("lib/python*/site-packages/kiro_crew/__init__.py"))
    except OSError:
        return True
    return installed and not _release_takes_liveness_lock(tree)


def _discard_if_unheld(tree: Path, *, remove: bool = True) -> bool:
    """Discard *tree* (see :func:`_discard_tree`) while holding its liveness lock exclusively.

    ``False``, and nothing touched, when some process holds the lock (or it
    cannot be opened); a tree with no lock file has no holder. Raises
    :class:`WheelUpdateError` when the tombstone rename fails.
    """
    try:
        fd: int | None = os.open(str(tree / LIVENESS_LOCK), os.O_RDWR)
    except FileNotFoundError:
        fd = None
    except OSError:
        return False
    try:
        if fd is not None and not try_acquire_lock(fd, exclusive=True):
            return False
        _discard_tree(tree, remove=remove)
        return True
    finally:
        if fd is not None:
            os.close(fd)


def _require_free_space(parent: Path) -> None:
    free = shutil.disk_usage(parent).free
    if free < _SHADOW_MIN_FREE_BYTES:
        raise WheelUpdateError(
            f"not enough free disk space for a shadow install "
            f"({free // (1024 * 1024)} MiB free, "
            f"{_SHADOW_MIN_FREE_BYTES // (1024 * 1024)} MiB required)"
        )


def build_shadow_venv(
    wheel_path: Path,
    shadow_dir: Path,
    stable_link: Path | None = None,
    *,
    ctx: _BuildContext = _NO_CONTEXT,
) -> None:
    """Build a FRESH venv at *shadow_dir* and install *wheel_path* into it.

    The interpreter is this process's own Python: the update replaces Kiro
    Crew's code, not the interpreter, so the shadow tree is built on the same
    base the current tree runs on (``python -m venv`` from inside a venv
    creates the new environment against that venv's base interpreter).

    A tree already at *shadow_dir* is replaced only on proof nothing needs it:
    this layout's engine built it (:func:`_is_owned_tree`), it is none of the
    trees :func:`_pinned_trees` names (the stable link's target, this
    process's, the launcher's), and no process holds its liveness lock, which
    is held exclusively while it is set aside. That covers an interrupted
    build and also a completed tree the prune kept as the previous one, which a
    channel move back to that version would otherwise refuse on every attempt.
    Anything else is refused: the stable link's target (re-running an update
    for the live version must not leave the link dangling for the rebuild), a
    tree in use, and a directory this engine did not build. Single-writer
    exclusion against a concurrent update run is the caller's job (see
    :func:`apply_wheel_update`'s update lock).

    The tree is claimed with :data:`TREE_MARKER` (naming the layout),
    :data:`_SHADOW_SENTINEL` and the liveness lock file before any build step.
    ``ctx.cancel`` kills the build child in flight and is re-checked after a
    leftover is cleared, so a cancel that lands during that clear claims nothing.
    """
    # The layout this tree belongs to, read off its own name: the version never
    # contains "-", so everything before the last one names the legacy venv.
    legacy = shadow_dir.with_name(shadow_dir.name.rsplit("-", 1)[0])
    layout = ManagedVenvLayout(
        legacy=legacy, stable_link=stable_link or legacy.with_name(f"{legacy.name}-current")
    )
    if shadow_dir.exists() or shadow_dir.is_symlink():
        if shadow_dir.is_symlink() or not shadow_dir.is_dir():
            raise WheelUpdateError(
                f"refusing to reuse {shadow_dir}: it exists and is not a plain directory"
            )
        if stable_link is not None and _is_stable_target(layout, shadow_dir):
            raise WheelUpdateError(
                f"{shadow_dir} is the stable link's current target — it is "
                "already promoted, not a leftover. Nothing to do."
            )
        if not _is_owned_tree(layout, shadow_dir):
            if (
                not (shadow_dir / _SHADOW_SENTINEL).exists()
                and not (shadow_dir / "pyvenv.cfg").exists()
            ):
                raise WheelUpdateError(
                    f"refusing to remove {shadow_dir}: it exists but is not a virtual "
                    "environment (no pyvenv.cfg)"
                )
            raise WheelUpdateError(
                f"{shadow_dir} already exists and was not built by this install's "
                "update engine — refusing to remove it. If you are certain nothing "
                "needs it, remove the directory and re-run."
            )
        if (
            _realpath(shadow_dir) in _pinned_trees(layout)
            or _may_run_without_the_lock(shadow_dir)
            or not _discard_if_unheld(shadow_dir)
        ):
            raise WheelUpdateError(
                f"{shadow_dir} already exists and a running kirocrew process uses it — "
                "refusing to remove it. Stop that process and re-run."
            )

    _require_free_space(shadow_dir.parent)
    _raise_if_cancelled(ctx.cancel)

    # Claim ownership BEFORE any build step: the marker and the sentinel are
    # what a future retry's reuse guard and the sweep key on, so they must exist
    # from the first moment a partial tree can. `python -m venv` tolerates a
    # non-empty directory.
    #
    # The root is created OWNER-ONLY (mode=0o700). It is mkdir'd here in the gateway
    # process under that process's own umask, so a permissive umask (002) would
    # otherwise leave it group-writable. mkdir(mode=0o700) passes any umask unchanged
    # (umask only masks group/other bits, and 0o700 sets none), so the root is born
    # owner-only. Owner-only is safe: the service runs the launcher as this same
    # user, and no other account needs to traverse the tree.
    try:
        shadow_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (shadow_dir / TREE_MARKER).write_text(f"{layout.legacy}\n", encoding="utf-8")
        (shadow_dir / _SHADOW_SENTINEL).write_text(
            "created by kiro_crew.platform.wheel_engine; removed after promotion\n",
            encoding="utf-8",
        )
        (shadow_dir / LIVENESS_LOCK).touch(mode=0o600)
    except OSError as exc:
        raise WheelUpdateError(f"could not claim the shadow directory: {exc}") from exc
    # Every interpreter child runs isolated (`-I`), the way the gateway's own
    # respawn runs, so an inherited PYTHONPATH never reaches pip's resolution.
    _run(
        [sys.executable, "-I", "-m", "venv", str(shadow_dir)],
        _VENV_CREATE_TIMEOUT_SECS,
        "venv creation",
        ctx=ctx,
    )
    shadow_pip = [str(shadow_dir / "bin" / "python3"), "-I", "-m", "pip"]
    try:
        # Best-effort pip refresh, exactly as cli.sh does; a failure here is
        # not a failed update (a cancel is, and propagates).
        _run(
            shadow_pip + ["install", "--quiet", "--upgrade", "pip"],
            _VENV_CREATE_TIMEOUT_SECS,
            "pip refresh",
            ctx=ctx,
        )
    except WheelUpdateCancelled:
        raise
    except WheelUpdateError:
        pass
    # Binary-only, exactly as cli.sh installs: a dependency with no wheel for
    # this host fails the update up front instead of being compiled from its
    # sdist in the shadow tree (the gateway host is not required to have a C
    # toolchain, and a half-built tree is what the sentinel cleanup exists for).
    policy = _pip_binary_policy()
    try:
        _run(
            shadow_pip + ["install", "--quiet"] + policy + [str(wheel_path)],
            _PIP_INSTALL_TIMEOUT_SECS,
            "pip install into the shadow venv",
            ctx=ctx,
        )
    except WheelUpdateCancelled:
        raise
    except WheelUpdateError as exc:
        # Same classification cli.sh's _report_pip_failure applies: under the
        # binary-only policy, pip's "No matching distribution found" means no
        # release it may install has a wheel for this host, OR the index could
        # not be reached, so the refusal names both, the platform and the way
        # out rather than leaving the operator with pip's raw text alone.
        missing = _no_wheel_packages(str(exc)) if policy else []
        if not missing:
            raise
        raise WheelUpdateError(_no_wheel_message(missing) + f" (pip said: {exc})") from exc


_NO_WHEEL_RE = re.compile(r"No matching distribution found for ([^\s]+)")


def _no_wheel_packages(pip_text: str) -> list[str]:
    """The requirements pip could not satisfy from wheels, in pip's own order.

    Deliberately not gated on pip's ``(from versions: ...)`` list: pip builds
    that list from the candidates that survived its link filter, so a package
    whose every wheel targets a newer libc or another arch reads ``none`` --
    the same text as an index that does not carry the package -- and a numeric
    list only means versions outside the required range have a wheel here.
    The message therefore states the usual cause and names the index as the
    other one rather than reading a verdict into the list. cli.sh's
    ``_report_pip_failure`` draws the same line.
    """
    seen: list[str] = []
    for name in _NO_WHEEL_RE.findall(pip_text):
        if name not in seen:
            seen.append(name)
    return seen[:5]


def _no_wheel_message(missing: list[str]) -> str:
    """The operator-facing refusal when no wheel of ``missing`` may be installed here.

    Mirrors cli.sh's message so an install and its later update describe the
    same platform floor and the same two causes; adds the one fact that is
    specific to the update path: only ``kirocrew update`` run from the
    operator's shell honours the opt-in with that shell's toolchain, because the
    gateway's own build steps run on the trusted system ``PATH``.
    """
    host = f"{platform.system()} {platform.machine()}"
    if platform.system() == "Linux":
        libc = platform.libc_ver()
        if libc[0]:
            host += f", {libc[0]} {libc[1]}"
    return (
        f"pip found no prebuilt wheel of {', '.join(missing)} it may install on this host "
        f"({host}). Kiro Crew installs prebuilt wheels only and never compiles a dependency. "
        "Usually this means the host is older than the wheels' floor: the current dependency "
        "set needs a newer Linux (Amazon Linux 2023, RHEL/Rocky 8+, Ubuntu 22.04+, Debian "
        "12+) on x86_64/aarch64, or macOS. It can also mean the package index could not be "
        "reached or does not carry these releases; pip's own words follow. To compile on "
        f"this host instead, install a C/C++ toolchain and the -dev headers, then run "
        f"`{_ALLOW_SOURCE_BUILDS_ENV}=1 kirocrew update` from a shell where that "
        "toolchain is on PATH: that command builds with the shell's own environment, "
        "while the gateway's automatic update runs its build steps on the trusted "
        "system PATH only"
    )


#: What the import probe loads: the package and the module the ``kirocrew``
#: console script imports first, so a tree whose dependencies are missing fails
#: here rather than at the next start.
_PROBE_SOURCE = "import kiro_crew, kiro_crew.cli; print(kiro_crew.__version__)"


def verify_shadow_venv(
    shadow_dir: Path, expected_version: str, *, ctx: _BuildContext = _NO_CONTEXT
) -> None:
    """Prove the shadow tree serves the promised version before promotion.

    Three checks, all against the shadow interpreter run isolated (``-I``), so
    the answer describes the shadow tree rather than the caller: ``pip check``
    (every installed distribution's requirements are met inside the tree), an
    import of the package and of the CLI entry module that prints the version,
    and the console script's presence. A tree that fails any of them is never
    promoted.
    """
    shadow_python = str(shadow_dir / "bin" / "python3")
    rc, out, err = _spawn_build_child(
        [shadow_python, "-I", "-m", "pip", "check"],
        _PROBE_TIMEOUT_SECS,
        "the shadow venv dependency check",
        ctx=ctx,
        want_stdout=True,
    )
    if rc != 0:
        # pip check names each unmet requirement on stdout.
        detail = _redacted_detail(out or err, FAILURE_TEXT_CHARS)
        raise WheelUpdateError(
            "shadow venv has unmet dependencies — not promoting" + (f": {detail}" if detail else "")
        )
    rc, out, err = _spawn_build_child(
        [shadow_python, "-I", "-X", "utf8", "-c", _PROBE_SOURCE],
        _PROBE_TIMEOUT_SECS,
        "the shadow venv import probe",
        ctx=ctx,
        want_stdout=True,
    )
    if rc != 0:
        detail = _redacted_detail(err, FAILURE_TEXT_CHARS)
        raise WheelUpdateError(
            "shadow venv cannot import kiro_crew — not promoting"
            + (f": {detail}" if detail else "")
        )
    # `-X utf8` makes the probe write UTF-8 whatever the host locale is.
    got = utf8_stdout(out).strip()
    if got != expected_version:
        raise WheelUpdateError(
            f"shadow venv reports version {got!r}, expected {expected_version!r} — not promoting"
        )
    if not (shadow_dir / "bin" / "kirocrew").exists():
        raise WheelUpdateError("shadow venv is missing the kirocrew console script")


def promote(shadow_dir: Path, stable_link: Path) -> None:
    """Atomically point *stable_link* at *shadow_dir*.

    Sibling symlink + ``os.replace`` — RFC §3's atomicity invariant. There is
    no window in which the stable name is missing, and a crash between the two
    steps leaves at worst an orphaned temp link beside it. A real directory at
    the stable name is corrupt state (the name has always been a symlink) and
    is refused rather than replaced.
    """
    if stable_link.exists() and not stable_link.is_symlink():
        raise WheelUpdateError(f"refusing to promote: {stable_link} exists and is not a symlink")
    tmp = stable_link.with_name(f"{stable_link.name}.{os.getpid()}.new")
    try:
        tmp.unlink(missing_ok=True)
        os.symlink(str(shadow_dir.resolve()), str(tmp))
        os.replace(str(tmp), str(stable_link))
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise WheelUpdateError(f"could not promote the stable link: {exc}") from exc


def repoint_launcher_symlink(layout: ManagedVenvLayout) -> bool:
    """Point ``~/.local/bin/kirocrew`` through the stable link.

    Only rewrites a symlink that already resolves into one of OUR trees — a
    launcher the operator pointed somewhere else (pipx, a wrapper script) is
    not this engine's to move. Returns whether the launcher now resolves
    through the stable link; every ``False`` is logged, because a launcher left
    behind keeps new shells and a service start on the previous version.
    """
    launcher = Path.home() / ".local" / "bin" / "kirocrew"
    target = layout.stable_link / "bin" / "kirocrew"
    if not launcher.is_symlink():
        logger.warning(
            "Launcher %s is missing or not a symlink; it was not repointed at %s",
            launcher,
            target,
        )
        return False
    try:
        current = Path(os.readlink(launcher))
    except OSError as exc:
        logger.warning("Could not read the launcher %s: %s", launcher, exc)
        return False
    if not current.is_absolute():
        current = launcher.parent / current
    if current == target:
        return True
    if not layout.is_managed_tree(current):
        logger.warning("Launcher %s points outside the managed trees; leaving it", launcher)
        return False
    tmp = launcher.with_name(f"{launcher.name}.{os.getpid()}.new")
    try:
        tmp.unlink(missing_ok=True)
        os.symlink(str(target), str(tmp))
        os.replace(str(tmp), str(launcher))
    except OSError as exc:
        logger.warning("Could not repoint %s: %s", launcher, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True


# ── Debris and pruning ──────────────────────────────────────────────────────

#: How many completed versioned trees survive a prune besides the stable link's
#: target: the most recent previous one, kept as a recovery target.
_KEEP_PREVIOUS_TREES = 1

#: A staging directory this old belongs to an apply that is gone (the process
#: exited mid-download). Well past every step's own bound.
_STAGING_STALE_SECS = 3600.0


def _pid_alive(pid: int) -> bool:
    from kiro_crew.platform_compat import pid_exists, pid_is_zombie

    return pid_exists(pid) and pid_is_zombie(pid) is not True


def _sweep_layout_debris(layout: ManagedVenvLayout, cancel: ApplyCancel | None = None) -> None:
    """Remove what earlier applies left: tombstones, unpromoted trees, staging.

    Runs under the update lock, so no other apply of this layout is writing.
    Deletes only what is provably this layout's debris (:func:`_is_owned_tree`):
    a tombstone; an owned tree whose build never completed and that nothing uses
    (:func:`_is_unpromoted_debris`) and that holds no installed release older
    than the liveness lock (:func:`_may_run_without_the_lock`); a
    ``crew-venv-current.<pid>.new`` link whose
    writer is gone; a staging directory past :data:`_STAGING_STALE_SECS`. An
    entry that cannot be read is skipped and kept.
    """
    for entry in _owned_siblings(layout):
        _raise_if_cancelled(cancel)
        if entry.name.startswith("."):
            _remove_tombstone(entry)
        elif _is_unpromoted_debris(layout, entry) and not _may_run_without_the_lock(entry):
            with contextlib.suppress(WheelUpdateError):
                _discard_if_unheld(entry)
    temp_link = re.compile(re.escape(f"{layout.stable_link.name}.") + r"(\d+)\.new")
    try:
        entries = list(layout.legacy.parent.iterdir())
    except OSError:
        entries = []
    for entry in entries:
        match = temp_link.fullmatch(entry.name)
        if match and entry.is_symlink() and not _pid_alive(int(match.group(1))):
            with contextlib.suppress(OSError):
                entry.unlink()
    try:
        staging = list(_staging_dir().iterdir())
    except OSError:
        staging = []
    now = time.time()
    for entry in staging:
        try:
            stale = now - entry.stat().st_mtime > _STAGING_STALE_SECS
        except OSError:
            continue
        if entry.name.startswith("kirocrew-update-") and stale:
            shutil.rmtree(entry, ignore_errors=True)


def _completed_trees(layout: ManagedVenvLayout) -> list[Path]:
    """Every owned, completed versioned tree, most recently installed first."""
    found: list[tuple[float, Path]] = []
    for entry in _owned_siblings(layout):
        if entry.name.startswith("."):
            continue
        try:
            if (entry / _SHADOW_SENTINEL).exists():
                continue
            found.append(((entry / "bin" / "kirocrew").stat().st_mtime, entry))
        except OSError:
            continue
    return [tree for _mtime, tree in sorted(found, key=lambda item: item[0], reverse=True)]


def _launcher_tree(layout: ManagedVenvLayout) -> Path | None:
    """The tree ``~/.local/bin/kirocrew`` (the persisted launcher) resolves into."""
    launcher = _realpath(Path.home() / ".local" / "bin" / "kirocrew")
    parent = _realpath(layout.legacy.parent)
    if launcher is None or parent is None:
        return None
    for tree in launcher.parents:
        if tree.parent == parent:
            return tree
    return None


def _pinned_trees(layout: ManagedVenvLayout) -> set[Path]:
    """What no prune or rebuild may remove, resolved: the stable link's target,
    the tree serving this process, and the one the persisted launcher resolves into.
    """
    trees = (_realpath(layout.stable_link), _realpath(Path(sys.prefix)), _launcher_tree(layout))
    return {path for path in trees if path is not None}


def _prune_superseded_trees(layout: ManagedVenvLayout) -> None:
    """Delete owned versioned trees nothing needs; keep the current and one previous.

    Runs after a promotion, under the update lock. Never deleted: the legacy
    directory, the stable link's target, the tree serving this process, the one
    the persisted launcher resolves into, the most recent
    :data:`_KEEP_PREVIOUS_TREES` others, any tree whose liveness lock some
    process holds (every process started from an engine-built tree holds it,
    see :mod:`kiro_crew.platform.tree_liveness`), and any tree whose installed
    release predates that lock (:func:`_release_takes_liveness_lock`), whose
    free lock proves nothing. A tree is deleted only while this call holds
    that lock exclusively, through a tombstone.
    """
    stable = _realpath(layout.stable_link)
    others = [tree for tree in _completed_trees(layout) if _realpath(tree) != stable]
    keep = _pinned_trees(layout)
    keep.update(path for path in map(_realpath, others[:_KEEP_PREVIOUS_TREES]) if path)
    for tree in others:
        if _realpath(tree) in keep or not _release_takes_liveness_lock(tree):
            continue
        with contextlib.suppress(WheelUpdateError):
            if _discard_if_unheld(tree):
                logger.info("Pruned the superseded tree %s", tree)


# ── The update lock ─────────────────────────────────────────────────────────


def _update_lock_path(layout: ManagedVenvLayout) -> Path:
    # BESIDE the trees it serializes, so every writer for this layout (this
    # engine in any process, and cli.sh's managed-venv branch) contends on the
    # same file whatever data home spawned it.
    return layout.stable_link.with_name(f"{layout.legacy.name}.update.lock")


def hold_update_lock() -> int:
    """Take this layout's update lock now; return the held descriptor.

    Raises :class:`WheelUpdateBusy` when another writer holds it, and
    :class:`WheelUpdateError` when the lock cannot be taken at all (a
    filesystem that cannot lock is never mistaken for a busy one). The caller
    owns the descriptor: pass it to :func:`apply_wheel_update` as ``held_lock_fd``
    and close it afterwards (closing releases the lock).
    """
    path = _update_lock_path(managed_venv_layout())
    try:
        fd = open_create_or_existing(path, os.O_RDWR, 0o600)
    except OSError as exc:
        raise WheelUpdateError(f"could not open the update lock {path}: {exc}") from exc
    try:
        taken = try_acquire_lock_or_raise(fd, exclusive=True)
    except OSError as exc:
        os.close(fd)
        raise WheelUpdateError(f"cannot lock {path}: {exc.strerror or exc}") from exc
    if not taken:
        os.close(fd)
        raise WheelUpdateBusy(
            "another kirocrew update is already in progress — wait for it to finish and re-run"
        )
    return fd


def release_update_lock(fd: int) -> None:
    """Release and close a descriptor :func:`hold_update_lock` returned."""
    with contextlib.suppress(OSError):
        release_lock(fd)
    os.close(fd)


# ── The apply ───────────────────────────────────────────────────────────────


def apply_wheel_update(
    *,
    channel: str,
    feed_base: str,
    artifact_base: str,
    expected_version: str,
    progress: Callable[[str], None] = lambda _msg: None,
    cancel: ApplyCancel | None = None,
    on_locked: Callable[[], None] = lambda: None,
    preflight: Callable[[], None] = lambda: None,
    before_promote: Callable[[], None] = lambda: None,
    held_lock_fd: int | None = None,
    trusted_env: bool = True,
) -> Path:
    """Run the full shadow flow; return the promoted tree's path.

    Under the update lock (taken here, or *held_lock_fd* when the caller already
    holds it): sweep earlier debris, repoint a dangling stable link at a tree that
    exists, run *preflight*, fetch + verify the signed
    manifest, refuse a release the signed metadata excludes, download + verify
    the wheel, build the shadow tree, prove it serves the promised version, run
    *before_promote*, promote the stable link, repoint the launcher, then prune
    superseded trees. Every failure before :func:`promote` leaves the install
    untouched; promote itself is atomic.

    *on_locked* runs once the lock is held, so a caller reports progress only
    for an apply that is actually running; a held lock raises
    :class:`WheelUpdateBusy` before anything is touched.

    *cancel* lets a caller whose thread it cannot stop withdraw the apply: it is
    checked before the lock, before every fetch, spawn and download read, and
    immediately before :func:`promote`, and setting it kills a child in flight.
    Once set, the call raises :class:`WheelUpdateCancelled` and nothing is
    promoted. A cancel that lands after promotion has begun is too late by
    design: the update completed.

    *trusted_env* chooses the children's environment (see
    :attr:`_BuildContext.trusted_env`).
    """
    _raise_if_cancelled(cancel)
    lock_fd = held_lock_fd if held_lock_fd is not None else hold_update_lock()
    try:
        _raise_if_cancelled(cancel)
        on_locked()
        return _apply_locked(
            managed_venv_layout(),
            channel=channel,
            feed_base=feed_base,
            artifact_base=artifact_base,
            expected_version=expected_version,
            progress=progress,
            ctx=_BuildContext(cancel=cancel, lock_fd=lock_fd, trusted_env=trusted_env),
            preflight=preflight,
            before_promote=before_promote,
        )
    finally:
        if held_lock_fd is None:
            release_update_lock(lock_fd)


def _check_buildable(payload: dict[str, str]) -> None:
    """Refuse, before any download, a release the SIGNED metadata excludes here."""
    from kiro_crew.dep_sync import python_floor_breach

    floor = python_floor_breach(payload.get("python_requires", ""), sys.version_info[:3])
    if floor:
        running = ".".join(str(part) for part in sys.version_info[:3])
        raise WheelUpdateIncompatible(
            f"kirocrew {payload['version']} requires Python >= {floor}, and this install "
            f"runs Python {running}, so the update cannot be built on it. It needs an "
            f"install on Python {floor} or newer",
            version=payload["version"],
            sha256=payload["sha256"],
        )


def _usable_promoted_tree(layout: ManagedVenvLayout, tree: Path) -> bool:
    """Does the stable link resolve, strictly, to *tree*, with a working interpreter?"""
    try:
        target = layout.stable_link.resolve(strict=True)
        return target == tree.resolve(strict=True) and _interpreter_in(target) is not None
    except (OSError, RuntimeError):
        return False


def _repair_dangling_stable_link(layout: ManagedVenvLayout) -> None:
    """Point a stable link whose target is gone back at a tree that exists.

    A dangling ``crew-venv-current`` breaks every launch path that resolves
    through it (the ``~/.local/bin/kirocrew`` launcher, a service ``ExecStart``)
    until something repoints it, and it must never be read as "promoted".
    ``cli.sh`` repoints it at the tree it just installed; here the target is the
    tree serving this process, else the legacy venv, whichever is this layout's
    and carries an interpreter. The apply that follows promotes its own tree over
    it as usual; one that fails leaves a link that at least starts something.
    """
    link = layout.stable_link
    try:
        if not link.is_symlink() or link.exists():
            return
    except OSError:
        return
    for tree in (_realpath(Path(sys.prefix)), layout.legacy):
        if tree is None or not layout.is_managed_tree(tree) or _interpreter_in(tree) is None:
            continue
        try:
            promote(tree, link)
        except WheelUpdateError as exc:
            logger.warning("Could not repair the dangling stable link %s: %s", link, exc)
            return
        logger.warning("The stable link %s named a missing tree; repointed it at %s", link, tree)
        return


def _apply_locked(
    layout: ManagedVenvLayout,
    *,
    channel: str,
    feed_base: str,
    artifact_base: str,
    expected_version: str,
    progress: Callable[[str], None],
    ctx: _BuildContext,
    preflight: Callable[[], None],
    before_promote: Callable[[], None],
) -> Path:
    """The lock-held body of :func:`apply_wheel_update`."""
    _sweep_layout_debris(layout, ctx.cancel)
    _repair_dangling_stable_link(layout)
    # Idempotent recovery for a handoff interrupted AFTER promote but BEFORE
    # the launcher was repointed: the stable link already targets this
    # version's tree, so a fresh build would hit build_shadow_venv's
    # stable-target refusal ("already promoted, not a leftover") and abort,
    # stranding the launcher on the old venv forever. Detect that state up
    # front and finish what remains — the sentinel, the launcher — rather than
    # refusing. Strict: a DANGLING link, or a target with no interpreter, is not
    # promoted, and falls through to a rebuild.
    expected_tree = layout.versioned_tree(expected_version)
    if _usable_promoted_tree(layout, expected_tree):
        _raise_if_cancelled(ctx.cancel)
        progress(f"{expected_version} is already promoted; completing the launcher handoff…")
        with contextlib.suppress(OSError):
            (expected_tree / _SHADOW_SENTINEL).unlink(missing_ok=True)
        repoint_launcher_symlink(layout)
        return expected_tree
    preflight()
    with tempfile.TemporaryDirectory(prefix="kirocrew-update-", dir=str(_staging_dir())) as tmp:
        workdir = Path(tmp)
        progress("Verifying the signed release manifest…")
        payload = fetch_verified_manifest(
            channel=channel,
            feed_base=feed_base,
            artifact_base=artifact_base,
            workdir=workdir,
            ctx=ctx,
        )
        version = payload["version"]
        if version != expected_version:
            # The feed moved between the caller's check and this fetch. Not an
            # error in itself, but the caller advertised one version and must
            # not silently install another.
            raise WheelUpdateError(
                f"the feed now serves {version}, not the {expected_version} the "
                "check reported — re-run the update"
            )
        _check_buildable(payload)
        _require_free_space(layout.legacy.parent)
        progress(f"Downloading kirocrew {version}…")
        wheel_path = download_verified_wheel(payload, workdir, cancel=ctx.cancel)
        progress("Wheel SHA-256 verified against the signed digest.")

        shadow_dir = layout.versioned_tree(version)
        # Which directory, if any, sat at the tree's name before this attempt.
        # A rebuild that replaces it creates a new one; a rebuild that refuses
        # leaves it as it was, and then it is not this attempt's to clean up.
        found_before = _dir_identity(shadow_dir)
        progress(f"Building the new environment at {shadow_dir}…")
        try:
            build_shadow_venv(wheel_path, shadow_dir, stable_link=layout.stable_link, ctx=ctx)
            progress("Verifying the new environment…")
            verify_shadow_venv(shadow_dir, version, ctx=ctx)
            before_promote()
            # The last point a cancel is honoured: past it the stable link flips.
            _raise_if_cancelled(ctx.cancel)
            progress("Promoting…")
            promote(shadow_dir, layout.stable_link)
        except BaseException:
            # Nothing was promoted (a tree the link already names is never
            # touched), so a tree this attempt claimed is debris. A tree it did
            # not claim (the rebuild refused it) is left exactly as it was. When the
            # process is ending (a shutdown or exec cancel), only the rename
            # runs and the next sweep deletes the tombstone.
            claimed = found_before is None or _dir_identity(shadow_dir) != found_before
            if claimed and _is_unpromoted_debris(layout, shadow_dir):
                cancel = ctx.cancel
                ending = (
                    cancel is not None
                    and cancel.is_set()
                    and cancel.reason not in _REMOVE_PARTIAL_TREE_REASONS
                )
                with contextlib.suppress(WheelUpdateError):
                    _discard_if_unheld(shadow_dir, remove=not ending)
            raise
    # The sentinel comes off only AFTER promotion: a crash in the window between
    # verify and promote would otherwise leave a verified, unpromoted tree with
    # no ownership marker. Until the link flips, the tree is still OURS to clear
    # on retry; once it flips, the stable-target rules protect it. A failure to
    # unlink after a successful promote is reported, not raised — the update
    # itself already happened, and the already-promoted path clears it later.
    try:
        (shadow_dir / _SHADOW_SENTINEL).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not clear the build sentinel in %s: %s", shadow_dir, exc)
        progress(f"Note: could not clear the build sentinel in {shadow_dir}: {exc}")
    if not repoint_launcher_symlink(layout):
        # The stable link is promoted, so restarts pick the new tree up, but a
        # launcher still aimed elsewhere keeps NEW shells on the old version.
        # repoint_launcher_symlink logged why.
        progress(
            "Note: the ~/.local/bin/kirocrew launcher was not repointed; "
            "new shells keep the previous version until the installer is re-run."
        )
    _prune_superseded_trees(layout)
    return shadow_dir


__all__ = [
    "CLI_MANIFEST_KEY_ID",
    "CLI_MANIFEST_PUBLIC_KEY_B64",
    "FAILURE_TEXT_CHARS",
    "ApplyCancel",
    "ManagedVenvLayout",
    "WheelUpdateBusy",
    "WheelUpdateCancelled",
    "WheelUpdateError",
    "WheelUpdateIncompatible",
    "WheelUpdateNotReady",
    "WheelUpdateSnapshotFailed",
    "apply_wheel_update",
    "build_shadow_venv",
    "check_release_version",
    "download_verified_wheel",
    "fetch_verified_manifest",
    "hold_update_lock",
    "managed_venv_layout",
    "parse_and_validate_manifest",
    "promote",
    "release_update_lock",
    "repoint_launcher_symlink",
    "respawn_executable",
    "running_from_managed_venv",
    "runs_through_stable_link",
    "stable_launch_path",
    "verify_shadow_venv",
]
