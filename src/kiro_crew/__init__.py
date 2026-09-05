"""KiroCrew — open-source personal AI agent."""

from __future__ import annotations

import asyncio
import logging
import os
import re

__version__ = "0.7.0"

# A distribution that repackages one core release as several builds of its own
# (an enterprise bundle vending ``0.6.0.10``, ``0.6.0.11``, ... of the same
# ``0.6.0``) names the build it shipped in a ``BUILD_VERSION`` file placed
# BESIDE this module at packaging time. The value replaces ``__version__`` for
# every reader -- the About page's version chip, ``/api/status``,
# ``/api/health``, ``kirocrew --version``, the diagnostics report, the
# governance minimum-version floor -- because all of them import this
# attribute, several as an import-time copy that nothing later can reach.
# Resolved at import for exactly that reason: it is the only point that
# precedes every copy. (The anonymous usage beacon is NOT a reader in this
# sense: ``beacon.release`` deliberately clamps to major.minor.patch, so a
# stamped build sends the same value as its base. The About page's changelog
# folds the stamp back onto its release -- ``changelog.running_release``.)
#
# A FILE IN THE PACKAGE, not an environment variable, on purpose. The version
# feeds the governance minimum-version floor (``update_required``), which is an
# enterprise ceiling the local operator must not be able to weaken; an env var
# is theirs to set, so ``KIROCREW_BUILD_VERSION=0.6.0.999`` would have waved a
# forbidden build past the floor. The file sits in the same site-packages tree
# as this code and carries exactly its trust: whoever can write it can write
# ``__init__.py`` too, so it adds no bypass the bytes did not already have. It
# is also the same model the release lanes use when they rewrite the literal
# above for a nightly or insider build -- the stamp lives in the bytes and is
# what a shadow-venv probe or a fresh interpreter naturally reports.
#
# Fail-closed on shape: only the core base itself, or the base followed by ONE
# dotted numeric segment, is honoured, and only over a bare numeric base.
# Anything else (a different base, a prerelease stamp, a non-numeric or second
# segment, a suffix over an ``rc`` base) is a build this core is not, and
# stamping it would make the process CLAIM bytes it does not run; it is ignored
# with one warning and the core base stays. The accepted shape parses as a PEP
# 440 release everywhere the version is compared (the update check's
# ``_is_newer``, ``base_version``, ``release_channel.channel``), so a stamped
# build orders and classifies exactly as its base would.
_BUILD_VERSION_FILENAME = "BUILD_VERSION"
_BUILD_VERSION_MAX_CHARS = 64  # a real stamp is ~10 chars; anything larger is not one
_BARE_RELEASE = re.compile(r"[0-9]+(?:\.[0-9]+)*")
_BUILD_VERSION_SUFFIX = re.compile(r"\.[0-9]+")


def _build_version_override(base: str, candidate: "str | None") -> "str | None":
    """Return *candidate* when it names a build OF *base*, else ``None``.

    Pure so it can be tested without a file. Accepts ``base`` itself and
    ``base`` + exactly one ``.N`` ASCII-numeric segment, over a bare numeric
    ``base`` only (a prerelease or nightly base takes no suffix: the composed
    string would not parse as a release); surrounding whitespace is stripped.
    Everything else is refused. Private: the contract is the FILE (its name
    and this shape), not a Python API -- a distribution writes the file at
    packaging time and imports nothing.
    """
    if not candidate or not base:
        return None
    value = candidate.strip()
    if value == base:
        return value
    if not _BARE_RELEASE.fullmatch(base) or not value.startswith(base):
        return None
    return value if _BUILD_VERSION_SUFFIX.fullmatch(value[len(base) :]) else None


def _apply_build_version_file(base: str, package_dir: "str | None") -> str:
    if not package_dir:
        return base
    path = os.path.join(package_dir, _BUILD_VERSION_FILENAME)
    try:
        with open(path, encoding="utf-8") as handle:
            # One byte past the cap, so an oversized file is detected rather
            # than silently truncated into a value that happens to parse.
            raw = handle.read(_BUILD_VERSION_MAX_CHARS + 1)
    except (OSError, ValueError):
        # Absent is the normal case: the file exists only in a distribution
        # that stamps one. Unreadable or undecodable is treated the same --
        # the base is the honest answer when the stamp cannot be read.
        return base
    if not raw.strip():
        return base
    override = _build_version_override(base, raw) if len(raw) <= _BUILD_VERSION_MAX_CHARS else None
    if override is None:
        logging.getLogger(__name__).warning(
            "%s %r does not name a build of kiro_crew %s "
            "(expected the base or base.N); ignoring it",
            _BUILD_VERSION_FILENAME,
            raw.strip()[:_BUILD_VERSION_MAX_CHARS],
            base,
        )
        return base
    return override


__version__ = _apply_build_version_file(__version__, os.path.dirname(__file__ or ""))


class _LazyShutdownEvent:
    """Proxy for ``asyncio.Event`` that defers construction until first use.

    On Python 3.9, ``asyncio.Event()`` captures the *current* event loop at
    construction time.  Creating it at module-import time binds it to
    whatever loop ``get_event_loop()`` implicitly returns (typically a
    throwaway loop created during import), which is NOT the loop that
    ``asyncio.run()`` later spins up for the gateway.  Awaiting such an
    Event from the gateway loop raises::

        RuntimeError: Task ... got Future ... attached to a different loop

    Python 3.10+ made ``asyncio.Event`` loop-less, but we still target 3.9.

    This proxy constructs the underlying Event lazily, **inside the running
    loop**, on first method access.  The real Event therefore binds to the
    correct loop.  The proxy also rebinds transparently if the running
    loop changes (tests, repeated ``asyncio.run()`` cycles).

    Why ``get_running_loop()`` and not ``get_event_loop()``:
        On Python 3.9 in the main thread, ``get_event_loop()`` never raises
        ``RuntimeError`` when no loop is running — it silently creates (or
        returns) a default loop.  Using it here would reintroduce the
        original cross-loop bug whenever the first access happens outside
        a running loop.  ``get_running_loop()`` raises cleanly when no
        loop is running, which is what we want so we can fall back to
        pending state without accidentally binding to a stray loop.

    For sync callers without a running loop (rare — in practice every
    caller in this codebase is inside an async context or signal handler
    registered on the running loop), we maintain a pending-set flag so
    the intent is preserved until a loop is available.
    """

    __slots__ = ("_event", "_loop", "_pending_set")

    def __init__(self) -> None:
        self._event: "asyncio.Event | None" = None
        self._loop: "asyncio.AbstractEventLoop | None" = None
        # Tracks set/clear calls made with no running loop, so the state
        # is applied to the real Event once one becomes available.
        self._pending_set: bool = False

    def _get_or_none(self) -> "asyncio.Event | None":
        """Return the Event bound to the current running loop, or None.

        Creates / rebinds the Event if needed.  Returns ``None`` when
        there is no running loop (caller must handle the pending state).
        """
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            return None

        if self._event is None or self._loop is not current_loop:
            new_event = asyncio.Event()
            if self._pending_set:
                new_event.set()
            self._event = new_event
            self._loop = current_loop
        return self._event

    def _get(self) -> asyncio.Event:
        """Return the Event, requiring a running loop."""
        event = self._get_or_none()
        if event is None:
            raise RuntimeError(
                "shutdown_event accessed without a running event loop; "
                "call from within an async context."
            )
        return event

    # ── asyncio.Event API ────────────────────────────────────────────
    def is_set(self) -> bool:
        event = self._get_or_none()
        if event is None:
            return self._pending_set
        return event.is_set()

    def set(self) -> None:
        self._pending_set = True
        event = self._get_or_none()
        if event is not None:
            event.set()

    def clear(self) -> None:
        self._pending_set = False
        event = self._get_or_none()
        if event is not None:
            event.clear()

    async def wait(self) -> bool:
        # Always runs from inside a coroutine, so a running loop exists.
        return await self._get().wait()

    def __repr__(self) -> str:
        state = self.is_set()
        return f"<_LazyShutdownEvent set={state}>"


# Process-wide shutdown signal.  Any background loop should use
# ``await shutdown_event.wait()`` (with a timeout) instead of plain
# ``asyncio.sleep()`` so it wakes instantly on Ctrl-C.
shutdown_event: _LazyShutdownEvent = _LazyShutdownEvent()
