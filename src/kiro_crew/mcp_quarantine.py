"""Consecutive-probe-failure quarantine for MCP servers.

A probe verdict was display-only AND forgotten between rounds. ``probe_server``
could time out on the same server eight times running and nothing anywhere
could say so: the two probe caches are process memory with a 600s and a 1800s
TTL, neither carries a consecutive-failure count, and the dashboard row showed
only the latest single reading. An error badge looked identical whether a server
had failed once on a cold cache or forty times in a row.

This module is the missing durable fact: a per-server count of CONSECUTIVE
failed probes, which the row then reports.

It does NOT unmount the failing server -- see issue #6171. Three levers for that
were implemented and each shown unsafe, because the generated agent config is
simultaneously the mount decision and the only home for agent-scope MCP
configuration. Nothing here writes any config file.

Nor does it ever write ``disabled``. That key is the USER's choice, living in
``~/.kiro/settings/mcp.json``; a count that flipped it would be
indistinguishable from the user having turned the server off, and a later probe
success could silently re-enable something they had switched off by hand. The
count lives only in this store, so clearing it is one counter reset.

Why a counter and not a single failure: a probe spawns a real process and one
failure is routinely transient (a cold npm cache, a laptop that just woke, a
registry blip). The claim being made is that a server is *consistently*
unreachable, which is the case worth reporting.

Statuses that carry no verdict are ignored rather than counted as either
outcome -- ``disabled`` (never probed), ``unknown`` / ``outdated`` (no fresh
result), and ``needs_auth``, which is a server working correctly and telling us
it wants a token. Counting ``needs_auth`` would quarantine every OAuth
connection the user has not signed into yet.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from kiro_crew.atomic_write import atomic_write
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.paths import data_home

logger = logging.getLogger(__name__)

STORE_FILENAME = "mcp-quarantine.json"
STORE_VERSION = 1

# A failed probe. Anything not in here and not "ok" carries no verdict at all.
FAILING_STATUSES = frozenset({"error", "timeout"})

# Bound the stored error so a pathological server cannot grow the state file
# without limit. The UI shows the live probe error anyway; this copy exists so
# the quarantine can explain itself after the probe cache has aged out.
_ERROR_MAX_CHARS = 400

# The probe status is one of a small set of known words; the bound is only here so
# a hand-written store cannot put an unbounded string on the wire.
_STATUS_MAX_CHARS = 40

# Ceiling for the stored counter. It exists to keep the value SERIALIZABLE, not to
# express a policy: an int past ``sys.get_int_max_str_digits()`` (4300 by default
# on 3.11+) cannot be re-encoded, so an absurd value written into the unfenced
# store would 500 the next probe on its way back out. Chosen far above any real
# reading -- a failing probe every ten minutes for ~19 years -- so it can never
# clamp a genuine count or a legitimately configured threshold.
_FAILS_MAX = 1_000_000

# Read cap for the store file. One record is a few hundred bytes, so 8 MiB is on
# the order of sixteen thousand MCP servers -- unreachable in practice, which is
# the point: the cap exists to stop an unbounded read of an agent-writable path,
# not to police a fleet size. Set low enough to be hit by our own writes it would
# make the store unreadable and then overwritable, which is data loss.
_STORE_MAX_BYTES = 8 * 1024 * 1024

# Tests point this at a tmp_path. Resolved per call, never at import, so a
# ``KIROCREW_HOME`` change between calls is honoured (the convention every
# other small state file in this tree follows).
_STORE_PATH: Path | None = None

# Every load-modify-save runs under this. Without it a probe round and a release
# race: both read the same records, and whichever saves last silently discards
# the other's decision -- so a release could report success while the record it
# deleted was written straight back, leaving the server quarantined and the
# button looking broken. Both writers live in the gateway process (the probe
# fan-out and the release endpoint are dashboard handlers), so a process-local
# lock closes the whole window. Readers take no lock: ``atomic_write`` renames,
# so a reader sees one whole version or the previous one, never a torn file.
_WRITE_LOCK = threading.Lock()


def store_path() -> Path:
    return _STORE_PATH if _STORE_PATH is not None else data_home() / STORE_FILENAME


def threshold() -> int:
    """Consecutive failures before a server is quarantined; 0 disables entirely.

    Read live rather than cached: the off switch has to be an off switch, and a
    stale copy would keep quarantining after an operator turned it off.
    """
    try:
        return max(0, int(KiroCrewConfig.load().agent.mcp_quarantine_after_failures))
    except Exception:
        logger.debug("cannot read mcp_quarantine_after_failures; feature off", exc_info=True)
        return 0


def _sanitize(rec: dict[str, Any]) -> dict[str, Any]:
    """Rebuild one loaded record from KNOWN fields only, each a bounded scalar.

    An allowlist, deliberately, not a copy-with-fixups. This store is not fenced
    under ``security._CREW_SECRET_LEAVES``, so an agent's file tools -- or a
    hand-edit -- can put anything in it, and a passthrough copy (``dict(rec)``)
    carried that anything straight back into ``json.dumps`` on the next write.
    Two ways that bit:

    * a ``fails`` of 4300 nines PARSES fine, so the corrupt arm never sees it, and
      then ``+ 1`` makes it 4301 digits, which ``json.dumps`` cannot encode;
    * an extra key holding ~900 nested arrays also parses fine, and then
      ``json.dumps`` recurses over it and raises ``RecursionError`` -- a
      ``RuntimeError``, so it escaped the save guard too. Whether it raises
      depends on the frames left when the write happens, so it reproduces through
      a handler and not from a shallow stack.

    Fixing either one in isolation leaves the other, and the next unknown key
    shape after that. Rebuilding from an allowlist means NOTHING from the file
    reaches the encoder: every value written is a scalar this module produced, so
    ``json.dumps`` cannot fail on content at all.

    Unknown keys are dropped rather than preserved. Nothing reads them, the store
    is ours, and the only thing keeping them ever did was carry this defect.
    """
    fails = rec.get("fails")
    if not isinstance(fails, int) or isinstance(fails, bool) or fails < 0:
        fails = 0
    status = rec.get("last_status")
    error = rec.get("last_error")
    return {
        "fails": min(fails, _FAILS_MAX),
        "last_status": status[:_STATUS_MAX_CHARS] if isinstance(status, str) else "",
        "last_error": error[:_ERROR_MAX_CHARS] if isinstance(error, str) else "",
        "last_failed_at": _as_time(rec.get("last_failed_at")),
        # 0.0 and absent mean the same thing everywhere this is read ("has not
        # crossed"), so normalising to a float loses nothing and keeps the record
        # shape fixed.
        "crossed_at": _as_time(rec.get("crossed_at")),
    }


def _as_time(value: Any) -> float:
    """A stored timestamp as a plain float, or 0.0 for anything else.

    ``bool`` is excluded explicitly: it is an ``int``, so ``True`` would otherwise
    become the epoch-adjacent timestamp 1.0 and read as "crossed".
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    try:
        out = float(value)
    except (OverflowError, ValueError):
        return 0.0
    # NaN and infinities are floats that ``json.dumps`` emits as bare NaN /
    # Infinity -- valid Python, not valid JSON, so the file would no longer
    # round-trip through a strict parser.
    if out != out or out in (float("inf"), float("-inf")) or out < 0:
        return 0.0
    return out


def _read() -> tuple[dict[str, dict[str, Any]], str]:
    """``(records, outcome)`` where outcome is ``ok`` / ``unreadable`` / ``corrupt``.

    Those last two are NOT the same thing and a mutation has to tell them apart:

    * ``unreadable`` -- an ``OSError``. The file may hold perfectly good records
      we simply could not get at this instant (a Windows sharing violation
      against the antivirus scanner, EIO, a permission flip). Retrying may well
      succeed, so a writer must ABORT rather than treat this as an empty store
      and save over history it never saw.
    * ``corrupt`` -- the bytes are there and they are not our format. Invalid
      JSON, invalid UTF-8, an integer past the interpreter's digit limit, a
      document nested past its recursion limit, or the wrong shape. No amount of
      retrying fixes that, and there is nothing recoverable to protect, so a
      writer may overwrite it. That is the ONLY path back to a working store --
      aborting on corrupt too would wedge the counter permanently with no way out
      but a hand-deleted file.

    A MISSING store is neither: that is the normal state on a machine where
    nothing has ever failed a probe.
    """
    path = store_path()

    # Two SEPARATE try blocks, each holding exactly one operation. The split is
    # what lets the second one catch broadly without hiding anything: an
    # ``OSError`` can only come from the read, and a parse failure can only come
    # from the parse, so neither arm can absorb the other's errors.
    #
    # The seam is BYTES, not text. Decoding is interpretation, not I/O -- a
    # ``read_text`` here would raise ``UnicodeDecodeError`` (a ValueError) from
    # the I/O half for a file that is simply not our format, and that error is
    # neither an ``OSError`` nor something this half should be classifying.
    # ``json.loads`` takes the bytes and does the decode itself, so invalid UTF-8
    # lands in the parse arm where it belongs.
    # BOUNDED, and only from a regular file. ``read_bytes`` reads to EOF, and this
    # path is agent-writable, so a symlink pre-planted at the leaf pointing at
    # ``/dev/zero`` turns every ``GET /api/mcp`` into an unbounded allocation that
    # takes the whole gateway down with it. A FIFO is the same shape with a
    # different ending: ``open`` on one blocks until a writer appears, so the
    # request hangs instead.
    #
    # ``O_NOFOLLOW`` refuses the link itself rather than trusting where it goes,
    # and ``O_NONBLOCK`` keeps the FIFO case from blocking in ``open`` before
    # ``fstat`` can reject it (both no-ops for a regular file, and absent on
    # Windows, hence the getattr). ``fstat`` is on the DESCRIPTOR, not the path,
    # so the thing measured is the thing read -- a swap between the two cannot
    # matter.
    #
    # Only the leaf is our problem here: a redirect planted at a PARENT is what
    # ``atomic_write._refuse_linked_parent`` covers for the write side, and
    # ``os.replace`` does not follow a leaf link, so a write replaces a planted
    # link with a real file. Reading is the half that had no bound.
    flags = os.O_RDONLY
    for name in ("O_NOFOLLOW", "O_NONBLOCK", "O_BINARY"):
        flags |= getattr(os, name, 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        # Checked BEFORE the OSError arm, which it would otherwise match.
        return {}, "ok"
    except OSError:
        # ELOOP from O_NOFOLLOW lands here. A planted link is not a transient
        # condition, but calling it `unreadable` is the safe direction: it makes a
        # writer ABORT rather than overwrite, and the next write is the recovery
        # anyway once the link is gone.
        return {}, "unreadable"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            logger.warning("MCP probe-failure store %s is not a regular file", path)
            return {}, "corrupt"
        if st.st_size > _STORE_MAX_BYTES:
            logger.warning(
                "MCP probe-failure store %s is %d bytes, over the %d cap",
                path,
                st.st_size,
                _STORE_MAX_BYTES,
            )
            return {}, "corrupt"
        # Read in chunks and stop as soon as the cap is exceeded, rather than
        # trusting st_size, which a file growing under us would understate.
        data = b""
        while len(data) <= _STORE_MAX_BYTES:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            data += chunk
        if len(data) > _STORE_MAX_BYTES:
            logger.warning("MCP probe-failure store %s exceeded the read cap", path)
            return {}, "corrupt"
    except OSError:
        return {}, "unreadable"
    finally:
        os.close(fd)

    try:
        raw = json.loads(data)
    except Exception:
        # Deliberately NOT a list of exception types. Four review rounds found
        # four different ones escaping successively wider tuples --
        # ``JSONDecodeError``, then ``UnicodeDecodeError`` (a ValueError from the
        # strict decode), then a plain ``ValueError`` from the scanner's own
        # ``int()`` past ``sys.get_int_max_str_digits()``, then ``RecursionError``
        # (a RuntimeError, not a ValueError at all) from a deeply nested
        # document. The classification here is a fact about the FILE, not about
        # which Python error happened to surface, so enumerating them is the bug.
        #
        # This store is not fenced under ``security._CREW_SECRET_LEAVES``, so its
        # bytes are attacker-influenced: the set of ways a parse can fail is
        # open-ended and grows with the interpreter. Anything that leaves the
        # bytes unparseable is the same operational fact -- not our format,
        # nothing recoverable to protect, safe to overwrite.
        #
        # Broad only because the block above it is one line of pure parsing with
        # no application logic in it. There is no bug of ours for this to mask.
        logger.debug("unparseable MCP probe-failure store %s", path, exc_info=True)
        return {}, "corrupt"

    if not isinstance(raw, dict):
        return {}, "corrupt"
    servers = raw.get("servers")
    if not isinstance(servers, dict):
        return {}, "corrupt"
    return (
        {k: _sanitize(v) for k, v in servers.items() if isinstance(k, str) and isinstance(v, dict)},
        "ok",
    )


def _load() -> dict[str, dict[str, Any]]:
    """Return the per-server records, or ``{}`` for any unreadable store.

    Fails OPEN on purpose, and only READERS may use it. The records only ever ADD
    a diagnostic to a row, so a store we cannot read must not be able to mislabel
    anything -- an empty record set renders exactly as a fleet that has never
    failed a probe.

    A mutation must NOT come through here: folding "cannot read" into "no records"
    and then saving replaces history with whatever this round happened to see. Use
    ``_load_for_update``.
    """
    return _read()[0]


def _load_for_update() -> dict[str, dict[str, Any]]:
    """Records for a read-modify-write. RAISES ``OSError`` if the store is unreadable.

    The fail-open in ``_load`` is right for display and wrong here: a transient
    read failure would present as an empty store, and the save that follows would
    erase every counter on disk. Refusing to mutate what we could not read costs
    one skipped increment; the alternative costs the history.
    """
    servers, outcome = _read()
    if outcome == "unreadable":
        raise OSError(f"cannot read {store_path()}")
    return servers


def _save(servers: dict[str, dict[str, Any]]) -> None:
    """Write the records. RAISES on failure -- each caller decides what that means.

    Deliberately not swallowed here. ``clear`` must not report a reset it did not
    persist (the caller reports it to the user), whereas ``record_verdicts`` can
    safely degrade to "the counter did not advance". Those are different decisions
    and only the callers know which one applies.
    """
    payload = {"version": STORE_VERSION, "servers": servers}
    atomic_write(store_path(), json.dumps(payload, indent=2) + "\n")


def record_verdicts(verdicts: Iterable[tuple[str, str, str]]) -> None:
    """Fold ``(name, status, error)`` triples into the store; one write.

    ``ok`` deletes a server's record outright rather than decrementing it -- the
    claim being made is "consistently unreachable", and one good handshake
    disproves it. A status outside ``FAILING_STATUSES`` and not ``ok`` carries no
    verdict and is skipped, so it neither advances nor clears the count.

    Returns nothing. Nothing acts on a crossing: this records a reading and the
    row reports it. (An earlier revision returned the names whose MOUNT state
    changed, for an unmount that is now issue #6171.)
    """
    limit = threshold()
    if limit <= 0:
        return
    with _WRITE_LOCK:
        try:
            servers = _load_for_update()
        except OSError as exc:
            # Cannot read means cannot safely write: saving now would replace
            # whatever is on disk with only what this round observed. Skipping
            # the increment degrades to "the counter did not advance".
            logger.warning("cannot read MCP probe-failure store %s: %s", store_path(), exc)
            return
        changed = False
        now = time.time()
        for name, status, error in verdicts:
            if not isinstance(name, str) or not name:
                continue
            if status == "ok":
                if servers.pop(name, None) is not None:
                    changed = True
                continue
            if status not in FAILING_STATUSES:
                continue
            rec = servers.setdefault(name, {})
            # ``fails`` is already a bounded non-negative int for anything that
            # came off disk (see ``_sanitize``); the guard here covers a record
            # this loop just created.
            fails = rec.get("fails")
            rec["fails"] = min(
                (fails if isinstance(fails, int) and fails > 0 else 0) + 1, _FAILS_MAX
            )
            rec["last_status"] = status
            rec["last_error"] = (error or "")[:_ERROR_MAX_CHARS]
            rec["last_failed_at"] = now
            changed = True
            if rec["fails"] >= limit and not rec.get("crossed_at"):
                rec["crossed_at"] = now
        if not changed:
            return
        try:
            _save(servers)
        except (OSError, ValueError) as exc:
            # A store we cannot write means the count did not advance, which is
            # exactly today's behaviour. Nothing downstream depends on it.
            #
            # ValueError as well as OSError: ``json.dumps`` raises it for a value
            # it cannot encode, and this store is attacker-influenced. Sanitizing
            # on read should make that unreachable -- this arm is here so a gap in
            # that reasoning degrades to a missed increment instead of a 500.
            logger.warning("cannot write MCP probe-failure store %s: %s", store_path(), exc)


def _state_from(rec: dict[str, Any], limit: int) -> dict[str, Any]:
    """Shape one record for the API against an already-read threshold."""
    raw_fails = rec.get("fails")
    fails = raw_fails if isinstance(raw_fails, int) else 0
    return {
        "fails": fails,
        "failing": bool(limit > 0 and rec.get("crossed_at") and fails >= limit),
        "lastStatus": rec.get("last_status", ""),
        "lastError": rec.get("last_error", ""),
        "since": rec.get("crossed_at") or 0,
    }


def state_for(name: str) -> dict[str, Any] | None:
    """One server's record, or ``None`` when it has no failures on file."""
    rec = _load().get(name)
    if not rec:
        return None
    return _state_from(rec, threshold())


def clear(name: str) -> dict[str, Any] | None:
    """Reset one server's count; returns the record it removed, or ``None`` if absent.

    This is what the dashboard's reset control calls. It clears the COUNTER as
    well as the crossed flag -- resetting a server but leaving it one failure away
    from crossing again would make the button look broken.

    The removed record is handed back so the caller can tell "reset something"
    apart from "there was nothing to reset" and answer 404 for the latter.

    Propagates a read OR write failure as ``OSError`` rather than swallowing it:
    the caller reports success to the user, so a reset that did not reach disk --
    whether because the store could not be read or could not be written -- must
    not read as a reset.
    """
    with _WRITE_LOCK:
        servers = _load_for_update()
        gone = servers.pop(name, None)
        if gone is None:
            return None
        _save(servers)
        return gone


def snapshot() -> dict[str, dict[str, Any]]:
    """Every server with failures on file, shaped for the API.

    ONE store read and ONE config read for the whole set. Calling ``state_for``
    per name would re-read both per record, so annotating an N-server probe
    response cost N file reads -- on the event loop, in the handler that runs on
    every dashboard poll.
    """
    limit = threshold()
    return {name: _state_from(rec, limit) for name, rec in _load().items()}
