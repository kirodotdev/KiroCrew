"""What every stage of the managed-venv update shares.

The size and time bounds, the pip binary-only policy, the channel manifest's grammar,
the update error family, the cooperative cancel flag with the build context it travels
in, and the release-version check. The offline trust root these bounds protect stays in
the facade, beside the signature check that reads it.

:mod:`kiro_crew.platform.wheel_engine` is the import path and the patch surface: callers
import these names from it and tests patch them there, and its mirror
(:mod:`kiro_crew._facade_mirror`) carries such a patch to this module. This module never
imports the facade. The update errors report the facade as their module, the name every
caller and traceback uses.
"""

from __future__ import annotations

import contextlib
import os
import re
import threading
from dataclasses import dataclass
from typing import Callable

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


# Named by the path callers import them from: an error renders as ``module.qualname`` in
# a traceback and in the log lines that carry one, so this module's name must not leak into
# that text. ``ApplyCancel`` is a flag, never raised, and reports this module.
WheelUpdateError.__module__ = "kiro_crew.platform.wheel_engine"
WheelUpdateCancelled.__module__ = "kiro_crew.platform.wheel_engine"
WheelUpdateBusy.__module__ = "kiro_crew.platform.wheel_engine"
WheelUpdateNotReady.__module__ = "kiro_crew.platform.wheel_engine"
WheelUpdateSnapshotFailed.__module__ = "kiro_crew.platform.wheel_engine"
WheelUpdateIncompatible.__module__ = "kiro_crew.platform.wheel_engine"
