"""kiro-cli refusing its v3 (KAS) engine on this host, read as one host verdict.

kiro-cli's ``acp --help`` lists ``--agent-engine v3`` even on a host where
``kiro-cli acp --agent-engine v3`` then prints::

    error: V3 is currently not supported on this system.

and exits 1 before ``initialize``. Nothing about the next spawn differs, so a
respawn only reproduces it, and a generic "process exited (rc=1)" card would
send the user to a retry that cannot help. The refusal is a fact about the
host, so it is recorded the way the gateway's KAS floor records a
kiro-cli that does not list the engine (``platform.bootstrap.apply_kas_cli_floor``):
KAS is marked unservable here, the Kiro-CLI-offers-v3 evidence is withdrawn, and
the live config is reloaded so new sessions take the Kiro CLI launch.

The runtime records it where every KAS start's stderr passes, its pre-``initialize``
latch (``AcpRuntime._drain_stderr``), so the verdict lands whoever spawned the
process: a chat session, the background one-liners, a companion runtime, doctor.

The verdict also outlives the process. :data:`KAS_ENGINE_START_LEAF` in the data
home keeps, per kiro-cli binary identity (path, mtime, size), whether its v3 engine
started or was refused: the start check (``agent_sdk.kas_engine_start_verdict``)
runs once per kiro-cli install instead of once per process, and a gateway restarted
on a host that refused falls back at its KAS floor before the first chat, as a later
``kirocrew chat`` or ``kirocrew run`` does before its config load
(``agent_sdk.backends.ensure_kiro_cli_kas_verdict``). A new kiro-cli (an update, a
reinstall) is a new identity and is checked again.

This module owns the one signature every reader matches (the floor's start check
and the runtime's stderr latch), the reason clause the floor notices and
``kirocrew doctor`` print, the error a KAS session start raises and that record.
Kept out of ``transport_errors.py`` for the file-line cap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from kiro_crew.acp.transport_errors import AcpError

logger = logging.getLogger(__name__)

#: kiro-cli's own refusal line, matched on its words alone: the ``error:`` prefix
#: and the trailing period are formatting kiro-cli may change. Casefolded, as the
#: haystack is before the containment check.
_KAS_ENGINE_REFUSAL_WORDS = "v3 is currently not supported on this system"

#: The refusal as a clause, worded to stand where ``agent_sdk.kas_engine_gap``'s
#: "this kiro-cli does not offer engine v3" stands: in the KAS floor's notices,
#: on the dashboard's KAS row (``host_unservable_reason``) and in doctor's engine row.
KAS_ENGINE_REFUSED_REASON = (
    'this kiro-cli refuses engine v3 on this host ("V3 is currently not supported '
    'on this system")'
)

#: What a KAS session start that kiro-cli refused tells the user when new
#: sessions now take the Kiro CLI launch instead.
KAS_ENGINE_REFUSED_MESSAGE = (
    'KAS could not start: kiro-cli refuses its v3 engine on this host ("V3 is '
    'currently not supported on this system"). Kiro Crew now starts new sessions '
    "on Kiro CLI, so send your message again. To use KAS here, update kiro-cli "
    "and restart Kiro Crew."
)

#: The same refusal where nothing can take KAS's place: KAS is this build's
#: governance floor (``agent_sdk.backends.governance_floor_backend``), so every
#: KAS session keeps failing until kiro-cli serves its v3 engine here.
KAS_ENGINE_REFUSED_NO_FALLBACK_MESSAGE = (
    'KAS could not start: kiro-cli refuses its v3 engine on this host ("V3 is '
    'currently not supported on this system"). Update kiro-cli to a release that '
    "runs its v3 engine here, then restart Kiro Crew."
)

#: The data-home file holding each kiro-cli's v3 start verdict across processes:
#: ``{path: {"mtime_ns": int, "size": int, "starts": bool}}``. A cache of a fact
#: about a local binary, no secret and no ceiling, so it is an ordinary sandbox-
#: visible leaf: a hand-written entry can only keep KAS off (the Kiro CLI launch the
#: policy already allows) or cost one refused start, which records the truth again.
KAS_ENGINE_START_LEAF = "kas_engine_start.json"

#: Whether this process has already logged the verdict and reloaded the live
#: config for it. Repeat refusals still raise; they log and reload nothing more.
_recorded = False


def is_kas_engine_refusal_output(haystack: str) -> bool:
    """True when *haystack* (kiro-cli's stderr) carries its refusal of the v3 engine."""
    return _KAS_ENGINE_REFUSAL_WORDS in haystack.casefold()


class AcpKasEngineRefused(AcpError):  # noqa: N818
    """kiro-cli refused to start its v3 (KAS) engine on this host.

    Non-retryable for the reason :class:`AcpDelegateLaunchFailed` is: the refusal
    is the host's, so a respawn reproduces it. A plain ``AcpError`` rather than an
    ``AcpProcessDied``, so the chat surfaces' process-death respawn arms never see
    it and their terminal error arm shows this fixed message (``user_worded``).
    """

    def __init__(self, message: str = KAS_ENGINE_REFUSED_MESSAGE) -> None:
        super().__init__(message, transient=False, user_worded=True)


def record_kas_engine_refusal() -> bool:
    """Record kiro-cli's v3 refusal as this process's KAS verdict; whether KAS falls back.

    Marks KAS unservable (``mark_backend_unservable``, a no-op where KAS is the
    governance floor) and withdraws the evidence that moved a Kiro CLI pick onto
    KAS (``revoke_kiro_cli_offers_kas``), so the next config load resolves a
    default, an explicit KAS and a promoted pick to the Kiro CLI launch. The first
    time in a process it also logs the verdict, evicts the dashboard's cached KAS
    install probe and wakes the config watcher for a forced reload, which rebuilds
    the provider factory from that load. The wake does not wait: this runs inside a
    session start, which may hold the pool-fill lock the reload's apply takes.
    In memory only and idempotent, so it is safe on the event loop and from every
    reader that sees the refusal; :func:`remember_kas_engine_refusal` keeps it on disk.
    """
    global _recorded
    from kiro_crew.agent_sdk.backends import (
        ACP_BACKEND_KAS,
        mark_backend_unservable,
        revoke_kiro_cli_offers_kas,
        selectable_backends,
    )

    revoke_kiro_cli_offers_kas()
    mark_backend_unservable(ACP_BACKEND_KAS, KAS_ENGINE_REFUSED_REASON)
    falls_back = ACP_BACKEND_KAS not in selectable_backends()
    if not _recorded:
        _recorded = True
        logger.warning(
            "KAS could not start: %s. %s",
            KAS_ENGINE_REFUSED_REASON,
            (
                "Kiro Crew runs new sessions on Kiro CLI instead. Update kiro-cli and "
                "restart Kiro Crew to use KAS."
                if falls_back
                else "KAS is this build's floor, so KAS sessions keep failing until "
                "kiro-cli runs its v3 engine here."
            ),
        )
        _follow_the_verdict(ACP_BACKEND_KAS)
    return falls_back


def kas_engine_refused() -> AcpKasEngineRefused:
    """Record kiro-cli's v3 refusal as this host's KAS verdict; the error to raise.

    :func:`record_kas_engine_refusal`, then the message that matches it: a fallback
    to Kiro CLI, or none where KAS is the governance floor.
    """
    falls_back = record_kas_engine_refusal()
    return AcpKasEngineRefused(
        KAS_ENGINE_REFUSED_MESSAGE if falls_back else KAS_ENGINE_REFUSED_NO_FALLBACK_MESSAGE
    )


async def record_refused_kas_start() -> None:
    """The runtime's recorder: the verdict in memory now, on disk off the loop. Never raises.

    Awaited by a KAS runtime's stderr drain the first time it reads the refusal
    before ``initialize``, which every KAS start passes whatever spawned it.
    """
    try:
        record_kas_engine_refusal()
    except Exception:  # noqa: BLE001 - a drain must keep reading whatever this does
        logger.debug("could not record the KAS refusal", exc_info=True)
    try:
        await asyncio.to_thread(remember_kas_engine_refusal)
    except Exception:  # noqa: BLE001 - a loop or executor shutting down refuses the hop
        # The in-memory verdict above stands; the next process's start records it again.
        logger.debug("could not keep the KAS refusal in the data home", exc_info=True)


def _follow_the_verdict(backend: str) -> None:
    """Make the dashboard and the provider factory read the verdict now. Never raises."""
    try:
        from kiro_crew.agent_sdk import backend_install
        from kiro_crew.config import live

        backend_install.forget_probe(backend)
        # The document did not change, but what it loads as did; the forced cycle
        # this asks for reloads it whatever its fingerprint says.
        live.watch().notify_written()
    except Exception:  # noqa: BLE001 - the verdict is recorded; the next load reads it
        logger.debug("could not reload the live config after the KAS refusal", exc_info=True)


# -- The record that outlives the process --


def _start_record_path() -> Path:
    from kiro_crew.config.paths import data_home

    return data_home() / KAS_ENGINE_START_LEAF


def _identity(binary: str) -> tuple[int, int] | None:
    """``(mtime_ns, size)`` of *binary* as it is now, or ``None`` when it cannot be read."""
    try:
        st = os.stat(binary)
    except (OSError, ValueError):
        return None
    return st.st_mtime_ns, st.st_size


def _read_start_records() -> dict[str, Any]:
    try:
        records = json.loads(_start_record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return records if isinstance(records, dict) else {}


def _entry_matches(entry: object, identity: tuple[int, int] | None) -> bool:
    return (
        identity is not None
        and isinstance(entry, dict)
        and isinstance(entry.get("starts"), bool)
        and (entry.get("mtime_ns"), entry.get("size")) == identity
    )


def recorded_start_verdict(binary: str) -> bool | None:
    """The v3 start verdict this data home holds for *binary* as it is now, or ``None``.

    ``True`` the engine started, ``False`` kiro-cli refused it, ``None`` nothing is
    recorded for this identity (never checked, or the binary changed since).
    Blocking (a stat and a small read); never raises.
    """
    entry = _read_start_records().get(binary)
    if not isinstance(entry, dict) or not _entry_matches(entry, _identity(binary)):
        return None
    return bool(entry["starts"])


def remember_start_verdict(binary: str, starts: bool) -> None:
    """Keep *starts* for *binary* as it is now in the data home. Blocking; never raises.

    Entries whose binary changed or vanished are dropped on the way, so the file
    holds only verdicts a reader can still use.
    """
    identity = _identity(binary)
    if identity is None:
        return
    try:
        records = _read_start_records()
        entry = {"mtime_ns": identity[0], "size": identity[1], "starts": starts}
        if records.get(binary) == entry:
            return
        kept = {
            path: value
            for path, value in records.items()
            if path != binary and _entry_matches(value, _identity(path))
        }
        kept[binary] = entry
        from kiro_crew.atomic_write import atomic_write

        atomic_write(_start_record_path(), json.dumps(kept, indent=1, sort_keys=True) + "\n")
    except Exception:  # noqa: BLE001 - the in-memory verdict stands; the next start re-checks
        logger.debug("could not keep the KAS start verdict for %s", binary, exc_info=True)


def remember_kas_engine_refusal() -> None:
    """Keep the refusal for the kiro-cli a KAS session runs. Blocking; never raises.

    Keyed as the floor's start check reads it
    (``agent_sdk.drivers.acp.kas_engine_start_binary``), including a kiro-cli
    reached only through ``PATH``, which that check never starts: this record is
    the only refusal it can have.
    """
    try:
        from kiro_crew.agent_sdk.drivers.acp import kas_engine_start_binary

        binary, _may_start = kas_engine_start_binary()
    except Exception:  # noqa: BLE001 - an unresolvable install has no identity to key on
        return
    if binary:
        remember_start_verdict(binary, False)
