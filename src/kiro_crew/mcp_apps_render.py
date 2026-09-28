"""Dashboard-side interception of MCP Apps render markers.

The gateway daemon (``src/kiro_crew/mcp_gateway/``) writes a UI payload to a
spool file at ``$KIROCREW_HOME/mcp-apps/<uuid4hex>.json`` and injects an opaque
marker ``[kirocrew-mcp-app:<uuid4hex>]`` into the tool result *text* that flows
back to kiro-cli. That text reaches the dashboard backend as an
``EVENT_TOOL_RESULT`` (``AcpEvent.tool_output``) inside ``_run_chat``.

This module is the tiny, self-contained seam the chat runner calls on every
tool result: it detects the marker, loads the spooled payload (strictly by a
validated 32-hex id — no attacker-controlled path ever touches the filesystem),
pushes an ``mcp_app_render`` websocket event to the slot, and returns the
transcript text with the marker stripped (a cosmetic rewrite, exactly like the
existing redaction passes rewrite displayed text).

Security posture:
  * The id is validated ``^[0-9a-f]{32}$`` and the spool path is built ONLY from
    that validated id joined to the spool dir — path traversal is impossible by
    construction (``../`` etc. never match the regex and are never used to form
    a path).
  * The LLM never reads the payload file; only this deterministic code does.
  * Missing / corrupt / oversized spool files are tolerated (return ``None``) so
    a bad payload can never crash a turn.
  * This side is flag-independent: if a marker appears we handle it. The
    ``KIROCREW_MCP_APPS`` opt-in gate lives on the gateway (producer) side.
"""

from __future__ import annotations

import asyncio
import heapq
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, NamedTuple

from kiro_crew import security
from kiro_crew.config.paths import config_dir

logger = logging.getLogger(__name__)


class ToolResultRender(NamedTuple):
    """What this seam did with one tool result.

    ``text`` is the transcript text with the marker stripped, which is what
    every caller needs.

    ``app_on_row`` is true when the spool record loaded, passed its
    session-binding check, and a render claim for it is SPENT -- whether this
    call spent it or an earlier one did. That is the fact a transcript needs,
    and the claim being spent is exactly why the app can never appear for this
    call again.

    It covers the already-spent case DELIBERATELY, and that is what makes the
    claim sidecar sufficient on its own. The sidecar is created durably, while
    this flag reaches disk only when the slot is next persisted, so a gateway
    death between the two would otherwise leave a spent claim with no flag: the
    app unrenderable forever and nothing on the row saying it existed, which is
    the exact silence this seam exists to remove. Because an EXISTING sidecar is
    itself proof the record was claimed, a replay after that crash reports the
    flag from the sidecar alone and the row recovers on its own, so no ordering
    between the two writes is required. The flag is put back on both paths: by
    the ``"spent"`` branch when the row is re-processed inside a live turn, and
    by :func:`load_claimed_row_groups` plus :func:`apply_claimed_rows` when a
    restart rebuilds the slot -- that second path DOES read the spool back per
    restored slot, which is what covers a crash no later replay reaches.

    A replay reporting the flag cannot mislabel someone else's row: the
    session-binding check runs in ``_load_bound``, BEFORE the claim is
    consulted, so a cross-session marker returns no record and never reaches
    this branch.

    It deliberately does NOT assert that a browser received the frame.
    ``broadcast_ws_owners`` drops the payload when no owner socket is attached,
    and an unattended run has no viewer at all -- which is precisely the case
    where the transcript must still be able to say this call produced an app.
    Gating on a delivered count would record nothing there, so a reader opening
    that session later would be told nothing, which is the gap this exists to
    close. Nor does it wait on the dispatch succeeding: the claim is spent
    before the send, so a send that raises leaves an app that can never be
    shown, which the reader still needs to know about.

    It is also the one fact a caller cannot work out for itself. A marker's
    PRESENCE does not mean a record was claimed: a record can be expired or
    unreadable, and a cross-session marker is refused, and every one of those
    returns stripped text that looks identical to a spent claim's.

    A return value rather than an optional callback, because mypy then makes
    every caller confront the flag. A hook defaulting to ``None`` can be
    dropped by the next call site, and the durable flag would quietly stop
    being written with nothing failing.
    """

    text: str
    app_on_row: bool


def _redact_leaves(obj: Any) -> Any:
    """Recursively apply credential + exfiltration-URL redaction to every
    string leaf of *obj*, returning a redacted copy.

    App payloads (``tool_input`` / ``structured_content`` / ``result_content``)
    are delivered into a server-authored iframe that can open network
    connections to its declared CSP origins; a credential or exfil URL that
    leaks through a tool result must be scrubbed before it crosses that trust
    boundary (same discipline the transcript/WS redaction passes apply). Bounded
    by the spool size cap already enforced on load.
    """
    if isinstance(obj, str):
        return security.redact(obj)
    if isinstance(obj, list):
        return [_redact_leaves(x) for x in obj]
    if isinstance(obj, dict):
        # Redact string KEYS too — a credential can appear as a dict key, not
        # just a value.
        return {
            (security.redact(k) if isinstance(k, str) else k): _redact_leaves(v)
            for k, v in obj.items()
        }
    return obj


# Opaque render marker: literal tag carrying a uuid4 hex (32 lowercase hex).
# Anchored to exactly 32 hex chars so a stray "[kirocrew-mcp-app:...]" with the
# wrong shape is simply ignored rather than treated as a spool id.
MARKER_RE = re.compile(r"\[kirocrew-mcp-app:([0-9a-f]{32})\]")

# A spool id is a bare uuid4 hex — validated independently of the marker so
# load_spool cannot be tricked into resolving a non-id string into a path.
_SPOOL_ID_RE = re.compile(r"\A[0-9a-f]{32}\Z")

# On-disk spool record schema version. Written by the gateway
# (``mcp_gateway/apps.py`` imports this constant — single source of truth) and
# ENFORCED by every reader: a record whose ``schema`` differs is rejected
# (fail-closed) so a future breaking shape change can never be silently
# mis-read by a stale reader.
SPOOL_SCHEMA_VERSION = 1

# Refuse to parse absurdly large spool files (defense against a runaway/hostile
# payload filling memory). The real HTML lives inside; 8 MiB is far past any
# realistic MCP App bundle while still bounding the read.
_MAX_SPOOL_BYTES = 8 * 1024 * 1024

#: Public alias so the writer (``mcp_gateway/apps.py``) enforces the SAME cap
#: it reads with — a record the reader would refuse is never written.
MAX_SPOOL_BYTES = _MAX_SPOOL_BYTES

#: Capability TTL enforced on read. Matches ``sweep_spool``'s 24h default: a
#: record (and its callback_secret) older than this is refused and reaped even
#: if no sweep has run since.
SPOOL_TTL_SECS = 24 * 3600


def _spool_dir() -> Path:
    """Directory holding spool files. ``KIROCREW_MCP_APPS_SPOOL`` overrides the
    default ``$KIROCREW_HOME/mcp-apps``."""
    override = os.environ.get("KIROCREW_MCP_APPS_SPOOL")
    if override:
        return Path(override).expanduser()
    return config_dir() / "mcp-apps"


def find_marker(text: str | None) -> str | None:
    """Return the spool id embedded in *text*, or ``None`` if no marker is present."""
    if not text:
        return None
    m = MARKER_RE.search(text)
    return m.group(1) if m else None


def strip_marker(text: str | None) -> str:
    """Return *text* with every render marker removed (cosmetic — the marker is
    an internal control token the user should never see in the transcript)."""
    if not text:
        return text or ""
    return MARKER_RE.sub("", text)


def load_spool(spool_id: str) -> dict[str, Any] | None:
    """Load and parse the spool file for *spool_id*.

    Returns the parsed dict, or ``None`` when the id is malformed, the file is
    missing, unreadable, too large, or not valid JSON object. The path is
    constructed STRICTLY from the validated id (``<spool_dir>/<id>.json``); the
    caller-supplied value is never used as a path fragment, so traversal such as
    ``"../../etc/passwd"`` cannot resolve (it fails the id regex first, and even
    if it somehow reached the join it would carry no ``.json`` id shape).
    """
    if not isinstance(spool_id, str) or not _SPOOL_ID_RE.match(spool_id):
        return None
    path = _spool_dir() / f"{spool_id}.json"
    try:
        # Resolve and re-verify containment as belt-and-suspenders: the resolved
        # file's parent must be the spool dir. (Given the id regex this always
        # holds; the check documents and enforces the invariant.)
        base = _spool_dir().resolve()
        resolved = path.resolve()
        if resolved.parent != base:
            logger.warning("mcp-apps spool path escaped spool dir; refusing")
            return None
        if not resolved.is_file():
            return None
        st = resolved.stat()
        if st.st_size > _MAX_SPOOL_BYTES:
            logger.warning("mcp-apps spool file %s exceeds size cap; ignoring", spool_id)
            return None
        # Enforce the documented capability TTL on READ, not only via the
        # startup/opportunistic sweep: a host that renders an app but neither
        # restarts its gateway nor writes another record would otherwise keep a
        # stale record — and its callback_secret — valid indefinitely. Reject
        # (and best-effort reap the record + its .rendered sidecar) past the
        # TTL, matching sweep_spool's 24h window.
        if time.time() - st.st_mtime > SPOOL_TTL_SECS:
            logger.info("mcp-apps spool %s is past its TTL; refusing", spool_id)
            for stale in (resolved, resolved.with_suffix(".rendered")):
                try:
                    stale.unlink()
                except OSError:
                    pass
            return None
        with resolved.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # Missing, unreadable, or corrupt JSON — tolerate silently (debug only).
        logger.debug("mcp-apps spool load failed for %s", spool_id, exc_info=True)
        return None
    if not isinstance(data, dict):
        return None
    if data.get("schema") != SPOOL_SCHEMA_VERSION:
        # Fail closed on any other version (or a missing field): the version
        # marker exists precisely so a stale reader rejects a record it does
        # not understand instead of silently mis-reading it.
        logger.warning(
            "mcp-apps spool %s has unsupported schema %r; refusing",
            spool_id,
            data.get("schema"),
        )
        return None
    return data


def _load_bound(spool_id: str, producing_session_key: str) -> dict[str, Any] | None:
    """Load the spool record and validate its session binding. Takes NO claim.

    ``producing_session_key`` is the CANONICAL producing-session key (e.g.
    ``"dashboard:<slot>"``) that the gateway recorded on the spool — NOT the
    bare frontend slot key used for WS routing. The two differ (the dashboard
    sets ``KIROCREW_SESSION_KEY`` to the prefixed form while ``slot.key`` is
    bare), so the binding check MUST compare against the canonical form or every
    real render is refused as a mismatch.

    ORDER MATTERS: the binding check runs BEFORE the claim (``_take_claim``,
    which a caller reaches only after this returns). A marker echoed into the
    WRONG session must not consume the record's single render, because a replay
    racing ahead of the legitimate slot would burn the claim and permanently
    suppress the real render (a cheap denial). Only a caller that passes the
    binding check may take the claim.

    Runs blocking filesystem work, so call via ``asyncio.to_thread``.
    """
    data = load_spool(spool_id)
    if data is None:
        return None
    # Slot binding: the record names the canonical session that produced it. A
    # marker echoed into a DIFFERENT session (transcript replay, cross-channel
    # paste) must not render the app under mismatched attribution. Empty
    # session_key (producer couldn't attribute) is allowed with a log line;
    # the claim below still gates it.
    record_session = data.get("session_key") or ""
    if record_session and record_session != producing_session_key:
        logger.warning(
            "mcp-apps marker %s bound to session %r arrived in %r; refusing render",
            spool_id,
            record_session,
            producing_session_key,
        )
        return None
    if not record_session:
        logger.info(
            "mcp-apps marker %s has no session binding; rendering in %r",
            spool_id,
            producing_session_key,
        )
    return data


def _take_claim(
    spool_id: str,
    slot_key: str,
    tool_call_id: str,
    row_ts: Sequence[str],
    row_ids: Sequence[str] = (),
) -> str:
    """Atomically take the record's ONE render for this caller.

    Returns ``"took"`` when this call won the claim, ``"spent"`` when a claim
    already existed AND names this same row, ``"inert"`` when it names a
    different row, and ``"error"`` when the create itself failed. The four are
    kept apart because they mean different things to a transcript: ``"spent"``
    PROVES this row's app was claimed, so the caller flags the row from the
    sidecar alone even though it cannot deliver the frame, which is what lets a
    row recover after a crash between the claim and the flag's own persistence.
    ``"inert"`` is the replayed-marker case and must flag nothing. ``"error"``
    proves nothing either way, so nothing is recorded.

    A record renders at most ONCE: markers travel in tool-result text, which
    the LLM (and any server) can echo into later turns and which persists in
    transcripts, so without a consume gate a replayed marker would re-render
    the app wherever the text lands. The claim is a sidecar (``<id>.rendered``)
    next to the record, created by ``os.link`` from a temp that already holds the
    owning row: the link is atomic on POSIX and fails with ``FileExistsError``
    exactly as ``O_CREAT|O_EXCL`` would, so exactly one caller wins even under
    concurrent replays. The record itself stays on disk,
    because the app-call capability path deliberately remains valid for the
    rendered app's lifetime (until the TTL sweep reaps both files).

    Separate from ``_load_bound`` so a caller can finish every CANCELLABLE step
    before taking it. Claiming is irreversible and makes every later replay
    inert, so a cancellation landing after the claim but before the caller
    records the app loses that app with nothing left to say it existed. Taking
    the claim LAST means a cancellation instead leaves the record unclaimed and
    a later replay can still render it.

    OFFLOAD this, never call it on the event loop. ``os.close()`` on a file
    descriptor is named in the ``no-blocking-call-on-event-loop`` rule
    (``AUTOSDE.yaml``), which carries ``blocking: true``: the gateway runs every
    task on one loop, so a syscall that stalls there -- a spool on network or
    FUSE storage is enough -- freezes the user's turn AND the liveness
    heartbeat until the watchdog kills the process and the supervisor respawns
    into the same condition. That the syscalls here are cheap on a local path is
    not a judgement this code gets to make; the rule's own closing line is "when
    in doubt, offload", and a frozen loop outranks the cost below.

    The cost the offload reintroduces, and how the caller pays it: an executor
    thread runs to completion whatever happens to the awaiting coroutine, so a
    cancellation delivered mid-``os.open`` creates the sidecar while the caller
    raises and records nothing. ``handle_tool_result`` closes that by shielding
    the call AND draining it through any further cancellation
    (``_drain_shielded``), because a shield protects the inner future rather than
    the awaiting coroutine; it then calls ``_release_claim`` when it cannot
    record the app after all.

    ``_load_bound`` is offloaded for the ordinary reason instead: it reads and
    parses a record up to ``MAX_SPOOL_BYTES``.
    """
    sentinel = _spool_dir() / f"{spool_id}.rendered"
    # The claim CARRIES the row it belongs to, and carries it atomically. A
    # spent claim then answers "spent by whom", which is what separates the two
    # ways a marker reaches this seam twice: this row being re-processed after a
    # gateway death (recover the flag) versus the model echoing the marker into
    # some LATER tool call's text (stay inert, flag nothing). Without the
    # identity, a replay could only guess, and flagging on the guess would put
    # "an app from this step is not viewable here" on a row that never had one.
    #
    # Written into a temp and LINKED into place, rather than created empty and
    # then written, because ``link`` is atomic and fails with
    # ``FileExistsError`` exactly as ``O_CREAT|O_EXCL`` does. So the claim never
    # exists without its identity, and no crash window can leave one that can
    # neither be attributed nor recovered. The temp carries the ``.tmp`` suffix
    # the spool sweep already ages out, so a hard kill between write and link
    # leaves no permanent residue.
    #
    # ``mkstemp`` rather than a name this code composes, because the temp is
    # PRIVATE SCRATCH and a file already sitting at its name must never be able
    # to fail the claim. A composed name could: ``<id>.claim.<pid>.tmp`` is
    # reproducible, so a hard kill between the open and the unlink leaves a
    # residue that a respawn handed the same pid collides with on
    # ``O_CREAT|O_EXCL``, and every later claim for that record answers
    # ``"error"`` until the 24h sweep -- the app neither rendered nor recorded,
    # which is the silence this seam exists to remove. ``mkstemp`` retries
    # internally on collision, so no pre-existing file is fatal rather than
    # merely unlikely, and it already opens ``O_CREAT|O_EXCL|O_RDWR`` at 0o600.
    # The prefix keeps a leaked temp attributable to its record by name.
    #
    # The owning identity is the ROW MIDS -- not the ``tool_call_id``, and not
    # the timestamps. ``meta.mid`` is minted once per row, so it names exactly
    # ONE row for the life of the slot. A ``ts`` does not: only the rows that go
    # through ``monotonic_transcript_ts`` are spaced by it, and ``_ChatSlot``'s
    # append deliberately preserves an explicit ``ts`` verbatim for a row
    # replayed from a channel transcript, because rewriting it would reorder the
    # replay it came from; a coarse clock also stamps two appends in one tick
    # identically, which is the collision ``meta.mid`` exists to answer. The
    # timestamps are still recorded, as the fallback identity for a claim body
    # that carries no ids. The list is written sorted, but both readers of it
    # compare it as a SET, so no consumer depends on its order. A ``tool_call_id`` carries no
    # such guarantee either: it is the BACKEND's own id, session-local in general, so a
    # session reset that preserves the transcript can hand a genuinely new call
    # an id an older row already used. Attributing on the id would then read a
    # replayed marker landing on that new row as "spent by this row" and put
    # "an app from this step is not viewable here" on a row that never had one,
    # which is precisely the false notice this attribution exists to prevent.
    # The claim body does NOT carry the call id: nothing reads it back, and an
    # unbounded field in a body with a byte limit made a long id push a legitimate
    # claim past that limit. A leaked claim stays traceable through the render frame
    # and this module's log lines, which both name the call.
    #
    # ``rows`` empty is NOT a failure: it means no transcript row carried this
    # call when the claim was taken, and the caller's flag loop is itself gated
    # on finding such a row, so there is nothing a later "spent" could restore.
    # Attribution therefore fails towards silence for it, like every other
    # unprovable case here.
    payload = _claim_payload(slot_key, tool_call_id, row_ts, row_ids)
    try:
        raw_fd, raw_temp = tempfile.mkstemp(
            prefix=f"{spool_id}.claim.", suffix=".tmp", dir=_spool_dir()
        )
    except OSError:
        logger.warning("mcp-apps render claim temp failed for %s", spool_id, exc_info=True)
        return "error"
    fd, temp = raw_fd, Path(raw_temp)
    try:
        _write_all(fd, payload)
    except OSError:
        logger.warning("mcp-apps render claim write failed for %s", spool_id, exc_info=True)
        _unlink_quietly(temp, fd)
        return "error"
    # The DIRECTORY ENTRY is the claim, and ``link`` is the only syscall that
    # can fail to create it. So the outcome of ``close`` must not be reported as
    # "not claimed": that left the sidecar in place with the seam reporting no
    # claim, which makes every later replay inert AND records nothing on the row
    # -- the exact silence this seam exists to remove. A deferred write error
    # surfacing at ``close()`` is a real case on the network or FUSE spool
    # storage the docstring above plans for, not a contrived one.
    #
    # Unlinking the sidecar instead is NOT the remedy here, even though it looks
    # like the tidier one: the caller strips the marker from the transcript on
    # both branches, so a released record has no replay left to render it and
    # reporting no claim would lose the app twice over. The descriptor is the
    # only thing given up, and only on this error path.
    try:
        os.close(fd)
    except OSError:
        logger.warning(
            "mcp-apps render claim close failed for %s; continuing to link",
            spool_id,
            exc_info=True,
        )
    try:
        os.link(temp, sentinel)
    except FileExistsError:
        _unlink_quietly(temp)
        return _attribute_spent_claim(sentinel, spool_id, slot_key, tool_call_id, row_ts, row_ids)
    except OSError:
        # NOT an error: a data home on a filesystem with no hard links (exFAT, a
        # number of FUSE and network mounts -- exactly the storage the docstring
        # above plans for) refuses EVERY link, so reporting "error" here made a
        # standing configuration property into silent loss of every app render.
        # The caller strips the marker on this branch, so nothing is left to
        # replay and nothing is recorded on the row.
        #
        # Falling back on ANY link failure rather than on an errno allowlist,
        # because the fallback cannot answer anything the link path would not:
        # its own O_EXCL yields FileExistsError for an existing claim (the same
        # spent branch), and a filesystem that can create no sentinel at all
        # still reaches "error" below. An errno list can only be incomplete --
        # EPERM, EOPNOTSUPP and ENOSYS are all in use for this by real drivers.
        logger.info(
            "mcp-apps render claim link failed for %s; creating the sentinel directly",
            spool_id,
            exc_info=True,
        )
        _unlink_quietly(temp)
        return _take_claim_without_link(
            sentinel, payload, spool_id, slot_key, tool_call_id, row_ts, row_ids
        )
    _unlink_quietly(temp)
    return "took"


def _take_claim_without_link(
    sentinel: Path,
    payload: bytes,
    spool_id: str,
    slot_key: str,
    tool_call_id: str,
    row_ts: Sequence[str],
    row_ids: Sequence[str] = (),
) -> str:
    """Take the claim on a filesystem that cannot hard-link.

    ``O_CREAT|O_EXCL`` keeps the two properties the link path exists for: the
    create is atomic, so two callers cannot both take one claim, and an existing
    sentinel still raises ``FileExistsError`` so a spent claim is still
    attributed rather than overwritten. ``os.replace`` is NOT the alternative
    here even though it needs no links: it replaces silently, so a second caller
    would take a claim another row already owns and the app would render twice
    with the first row's record destroyed.

    What it gives up is that the sentinel briefly exists with no identity in it,
    where the link path publishes content and entry together. A write that FAILS
    there is recovered: the sentinel is unlinked, restoring the pre-claim state so
    a later replay can still take it. A hard DEATH inside that window is not,
    because the empty sentinel it leaves is byte-for-byte what the shipped code
    leaves on a successful render, so nothing can tell the two apart and
    ``_attribute_spent_claim`` must read both as ``"inert"`` -- see its docstring.
    That costs one app, once, on a kill between two syscalls, against the
    alternative of re-rendering every legacy app on the host.
    """
    try:
        fd = os.open(sentinel, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return _attribute_spent_claim(sentinel, spool_id, slot_key, tool_call_id, row_ts, row_ids)
    except OSError:
        logger.warning("mcp-apps render claim create failed for %s", spool_id, exc_info=True)
        return "error"
    try:
        _write_all(fd, payload)
    except OSError:
        # Undo our own create: an identity-less sentinel we know nobody owns is
        # worse than no sentinel, and unlinking it is the recovery.
        logger.warning("mcp-apps render claim body failed for %s", spool_id, exc_info=True)
        _unlink_quietly(sentinel, fd)
        return "error"
    try:
        os.close(fd)
    except OSError:
        # Same reasoning as the link path: the DIRECTORY ENTRY plus its body is
        # the claim, and both are now on disk, so a deferred error surfacing at
        # close must not be reported as "not claimed".
        logger.warning(
            "mcp-apps render claim close failed for %s; the claim stands",
            spool_id,
            exc_info=True,
        )
    return "took"


#: Row identities ONE claim carries. A claim describes a single app occurrence, whose
#: rows are the tool rows of one call -- two for an auto-approved call, a few more for a
#: call whose frames repeat -- so this sits far above the real shape and exists to make
#: the retention bounded rather than to shape it.
#:
#: Capping the number of claims while letting one claim's set grow leaves the retention
#: unbounded through the nesting, which is the whole of the objection this answers: a
#: bound has to bound every field it retains.
_MAX_CLAIM_ROWS = 64

#: Longest row identity a claim may carry. A ``meta.mid`` is a short minted token and a
#: ``ts`` is an ISO stamp, so anything longer is malformed and is dropped rather than
#: retained. The sidecar is disk content this pass does not author, so an oversized or
#: corrupt body has to cost a bounded amount of memory.
_MAX_ROW_ID_LEN = 128


#: Longest slot key a claim may carry. Unlike the row identities this CANNOT be
#: truncated -- it is what a restore matches on, and a shortened key is a real key
#: belonging to whichever slot's own key is that prefix, so truncating attributes the
#: claim to the WRONG slot rather than merely losing it. A claim whose key is longer is
#: therefore written with the key OMITTED: the body stays under the byte limit below, and
#: a claim with no key matches no slot, so it recovers nothing and can mis-attribute
#: nothing. Only the recovery is given up; the entry itself still bars a second render.
#:
#: Far above any real key: these are ``dashboard:chat-<n>-<n>`` and the channel forms.
_MAX_SLOT_KEY_LEN = 512


#: Bytes one claim's sidecar may occupy. DERIVED from the caps above rather than picked,
#: so it tracks them: two lists of ``_MAX_CLAIM_ROWS`` identities at ``_MAX_ROW_ID_LEN``
#: characters, plus a ``_MAX_SLOT_KEY_LEN`` key and quoting, is the largest body this
#: code can WRITE, and the limit is twice that so a legitimate claim cannot trip it.
#:
#: Every field the body carries has to be in this sum. A field left out of it was the
#: defect this replaces: the key was unbounded while the limit assumed it was small, so
#: a long one pushed a legitimate claim past the limit and the reader refused a claim
#: this code had written -- losing the notice in the crash window the claim exists for.
#:
#: The caps above bound what a read KEEPS; this bounds what it READS. Without it a
#: sidecar came in whole at whatever size it happened to be, so the entire body was in
#: memory before any cap applied.
_MAX_CLAIM_BYTES = 2 * (2 * _MAX_CLAIM_ROWS * (_MAX_ROW_ID_LEN + 4) + _MAX_SLOT_KEY_LEN + 128)


def _read_claim_body(path: str) -> bytes | None:
    """One claim's sidecar body, or ``None`` when it is larger than a claim can be.

    Reads one byte PAST the limit, so an oversized body is detected without being
    read: the read stops there rather than at the file's end. A sidecar is disk
    content this module does not author, so an oversized one is refused rather than
    parsed, and each caller answers it the way it answers any unusable claim.
    """
    with open(path, "rb") as handle:
        raw = handle.read(_MAX_CLAIM_BYTES + 1)
    return None if len(raw) > _MAX_CLAIM_BYTES else raw


def _bounded_row_ids(values: object) -> set[str]:
    """The row identities one claim contributes, bounded in COUNT and in LENGTH.

    Sorted before the cut, so the subset kept is the same on every restore rather than
    depending on the order a JSON list happened to arrive in. Past the cap a row goes
    unflagged, which is the silence this module chooses wherever a row cannot be
    identified, and it is reached only by a claim far larger than one occurrence.
    """
    if not isinstance(values, list):
        return set()
    usable = sorted({v for v in values if isinstance(v, str) and v and len(v) <= _MAX_ROW_ID_LEN})
    return set(usable[:_MAX_CLAIM_ROWS])


def _claim_payload(
    slot_key: str,
    tool_call_id: str,
    row_ts: Sequence[str],
    row_ids: Sequence[str] = (),
) -> bytes:
    """The claim's body: who owns it, written with the entry that publishes it.

    ``row_ids`` is the owning rows' ``meta.mid`` values and is the IDENTITY;
    ``rows`` is their ``ts``, kept as the fallback identity for a body that
    carries no ids; it is written sorted, but every reader compares it as a set.
    A ``ts`` does not name a row: ``_ChatSlot.append``
    preserves an explicit ``ts`` verbatim for a row replayed from a channel
    transcript, so the monotonic stamper is bypassed there, and a coarse clock
    stamps two rows appended in one tick identically -- which is the reason
    ``meta.mid`` is minted at all. Both are recorded so a claim is attributable
    by identity and still orderable.
    """
    # ``tool_call_id`` is deliberately NOT in the body. Nothing reads it back -- a
    # restore matches on the slot key and the row identities, and the lead is keyed on
    # the claim's own index rather than on the call id -- so carrying it added an
    # unbounded field to a bounded body, and a long one pushed a legitimate claim past
    # the byte limit where the reader then refused it.
    if len(slot_key) > _MAX_SLOT_KEY_LEN:
        # A key this long cannot be encoded without pushing the body past the read
        # limit, and it cannot be shortened either, so the attribution is lost whichever
        # way this goes. OMITTING the key is the answer that cannot do anything worse
        # than lose it: a claim with no key matches no slot, because the read compares
        # ``owner.get("slot_key") != slot_key`` and a missing field is never equal to a
        # real key. A TRUNCATED key would instead be a real key belonging to whichever
        # slot's own key is that prefix, which is an attribution to the wrong slot.
        #
        # Failing the call loses far more than the attribution: the error reaches the
        # blanket handler around the claim, which strips the marker and records nothing
        # on the row, so the app is neither rendered nor recoverable. The sidecar is two
        # things -- the render-once entry and the crash-window recovery -- and only the
        # second needs the key, so the entry is still written and the app still renders.
        logger.warning(
            "mcp-apps render claim written without attribution: slot key is %d characters",
            len(slot_key),
        )
    body: dict[str, object] = {}
    if len(slot_key) <= _MAX_SLOT_KEY_LEN:
        body["slot_key"] = slot_key
    # Bounded on the way IN as well, so an oversized claim is never written
    # and a reader is not left to be the only guard.
    body["rows"] = sorted(_bounded_row_ids(list(row_ts)))
    body["row_ids"] = sorted(_bounded_row_ids(list(row_ids)))
    return json.dumps(body).encode()


def _write_all(fd: int, payload: bytes) -> None:
    """Write every byte, because one ``os.write`` may write fewer than asked.

    A short write is not an error and raises nothing, so the single call this
    replaces could leave a TRUNCATED claim whose owner cannot be parsed -- read
    later as "unreadable owner", which is `inert`, so the owning row's app went
    silent with the claim still in place. Pipes and some network filesystems
    short-write on ordinary traffic, not only under failure.
    """
    view = memoryview(payload)
    while view:
        view = view[os.write(fd, view) :]


def _unlink_quietly(path: Path, fd: int | None = None) -> None:
    """Drop a claim temp (and its descriptor) without masking the real outcome.

    Every caller is already on a decided path, so a failure here must not change
    what that path reports: the temp carries the swept ``.tmp`` suffix, so the
    worst case is one file aged out later rather than a claim misreported.
    """
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    try:
        path.unlink()
    except OSError:
        logger.debug("mcp-apps claim temp %s not removed", path.name, exc_info=True)


def _attribute_spent_claim(
    sentinel: Path,
    spool_id: str,
    slot_key: str,
    tool_call_id: str,
    row_ts: Sequence[str],
    row_ids: Sequence[str] = (),
) -> str:
    """Decide what an ALREADY-SPENT claim means for the row now asking.

    ``"spent"`` when the claim names one of the rows now asking, which is the
    crash-recovery case: the sidecar reached disk and the row's own flag did not,
    so reporting it here is what puts the flag back. ``"inert"`` when it names
    different rows or cannot be read, because then this row never had the app and
    a flag would point at nothing. Unreadable therefore fails towards silence on
    THIS row rather than towards a false notice, and the owning row keeps
    whatever it already recorded.

    A ZERO-BYTE sentinel is ``"inert"`` too, and deliberately so. It is exactly
    what the shipped code leaves on every SUCCESSFUL render -- it created the
    sidecar with ``O_CREAT|O_EXCL`` and closed it unwritten -- so after an upgrade
    the spool holds empty sentinels whose apps have already rendered. Emptiness
    therefore cannot distinguish "nobody owns this" from "rendered before the
    format carried an owner", and handing an empty one to the asking row would
    re-render every legacy app on whichever row replayed its marker. Inert is
    also what the shipped code answers a replay, so nothing regresses. What it
    costs is the link-less path's own crash window: a death between that path's
    ``O_CREAT|O_EXCL`` and its body write leaves an empty sentinel this cannot
    tell from a legacy one, so that record stays inert -- one app, once, on a
    hard kill inside two syscalls. The write-failure branch is recovered instead,
    by unlinking a sentinel it could not fill.

    The match is on the ROW MIDS whenever the claim recorded any, which is every
    claim this code writes: ``meta.mid`` is minted once per row, so it names
    exactly one row. Timestamps are the FALLBACK, for a claim written before the
    ids, and only a fallback because a ``ts`` does not name a row -- an explicit
    one is kept verbatim for a replayed channel row, and a coarse clock stamps
    two same-tick appends alike. The two are never mixed: a claim carrying ids is
    matched on ids ALONE, or a foreign row that merely shares a claimed ``ts``
    could answer for a row it is not. ``tool_call_id`` is deliberately NOT part
    of it either: it is the
    backend's own session-local id, so a reset that preserves the transcript can
    reissue an id an older row already used, and matching on it would flag a new
    row that never had an app. An empty intersection -- including the case where
    either side carried no rows at all -- is therefore ``"inert"``.
    """
    try:
        raw = _read_claim_body(str(sentinel))
        if not raw:
            # BOTH unusable bodies land here, and deliberately one branch: the reader
            # answers ``None`` for a body larger than a claim can be, which this test
            # already catches, so a separate branch for it would return "inert" from a
            # line this one reaches anyway.
            # A ZERO-BYTE sentinel is exactly what the SHIPPED code leaves behind
            # on every successful render: it created `<id>.rendered` with
            # ``O_CREAT|O_EXCL`` and closed it without writing anything. So after
            # an upgrade the spool is full of empty sentinels whose apps HAVE
            # rendered, and emptiness therefore cannot mean "nobody owns this".
            # Reading it as an unowned claim and handing it to the asking row
            # would re-render every one of them, on whichever row replayed the
            # marker. Inert is both the safe answer and the same answer the
            # shipped code gives a replay.
            logger.info(
                "mcp-apps claim for %s %s; replay is inert",
                spool_id,
                "is larger than a claim can be" if raw is None else "carries no identity",
            )
            return "inert"
        owner = json.loads(raw.decode())
        claimed_rows = owner.get("rows") if isinstance(owner, dict) else None
        claimed_ids = owner.get("row_ids") if isinstance(owner, dict) else None
        # Identity is the row MID when the claim recorded any, and ONLY then --
        # mixing the two would let a foreign row sharing a claimed ``ts`` answer
        # for a row it is not. A claim with no ids is a legacy one, whose rows are
        # all the ts it has, so it keeps the ts match rather than recovering
        # nothing.
        owned_ids = _bounded_row_ids(claimed_ids)
        if owned_ids:
            overlap = bool({m for m in row_ids if m} & owned_ids)
        else:
            overlap = isinstance(claimed_rows, list) and bool(
                {t for t in row_ts if t} & {r for r in claimed_rows if isinstance(r, str)}
            )
        same_row = isinstance(owner, dict) and owner.get("slot_key") == slot_key and overlap
    except (OSError, ValueError, UnicodeDecodeError):
        logger.info("mcp-apps claim for %s is spent, owner unreadable", spool_id)
        return "inert"
    if same_row:
        logger.info("mcp-apps claim for %s is spent by this row; flag recovered", spool_id)
        return "spent"
    logger.info(
        "mcp-apps marker %s already rendered elsewhere; replay under call %r is inert",
        spool_id,
        tool_call_id,
    )
    return "inert"


def _release_claim(spool_id: str) -> None:
    """Give a claim back, for a caller torn down before it could record the app.

    Only ``handle_tool_result``'s cancellation path calls this, and only for a
    claim IT won. A claim nobody can record is worse than no claim: it makes
    every later replay inert, so the app is gone with nothing left to say it
    existed. Releasing it puts the record back where a later replay renders it.

    Offloaded for the same reason as ``_take_claim`` -- ``unlink`` is a metadata
    syscall on the same possibly-stalled filesystem.

    One residue, bounded to the interval between the claim and this unlink: a
    concurrent replay arriving inside it sees the sidecar and is told the record
    is already rendered, so that replay shows the absence notice for an app that
    becomes renderable again a moment later. The window is two metadata syscalls
    wide and the next replay renders for real, which is why this is the accepted
    side of the trade against a claim that can never be given back.
    """
    try:
        (_spool_dir() / f"{spool_id}.rendered").unlink()
    except OSError:
        logger.warning("mcp-apps claim release failed for %s", spool_id, exc_info=True)


async def _drain_shielded(aw: Any) -> Any:
    """Await ``aw`` to completion even if THIS task is cancelled repeatedly.

    ``asyncio.shield`` protects the inner future, not the awaiting coroutine: a
    second ``.cancel()`` on this task raises ``CancelledError`` out of the
    ``await`` itself, so any cleanup that followed it is skipped. A plain shield
    is therefore not enough to make the claim's outcome reachable -- and a
    double cancellation is ordinary operation here, not a contrived one, since a
    turn deadline and a slot deletion can both fire on the same task.

    The loop cannot spin: each iteration either suspends on a pending inner
    future or, once that future is done, returns without a suspension point --
    so an iteration is only ever driven by a further ``.cancel()``, and the
    count of those is bounded by the callers that issue them.

    The caller re-raises the cancellation itself; absorbing it here would
    silently un-cancel the turn.
    """
    inner = asyncio.ensure_future(aw)
    while True:
        try:
            return await asyncio.shield(inner)
        except asyncio.CancelledError:
            if inner.done():
                # Cancellation and completion raced. The worker's outcome is
                # already here, so read it rather than re-entering the await.
                return inner.result()


#: Claims one restore keeps, newest first. The spool is swept at 24h, so a slot
#: that renders apps steadily can leave far more sidecars than a restored
#: transcript could ever match, and retaining one group per claim made the
#: retention grow with spool age rather than with the work in front of it.
#:
#: Newest-first is what makes a cap safe here. The loss this recovery exists to
#: repair is the one that just happened, so the most recent claims are exactly
#: the ones a restored row can still match. Past the cap the OLDEST claims go
#: unrecovered, costing one notice on a row that old -- the same silence this
#: module already chooses wherever a row cannot be identified, rather than a new
#: kind of compromise.
#:
#: What this does NOT bound: a slot whose claims are outnumbered by other slots'
#: sidecars still examines them to find its own, so the scan stays proportional
#: to the spool. Bounding that needs the slot in the sidecar NAME, which the
#: gateway pairs and sweeps by (`mcp_gateway/apps.py`), so it is deliberately
#: not done here.
_MAX_RESTORE_CLAIMS = 512


def load_claimed_row_groups(slot_key: str) -> list[set[str]]:
    """Put back app flags whose claim reached disk but whose row flag did not.

    The claim and the row's ``mcp_app`` flag land in different places at
    different times: ``_take_claim`` publishes the sidecar immediately, while the
    flag rides the slot's next save. A gateway death between those two writes is
    an ordinary crash, not an extreme one, and it leaves a SPENT claim with an
    unflagged row -- so the app is gone and nothing on the row says it existed,
    which is the exact silence this module was added to remove.

    Nothing recovered it before this pass. The in-turn recovery lives in
    ``handle_tool_result``'s ``"spent"`` branch, and that function has exactly one
    caller, the live turn path: a restart never re-runs it, and the marker the
    caller strips is not on disk either, so there is no replay left to detect.
    Recovery therefore has to come from the sidecar, which is why the claim
    carries ``{slot_key, rows}`` -- this reads back what that write recorded.

    Restore-time and read-only towards the claim: it sets the row's flag and
    never unlinks or rewrites a sidecar, so running it twice is a no-op and a
    crash inside it leaves the same state it started from. Rows already flagged
    are left alone, so a row that kept its flag keeps its lead marker too.

    Ask with the key the CLAIM was written under, which is the canonical
    producing-session key (``_take_claim`` receives ``binding_key``, not the bare
    slot key every other chat event routes on). A bare key here matches nothing
    and recovers nothing, silently -- the same bare-vs-canonical trap the render
    call guards against, one layer down on the read side.

    Returns ONE GROUP of row ``ts`` per matching claim, not a flat set. Each claim
    is one app occurrence, and the grouping is the only record of which rows belong
    to which occurrence -- a ``tool_call_id`` cannot stand in for it, because a
    transcript-preserving reset can hand a genuinely new call an id an older row
    already used. Flattening would let one occurrence's lead marker suppress
    another's, which is the defect the live path's ``mcp_app_lead`` exists to
    prevent, reached through the restore instead.

    This half does the FILESYSTEM work -- a spool scan plus one read per sidecar --
    so it must NOT run on the event loop: hand it to a worker thread, in the
    prefetch phase the restore paths already use for their disk reads. The
    in-memory half is :func:`apply_claimed_rows`.
    """
    if not slot_key:
        return []

    def _modified_at(entry: os.DirEntry[str]) -> float:
        # A sidecar that vanished mid-scan sorts oldest instead of raising: a claim
        # this pass cannot stat is one it simply does not see, and a restore must
        # not fail over a best-effort pass.
        try:
            return entry.stat().st_mtime
        except OSError:
            return 0.0

    # A BOUNDED min-heap of the newest matching claims, filled while STREAMING the
    # directory. Materialising the listing and sorting it is what a cap cannot fix:
    # that retains one entry per sidecar in the spool however low the cap is set, so
    # the cap bounds the groups and nothing else. The heap never holds more than the
    # cap -- each claim past it displaces the oldest one held -- so this pass keeps
    # the newest claims and retains nothing besides them.
    #
    # `counter` breaks mtime ties so the payload sets are never compared -- a set has
    # no ordering, and two sidecars written inside one coarse clock tick do collide.
    heap: list[tuple[float, int, set[str]]] = []
    counter = 0

    def _keep(owned: set[str], when: float) -> None:
        nonlocal counter
        counter += 1
        item = (when, counter, owned)
        if len(heap) < _MAX_RESTORE_CLAIMS:
            heapq.heappush(heap, item)
        else:
            # Displaces the OLDEST held claim, never the incoming one blindly: the
            # loss this pass repairs is the most recent, so an older claim is the
            # one worth dropping.
            heapq.heappushpop(heap, item)

    try:
        spool = _spool_dir()
        entries = os.scandir(spool)
    except OSError:
        # No spool, or an unreadable one: there is nothing to recover FROM, and a
        # restore must not fail over a best-effort pass.
        logger.debug("mcp-apps claim reconcile skipped, spool unreadable", exc_info=True)
        return []

    for entry in entries:
        if not entry.name.endswith(".rendered"):
            continue
        try:
            raw = _read_claim_body(entry.path)
            if raw is None:
                logger.debug(
                    "mcp-apps claim ignored, sidecar larger than a claim can be: %s",
                    entry.name,
                )
                continue
            owner = json.loads(raw.decode())
        except (OSError, ValueError, UnicodeDecodeError):
            # This is where a LEGACY claim lands, and it is the common case after
            # an upgrade: the shipped code created the sidecar with
            # ``O_CREAT|O_EXCL`` and closed it unwritten, so every already-rendered
            # app leaves a zero-byte file whose empty body is not JSON. Such a
            # claim names no slot and no row, so it cannot say which row to flag,
            # and skipping it is why this pass cannot scatter flags across a
            # restored transcript. An explicit emptiness check here would be dead
            # code -- an empty body reaches this branch on its own.
            continue
        if not isinstance(owner, dict) or owner.get("slot_key") != slot_key:
            continue
        # One group per claim, holding its row MIDS when it recorded any and its
        # timestamps otherwise. Never both: a group mixing the two would let a
        # foreign row that merely shares a claimed ``ts`` be flagged as this
        # claim's, which is the corruption the ids exist to prevent. A claim with
        # no ids predates them, so ts is the only identity it has.
        owned = _bounded_row_ids(owner.get("row_ids"))
        if owned:
            _keep(owned, _modified_at(entry))
            continue
        claimed = _bounded_row_ids(owner.get("rows"))
        if claimed:
            _keep(claimed, _modified_at(entry))

    # The iterator holds a directory handle, so it is closed here rather than left to
    # the collector. Closing after the loop is enough: every read inside it is already
    # wrapped, so the loop does not raise past this point.
    entries.close()

    # Newest first, which is the order the lead assignment in ``apply_claimed_rows``
    # walks. The heap is at most the cap, so this sort is bounded by it, not by the
    # spool.
    return [owned for _when, _tiebreak, owned in sorted(heap, reverse=True)]


def apply_claimed_rows(groups: list[set[str]], rows: list[dict[str, Any]]) -> int:
    """Flag the restored *rows* that :func:`load_claimed_row_groups` found claims for.

    Pure memory -- it touches no file and never writes the sidecar back -- so it
    is safe on the event loop, which is where the restore paths do their apply
    phase, and running it twice changes nothing after the first pass.

    Rows already flagged are left alone, so a row whose flag survived keeps the
    lead marker exactly as the turn wrote it. Returns the number of rows newly
    flagged: a NON-ZERO count means the restore has in-memory state that is not
    on disk, and the caller must leave the slot dirty or the recovery is lost at
    the sidecar's TTL.

    One lead per CLAIM, keyed on the claim's index rather than on the row's
    ``tool_call_id``. Keying on the call id was wrong and review caught it: a
    transcript-preserving reset can hand a genuinely new call an id an older row
    already used, so an older flagged row would claim the id and the newer
    occurrence recovered beside it would get no lead -- its notice never renders,
    and two lost apps read as one. That is the same mistake the client's
    superseded rules made, reached through the restore instead, which is why the
    claim's grouping is carried this far rather than flattened on the way in.
    """
    if not rows:
        # An empty `groups` needs no guard: the membership test below skips every
        # row on its own, so a check here would be one that changes nothing.
        return 0

    # First-wins applies to an UNAMBIGUOUS identity only, which is the whole of what
    # this map is for. A `meta.mid` names exactly one row. A `ts` does not: a coarse
    # clock stamps two rows appended in one tick alike, which the collision check
    # below is what handles -- a ts shared by two restored rows flags NEITHER. So the
    # first-wins here settles which CLAIM owns an identity, not which row a shared
    # timestamp meant, and `_run_chat` excluding every pre-turn row from the claim it
    # writes is why two live claims cannot contend for one row in the first place.
    group_of: dict[str, int] = {}
    for index, group in enumerate(groups):
        for ts in group:
            group_of.setdefault(ts, index)

    # A ``ts`` shared by two restored rows identifies NEITHER, so a claim that
    # carries only timestamps must flag neither rather than guess. Silence on a
    # row is this module's failure direction everywhere else, and a wrong flag
    # here is durable -- it is never written back ``False`` -- so it would leave
    # "an app from this step is not viewable here" on a row that never had one.
    ts_seen: dict[str, int] = {}
    for row in rows:
        key = str(row.get("ts") or "")
        ts_seen[key] = ts_seen.get(key, 0) + 1

    flagged = 0
    lead_taken: set[int] = set()
    for row in rows:
        meta = row.get("meta")
        if not isinstance(meta, dict):
            continue
        # The row's own ``meta.mid`` first: that is what a claim records as its
        # identity, and it is minted per row, so it survives both a clock that
        # does not tick between two appends and a replayed channel row whose
        # ``ts`` came in verbatim. The ts is consulted only for a claim that
        # predates the ids. A group holds one kind or the other, never both, so
        # neither key can match the other kind's group by accident.
        mid = meta.get("mid")
        claim = group_of.get(mid) if isinstance(mid, str) and mid else None
        if claim is None:
            ts = str(row.get("ts") or "")
            claim = group_of.get(ts) if ts and ts_seen.get(ts) == 1 else None
        if claim is None:
            continue
        if meta.get("mcp_app") is True:
            # Already recorded, so this claim's flag survived; leave its lead
            # marker exactly as the turn wrote it, and let no recovered sibling of
            # the SAME claim take a second lead. Reachable from a sidecar naming a
            # mix of rows, which is disk content this pass does not author.
            lead_taken.add(claim)
            continue
        # ONE update, for the reason the live path uses one: a flush serializing this
        # live dict between two assignments would store `mcp_app` with no
        # `mcp_app_lead`, and THIS pass is what would otherwise repair that -- it
        # reads a surviving `mcp_app` above as proof the lead survived, so it would
        # mark the lead taken and skip the row for good.
        recovered: dict[str, Any] = {"mcp_app": True}
        # `rows` is in transcript order, so the first recovered row of a claim is
        # its earliest, matching what the live path would have written.
        if claim not in lead_taken:
            recovered["mcp_app_lead"] = True
            lead_taken.add(claim)
        meta.update(recovered)
        flagged += 1
    if flagged:
        logger.info("mcp-apps recovered %d app flag(s) from spent claims", flagged)
    return flagged


async def handle_tool_result(
    state: Any,
    *,
    slot_key: str,
    tool_call_id: str,
    text: str,
    producing_session_key: str | None = None,
    row_ts: Sequence[str] = (),
    row_ids: Sequence[str] = (),
    persist_rows: Callable[[], Awaitable[Any]] | None = None,
) -> ToolResultRender:
    """Interception seam for the ``EVENT_TOOL_RESULT`` handler.

    ``slot_key`` is the bare frontend slot key used for WS routing (every other
    chat event routes on it). ``producing_session_key`` is the CANONICAL session
    key the gateway recorded on the spool (``"dashboard:<slot>"``); it is used
    ONLY for the binding check and defaults to ``slot_key`` for callers that
    already pass a canonical key. Keeping the two separate fixes the silent
    render-suppression bug where the bare slot key never matched the prefixed
    recorded session (so no app ever rendered on a real dashboard).

    ``row_ts`` is the timestamps of the transcript rows this call already owns,
    and ``row_ids`` their ``meta.mid`` values. The ids are the IDENTITY the claim
    is attributed by; the timestamps are kept as the fallback identity for a
    claim that carries no ids, and every reader compares them as a set. A ``ts`` cannot
    identify a row on its own -- a
    replayed channel row keeps its own verbatim, and a coarse clock stamps two
    same-tick appends alike.
    The IDS are what a spent claim is attributed against; the timestamps are
    recorded beside them as the fallback identity for a claim that carries no
    ids, compared as a set. Neither is a ``tool_call_id``, which is the backend's own session-local
    id and can be reissued to a later call. Passing neither is safe and means no
    spent claim can be attributed to this call, which is the same silence a caller
    with no such row would render anyway.

    ``persist_rows`` is awaited once, immediately before the claim is taken, and
    only when the claim will name a row. The claim is durable as soon as it is
    taken, so the rows it names must reach disk FIRST, or a hard exit between the
    two leaves a sidecar pointing at rows no recovery can find. It is best-effort:
    a caller whose persist fails is expected to log and leave the rows owed to its
    periodic flush, which leaves the pre-existing window rather than widening it.
    Omitting it keeps the old ordering, so a caller that persists no rows at all
    loses nothing it had.

    If *text* carries a render marker, load the spooled payload and broadcast an
    ``mcp_app_render`` websocket event scoped to ``slot_key``, then return *text*
    with the marker stripped. When there is no marker — the overwhelming common
    case — return *text* unchanged after a single cheap check.

    Returns a ``ToolResultRender``: the transcript text, and whether the record's
    one render is spent for this row -- by THIS call when it took the claim, and
    by an EARLIER one when the claim is reported ``"spent"`` and this call is
    recovering the flag rather than delivering a frame. See that type for what the
    second field does and does not assert, and why a caller cannot derive it.

    The spool read is offloaded via ``asyncio.to_thread``. Never raises for a
    failure of its own: every error path logs and returns the stripped text. It
    DOES propagate ``asyncio.CancelledError``, which is a ``BaseException`` and
    is not a failure of this seam but the caller's turn being torn down. The one
    thing that propagation must not do is leave a claimed record behind, so the
    claim is shielded and given back on that path.
    """
    spool_id = find_marker(text)
    if not spool_id:
        return ToolResultRender(text, False)
    binding_key = producing_session_key if producing_session_key is not None else slot_key
    # Set on the CLAIM, which is the irreversible half: past that point the
    # record's one render belongs to this call and no later frame can use it.
    # The already-spent case returns earlier, from its own branch, because it
    # reports the same flag without this call having sent anything.
    app_on_row = False
    try:
        data = await asyncio.to_thread(_load_bound, spool_id, binding_key)
        if data is not None:
            # OWNER-scoped delivery: the frame carries the ``callback_secret``
            # — the capability that authorizes app→gateway callbacks —
            # so a non-owner/guest WebSocket must never receive it. Falls back
            # to broadcast_ws only if the state predates the owner channel
            # (tests with minimal fakes).
            send = getattr(state, "broadcast_ws_owners", None) or state.broadcast_ws
            # Offload redaction: the passes recurse over payloads that can be
            # multi-MB, and this seam runs on the dashboard event loop.
            #
            # BEFORE the claim, deliberately. This await is cancellable, and
            # `CancelledError` is a BaseException that the `except Exception`
            # below does not catch, so a turn cancelled here returns nothing to
            # the caller at all. With the claim already taken, that would leave
            # an app no replay can ever render and no row recording that it
            # existed, which is the exact silence this seam's caller exists to
            # remove. Redacting first costs one wasted pass in the rare case a
            # concurrent replay wins the claim, and buys a cancellation window
            # in which the record is still claimable.
            red_structured, red_input, red_result = await asyncio.to_thread(
                lambda: (
                    _redact_leaves(data.get("structured_content")),
                    _redact_leaves(data.get("tool_input")),
                    _redact_leaves(data.get("result_content")),
                )
            )
            # BEFORE the claim as well, and for the same reason one step further
            # out: the claim is DURABLE the moment `_take_claim` returns, while
            # the rows it names live in memory until a flush writes them. A hard
            # exit in between leaves a sidecar naming rows that never reached
            # disk -- and because the rows are gone, neither recovery path can
            # reach them: the `"spent"` branch below needs the row to replay, and
            # the restore-time pass matches claims against rows the transcript
            # actually has. The app was rendered and nothing records that it
            # existed. Persisting first makes the durable claim strictly NEWER
            # than the durable rows, so every crash after the claim leaves a row
            # for that recovery to flag.
            #
            # Skipped when the claim names no row: a claim carrying neither ids
            # nor timestamps attributes nothing, so no row has to be durable for
            # it, and the collapsed-id path pays nothing.
            #
            # BEST-EFFORT BY CONTRACT, and the direction matters: the caller's
            # persist logs its own failure and leaves the rows owed to the
            # periodic flush, so a failed persist leaves the pre-existing window
            # rather than widening it. Refusing the claim instead would trade a
            # rare crash for a common silent no-render under benign lock
            # contention, which is the silence this seam exists to remove. This
            # await is also cancellable while the record is still claimable,
            # which is why it belongs here rather than after the claim.
            if persist_rows is not None and (row_ids or row_ts):
                await persist_rows()
            # OFFLOADED, and DRAINED so the offload cannot lose the app.
            # `os.close` on the loop is forbidden by the `blocking: true`
            # `no-blocking-call-on-event-loop` rule; see `_take_claim`. The bare
            # offload would reopen the loss window one step narrower -- the
            # executor thread creating the sidecar after the awaiting coroutine
            # has already raised -- so the claim is shielded to keep the worker's
            # outcome knowable, and the cancellation path drains that outcome
            # through any FURTHER cancellation before giving the claim back.
            claim = asyncio.ensure_future(
                asyncio.to_thread(
                    _take_claim,
                    spool_id,
                    binding_key,
                    tool_call_id,
                    tuple(row_ts),
                    tuple(row_ids),
                )
            )
            try:
                outcome = await asyncio.shield(claim)
            except asyncio.CancelledError:
                # A shield keeps the WORKER alive; it does not keep THIS
                # coroutine from being cancelled again at the awaits below. A
                # second cancellation landing here would leave the sidecar taken
                # and the release skipped -- an app no replay can render, which
                # is the exact silence this seam exists to remove. So both awaits
                # DRAIN through repeated cancellation, and the original
                # cancellation propagates afterwards.
                try:
                    # Only a claim THIS call won may be given back. "spent"
                    # belongs to whoever took it, and releasing it would hand
                    # that caller's record to a replay it already owns.
                    if await _drain_shielded(claim) == "took":
                        await _drain_shielded(asyncio.to_thread(_release_claim, spool_id))
                except Exception:
                    logger.warning("mcp-apps claim release skipped for %s", spool_id, exc_info=True)
                raise
            if outcome != "took":
                # "spent" means the claim names THIS row, which is proof its app
                # was claimed, so the row is flagged from the sidecar alone even
                # though this call sends no frame. That is what makes the durable
                # sidecar sufficient by itself: a gateway death between the claim
                # and the flag reaching disk leaves the claim spent with no flag,
                # and this branch recovers the row on the next replay instead of
                # leaving an unrenderable app with nothing saying it existed.
                # "inert" is a marker echoed into a DIFFERENT call's text and
                # flags nothing; "error" proves nothing and records nothing.
                return ToolResultRender(strip_marker(text), outcome == "spent")
            app_on_row = True
            send(
                "mcp_app_render",
                {
                    "session_key": slot_key,
                    "tool_call_id": tool_call_id,
                    "server": data.get("server", ""),
                    "tool": data.get("tool", ""),
                    "html": data.get("html", ""),
                    "csp": data.get("csp", ""),
                    "permissions": data.get("permissions", []),
                    "spool_id": spool_id,
                    # Callback capability — owner-WS ONLY. The iframe
                    # replays this on every callback; the model-visible marker
                    # (spool_id) authorizes nothing without it.
                    "callback_secret": data.get("callback_secret", ""),
                    # App-bound tool data — credential/exfil-URL redacted before
                    # it crosses into the server-authored iframe (the iframe can
                    # reach its declared CSP origins).
                    "structured_content": red_structured,
                    "tool_input": red_input,
                    "result_content": red_result,
                },
            )
        else:
            logger.info("mcp-apps marker %s had no loadable spool payload", spool_id)
    except Exception:
        logger.warning("mcp-apps render broadcast failed for %s", spool_id, exc_info=True)
    return ToolResultRender(strip_marker(text), app_on_row)
