"""The version of a kiro-cli found only through ``PATH``, read inside the sandbox.

A session whose kiro-cli is the pinned install has its version read by
``kiro_cli.kiro_cli_version_at``, inside the sandbox at the configured tier and,
on a host with no sandbox backend, unconfined: the pin is the operator's own
install. A kiro-cli reached only through ``PATH`` is not: ``PATH`` can lead with
a directory the agent writes to. The session itself runs that file inside the OS
sandbox, so the rule here is that the probe never has more reach than that launch:

* It runs through the readiness probe's own runner for an unverified candidate
  (``kiro_prerequisite._run_process``) in strict mode: the sandbox chokepoint
  every agent-influenced spawn uses, entered through the absolute system
  ``env``, so a file named ``kiro-cli`` is never taken for the trusted spawn
  that may delegate to the harness's own sandbox; output captured up to a fixed
  bound; the whole process tree ended on a timeout.
* A wrap that did not put the child in Kiro Crew's own sandbox is not run
  (the runner's ``require_crew_sandbox``).
* Its environment is the readiness probe's fixed ``--version`` allowlist: no
  credential, no proxy.
* Windows has no OS sandbox backend, so nothing runs there.

Every failure is "unknown", which the caller reads as "keep the full tool list".
The answer is never waited for: :func:`version_if_known` reads a cache and, on a
miss, starts the probe in the background. One answer per file identity (path and
mtime), so a replaced file is read again. Only the file's own answer is kept: a
read that did not run, or that was cut off, is asked again by the next caller.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import threading
import time

logger = logging.getLogger(__name__)

_Version = tuple[int, int, int]

#: ``None`` is an answer too: the file ran to its own exit and gave no version.
_versions: dict[tuple[str, int], _Version | None] = {}
_in_flight: set[tuple[str, int]] = set()
_lock = threading.Lock()


class _NoAnswer(Exception):
    """The file gave no answer of its own: it was not run, or its run was cut off."""


def _identity(binary: str) -> tuple[str, int] | None:
    try:
        return (binary, os.stat(binary).st_mtime_ns)
    except OSError:
        return None


def version_if_known(binary: str) -> _Version | None:
    """The cached version of *binary*, or ``None`` while it is unknown.

    Never spawns and never waits. A file with no answer yet has its probe
    started in the background, so a later caller finds one.
    """
    key = _identity(binary)
    if key is None:
        return None
    with _lock:
        if key in _versions:
            return _versions[key]
        if key in _in_flight:
            return None
        _in_flight.add(key)
    try:
        threading.Thread(
            target=_probe_and_record, args=(key,), name="kiro-cli-path-probe", daemon=True
        ).start()
    except RuntimeError:
        with _lock:
            _in_flight.discard(key)
    return None


def warm() -> None:
    """Start the probe for this host's ``PATH``-only install, if it has one.

    Returns at once: finding the install stats directories and the probe spawns,
    so both run on a thread of their own.
    """

    def _warm() -> None:
        try:
            from kiro_crew import kiro_cli

            binary = kiro_cli.path_only_install()
            if binary:
                version_if_known(binary)
        except Exception:  # noqa: BLE001 - a warm-up that cannot run changes nothing
            logger.debug("kiro-cli PATH probe warm-up failed", exc_info=True)

    with contextlib.suppress(RuntimeError):
        threading.Thread(target=_warm, name="kiro-cli-path-probe-warm", daemon=True).start()


def _probe_and_record(key: tuple[str, int]) -> None:
    version: _Version | None = None
    answered = False
    started = time.monotonic()
    try:
        version = _sandboxed_version(key[0])
        answered = True
    except _NoAnswer:
        pass
    except Exception as exc:  # noqa: BLE001 - an unanswerable probe is "unknown"
        # The class only: the message can carry a path read back from the wrap.
        logger.debug("kiro-cli PATH probe failed: %s", type(exc).__name__)
    with _lock:
        if answered:
            # A read that did not run or was cut off (a slow host at boot, a
            # sandbox that could not be built) says nothing about the file, so
            # it is not kept: the next caller starts another.
            _versions[key] = version
        _in_flight.discard(key)
    elapsed = time.monotonic() - started
    if version:
        logger.info(
            "kiro-cli found only through PATH: version %s, read inside the sandbox in %.2fs",
            ".".join(map(str, version)),
            elapsed,
        )
    elif answered and sys.platform == "win32":
        logger.info(
            "kiro-cli found only through PATH: its version is not read on Windows, "
            "so it was not run and Kiro Crew's own MCP servers stay out of Tool "
            "Search deferral for it"
        )
    elif answered:
        logger.info(
            "kiro-cli found only through PATH: it gave no version inside the "
            "sandbox (%.2fs), so Kiro Crew's own MCP servers stay out of Tool Search "
            "deferral for it",
            elapsed,
        )
    else:
        # Asked again on the next spawn, so not at a level that repeats in the log.
        logger.debug(
            "kiro-cli found only through PATH: its version could not be read inside "
            "the sandbox this time (%.2fs)",
            elapsed,
        )


def _sandboxed_version(binary: str) -> _Version | None:
    """One bounded ``--version`` of *binary* inside the strict sandbox. Blocking.

    Returns the file's own answer: its version, or ``None`` when it ran to its
    own exit and gave none. Raises :class:`_NoAnswer` when it was not run or its
    run was cut off; any other exception means the same.
    """
    if sys.platform == "win32":
        # A fact of the platform, not of this run, so it is an answer.
        return None
    import asyncio

    from kiro_crew.kiro_cli import _VERSION_PROBE_TIMEOUT_SECS, _probe_ran
    from kiro_crew.kiro_prerequisite import _probe_env, _run_process
    from kiro_crew.mcp_hot_reload import parse_kiro_cli_version
    from kiro_crew.sandbox import launcher_refusal

    # This thread runs no event loop: the probe is started on a thread of its own.
    result = asyncio.run(
        _run_process(
            binary,
            ["--version"],
            env=_probe_env(dict(os.environ), os.environ.get("PATH", "")),
            timeout_secs=_VERSION_PROBE_TIMEOUT_SECS,
            sandbox_mode="strict",
            require_crew_sandbox=True,
        )
    )
    if not _probe_ran(result) or (result.returncode or 0) < 0:
        # Not run (no sandbox of Kiro Crew's own, no wrapper), cut off (a timeout,
        # the sandbox's own refusal, the wrapper ended by a signal), or more output
        # than the runner keeps.
        raise _NoAnswer
    if not result.ok:
        if launcher_refusal(result.output) is not None:
            # The sandbox launcher's own refusal line with no verdict behind it (a
            # momentary refusal). The line is untrusted, since the file can print
            # it, which only ever costs that file another read.
            raise _NoAnswer
        # shortcut: the Linux launcher reports a file a signal killed as exit 1,
        # so that run is kept as "no version"; upgrade when the launcher passes
        # the signal on.
        return None
    return next(
        (v for line in result.output.splitlines() if (v := parse_kiro_cli_version(line))),
        None,
    )
