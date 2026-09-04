"""Shared diagnostics / support-bundle collector.

Single code path behind both surfaces:

  * CLI  — ``kirocrew doctor --bundle``
  * UI   — Settings › About › "Report a Problem" (POST /api/diagnostics/collect)

The collector gathers the logs and crash reports needed to debug a user-reported
failure (the classic "process exited (rc=None)" ACP crash and friends), scrubs
every text member of secrets, zips them, and returns a :class:`BundleResult`
that carries a pre-filled GitHub issue URL the caller can open.

SECURITY: every text member is passed through the shared redaction pipeline
(``redact_exfiltration_urls`` then ``redact_credentials`` — same order used
everywhere else in the codebase) plus a small set of extra rules for the
patterns those two miss (``Bearer`` / ``Authorization`` headers, ``mc_token``
auth cookies, OAuth ``*_token`` JSON fields). Nothing is written to the zip
before it has been scrubbed. Sources that do not exist are skipped, never fatal.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import re
import stat as stat_module
import subprocess
import sys
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode

from kiro_crew import __version__, platform_compat, release_channel
from kiro_crew.config.loader import config_dir
from kiro_crew.security import (
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)
from kiro_crew.validation import strip_hidden_unicode

logger = logging.getLogger(__name__)

# ── GitHub issue target ──────────────────────────────────────────────────────
_ISSUE_REPO = "kirodotdev/KiroCrew"
_ISSUE_NEW_URL = f"https://github.com/{_ISSUE_REPO}/issues/new"

# Cap how many rolling files we pull so a huge crash-dump backlog can't bloat
# the bundle (or, worse, the redaction pass).
_MAX_CRASH_DUMPS = 5
_MAX_IPS_REPORTS = 5
# Keep only the newest N bundles in the output dir so a repeatedly-clicked
# "Report a Problem" can't grow the diagnostics dir unbounded.
_MAX_KEPT_BUNDLES = 10
# Per-member byte cap for text sources — tail the last N bytes of a giant log
# rather than shipping (and scrubbing) hundreds of MB.
_MAX_MEMBER_BYTES = 4 * 1024 * 1024


# ── Extra redaction rules ────────────────────────────────────────────────────
# ``redact_credentials`` / ``redact_exfiltration_urls`` do NOT cover live bearer
# tokens, Authorization headers, or the dashboard's ``mc_token`` auth cookie —
# all three appear verbatim in gateway.log / kiro-chat.log. Cover them here.
_EXTRA_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Sensitive HEADERS: redact the ENTIRE value to end-of-line. A header's
    # whole value is sensitive, so this deliberately over-redacts (safe) rather
    # than parsing it — that kills every delimiter/scheme edge case (comma-
    # delimited creds, `Basic <base64>`, multiple cookies on one Set-Cookie
    # line, etc.).
    # Deliberately NOT anchored to start-of-line: log lines and user notes
    # routinely embed a header mid-line ("request used Authorization: Basic
    # dXNlcjpwYXNz"), and an `^`-anchored rule passed those through verbatim.
    # `.` never matches a newline, so the match still stops at end-of-line.
    # The optional quotes matter for SERIALIZED headers: a JSON/dict dump writes
    # `"authorization": "Basic <b64>"`, where the `"` between the name and the
    # `:` defeated an unquoted `name[ \t]*[:=]` pattern and let the credential
    # through. Same `["']?` idiom as the OAuth-token rule below.
    (
        re.compile(
            r"(?i)([\"']?(?:set-cookie|cookie|authorization|proxy-authorization|"
            r"www-authenticate|x-api-key|x-amz-security-token)[\"']?[ \t]*[:=]"
            r"[ \t]*).+"
        ),
        r"\1[REDACTED]",
    ),
    # Bare `Bearer <tok>` appearing OUTSIDE a header line (e.g. mid-prose, JSON).
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"), "Bearer [REDACTED]"),
    # Dashboard session/refresh cookies appearing outside a Cookie header line
    # (e.g. in a URL or JSON body): mc_token / mc_refresh (+ _<port>).
    (
        re.compile(r"(mc_(?:token|refresh)(?:_\d+)?\s*=\s*)[^\s;,\"']+"),
        r"\1[REDACTED]",
    ),
    # OAuth token fields in JSON / kv form: access_token / refresh_token / id_token
    (
        re.compile(
            r"(?i)([\"']?(?:access|refresh|id|session|api)[_-]?token[\"']?\s*[:=]\s*[\"']?)"
            r"[A-Za-z0-9._~+/=\-]{6,}"
        ),
        r"\1[REDACTED]",
    ),
)


@dataclass
class BundleResult:
    """Outcome of a :func:`collect_bundle` run."""

    zip_path: Path
    filename: str
    included: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: member name -> number of redactions applied (0 = clean)
    redaction_summary: dict[str, int] = field(default_factory=dict)
    github_issue_url: str = ""

    @property
    def total_redactions(self) -> int:
        return sum(self.redaction_summary.values())

    def as_dict(self) -> dict:
        return {
            "zip_path": str(self.zip_path),
            "filename": self.filename,
            "included": self.included,
            "skipped": self.skipped,
            "redaction_summary": self.redaction_summary,
            "total_redactions": self.total_redactions,
            "github_issue_url": self.github_issue_url,
        }


def _scrub(text: str) -> tuple[str, int]:
    """Run the full redaction pipeline over ``text``; return (clean, count).

    Hidden characters go FIRST, and the order is the whole point.
    ``redact_credentials`` matches on the literal shape of a secret, so an
    invisible planted inside one defeats it: ``AKIA<ZWSP>IOSFODNN7EXAMPLE``
    matches no pattern. Stripping afterwards is worse than not stripping at all,
    because the strip REJOINS the secret into a usable credential that redaction
    has already been asked about and declined. ``strip_hidden_unicode`` documents
    this as its own contract -- it is written to run before
    ``redact_credentials`` -- and every consumer of this text sanitizes later:
    ``validation.build_tool_response`` puts an MCP tool response through
    ``sanitize_response``, and a bundle member is normalized by whatever reads the
    zip. So the strip has to happen here, ahead of the patterns, for the
    redaction verdict to mean anything.
    """
    text = strip_hidden_unicode(text)
    count = 0
    text, warnings = redact_exfiltration_urls(text)
    count += len(warnings)
    text, warnings = redact_credentials(text)
    count += len(warnings)
    for pattern, repl in _EXTRA_REDACTIONS:
        text, n = pattern.subn(repl, text)
        count += n
    return text, count


def _kiro_log_dirs() -> list[Path]:
    """Candidate ``kiro-log`` parents, most explicit first.

    kiro-cli does not put this log in the same place on every platform: on Linux
    it follows ``XDG_RUNTIME_DIR`` when the session has one, while on macOS it
    lands under ``TMPDIR``. Probing only ``TMPDIR`` silently omitted the primary
    chat log on Linux — the single most useful member of the bundle for the
    `process exited (rc=None)` class this collector exists for. Rather than
    guessing which one is authoritative per platform, try every candidate and
    take the first that actually exists; ``KIRO_CHAT_LOG_FILE`` overrides all of
    them for anyone running a non-default layout.
    """
    out: list[Path] = []
    for env in ("XDG_RUNTIME_DIR", "TMPDIR"):
        value = os.environ.get(env)
        if value:
            out.append(Path(value) / "kiro-log")
    out.append(Path("/tmp") / "kiro-log")
    # Preserve order while dropping duplicates (TMPDIR is often /tmp), and drop
    # symlinked candidates: a symlinked `kiro-log` would resolve to off-tree
    # files that are not themselves symlinks, so the per-file guard would pass
    # them through (same class as the crash-dump directory).
    unique: list[Path] = []
    for candidate in out:
        if candidate not in unique and _usable_dir(candidate):
            unique.append(candidate)
    return unique


def _usable_log(p: Path) -> bool:
    return p.is_file() and not p.is_symlink()


def _usable_dir(p: Path) -> bool:
    """A directory safe to ENUMERATE.

    Rejecting only symlinked *files* is not enough: ``Path.is_dir()`` follows
    links, so a symlinked ``crash-dumps`` (or DiagnosticReports) directory is
    walked through to its target, and the real files found there are not
    themselves symlinks — they sail past the per-file guard and get packaged.
    The guard therefore has to sit one level up, at every directory this module
    globs.

    ``is_link_or_junction``, not ``Path.is_symlink()``: a Windows junction is a
    reparse point ``is_symlink`` does not report, and it is the only directory
    link an unprivileged Windows user can create (a symlink needs
    ``SeCreateSymbolicLinkPrivilege``). A symlink-only guard is therefore open
    on exactly the platform where planting one is easiest, and what leaks
    through it is a bundle the user then attaches to a public issue.
    """
    return p.is_dir() and not platform_compat.is_link_or_junction(p)


def _kiro_cli_chat_log() -> Path | None:
    """Locate the kiro-cli chat log (``<runtime-or-tmp>/kiro-log/kiro-chat.log``)."""
    override = os.environ.get("KIRO_CHAT_LOG_FILE")
    if override:
        p = Path(override)
        # The override names an arbitrary path, so it is the one input here that
        # could aim the collector at a credential store — `KIRO_CHAT_LOG_FILE=
        # ~/.netrc` would copy plaintext logins into a bundle destined for a
        # public issue. Redaction is not a backstop for that: .netrc/.pem bodies
        # do not match the credential patterns. Refuse the path outright.
        if is_sensitive_path(str(p)):
            logger.warning("ignoring KIRO_CHAT_LOG_FILE: %s resolves to a sensitive path", p)
            return None
        return p if _usable_log(p) else None
    for base in _kiro_log_dirs():
        p = base / "kiro-chat.log"
        if _usable_log(p):
            return p
    return None


def _kiro_cli_extra_logs() -> list[Path]:
    out: list[Path] = []
    for base in _kiro_log_dirs():
        for name in ("mcp.log", "lsp.log"):
            p = base / name
            if _usable_log(p) and p.stat().st_size > 0:
                out.append(p)
        if out:
            # One kiro-log dir wins; do not mix members from two of them, or the
            # zip would carry two different `mcp.log` entries under one name.
            break
    return out


# ── Live log reader (kiro_cli_logs MCP tool) ─────────────────────────────────
# A dedicated read-only view of kiro-cli's OWN protocol logs, so the agent
# driving kiro-cli has first-hand diagnostics when the backend rejects a turn.
# The sources sit OUTSIDE the eight fenced identity stores
# (``identity_stores.IDENTITY_STORE_ROOTS`` -> ``kiro-log/{mcp,lsp}.log`` under
# XDG_RUNTIME_DIR|TMPDIR).
#
# SCOPE IS THE SECURITY PROPERTY, not just the redaction. Two sources kiro-cli
# writes are deliberately NOT read, for the same reason:
#
# * ``sessions/cli/<sid>.jsonl`` transcripts, and
# * ``kiro-chat.log`` — one FIXED host path (see :func:`_kiro_cli_chat_log`),
#   not a per-session file, so on a host running one gateway with many
#   concurrent sessions (web, CLI, messaging, subagents, cron) it interleaves
#   every session's request/response traffic.
#
# Both carry CONVERSATION PROSE across the session boundary, and ``_scrub`` is a
# CREDENTIAL pass: it strips tokens/headers/cookies, not prose. There is no
# per-session kiro-cli chat log to scope to and no reliable per-session
# delimiter in the flat one, so an agent-callable read of it would hand session
# A's private conversation to session B with nothing to narrow it — and a
# disclosure into a model context cannot be recalled. The bundle collector still
# includes the chat log because that path is USER->USER (the user triggers the
# collection and downloads it for their own machine); this tool is CROSS-SESSION
# and therefore stays on the mcp/lsp protocol logs, which explain a rejected
# turn without carrying the conversation.
#
# This does NOT open a path carve-out on the fence: the output still flows
# through the exact ``_scrub`` stack the bundle uses
# (``redact_exfiltration_urls`` then ``redact_credentials`` then
# ``_EXTRA_REDACTIONS``), so redaction closes the credential gap while the
# narrow source list closes the conversation-content one. Two controls, because
# redaction alone cannot reach the second: it is a per-pattern pass, and
# conversation prose matches no pattern. The source list is therefore the
# primary control here and the redaction is the secondary one.
#
# THE PROPERTY THIS SCOPE DEPENDS ON, and how it is held. mcp.log / lsp.log are
# safe to read here only while they record protocol TRAFFIC (method names, ids,
# timings, errors) and not full frame BODIES. They share ``kiro-chat.log``'s
# single-fixed-path, all-sessions-interleaved shape, so payload content is the
# ONLY thing separating them, and an MCP ``tools/call`` frame carries
# conversation-derived arguments (search queries, message bodies, file contents).
#
# Measured on kiro-cli 2.21.1: mcp.log is empty (0 bytes) across a session making
# continuous MCP tool calls, and every lsp.log record is a single-line
# ``<timestamp> ERROR <module>: <message>`` with no JSON-RPC envelope and a
# longest line of 313 bytes. Sentinel strings passed as tool-call ARGUMENTS in
# that same session appear in neither file, so this binary logs no frame payloads.
#
# That is a measurement of one version, not a guarantee about a component this
# repo neither builds nor pins, so a later kiro-cli could start logging bodies.
# :func:`_looks_like_protocol_frames` is the tripwire for exactly that: a source
# whose text carries serialized frames is REFUSED whole and visibly, so the drift
# surfaces as a refusal rather than as a silent widening. Refusing the whole
# source is deliberate -- it decides only whether a file is the KIND the tool
# assumes, and never claims to separate one session's frames from another's.

#: Hard ceiling on the returned text PER SOURCE, independent of ``tail``. A log
#: tail is precisely the shape that deadlocks a pipe, so the bound is by BYTES;
#: ``tail`` (lines) narrows within it but can never raise it.
_MAX_LOG_READ_BYTES = 64 * 1024
#: Ceiling on the WHOLE assembled response, and the reason the per-source cap
#: above is divided among the sources rather than applied to each in full.
#:
#: The MCP transport has its own ceiling: every tool response leaves through
#: ``validation.build_tool_response``, which calls ``sanitize_response`` -- and
#: that truncation drops the TAIL (``text[:max_len]``). For a LOG TAIL that is
#: the worst possible end to lose: the newest lines are the entire reason the
#: tool was called, and its ``[response truncated]`` marker reads like the tool
#: cut the OLDEST content, which is the normal convention for a tail. So the
#: agent would be misled about which end it lost, not merely short-changed.
#:
#: This reader therefore does its own trimming, from the FRONT, so the newest
#: output always survives and the drop is labelled for what it is. Kept a round
#: number well under the transport's limit rather than imported from
#: ``validation``: ``diagnostics`` does not otherwise depend on that module, and
#: ``test_response_budget_stays_under_the_transport_ceiling`` imports both and
#: pins the relationship, so the invariant is enforced without the coupling.
#: (``mcp_tools/skills.py`` bounds its own fields against the same ceiling for
#: the same reason, so this is an existing contract, not a new one.)
_MAX_LOG_RESPONSE_CHARS = 80_000
#: Prepended when the response budget forced older output out. Deliberately
#: says which end went, since the transport's own marker does not.
_RESPONSE_TRIM_NOTE = (
    "...[older output dropped to fit the tool-response limit; "
    "the NEWEST lines are kept -- narrow with `tail` or `since` to see more]...\n"
)
#: Default line count when the caller does not pass ``tail``.
_DEFAULT_LOG_TAIL = 200


def _read_log_tail(path: Path, max_bytes: int) -> str | None:
    """Tail a log file through one descriptor, refusing a symlinked final component.

    Closes the check-then-open TOCTOU window that a plain ``path.open()`` leaves:
    the source directories include the world-writable ``/tmp/kiro-log``, so a
    local principal could swap a validated regular file for a symlink to
    ``~/.ssh/config`` between the ``is_sensitive_path`` check and the read, and a
    following ``open`` would return the sensitive target's bytes (redaction is no
    backstop — a ``.ssh``/``.pem`` body matches no credential pattern).

    Where the platform offers ``O_NOFOLLOW`` (POSIX) the open uses it, so the
    kernel refuses at open time if the final component is a symlink (``ELOOP``);
    the descriptor is then ``fstat``-confirmed to be a regular file and THAT
    descriptor is tailed, so the bytes returned are provably from the path that
    was validated, not one swapped in afterward. ``O_NOFOLLOW`` guards only the
    FINAL component; the intermediate dirs are the source pickers' own fixed,
    non-symlinked anchors (``_usable_dir`` already rejects a linked ``kiro-log``).

    Where the platform lacks ``O_NOFOLLOW`` (Windows) the flag is omitted, and an
    ``is_symlink`` check plus the regular-file ``fstat`` is not enough on its own:
    a Windows JUNCTION is a reparse point ``is_symlink`` does not report, and it
    is the only link an unprivileged Windows user can create -- so the platform
    where planting one is easiest is the platform with no kernel no-follow. A
    reparse point swapped in after the check and pointing at a regular sensitive
    file satisfies ``S_ISREG``, and its bytes would reach the caller.

    An IDENTITY MATCH closes that, and it closes it on every platform rather than
    only the one missing a flag. ``os.lstat`` describes the path WITHOUT following
    its final component, ``os.fstat`` describes what the descriptor actually
    opened, and this function refuses unless the two name the same file
    (``st_dev``/``st_ino``; on Windows those come from the file's volume and
    index). Two things fail that comparison: a final component that is a link of
    any kind, because ``lstat`` sees the link while the descriptor sees its
    target, and a swap landing between the ``lstat`` and the open, because the
    descriptor then holds a different file. So the bytes returned are provably
    from the path that was checked. ``O_NOFOLLOW`` remains a POSIX
    STRENGTHENING -- it refuses at open time rather than after -- not a
    precondition for reading.

    This is the module's ONLY tail reader: both the agent-facing
    :func:`read_kiro_cli_logs` and :func:`collect_bundle` read through it, so the
    check-then-open hardening cannot be present in one path and missing in the
    other (a second reader with mirrored comments would drift).

    Returns ``None`` (skip this source) on any refusal or non-regular file rather
    than raising. The byte cut lands on a LINE boundary, same as the bundle
    collector's own tail: a cut inside ``Authorization: Basic <b64>`` would keep
    the credential and drop the header token that redacts it, so the partial
    first line is discarded. When the window holds NO newline at all -- the
    file's final line is itself longer than ``max_bytes`` -- there is no boundary
    to cut on and the whole chunk is a partial line, so only the truncation
    marker is returned. That case loses one oversized entry, which is the
    correct trade against emitting a fragment whose redaction anchor was cut
    away.

    ``O_NONBLOCK`` is set for the same check-then-open window: ``O_NOFOLLOW``
    refuses a swapped-in SYMLINK, but the same local writer to the world-writable
    ``/tmp/kiro-log`` can swap the validated regular file for a FIFO, and an
    ``O_RDONLY`` open of a FIFO with no writer BLOCKS INDEFINITELY — the
    ``S_ISREG`` guard below cannot run, because it only sees a descriptor the
    open already returned, so the agent's tool call would hang rather than be
    refused. With ``O_NONBLOCK`` the FIFO open returns immediately and the
    ``S_ISREG`` check then rejects it. It is a no-op for the regular-file case
    (``O_NONBLOCK`` has no effect on regular-file reads).
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        # lstat BEFORE the open, and do not follow the final component: this is
        # the identity the descriptor below must match.
        pre = os.lstat(path)
    except OSError:
        return None
    try:
        fd = os.open(str(path), flags)
    except OSError:
        # ELOOP (final component is a symlink, where O_NOFOLLOW is honored) lands
        # here too — refuse, do not fall back to a following open.
        return None
    try:
        st = os.fstat(fd)
        if not stat_module.S_ISREG(st.st_mode):
            return None
        if (st.st_dev, st.st_ino) != (pre.st_dev, pre.st_ino):
            # The descriptor is not the file that was checked: either the final
            # component is a link the open followed (lstat saw the link, fstat
            # sees its target) or it was swapped between the two calls. This is
            # the whole Windows defense, where there is no O_NOFOLLOW to refuse
            # a junction at open time, and it costs one stat on every platform.
            return None
        size = st.st_size
        if size > max_bytes:
            os.lseek(fd, size - max_bytes, os.SEEK_SET)
            marker = b"...[truncated: showing last %d bytes]...\n" % max_bytes
            raw = os.read(fd, max_bytes)
            # Drop the partial line the offset landed inside (line-boundary cut).
            nl = raw.find(b"\n")
            if nl == -1:
                # No newline anywhere in the window: the file's final line is
                # itself longer than max_bytes, so the WHOLE chunk is one
                # partial line and there is no boundary to cut on. Return only
                # the marker. Emitting the fragment would be a credential
                # disclosure, not a cosmetic truncation: `_EXTRA_REDACTIONS`
                # anchors on the header NAME and redacts to end-of-line, so a
                # cut landing after `Authorization:` but before its value
                # strips the token that redacts it and hands the raw secret to
                # the caller -- and the caller here is an agent's model
                # context. A serialized JSON-RPC frame is exactly the shape
                # that gets this long. Dropping one oversized entry loses
                # evidence; keeping half of it leaks.
                raw = marker
            else:
                raw = marker + raw[nl + 1 :]
        else:
            # `size` is a snapshot, so the read is bounded by `max_bytes` and not
            # by EOF: a file being appended to faster than this loop drains it
            # never reaches EOF, and an unbounded loop would grow `chunks` until
            # the MCP process is OOM-killed. The world-writable log directory is
            # exactly where a local writer can do that, and it is the same
            # adversary O_NOFOLLOW and O_NONBLOCK are here for. `max_bytes` is a
            # hard ceiling on every branch, not only the truncating one.
            chunks: list[bytes] = []
            remaining = max_bytes
            while remaining > 0:
                block = os.read(fd, min(1 << 16, remaining))
                if not block:
                    break
                chunks.append(block)
                remaining -= len(block)
            raw = b"".join(chunks)
    except OSError:
        return None
    finally:
        os.close(fd)
    return raw.decode("utf-8", errors="replace")


def _tail_lines(text: str, tail: int) -> str:
    """Keep the last ``tail`` lines of ``text`` (whole file when tail <= 0)."""
    if tail <= 0:
        return text
    lines = text.splitlines(keepends=True)
    if len(lines) <= tail:
        return text
    return "".join(lines[-tail:])


def _filter_since(text: str, since: str) -> str:
    """Keep only events at/after ``since``, each with its own continuation lines.

    kiro-cli log lines lead with an ISO-ish timestamp, so a lexical compare of
    the line's leading token against ``since`` is a monotone filter without
    parsing every timestamp shape. ``since`` is matched against the start of each
    line, so any leading slice of that timestamp works: a bare calendar day, or a
    day plus hour to narrow within it.

    A line with no recognizable leading timestamp is a CONTINUATION of the event
    above it (a JSON body, a stack trace), so it inherits that event's verdict:
    kept when the event was kept, dropped when the event was filtered out. Both
    halves matter. Keeping continuations unconditionally leaves orphaned bodies
    from events ``since`` rejected -- so the filter would not actually narrow the
    window, and the bulk of an old event (its payload) would ride along without
    the header line that identified it. Dropping them unconditionally would
    silently cut a kept event's own multi-line frame in half.

    Continuation lines appearing BEFORE any timestamped line are kept: that is
    the truncation marker :func:`_read_log_tail` prepends, plus any fragment
    ahead of the first full event, and there is no earlier verdict to inherit.
    """
    since = since.strip()
    if not since:
        return text
    out: list[str] = []
    # No event seen yet -> keep, so the truncation marker survives.
    event_kept = True
    for line in text.splitlines(keepends=True):
        head = line.lstrip()
        # A record START carries the timestamp and sets the verdict the following
        # continuation lines inherit. Same predicate the boundary trim uses, so a
        # line the two classify differently cannot exist.
        if _starts_a_record(line):
            event_kept = head[: len(since)] >= since
        if not event_kept:
            continue
        out.append(line)
    return "".join(out)


#: A serialized JSON-RPC ENVELOPE KEY. Its presence means a source is recording
#: protocol FRAMES rather than log records, which is the one condition under which
#: reading it would cross the session boundary this tool's source list exists to
#: hold: a ``tools/call`` frame carries conversation-derived arguments.
#:
#: Keyed on the quoted JSON key rather than on line LENGTH. Length is the obvious
#: signal and the wrong one: a legitimate record can be long (a deep path, a
#: wrapped error, a stack frame), so a length rule both misses a short frame and
#: refuses honest output. The quoted-key form also does not fire on prose that
#: merely mentions the protocol, e.g. ``failed to parse jsonrpc reply``.
_FRAME_BODY_MARKER = re.compile(r'"jsonrpc"\s*:|"params"\s*:\s*[{\[]|"result"\s*:\s*[{\[]')


#: Leading text of the note :func:`_read_log_tail` prepends to a cut window. Both
#: that function and :func:`_begin_at_event_boundary` key on it, so it lives here
#: rather than being spelled twice.
_TRUNCATION_NOTE_PREFIX = "...[truncated"

#: The shape that STARTS a log record: an ISO calendar date, then ``T`` or a space
#: before the time. Anything else on a line's left edge is a continuation of the
#: record above it.
#:
#: A bare "leading digit" test is not enough, and the difference is a disclosure
#: rather than a tidiness point: a serialized frame can hold a JSON array of bare
#: numbers, so ``      1000000,`` begins with a digit and would pass as a record
#: boundary -- which lets :func:`_begin_at_event_boundary` stop trimming inside a
#: frame and return the argument text after it. A JSON number cannot match this
#: pattern, because ``1234-56-78`` is not a number and a quoted string starts with
#: ``"``.
_RECORD_START = re.compile(r"\d{4}-\d{2}-\d{2}[T ]")


def _starts_a_record(line: str) -> bool:
    """True when ``line`` begins a log record rather than continuing one.

    One predicate for both :func:`_begin_at_event_boundary` and
    :func:`_filter_since`, which each need the same event/continuation split. Two
    copies would drift, and here they would drift on a security boundary.
    """
    return _RECORD_START.match(line.lstrip()) is not None


def _begin_at_event_boundary(text: str) -> str:
    """Drop leading lines of a CUT window until a timestamped event starts one.

    Closes a bypass in :func:`_looks_like_protocol_frames`. That check keys on the
    envelope tokens of a serialized frame, and in a pretty-printed frame those sit
    at the TOP. :func:`_read_log_tail` cuts from the front, so an oversized
    multiline frame can have its envelope fall outside the window while its
    trailing ARGUMENT lines remain -- the marker then finds nothing and those
    lines, which carry another session's conversation content, are returned.

    A cut window whose first line is not an event is therefore sitting inside a
    record whose beginning is gone, and nothing here can know what that record
    was. Those orphaned lines are discarded, so the window starts where a record
    does. A frame that STARTS inside the window still carries its envelope and is
    caught by the marker, so the two together cover both positions of the cut. A
    window with no event line at all yields the note alone.

    Only a CUT window is trimmed. When nothing was truncated the first line is
    genuinely the file's own first line, so there is no orphan to drop, and
    trimming would discard real output for no safety gain.

    This lives in the agent-facing reader rather than in :func:`_read_log_tail`,
    which :func:`collect_bundle` shares. Unlike the hidden-character strip in
    :func:`_scrub`, this DISCARDS data, and the only thing that justifies the loss
    is the cross-session boundary -- which the bundle, a user-to-user path, does
    not cross. Placement follows the boundary, not convenience.
    """
    lines = text.splitlines(keepends=True)
    if not lines or not lines[0].startswith(_TRUNCATION_NOTE_PREFIX):
        return text
    note, rest = lines[0], lines[1:]
    for i, line in enumerate(rest):
        # Must be a record START, not merely a line beginning with a digit: a
        # frame's JSON array of bare numbers would satisfy the looser test and let
        # the trim stop inside the frame.
        if _starts_a_record(line):
            return note + "".join(rest[i:])
    return note


def _looks_like_protocol_frames(text: str) -> bool:
    """True when ``text`` reads as serialized JSON-RPC frames rather than records.

    The tripwire behind this tool's scope argument. That argument holds only while
    mcp.log / lsp.log record protocol TRAFFIC and not frame BODIES -- a property
    of kiro-cli, which this repo neither builds nor pins. Rather than leave the
    assumption asserted and undetectable, a source that trips this is REFUSED
    whole, so a kiro-cli that starts logging payloads produces a visible refusal
    instead of a silent widening.

    Refusing the whole source is the point, and it is what separates this from the
    filter this module deliberately does not implement: a per-line filter over
    interleaved sessions would look scoped without being scoped, which is worse
    than nothing. This makes no claim to separate one session's frames from
    another's -- it decides only whether the file is the KIND of file the tool
    assumes, and stops if it is not.
    """
    return _FRAME_BODY_MARKER.search(text) is not None


def read_kiro_cli_logs(
    *,
    tail: int | None = None,
    since: str | None = None,
) -> str:
    """Redacted tail of kiro-cli's own PROTOCOL logs, for diagnosing a rejected turn.

    Reads ONLY the ``kiro-log`` mcp/lsp log files — never the fenced identity
    stores, and deliberately NOT the two conversation-bearing sources kiro-cli
    also writes:

    * ``sessions/cli/<sid>.jsonl`` transcripts, and
    * ``kiro-chat.log``, which is ONE fixed host path rather than a per-session
      file, so a host running one gateway with many concurrent sessions
      interleaves every session's traffic into it.

    Both are conversation content shared across every gateway session (including
    incognito/temporary ones), and ``_scrub`` is a CREDENTIAL pass that does not
    narrow prose — so returning either would disclose another session's private
    conversation to the calling session, with no per-session delimiter available
    to filter on. This tool therefore stays on the mcp/lsp protocol logs, which
    are what explain a rejected turn. :func:`collect_bundle` still includes the
    chat log because that path is user-to-user (the user triggers the collection
    and downloads it themselves); this one is cross-session.

    Every source is byte-capped, line-tailed to ``tail`` (default
    :data:`_DEFAULT_LOG_TAIL`), optionally filtered to lines at/after ``since``,
    and then passed through the shared redaction stack via :func:`_scrub`.
    Returns a human-readable report with one ``===`` section per source, or a
    note when no logs exist.

    Output is bounded at BOTH levels, and the second one exists because of which
    END the transport drops. :data:`_MAX_LOG_READ_BYTES` caps each source (a log
    tail is the shape that deadlocks a pipe, so that bound is by BYTES; ``tail``
    narrows lines within it but can never raise it), and
    :data:`_MAX_LOG_RESPONSE_CHARS` caps the whole assembled response. Every MCP
    response leaves through ``validation.build_tool_response``, whose
    ``sanitize_response`` truncates the TAIL -- so left alone it would drop the
    NEWEST log lines, the entire reason a tail was requested, behind a marker
    that reads like the oldest were cut. This function therefore trims its own
    output from the FRONT and labels the drop, so the newest lines always
    survive.

    The read goes through :func:`_read_log_tail`, which opens each source and
    (where the platform offers ``O_NOFOLLOW``) refuses a symlinked final
    component at open time, tailing that same descriptor — so a symlink swapped in
    after the ``is_sensitive_path`` check (the source dirs include the shared
    ``/tmp/kiro-log``) is refused at open time rather than followed to a
    sensitive target. Redaction runs AFTER truncation on purpose: the byte cut
    lands on a line boundary, so no credential is separated from the header token
    that redacts it.
    """
    tail = _DEFAULT_LOG_TAIL if tail is None else tail
    sections: list[str] = []
    total_redactions = 0

    # mcp/lsp only. `kiro-chat.log` is deliberately absent — see the docstring:
    # it is one shared host file carrying every session's conversation prose,
    # which `_scrub` (a credential pass) does not narrow.
    sources: list[tuple[str, Path]] = [(p.name, p) for p in _kiro_cli_extra_logs()]

    # Read the caps from the module at CALL time so a test can monkeypatch them.
    # Divide the response budget among the sources instead of giving each the
    # full per-source cap: `_read_log_tail` keeps the NEWEST bytes of whatever
    # window it is given, so dividing up front means every source contributes its
    # own newest lines and the assembled total already fits. Applying the full
    # cap to each and trimming afterwards would throw away one source entirely.
    max_bytes = min(_MAX_LOG_READ_BYTES, _MAX_LOG_RESPONSE_CHARS // max(len(sources), 1))

    for label, path in sources:
        try:
            # First-pass filters over the source pickers (defense in depth): a
            # symlink or a path resolving into a fenced/sensitive store is
            # skipped before we even open. On POSIX the authoritative guard
            # against a check-then-open swap is _read_log_tail's O_NOFOLLOW open;
            # on Windows (no O_NOFOLLOW) this pre-open is_symlink check plus
            # _read_log_tail's fstat regular-file check are the symlink defense.
            if path.is_symlink() or not path.is_file():
                continue
            if is_sensitive_path(str(path)):
                continue
        except OSError:
            continue
        text = _read_log_tail(path, max_bytes)
        if text is None:
            continue
        # A cut window can start inside a record whose beginning is gone. Discard
        # those orphaned lines FIRST, so the frame check below cannot be bypassed
        # by a multiline frame whose envelope fell outside the window.
        text = _begin_at_event_boundary(text)
        if _looks_like_protocol_frames(text):
            # Fail closed: this source is recording protocol FRAMES, not log
            # records, so it can carry another session's conversation-derived
            # arguments, and the scope argument for reading it does not cover
            # that. Refuse the whole source and SAY SO -- a silent skip reads as
            # "no logs", which is the same output as a healthy empty log.
            sections.append(
                f"=== {label} (REFUSED) ===\n"
                "This source contains serialized JSON-RPC frames rather than log\n"
                "records. Frame arguments carry conversation content from every\n"
                "session sharing this host file, so it is not readable here. Use\n"
                "the diagnostics bundle, which is a user-to-user path.\n"
            )
            continue
        if since:
            text = _filter_since(text, since)
        text = _tail_lines(text, tail)
        clean, n = _scrub(text)
        total_redactions += n
        sections.append(f"=== {label} ({n} secret(s) redacted) ===\n{clean.rstrip(chr(10))}\n")

    if not sections:
        return (
            "No kiro-cli protocol logs found. kiro-cli writes mcp.log / lsp.log "
            "under $XDG_RUNTIME_DIR|$TMPDIR/kiro-log/; none exist or are readable "
            "here. (kiro-chat.log is a single shared host file carrying every "
            "session's conversation, so it is deliberately not a source for this "
            "tool.)"
        )
    header = (
        f"kiro-cli logs (tail={tail}"
        + (f", since={since}" if since else "")
        + f", {total_redactions} secret(s) redacted, capped at {max_bytes // 1024} KiB/source)\n"
    )
    out = header + "\n".join(sections)
    if len(out) <= _MAX_LOG_RESPONSE_CHARS:
        return out
    # Belt and braces over the pre-read division above, which bounds BYTES while
    # the transport's ceiling counts CHARACTERS: `_scrub` can grow the text (a
    # short token replaced by `[REDACTED]`), and the section framing adds to it.
    # Keep the header and the NEWEST characters; the header must survive because
    # it is what states the tail/since/redaction count the rest is read against.
    budget = _MAX_LOG_RESPONSE_CHARS - len(header) - len(_RESPONSE_TRIM_NOTE)
    if budget <= 0:
        # Degenerate only if the ceiling is set below the framing itself.
        return (header + _RESPONSE_TRIM_NOTE)[:_MAX_LOG_RESPONSE_CHARS]
    kept = out[-budget:]
    # Start on a line boundary, purely so the first surviving line is readable:
    # unlike the cut in `_read_log_tail`, this one cannot expose a credential,
    # because `_scrub` has already run over every section above.
    nl = kept.find("\n")
    if nl != -1:
        kept = kept[nl + 1 :]
    return header + _RESPONSE_TRIM_NOTE + kept


def _macos_crash_reports() -> list[Path]:
    """Newest kiro-cli / kiro .ips crash reports (macOS only)."""
    if sys.platform != "darwin":
        return []
    reports: list[Path] = []
    for base in (
        Path.home() / "Library" / "Logs" / "DiagnosticReports",
        Path("/Library/Logs/DiagnosticReports"),
    ):
        if not _usable_dir(base):
            continue
        try:
            reports.extend(p for p in base.glob("kiro*.ips") if p.is_file() and not p.is_symlink())
        except OSError:
            continue
    reports.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return reports[:_MAX_IPS_REPORTS]


def _kiro_cli_version() -> str:
    try:
        out = subprocess.run(
            ["kiro-cli", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return (out.stdout or out.stderr).strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


#: PEP 440 prerelease segment — see
#: :data:`kiro_crew.release_channel._PEP440_PRERELEASE`, which owns the rule.
#: Kept here only so the pattern is greppable from this module's tests.
_PEP440_PRERELEASE = release_channel._PEP440_PRERELEASE


def _channel() -> str:
    """This build's release channel.

    Thin alias for :func:`kiro_crew.release_channel.channel`, which owns the
    rule (and documents why it is not a one-line substring test) — the status
    payload needs the same answer without importing this collector.

    Passes this module's ``__version__`` EXPLICITLY rather than letting the
    other module read its own: every other version-dependent field in the
    pre-filled issue URL comes from ``diagnostics.__version__``, so a single
    patch point keeps the URL internally consistent instead of letting the
    channel and the version field disagree.
    """
    return release_channel.channel(__version__)


#: Release channel -> repository label. Owned by :mod:`release_channel` so the
#: dashboard, this flow, and the triage workflow share one vocabulary.
_CHANNEL_LABELS = release_channel.CHANNEL_LABELS

#: ``beacon.distribution()`` value -> the exact option text of bug_report.yml's
#: "How is it installed?" dropdown. Prefilling a dropdown requires the option
#: string VERBATIM; an unmatched value leaves the field empty rather than
#: erroring, so a drift here degrades to "user picks it themselves".
_INSTALL_OPTIONS = {
    "dmg": "Desktop app",
    "appimage": "Desktop app",
    "deb": "Desktop app (deb)",
    "rpm": "Desktop app (rpm)",
    "wheel": "pip / pipx",
    "docker": "Docker",
    "source": "From source",
}

#: Release channel -> the exact option text of bug_report.yml's "Release
#: channel" dropdown. Same verbatim-match requirement as ``_INSTALL_OPTIONS``;
#: ``test_diagnostics.py`` asserts both maps against the template's real option
#: lists so a rename there cannot silently stop prefilling.
_CHANNEL_OPTIONS = release_channel.CHANNEL_FORM_OPTIONS


def _versions_text(note: str) -> str:
    lines = [
        f"kirocrew_version: {__version__}",
        f"channel: {_channel()}",
        f"kiro_cli_version: {_kiro_cli_version()}",
        f"python: {platform.python_version()}",
        f"platform: {platform.platform()}",
        f"machine: {platform.machine()}",
        f"collected_at: {datetime.now(timezone.utc).isoformat()}",
        f"data_home: {config_dir()}",
    ]
    if note.strip():
        lines.append("")
        lines.append("user_note:")
        lines.append(note.strip())
    return "\n".join(lines) + "\n"


def _issue_url(result: BundleResult, note: str, *, prefill: bool = True) -> str:
    """Build the pre-filled new-issue URL the modal's primary button opens.

    Routes through the ``bug_report.yml`` ISSUE FORM rather than posting a
    free-form ``body=``, for two reasons that both serve triage:

    * **The channel label is attached at filing time.** ``labels=`` carries
      ``channel: <lane>`` derived from the running build, so a nightly or
      insider report is filterable the instant it lands — no maintainer has to
      read a version string out of the body to know which lane it came from,
      and ``issue-triage.yml``'s model never gets a chance to guess it.
    * **The form's own fields arrive filled.** A free-form body skipped the
      form entirely, so reports from this flow lacked the version / install
      answers that triage reads. Field ids double as query params, so they can
      be prefilled from what the gateway already knows.

    Deliberately NOT prefilled: ``platform``. The form's own help text warns
    that a guess there becomes a wrong ``platform:`` label, and the host OS is
    exactly a guess — it says where the bug was SEEN, not that it is specific
    to that OS. The ``search`` checkbox is also left untouched: it is an
    attestation, and prefilling an attestation makes it worthless.
    """
    # Imported lazily: ``beacon`` is a heavier module than diagnostics needs at
    # import time, and this is the only place that wants it.
    from kiro_crew import beacon

    channel = _channel()
    params = {
        "template": "bug_report.yml",
        "labels": ",".join(["bug", _CHANNEL_LABELS[channel]]),
        "title": "[bug] ",
        "version": __version__,
        "channel": _CHANNEL_OPTIONS[channel],
        "what-happened": note.strip() or "",
        "context": "\n".join(
            [
                f"Diagnostics bundle: `{result.filename}`",
                "",
                f"Collected locally at `{result.zip_path}` — "
                f"{result.total_redactions} secret(s) auto-redacted before "
                "packaging.",
                "",
                f"kiro-cli: `{_kiro_cli_version()}`",
                f"Host: `{platform.platform()}`",
                "",
                "<!-- Drag the .zip into this issue before submitting. -->",
            ]
        ),
    }
    install = _INSTALL_OPTIONS.get(beacon.distribution())
    if install:
        params["install"] = install
    if not prefill:
        # Terminal emission: drop the two free-form fields. `context` alone is
        # ~430 chars and `what-happened` carries the user's note, so with them
        # the query is both over the exfil query-length threshold (200) and
        # UNBOUNDED — a long note pushes any fixed budget back over. What
        # remains comes from fixed option maps and the version stamp, so the
        # query is bounded by construction (~140 chars) and the printed link
        # survives `redact_exfiltration_urls` on any surface that scans it.
        # Nothing is lost: `kirocrew doctor` already prints the bundle path and
        # redaction count, and reports the kiro-cli/host versions in its own
        # output, which is everything `context` restates.
        params.pop("context", None)
        params.pop("what-happened", None)
    # quote (not quote_plus): a literal `+` in a version stamp or path would
    # decode back as a space under form-encoding, and `%20` is unambiguous
    # everywhere GitHub parses this.
    return f"{_ISSUE_NEW_URL}?" + urlencode(params, quote_via=quote)


def terminal_issue_url(result: BundleResult, note: str = "") -> str:
    """Issue link shaped for printing to a terminal rather than for a browser.

    The dashboard renders `BundleResult.github_issue_url` out of a JSON response
    that no redactor scans, so it keeps the full pre-filled body. A link PRINTED
    to stdout has no such guarantee — it gets selected into chat, captured from a
    shell tool, or relayed by an agent, and on those paths the pre-filled variant
    is replaced wholesale by ``[REDACTED: suspicious URL to github.com]``. This
    variant trades the pre-filled body for a link that arrives intact.
    """
    return _issue_url(result, note, prefill=False)


def _prune_old_bundles(out_dir: Path, keep: int) -> None:
    """Keep only the newest ``keep`` diagnostics zips in ``out_dir``."""
    try:
        bundles = sorted(
            out_dir.glob("kirocrew-diagnostics-*.zip"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return
    for stale in bundles[keep:]:
        try:
            stale.unlink()
        except OSError:
            pass


def collect_bundle(
    *,
    note: str = "",
    include_logs: bool = True,
    output_dir: Path | None = None,
) -> BundleResult:
    """Collect, redact, and zip a diagnostics bundle.

    Args:
        note: optional free-text description from the user (kept in the bundle
            and pre-filled into the GitHub issue body).
        include_logs: when False, only versions + crash reports are bundled
            (no full gateway / chat logs) for a lighter, lower-sensitivity zip.
        output_dir: where to write the zip. Defaults to ``<data_home>/diagnostics``.

    Returns:
        :class:`BundleResult` with the zip path and a pre-filled issue URL.
    """
    home = config_dir()
    out_dir = output_dir or (home / "diagnostics")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        # The rule's suggested 0o644 is WRONG for a directory: it drops the owner
        # execute bit (making the dir untraversable) and adds world-read. 0o700 is
        # strictly MORE restrictive than the suggestion — owner-only, which is what
        # a dir holding local diagnostic logs wants. Suppression must sit on the
        # finding line (or the one directly above) for semgrep to honor it.
        # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions
        os.chmod(out_dir, 0o700)
    except OSError:
        pass

    # Scrub the user-supplied note ONCE up front. It flows into versions.txt,
    # manifest.json, AND the pre-filled GitHub issue URL — a user may paste a
    # secret (bearer token, key) into "what happened?", so it needs the same
    # redaction the log members get below.
    note, _ = _scrub(note or "")

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    filename = f"kirocrew-diagnostics-{stamp}-{uuid.uuid4().hex[:8]}.zip"
    zip_path = out_dir / filename

    result = BundleResult(zip_path=zip_path, filename=filename)

    # (member_name, source_path, gated_by_include_logs)
    text_sources: list[tuple[str, Path, bool]] = []
    if include_logs:
        text_sources.append(("gateway.log", home / "gateway.log", True))
        text_sources.append(("gateway.log.prev", home / "gateway.log.prev", True))
        chat = _kiro_cli_chat_log()
        if chat is not None:
            text_sources.append(("kiro-chat.log", chat, True))
        for extra in _kiro_cli_extra_logs():
            text_sources.append((f"kiro-cli-{extra.name}", extra, True))

    # Crash artifacts are always useful and low-volume — include regardless.
    text_sources.append(("crash.log", home / "logs" / "crash.log", False))
    dumps_dir = home / "logs" / "crash-dumps"
    if _usable_dir(dumps_dir):
        dumps = sorted(
            (p for p in dumps_dir.glob("*") if p.is_file() and not p.is_symlink()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:_MAX_CRASH_DUMPS]
        for d in dumps:
            text_sources.append((f"crash-dumps/{d.name}", d, False))
    for ips in _macos_crash_reports():
        text_sources.append((f"crash-reports/{ips.name}", ips, False))

    # Create the archive 0o600 from the start (not chmod-after, which leaves a
    # world-readable window) — the bundle holds local diagnostic logs.
    #
    # os.O_BINARY is REQUIRED on Windows: os.open() there defaults to TEXT mode,
    # and os.fdopen(fd, "wb") cannot change the translation mode of an fd it was
    # handed. Every 0x0A in the DEFLATE stream would be written as 0x0D 0x0A,
    # desynchronising the central-directory offsets and producing an archive that
    # will not open. getattr keeps this a no-op on POSIX, which has no text mode.
    # Same reason as dashboard/handlers/files.py and dashboard/token_secret.py.
    _fd = os.open(
        zip_path,
        os.O_CREAT | os.O_WRONLY | os.O_TRUNC | getattr(os, "O_BINARY", 0),
        0o600,
    )
    with os.fdopen(_fd, "wb") as _raw, zipfile.ZipFile(_raw, "w", zipfile.ZIP_DEFLATED) as zf:
        # Generated members first.
        versions = _versions_text(note)
        zf.writestr("versions.txt", versions)
        result.included.append("versions.txt")
        result.redaction_summary["versions.txt"] = 0

        for member, src, _gated in text_sources:
            try:
                # Never follow a symlink — a symlinked source could pull an
                # arbitrary off-tree target (e.g. ~/.ssh/id_rsa) into the bundle.
                if src.is_symlink() or not src.is_file():
                    result.skipped.append(member)
                    continue
            except OSError:
                result.skipped.append(member)
                continue
            # Same hardened reader the agent-facing tool uses: the check above is
            # check-then-open, and these sources include the world-writable
            # /tmp/kiro-log, so the O_NOFOLLOW|O_NONBLOCK open plus the fstat
            # regular-file check are what make the bytes provably come from the
            # path that was validated. Returns None (skip) instead of raising.
            text = _read_log_tail(src, _MAX_MEMBER_BYTES)
            if text is None:
                result.skipped.append(member)
                continue
            clean, n = _scrub(text)
            zf.writestr(member, clean)
            result.included.append(member)
            result.redaction_summary[member] = n

        # Manifest last so it reflects the final included/skipped/redaction state.
        manifest = {
            "tool": "kirocrew-diagnostics",
            "kirocrew_version": __version__,
            "channel": _channel(),
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "include_logs": include_logs,
            "note": note.strip(),
            "included": result.included,
            "skipped": result.skipped,
            "redaction_summary": result.redaction_summary,
            "total_redactions": result.total_redactions,
        }
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        result.included.append("manifest.json")

    result.github_issue_url = _issue_url(result, note)
    _prune_old_bundles(out_dir, _MAX_KEPT_BUNDLES)
    return result
