"""Recognise and repair kiro-cli's broken KAS extract on the relay route.

kiro-cli unpacks the KAS engine once, into ``<data dir>/kas/<version>-<sha256>/``, and
afterwards checks only that the directory exists. A copy of a home directory that left
the directory behind without its ``node_modules`` (or unreadable) makes every
``kiro-cli acp --agent-engine v3`` exit at once with::

    error: failed to canonicalize KAS server path: <dir>/node_modules/@kiro/agent/dist/server/acp-server.js

kiro-cli never re-extracts on its own and has no command to make it, so every respawn
fails the same way. Moving the directory aside is the whole fix: the next start unpacks
a fresh copy. This module decides whether Kiro Crew may do that move itself
(:func:`repair`), words the fix for a person when it may not (:func:`fix_message`), and
holds the warm pool while a recorded broken extract is still in place
(:func:`warm_pool_held`).

Only the relay prints the marker: it is kiro-cli's own error, and the bundle route runs
Node with no kiro-cli in the chain. Nothing here runs unless a start already failed with
that line, so a healthy host pays no filesystem call.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from kiro_crew import platform_compat_links
from kiro_crew.acp.transport_errors import AcpError

logger = logging.getLogger(__name__)

#: kiro-cli's stderr line for an extract whose server entry does not resolve.
BROKEN_EXTRACT_MARKER = "failed to canonicalize KAS server path:"

#: The server entry's path below one ``kas/<version>`` directory.
SERVER_ENTRY_PARTS = ("node_modules", "@kiro", "agent", "dist", "server", "acp-server.js")

#: The directory kiro-cli keeps its extracts in, under its data dir.
KAS_EXTRACTS_DIR_NAME = "kas"

#: Suffix a moved-aside extract gets; the UTC stamp follows it. The extract moves to
#: kiro-cli's data dir, beside ``kas/`` and not inside it: kiro-cli deletes every entry
#: of ``kas/`` it does not track (renamed to ``.incoming-<pid>-<name>``, then removed),
#: so a copy left there would be gone at the next start.
BROKEN_SUFFIX = ".kas-broken-"

#: A ``<version>`` directory, or one of its direct entries, changed more recently than
#: this is left alone: another process (a second gateway, a terminal kiro-cli) may be
#: writing it, and the per-process lock below cannot see that.
MIN_EXTRACT_AGE_SECS = 60.0

#: Serialises the check-and-rename across concurrent failed starts in one gateway.
_REPAIR_LOCK = threading.Lock()

#: ``(version dir, server path)`` of the last broken extract that could not be
#: repaired, or ``None``. Read by the warm pool; one slot, because one kiro-cli install
#: has one current extract.
_BLOCKED: tuple[str, str] | None = None


@dataclass(frozen=True)
class RepairVerdict:
    """What :func:`repair` did with one broken extract."""

    subject: str
    """The ``kas/<version>`` directory (or the raw server path when it cannot be parsed)."""

    retry: bool
    """True when one more start can succeed: the directory was moved, or is already gone."""

    renamed_to: str = ""
    """Where the directory was moved, when this call moved it."""


def server_path_from_stderr(stderr: str) -> str | None:
    """The server path kiro-cli could not resolve, or ``None`` when *stderr* lacks the line."""
    for line in stderr.splitlines():
        _, found, rest = line.partition(BROKEN_EXTRACT_MARKER)
        if found and rest.strip():
            return rest.strip()
    return None


def version_dir_of(server_path: str) -> Path | None:
    """The ``kas/<version>`` directory *server_path* sits in, judged by its spelling only.

    ``None`` unless the path is absolute, ends in :data:`SERVER_ENTRY_PARTS`, the
    ``<version>`` segment is a real name (not empty, ``.`` or ``..``), and the directory
    above it is named :data:`KAS_EXTRACTS_DIR_NAME`.
    """
    path = Path(server_path)
    if not path.is_absolute():
        return None
    tail = len(SERVER_ENTRY_PARTS)
    if path.parts[-tail:] != SERVER_ENTRY_PARTS or len(path.parts) < tail + 3:
        return None
    version_dir = path.parents[tail - 1]
    if version_dir.name in ("", ".", "..") or version_dir.parent.name != KAS_EXTRACTS_DIR_NAME:
        return None
    return version_dir


def _kiro_cli_data_dirs(home: Path, environ: Mapping[str, str]) -> set[str]:
    """kiro-cli's data directories on this platform, resolved."""
    from kiro_crew.identity_stores import state_db_candidates

    return {os.path.realpath(db.parent) for db in state_db_candidates(sys.platform, home, environ)}


def _repair_enabled() -> bool:
    """``agent.kas_repair_kiro_cli_extract`` as configured. Blocking.

    On by default, so the read fails CLOSED, as ``kas_secret_storage.persistence_enabled``
    does: a load that raised, or one that came back degraded (an unreadable
    ``config.json`` or ``config.local.json``, or a discarded ``agent`` section), builds
    the field default, which is the opposite of a ``false`` the owner may have written
    in the file that could not be read. Such a load moves no files.
    """
    try:
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.config.resolution import DEGRADED_WHOLE_CONFIG

        cfg = KiroCrewConfig.load()
    except Exception:  # noqa: BLE001 - a config that cannot be read leaves the files alone
        logger.debug("could not read agent.kas_repair_kiro_cli_extract", exc_info=True)
        return False
    degraded = cfg.degraded_sections
    if (
        cfg._base_unreadable
        or cfg._overlay_unreadable
        or "agent" in degraded
        or DEGRADED_WHOLE_CONFIG in degraded
    ):
        logger.info("the config read was degraded; not moving kiro-cli's KAS extract")
        return False
    return cfg.agent.kas_repair_kiro_cli_extract is True


def _newest_mtime(version_dir: Path) -> float:
    """The newest mtime of *version_dir* and its direct entries, links not followed."""
    newest = os.lstat(version_dir).st_mtime
    with os.scandir(version_dir) as entries:
        for entry in entries:
            newest = max(newest, entry.stat(follow_symlinks=False).st_mtime)
    return newest


def repair(
    server_path: str,
    *,
    enabled: bool | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RepairVerdict:
    """Move a broken extract aside when every guard holds. Blocking; never raises.

    The guards: the path parses (:func:`version_dir_of`), the ``kas`` directory resolves
    to ``<kiro-cli data dir>/kas`` (so it is not a link), ``<version>`` is a real
    directory and not a link or junction, the server path really does not resolve, the
    switch is on (*enabled*, else the config), and nothing in ``<version>`` changed in
    the last :data:`MIN_EXTRACT_AGE_SECS`. Then ``<version>`` is moved to ``<data dir>/<version>.kas-broken-<UTC stamp>``
    (moved, never deleted; outside ``kas/``, which kiro-cli sweeps). A ``<version>`` already gone, or one whose server entry resolves
    again, means a concurrent start repaired it: retry without moving anything.
    """
    version_dir = version_dir_of(server_path)
    if version_dir is None:
        return RepairVerdict(subject=server_path, retry=False)
    subject = str(version_dir)
    home = Path.home() if home is None else home
    environ = os.environ if environ is None else environ
    try:
        kas_dirs = {
            os.path.join(d, KAS_EXTRACTS_DIR_NAME) for d in _kiro_cli_data_dirs(home, environ)
        }
        # Resolved: a ``kas`` that is itself a link resolves elsewhere and is refused.
        if os.path.realpath(version_dir.parent) not in kas_dirs:
            logger.info(
                "KAS extract %s is not under kiro-cli's data directory; not moving it", subject
            )
            return RepairVerdict(subject=subject, retry=False)
        with _REPAIR_LOCK:
            if not os.path.lexists(version_dir) or os.path.exists(server_path):
                return RepairVerdict(subject=subject, retry=True)
            if platform_compat_links.is_link_or_junction(version_dir) or not version_dir.is_dir():
                return RepairVerdict(subject=subject, retry=False)
            if not (_repair_enabled() if enabled is None else enabled):
                return RepairVerdict(subject=subject, retry=False)
            if time.time() - _newest_mtime(version_dir) < MIN_EXTRACT_AGE_SECS:
                logger.info("KAS extract %s changed in the last minute; not moving it", subject)
                return RepairVerdict(subject=subject, retry=False)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            target = version_dir.parent.parent / f"{version_dir.name}{BROKEN_SUFFIX}{stamp}"
            os.rename(version_dir, target)
    except OSError:
        logger.warning("could not move the broken KAS extract %s aside", subject, exc_info=True)
        return RepairVerdict(subject=subject, retry=False)
    logger.warning(
        "kiro-cli's KAS extract %s was incomplete (%s does not resolve); moved it to %s so "
        "kiro-cli extracts a fresh copy, and retrying the start once. The moved copy is not "
        "used again and can be deleted",
        subject,
        server_path,
        target,
    )
    return RepairVerdict(subject=subject, retry=True, renamed_to=str(target))


def fix_message(verdict: RepairVerdict) -> str:
    """The one sentence a person needs: what is broken and the command that fixes it."""
    version_dir = Path(verdict.subject)
    if version_dir.parent.name != KAS_EXTRACTS_DIR_NAME:
        # The path did not parse to kiro-cli's layout: name it, without a command.
        return (
            f"Kiro CLI cannot start the KAS engine: it could not resolve {verdict.subject}. "
            "Move the kas/<version> directory that holds it out of kas/ so kiro-cli unpacks "
            "a fresh copy, then retry."
        )
    target = version_dir.parent.parent / f"{version_dir.name}{BROKEN_SUFFIX}manual"
    # shortcut: the command is POSIX shell; a Windows host gets the same mv wording, which
    # PowerShell accepts as an alias. Upgrade when a Windows report shows this marker.
    moved = (
        f" Kiro Crew moved it to {verdict.renamed_to}, and the new copy failed the same way."
        if verdict.renamed_to
        else ""
    )
    return (
        f"Kiro CLI cannot start the KAS engine: its unpacked copy at {verdict.subject} is "
        "incomplete (acp-server.js is missing or unreadable), which usually follows copying "
        f"a home directory to another machine.{moved} Move it out of kas/ so kiro-cli unpacks "
        f"a fresh copy, then retry: mv {shlex.quote(verdict.subject)} {shlex.quote(str(target))}"
    )


class AcpKasExtractBroken(AcpError):  # noqa: N818
    """kiro-cli's unpacked KAS engine is incomplete, so every relay start exits at once.

    Non-retryable for the reason :class:`~kiro_crew.acp.transport_errors.AcpDelegateLaunchFailed` is: kiro-cli fails
    the same way on every respawn until the directory is moved, and it is a distinct
    ``AcpError``, NOT an ``AcpProcessDied``, so the chat surfaces' respawn arms never
    see it and their terminal arm shows the message. The message names the directory
    and the command (:func:`fix_message`); the runtime
    has already moved the directory aside once when its guards allowed.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, transient=False, user_worded=True)


# Named by the path callers import it from, like the classes in ``transport_errors``.
AcpKasExtractBroken.__module__ = "kiro_crew.acp.client"


def note_blocked(verdict: RepairVerdict, server_path: str) -> None:
    """Record a broken extract that is still in place, so the warm pool stops respawning."""
    global _BLOCKED
    record = (verdict.subject, server_path)
    if _BLOCKED == record:
        return
    _BLOCKED = record
    logger.warning("%s The warm pool waits until it is moved.", fix_message(verdict))


def _still_blocked() -> bool:
    """Whether the recorded broken extract is still there and still broken. Blocking."""
    global _BLOCKED
    record = _BLOCKED
    if record is None:
        return False
    version_dir, server_path = record
    if os.path.lexists(version_dir) and not os.path.exists(server_path):
        return True
    _BLOCKED = None
    logger.info("KAS extract %s changed; the warm pool spawns again", version_dir)
    return False


def _spawns_kas_relay(provider: object) -> bool:
    """Whether *provider*'s next start is KAS through kiro-cli's relay. No IO.

    The KAS harness is named positively (``is_kas_backend``), and a gateway whose
    last KAS spawn took the bundle route is not held: that route never reads
    kiro-cli's extract.
    """
    from kiro_crew.acp.kas_route import last_spawn_route

    if getattr(provider, "is_kas_backend", False) is not True:
        return False
    route = last_spawn_route()
    return route is None or not route.bundle


async def warm_pool_held(provider: object) -> bool:
    """True while a recorded broken extract is unmoved and *provider* would start on it.

    No filesystem call unless a broken extract was recorded.
    """
    if _BLOCKED is None or not _spawns_kas_relay(provider):
        return False
    return await asyncio.to_thread(_still_blocked)
