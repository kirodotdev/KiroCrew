"""Gateway identity — one stable random id per ``KIROCREW_HOME``.

Answers exactly one question: *are these two dashboard ports the same gateway?*
Remote Crew chaining needs that answer to refuse a connection that closes a loop
(a hub reached through one of its own children), and a host string cannot give
it — one machine has many spellings (``localhost``, an ssh alias, an FQDN, an
IP), so comparing ``ssh_host`` would refuse unrelated crews and admit real
cycles. The id is compared instead, over the tunnel that was just opened.

Properties that make it safe to hand to a peer:

* **Random, not derived.** A ``uuid4`` hex string. It carries no hostname, no
  user, no MAC address and no path, so a peer learns only "this is the same
  gateway I already talked to" or "this is a different one".
* **Per data home, not per process.** Persisted under the data home, so a
  gateway restart keeps its identity and two gateways sharing a machine but
  running separate ``KIROCREW_HOME`` directories are correctly distinct.
* **Distinct from the telemetry install id.** ``beacon.install_id()`` is the
  telemetry egress identity and is materialised only once telemetry consent
  exists, so reading it here would mint a telemetry identity on a host that
  opted out. The technique is copied; the value deliberately is not, so
  disabling one cannot change what the other means.

The first mint is serialised by a cross-process lock, and the file is then put in
place with an atomic ``os.replace``: two processes racing it converge on one id
instead of overwriting each other, because the second takes the lock, re-reads,
and adopts the id the first wrote. Two ids for one gateway would make the cycle
guard miss a loop it exists to catch.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path

from kiro_crew import platform_compat
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: Filename under the data home holding this gateway's id.
GATEWAY_ID_FILE = "gateway_id"

#: Accepted shape: a 32-char lowercase ``uuid4().hex``. Validated on read as
#: well as on mint, so a hand-edited or truncated file is treated as absent
#: rather than published to a peer as an identity.
_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")

#: Read cap for the id file. The content is 32 bytes; this is ample slack while
#: still bounding a corrupt or hostile file.
_MAX_ID_BYTES = 4096

#: Fallback id for a process that cannot persist one. Process-local and stable
#: for this process's lifetime, so the cycle guard still compares something
#: meaningful within one run; a restart gets a new one.
_IN_MEMORY_ID = uuid.uuid4().hex

# Resolved ids, keyed by the id FILE's path so two data homes stay two gateways.
# The id is immutable once written, and `/api/health` is the most frequently
# polled endpoint there is, so re-reading the file per request would put a
# filesystem round-trip on the event loop for a value that cannot change. Only a
# valid persisted id is cached: an empty `create=False` miss and the process-local
# fallback are both conditions a later call can legitimately resolve differently.
_CACHED_IDS: dict[str, str] = {}


def _read_id(path: Path) -> str:
    """Read the id file safely, or return ``""`` for anything unusable.

    Mirrors ``_settings_path_holds``'s guards, each closing a distinct failure
    mode: regular files only, a bounded read, and a lenient decode (a strict
    decode raises ``UnicodeDecodeError``, a ``ValueError`` rather than an
    ``OSError``, which would escape the caller's handler).

    **The guards inspect the DESCRIPTOR, not the path.** Checking the path and
    then opening it resolves the name twice, and the two answers need not
    describe the same object: whatever is at that moment at the path is what gets
    opened. The path sits in the owner-writable data home, so between the two a
    regular file can become a symlink -- which the open would follow, reading
    somewhere else entirely -- or a FIFO, whose open blocks until a writer
    appears. That block is the worse half: this runs in ``asyncio.to_thread`` on
    the SHARED default executor, so a held worker is the gateway losing capacity
    until restart rather than one slow request, and ``/api/health`` re-enters
    here on every poll because a rejected file is not cached.

    So the open goes through :func:`platform_compat.open_file_no_reparse`, whose
    docstring carries the platform reasoning; it refuses the redirect at the final
    name on either platform and, with ``nonblocking``, declines to wait. It does
    NOT refuse a FIFO -- it only stops the open from waiting for one -- so the
    ``S_ISREG`` check below is this function's own and is load-bearing: it is what
    rejects a pipe whose writer wins the race to supply bytes. ``fstat`` judges the
    object the descriptor already names, leaving no second resolution to disagree.
    """
    try:
        fd = platform_compat.open_file_no_reparse(path, nonblocking=True)
    except OSError:
        return ""
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            logger.debug("gateway id %s is not a regular file; ignoring", path.name)
            return ""
        raw = os.read(fd, _MAX_ID_BYTES)
    except (OSError, ValueError):
        return ""
    finally:
        os.close(fd)
    return raw.decode("utf-8", errors="replace").strip()


def gateway_id(*, create: bool = True) -> str:
    """Return this gateway's stable random id, minting it on first use.

    With ``create=False`` the file is only read: a caller that merely reports
    identity must not materialise one as a side effect.

    Never raises. A data home that cannot be read or written yields the
    process-local fallback, because the alternative — refusing — would turn an
    unwritable directory into a gateway that cannot connect a crew at all. The
    fallback still tells two live gateways apart, which is the comparison the
    cycle guard makes.
    """
    try:
        path = config_dir() / GATEWAY_ID_FILE
        cached = _CACHED_IDS.get(str(path))
        if cached:
            return cached
        existing = _read_id(path) if path.exists() else ""
        if _ID_RE.match(existing):
            _CACHED_IDS[str(path)] = existing
            return existing
        if not create:
            return ""
        path.parent.mkdir(parents=True, exist_ok=True)
        # Everything past here REPLACES the file, so it runs under a
        # cross-process lock and caches what is on DISK at the end rather than
        # what this process wrote. Two gateways repairing one corrupt file
        # otherwise each remove the other's replacement and cache a different
        # id for the same data home -- and the id exists to tell two gateways
        # apart, so two answers for one home is the one failure it must not have.
        # The lock is a sibling file: locking the id file itself would mean
        # holding an fd on the path being replaced.
        with platform_compat.open_lock_file(str(path) + ".lock") as lock_fd:
            with platform_compat.file_lock(lock_fd, exclusive=True):
                # Re-read INSIDE the lock: the holder before us may have
                # repaired it, and adopting its answer is what makes the two
                # processes agree.
                settled = _read_id(path) if path.exists() else ""
                if not _ID_RE.match(settled):
                    _install_fresh_id(path)
                    settled = _read_id(path) if path.exists() else ""
        if _ID_RE.match(settled):
            _CACHED_IDS[str(path)] = settled
            return settled
        return _IN_MEMORY_ID
    except (OSError, ValueError) as e:
        logger.debug("could not persist a gateway id (%s); using a process-local one", e)
        return _IN_MEMORY_ID


def _install_fresh_id(path: Path) -> None:
    """Write a fresh id to *path*, replacing whatever is there.

    The caller holds the cross-process lock, so this does not race another
    gateway and needs no link-vs-rename trick: ``os.replace`` is atomic, so a
    concurrent READER sees either the old bytes or the new ones, never a
    half-written file. Permissions are set on the temp file BEFORE it takes the
    final name, so the id is never briefly world-readable under its real path.
    """
    tmp_fd, tmp_path = tempfile.mkstemp(dir=str(path.parent))
    try:
        os.write(tmp_fd, uuid.uuid4().hex.encode("utf-8"))
        os.close(tmp_fd)
        tmp_fd = -1
        # restrict_to_owner, NOT a bare os.chmod under an IS_POSIX branch: the
        # raw call is a silent no-op on Windows, which would leave the id
        # world-readable.
        with contextlib.suppress(OSError):
            platform_compat.restrict_to_owner(tmp_path)
        os.replace(tmp_path, str(path))
        tmp_path = ""
    finally:
        if tmp_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(tmp_fd)
        if tmp_path:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
