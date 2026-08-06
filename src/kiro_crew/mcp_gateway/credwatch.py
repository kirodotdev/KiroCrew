"""Credential-file change watcher for the MCP gateway.

Polls a credential file and invokes ``on_change`` only when its
**content** changes. The MCP gateway uses this to drain pooled backends
across a credential rotation: backends spawned with the old credential no
longer reach credential-gated services after a rotation, so the gateway
evicts them on change to force respawn with the fresh credential.

The watched path is supplied by the caller (see the ``--credential-watch-path``
flag on ``gatewayd``); this module never hardcodes a path and never
interprets the file's content — the bytes are only hashed for change
detection.

Detection is by **content digest, not bare mtime**. Credential refresh
daemons commonly rewrite the credential file frequently with
*byte-identical* content, bumping mtime without rotating the credential.
An mtime-only watcher fires ``on_change`` on every such no-op rewrite —
observed at 13-26 fires/min — and each fire runs a full
``evict_idle(0.0, include_pinned=True)`` + re-warm, churning pooled
backends out from under live sessions (surfacing to kiro as
"Transport to MCP server is closed"). Hashing the bytes and firing only
on a real content change collapses the storm to the true rotation
cadence (a few times a day).

The digest is recomputed on **every** poll — there is deliberately no
mtime "cheap gate" that skips re-hashing when the timestamp is unchanged.
This module explicitly targets network-mounted home directories, where
timestamp granularity is coarse (NFS/FAT, 1s or worse): a rotation that
rewrites the file in-place within the same mtime tick as the last
observation would leave mtime unchanged, and an mtime-gated watcher would
skip the re-hash and **permanently** miss that rotation (baseline_mtime
never advances, so every later poll short-circuits identically). Hashing
a small credential file each poll is cheap and is already off-loaded to a
worker thread; the storm suppression comes entirely from the digest
comparison rather than from any mtime gate.

Loop shape mirrors :func:`kiro_crew.mcp_gateway.gatewayd._idle_sweeper`:
``wait_for(stop_event.wait(), timeout=interval)`` so the watcher reacts
to graceful shutdown within one tick instead of one full interval, and
``CancelledError`` exits cleanly under ``task.cancel()``.

Behavior contract:

* The very first observation is the **baseline** — it never fires
  ``on_change``. This avoids a spurious "credential changed" the moment
  the watcher starts. The baseline is whatever the first probe sees:
  a present file (its digest) OR an **absent** file (recorded as an absent
  baseline).
* If the first observation is an **absent** file, a later **appearance** is
  a real "no credential -> credential" transition and DOES fire ``on_change``
  — this drains any backend prewarmed during the absent startup window
  (prewarm is scheduled before the watcher's first probe). Only the initial
  absent observation itself is silent.
* After a **present** baseline, the file being **deleted** (present -> absent)
  is a credential *revocation* and DOES fire ``on_change`` — otherwise pooled
  backends keep the revoked credential until deadline/restart. The baseline
  moves to absent, so a later re-appearance fires again. Genuine absence only:
  a transient stat/read ``OSError`` is skipped without firing (a network-mount
  glitch must not spuriously drain).
* An mtime move with **unchanged content** refreshes the mtime gate but
  does NOT fire — this is the no-op-rewrite storm fix.
* Only a **content change** past the baseline fires ``on_change``.
* ``on_change`` may be a sync callable or one returning an awaitable.
  Awaitables are awaited; sync callables run inline.
* Exceptions raised by ``on_change`` are logged and swallowed — one bad
  drain must not kill the watcher.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
from pathlib import Path
from typing import Awaitable, Callable, Optional, Union

OnChange = Union[Callable[[], Awaitable[None]], Callable[[], None]]


#: Chunk size for the streaming digest — the file is hashed block-by-block so
#: the full credential is NEVER materialized in a variable; only a rolling
#: sha256 state and one 64 KiB block are ever in memory.
_DIGEST_CHUNK_BYTES = 64 * 1024


def _content_digest(cred_path: Path) -> Optional[str]:
    """Return a sha256 hex digest of ``cred_path``'s bytes.

    The bytes are STREAMED through the hasher in ``_DIGEST_CHUNK_BYTES`` blocks
    and never retained: this function computes a change-detection fingerprint,
    it does not read credentials into memory as a whole and it never returns,
    logs, or interprets the content. (This is intentionally NOT routed through
    ``hooks.is_sensitive_path`` — that gate governs *agent*-initiated reads of
    the agent's own ceiling; this is a companion-configured infrastructure
    rotation-watcher whose path comes only from ``IdentityProvider`` and which
    would be *blocked* from doing its job by that agent-facing gate.)

    Returns ``None`` if the file does not exist. Propagates other ``OSError`` so
    the caller can log-and-skip a transient read failure without advancing the
    baseline.
    """
    hasher = hashlib.sha256()
    try:
        with open(cred_path, "rb") as fh:
            for block in iter(lambda: fh.read(_DIGEST_CHUNK_BYTES), b""):
                hasher.update(block)
    except FileNotFoundError:
        return None
    return hasher.hexdigest()


def _probe_credential(
    cred_path: Path,
) -> tuple[Optional[float], Optional[str]]:
    """Blocking stat + content hash, run in a worker thread so the event loop
    never blocks on (possibly network-mounted) credential IO.

    Returns ``(mtime, digest)``:
      * ``(None, None)``   -- file missing, or vanished between stat and read
      * ``(mtime, digest)`` -- content read and hashed

    The content is hashed on **every** probe — there is no mtime cheap-gate.
    Coarse-granularity filesystems (NFS/FAT) can rewrite a file in-place with
    new content but an unchanged st_mtime; gating the re-hash on mtime would
    permanently miss such a rotation. See the module docstring.

    Propagates non-``FileNotFoundError`` ``OSError`` so the caller can
    log-and-skip a transient failure without advancing the baseline.
    """
    try:
        mtime = cred_path.stat().st_mtime
    except FileNotFoundError:
        return None, None
    return mtime, _content_digest(cred_path)


async def watch_credential(
    cred_path: Path,
    interval_secs: float,
    stop_event: asyncio.Event,
    on_change: OnChange,
    logger: logging.Logger,
    on_probe_complete: Optional[Callable[[], None]] = None,
) -> None:
    """Poll ``cred_path`` and fire ``on_change`` whenever its content
    changes.

    Args:
        cred_path: Credential file to watch (supplied by the caller — this
            module hardcodes no path).
        interval_secs: Seconds between polls. Must be positive.
        stop_event: Caller-owned event; setting it ends the loop within
            one tick.
        on_change: Sync or async no-arg callable invoked after every
            detected content change (not on the baseline observation, and
            not on an mtime-only no-op rewrite).
        logger: ``logging.Logger`` used for state-change INFO and
            handler-error WARNING messages.
        on_probe_complete: Test seam, ``None`` in production. Optional
            no-arg callable invoked after every probe cycle finishes,
            including cycles that fire nothing. It lets a caller await
            "one poll has happened" instead of sleeping a wall-clock
            guess. The sleep-based tests were flaky on Windows runners,
            whose coarser timer resolution let a write land outside the
            intended window (issue #1105).
    """
    baseline_mtime: Optional[float] = None
    baseline_digest: Optional[str] = None
    # Whether the FIRST observation of this process has been recorded yet. The
    # first observation — whether the file is present OR absent — is the
    # no-fire baseline. Tracking absence explicitly (not just baseline_digest is
    # None) closes a startup race: if the file is absent at first probe and
    # appears later, that appearance is a real "no credential -> credential"
    # transition that MUST fire on_change to drain any backend prewarmed during
    # the absent window (prewarm is scheduled before the watcher's first probe).
    baseline_established = False
    first_probe = True
    try:
        while not stop_event.is_set():
            # Probe IMMEDIATELY on the first iteration so the baseline is
            # captured at startup, not one interval later. Waiting first would
            # leave a window where a rotation that lands after old-credential
            # backends start but before the first probe is silently adopted as
            # the baseline, so on_change never fires and pinned backends keep
            # stale credentials. Subsequent iterations wait between polls.
            if not first_probe:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_secs)
                    return  # stop_event fired — exit cleanly
                except asyncio.TimeoutError:
                    pass
            first_probe = False
            try:

                # Offload the blocking stat + content hash to a worker thread so
                # the event loop never stalls on credential IO — the file may live
                # on a network-mounted home directory. The content is hashed every
                # poll (no mtime cheap-gate) so a coarse-mtime in-place rotation is
                # never permanently missed — see the module docstring.
                try:
                    mtime, digest = await asyncio.to_thread(_probe_credential, cred_path)
                except OSError as exc:
                    # stat() or read() failed transiently — log and skip without
                    # advancing the baseline.
                    # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- logs a path + exception, never credential bytes
                    logger.warning("credential watcher: probe(%s) failed: %s", cred_path, exc)
                    continue
                if digest is None:
                    # Genuinely absent (``_probe_credential`` returns None ONLY on
                    # FileNotFoundError; a transient OSError propagated and was caught
                    # above without advancing the baseline).
                    if not baseline_established:
                        # FIRST observation is absent — record it as the absent
                        # baseline so a later appearance is treated as a change (not
                        # silently adopted as a no-fire baseline).
                        baseline_established = True
                        logger.info(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- logs a path only, never credential bytes
                            "credential watcher: baseline is ABSENT (no file yet) for %s",
                            cred_path,
                        )
                        continue
                    if baseline_digest is not None:
                        # PRESENT -> ABSENT: the credential was deleted/revoked. This
                        # is a real transition and MUST fire on_change — otherwise
                        # pooled/pinned backends keep the revoked credential and stay
                        # authenticated until deadline/restart, defeating revocation.
                        # Move the baseline to absent so a later re-appearance fires
                        # again (absent -> present) rather than silently rebaselining.
                        logger.info(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- logs a truncated content hash + path, never credential bytes
                            "credential watcher: file removed (was digest=%s) for %s "
                            "— firing (credential revoked)",
                            baseline_digest[:12],
                            cred_path,
                        )
                        baseline_mtime = None
                        baseline_digest = None
                        try:
                            result = on_change()
                            if inspect.isawaitable(result):
                                await result
                        except Exception:
                            logger.exception(
                                "credential watcher: on_change handler failed; continuing"
                            )
                    # Already absent (baseline_digest is None) — nothing to compare;
                    # wait for it to (re)appear.
                    continue

                if not baseline_established:
                    # First observation and the file is PRESENT — no-fire baseline.
                    baseline_established = True
                    baseline_mtime = mtime
                    baseline_digest = digest
                    logger.info(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- logs a truncated content hash + path, never credential bytes
                        "credential watcher: baseline mtime=%.3f digest=%s for %s",
                        mtime,
                        digest[:12],
                        cred_path,
                    )
                    continue

                if baseline_digest is None:
                    # Baseline was ABSENT and the file has now APPEARED — a real
                    # "no credential -> credential" transition. Fire on_change so any
                    # backend prewarmed during the absent window is drained and
                    # respawned against the now-present credential, then adopt it as
                    # the new baseline.
                    logger.info(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- logs a truncated content hash + path, never credential bytes
                        "credential watcher: file appeared (digest=%s) after absent "
                        "baseline for %s — firing",
                        digest[:12],
                        cred_path,
                    )
                    baseline_mtime = mtime
                    baseline_digest = digest
                    try:
                        result = on_change()
                        if inspect.isawaitable(result):
                            await result
                    except Exception:
                        logger.exception("credential watcher: on_change handler failed; continuing")
                    continue

                if digest == baseline_digest:
                    # mtime moved but the bytes are identical — a no-op rewrite
                    # by a credential refresh daemon. Advance the mtime gate so we
                    # do not re-hash next poll, but do NOT fire on_change.
                    if mtime != baseline_mtime:
                        logger.debug(
                            "credential watcher: mtime moved %.3f -> %.3f, content "
                            "unchanged (digest=%s); not firing for %s",
                            baseline_mtime,
                            mtime,
                            digest[:12],
                            cred_path,
                        )
                    baseline_mtime = mtime
                    continue

                # Real content change — credential rotation.
                logger.info(  # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure -- logs truncated content hashes + path, never credential bytes
                    "credential watcher: content changed digest %s -> %s for %s",
                    baseline_digest[:12],
                    digest[:12],
                    cred_path,
                )
                baseline_mtime = mtime
                baseline_digest = digest
                try:
                    result = on_change()
                    if inspect.isawaitable(result):
                        await result
                except Exception:
                    logger.exception("credential watcher: on_change handler failed; continuing")
            finally:
                # Fire on EVERY path out of the body, including the six
                # ``continue`` branches. A caller that awaits this seam to
                # mean "one poll has happened" would deadlock if a branch
                # skipped the notify.
                if on_probe_complete is not None:
                    on_probe_complete()
    except asyncio.CancelledError:
        pass
