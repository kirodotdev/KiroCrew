"""The fleet probe's session side: what a worker's transcript tail says.

Loaded by ``fleet_probe.py``, which re-exports every name defined here; it is not run on
its own. It holds the protocol tag vocabulary, the reading of one session's transcript
tail (its ``i=`` index and the tag it classifies to), the tail match the delivery
counters use, and the handled set: its state file and the rules that suppress a signal
the conductor has already acted on. New work on how a session's tail is read, or on
when a dispositioned signal stays quiet, goes here.

Standard library only, importing nothing from the package or from a sibling script, so
the probe stays runnable by a bare interpreter.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any

#: The protocol words a worker may open a report with. The single source for
#: both the regex below and the handled set's record of the last DISPOSITIONED
#: protocol tag, which has to recognise one without re-matching prose.
PROTO_TAGS = frozenset({"GREEN", "PR", "BLOCKED", "STANDDOWN", "PROPOSAL", "WORKING"})

#: A worker report is ``<WORD>:`` — the colon IS the protocol. A bare word
#: boundary reads prose as a report: one ``^<WORD>\b`` match in five was not a
#: report (often a bare ``PR #<n>``). Built from ``PROTO_TAGS``, longest first so
#: ``PR`` vs ``PROPOSAL`` does not depend on backtracking.
PROTO = re.compile(r"^(" + "|".join(sorted(PROTO_TAGS, key=lambda w: (-len(w), w))) + r")\s*:")

#: Markdown decoration a report may be wearing at position zero: emphasis and
#: strikethrough (``*``, ``_``, ``~``), code ticks, heading hashes, blockquote
#: arrows, and list markers (``-``, ``*``, ``+``, or a number with ``.``/``)``),
#: in any combination and with any whitespace between them.
_LEADING_DECOR = re.compile(r"^(?:[\s>#*_~`+-]|\d+[.)])+")


def _proto_tag(text: str) -> str | None:
    """The protocol tag *text* reports, or None -- decoration and all.

    ``PROTO`` is anchored at position zero, so leading decoration
    (``**BLOCKED:**``) would make a report silently read as no-prefix. Emphasis
    is ordinary habit, so the reader normalises it rather than the worker.

    Only the text matched here is normalised; callers keep the raw text, so the
    digest covers what the worker wrote. Decoration is stripped from the FRONT
    only: a bolded protocol word mid-sentence still sets no tag.
    """
    match = PROTO.match(_LEADING_DECOR.sub("", text, count=1))
    return match.group(1) if match else None


#: Roles whose rows are TOOL ACTIVITY, never a worker's own protocol message.
#: ``role`` is the only persisted field that separates them (same set as
#: ``history_consolidation._TOOL_ROLES``). A protocol word in a tool row is quoted
#: text, and no measured error/stall/throttle line ever landed on a tool row.
TOOL_ROLES = frozenset({"tool", "tool_call", "tool_result"})

#: The roles the SESSION itself produces: its own messages and its tool activity.
#: Everything else (``nudge``/``inject`` notices, ``user`` turns, the header)
#: must not count as the session having spoken. Byte needles because the whole
#: file is counted; spelled as ``json.dumps`` default separators emit them.
#: Anchored at a LINE boundary, so text inside another row (or a torn line at the
#: window edge) is never counted.
_OWN_ROW_NEEDLES = tuple(
    f'{{"role": "{role}"'.encode() for role in ("assistant", *sorted(TOOL_ROLES))
)


def _count_own_rows(raw: bytes) -> int:
    """How many rows in *raw* the session itself produced.

    A needle matches only at the start of a line, which is where a row's own
    opening brace is. The first line has no preceding newline, so it is checked
    separately rather than being silently missed.
    """
    total = 0
    for needle in _OWN_ROW_NEEDLES:
        total += raw.count(b"\n" + needle)
        if raw.startswith(needle):
            total += 1
    return total


#: Error shapes observed in real worker tails, not shapes invented here.
DEFAULT_ERR_RES = (
    # Both throttle wordings: "Bedrock is throttling ..." (kiro-cli) and
    # "The model provider is throttling ..." (every other harness).
    r"is throttling",
    r"dispatch failure",
    r"initialize timed out",
)


#: An initialize-timeout tail: the session never got a live backend, so nothing
#: it was told to do was ever delivered. Literal from the emitters
#: (``mcp_gateway.backend`` records ``initialize timed out on respawn``).
DEFAULT_INIT_TIMEOUT_RES = (r"initialize timed out",)

#: A turn ended by the stall watchdog rather than by the worker. Literals from
#: the emitters: ``dashboard.state.TOOL_STALL_RECOVERY_PREFIX`` /
#: ``STALE_RECOVERY_PREFIX`` (the recovery notice injected into the transcript)
#: and ``acp.types.STOP_REASON_TOOL_STALL``. The dash inside the bracketed
#: notices is matched as ``.*`` so an em-dash/hyphen change in the emitter does
#: not silently stop counting.
DEFAULT_WATCHDOG_RES = (
    r"\[Tool stall\b.*automatic recovery\]",
    r"\[Stalled turn\b.*automatic recovery\]",
    r"error: tool stall",
    r"tool stalled\b.*no data for",
)

IDLE_TAG = "IDLE"
TERMINAL_TAG = "TERMINAL"
NOPROGRESS_TAG = "NOPROGRESS"

#: Reports that END an assignment. A worker that files one and then writes an
#: unprefixed line is finished, not wedged, and must not age into IDLE.
#:
#: ``GREEN`` is every worker's literal exit condition ("report GREEN and stop"),
#: so it must be here. ``PR`` is deliberately NOT: work continues past opening a
#: pull request, and that worker still owes a green.
TERMINAL_TAGS = frozenset({"GREEN", "STANDDOWN", "PROPOSAL"})

#: Reports that keep their meaning until the conductor ACTS on them.
#:
#: A probe samples, so it sees only the newest message, and a blocked worker must
#: keep reporting status, which displaces its own ``BLOCKED``. A sticky report is
#: cleared only by another real report; ``--mark-handled`` quiets it.
#:
#: NOT merged with ``TERMINAL_TAGS``: terminal means close me, sticky means a
#: ruling is owed, which call for opposite actions.
#:
#: KNOWN BOUND: stickiness reaches only as far back as ``tail_bytes`` (200 KB), so
#: a long-heartbeating blocked worker can push its report out of the window. The
#: handled set cannot close this: its ``proto`` exists only once the ruling was
#: delivered, so the transcript stays the source of truth.
STICKY_TAGS = frozenset({"BLOCKED"})

#: A heartbeat reports that the worker is ALIVE, not what state it is in, so it
#: cannot clear a sticky report. Every other protocol word can: a worker that
#: files ``PR``, ``GREEN``, ``STANDDOWN`` or ``PROPOSAL`` after being blocked has
#: moved on, and no ruling is owed any more.
HEARTBEAT_TAGS = frozenset({"WORKING"})

#: Reports that carry a PAYLOAD the conductor acts on, as opposed to a heartbeat
#: (alive, no state) or a condition the probe derives (``IDLE``, ``NOPROGRESS``,
#: ``GONE``, ``ERR``). A payload disposition is the one fact that must survive
#: every later mark: the handled set holds one entry per key, so without that the
#: record of an answered report is overwritten by whatever is reported next and
#: the answered report presents again.
_PAYLOAD_TAGS = PROTO_TAGS - HEARTBEAT_TAGS

_FIRING = {
    "GREEN",
    "PR",
    "BLOCKED",
    "STANDDOWN",
    "PROPOSAL",
    "ERR",
    IDLE_TAG,
    TERMINAL_TAG,
    NOPROGRESS_TAG,
}

#: Tags whose disposition EXPIRES, so the condition re-alerts while it holds.
#: Both describe an ongoing state rather than a payload that was filed once.
_EXPIRING_TAGS = frozenset({IDLE_TAG, NOPROGRESS_TAG})

#: A session key is a filename STEM, never a path: one path-safe token. This is
#: what keeps an agent-authored key (``../../etc/foo``, an absolute path) from
#: steering the transcript read outside the sessions directory.
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")


def data_home() -> Path:
    env = os.environ.get("KIROCREW_HOME")
    return Path(env) if env else Path.home() / ".kiro" / "crew"


#: Attempts at the final rename on Windows, and the pause between them. The
#: script is stdlib-only, so this is a small inline twin of
#: ``kiro_crew.atomic_write.replace_with_retry`` rather than an import of it.
_REPLACE_ATTEMPTS = 5
_REPLACE_BACKOFF_SECONDS = 0.05
#: Only Windows refuses to replace a file another handle has open.
_ON_WINDOWS = os.name == "nt"


def _replace(tmp: str, path: Path) -> None:
    """``os.replace``, retrying while another handle holds the file on Windows.

    On Windows a rename onto a file that an indexer, an AV scan or a reader has
    open fails with ``PermissionError`` for as long as that handle lives. On
    POSIX the same error is genuine, so it is raised at once.
    """
    for _ in range(_REPLACE_ATTEMPTS - 1):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if not _ON_WINDOWS:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS)
    os.replace(tmp, path)


def _atomic_write(path: Path, payload: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        _replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _text_of(entry: dict[str, Any]) -> str:
    content = entry.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(entry.get("text", ""))


def _tail_entries(path: Path, max_bytes: int) -> tuple[list[dict[str, Any]], int | None]:
    """Parse the tail window, and report how many rows the SESSION has produced.

    The index is 2a's no-progress discriminator: unchanged across two probes
    means the session has not spoken, which a deadlocked worker cannot fake.

    It counts from the START of the file: a window-relative count freezes once a
    transcript passes ``tail_bytes`` (200 KB) and reads as a deadlock.

    It counts only rows the session PRODUCED, tool rows included, never inbound
    ones: otherwise the conductor's own nudge would read as the worker's
    progress. The byte needles match ``json.dumps`` default separators; the
    round-trip test pins that against the real writer.

    Returns ``(entries, last_index)``; ``last_index`` is None for an unreadable
    or empty file, or one with no session rows yet, which prints as ``i=?``
    exactly like an unknown age.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return [], None
    window = raw[-max_bytes:] if len(raw) > max_bytes else raw
    entries: list[dict[str, Any]] = []
    for line in window.splitlines():
        try:
            parsed = json.loads(line)
        except Exception:
            continue  # a truncated first line is expected when tailing
        if isinstance(parsed, dict):
            entries.append(parsed)
    produced = _count_own_rows(raw)
    return entries, (produced - 1 if produced else None)


def _classify(entries: list[dict[str, Any]], err_res: list[re.Pattern[str]]) -> tuple[str, str]:
    """Return (tag, tail_text) for one session's transcript tail.

    Tool rows are dropped first, so an error pattern quoted in a tool title
    cannot raise ERR on a healthy worker.

    The tag is the newest REPORT, and a sticky report outlives later messages
    (see ``STICKY_TAGS``). The returned tail is the sticky report's OWN text, so
    its digest stays stable across heartbeats and it fires once.

    ERR takes precedence, since an errored session must be resumed first; that
    defers a sticky report by one cycle at most.
    """
    spoken = [entry for entry in entries if str(entry.get("role", "")) not in TOOL_ROLES]
    last_assistant = ""
    newest_report: tuple[str, str] | None = None
    sticky_report: tuple[str, str] | None = None
    for entry in reversed(spoken):
        if entry.get("role") != "assistant":
            continue
        text = _text_of(entry).strip()
        if not text:
            continue
        if not last_assistant:
            last_assistant = text
        tag = _proto_tag(text)
        if tag is None:
            continue
        if newest_report is None:
            newest_report = (tag, text)
        if tag not in HEARTBEAT_TAGS:
            # The newest report that actually states a state. Walking stops here:
            # anything older has already been superseded by this one.
            sticky_report = (tag, text)
            break

    last_any = spoken[-1] if spoken else {}
    last_any_text = _text_of(last_any)
    if "error" in str(last_any.get("role", "")).lower() or any(
        rx.search(last_any_text) for rx in err_res
    ):
        return "ERR", (last_any_text.strip() or last_assistant)

    if sticky_report is not None and sticky_report[0] in STICKY_TAGS:
        return sticky_report
    if newest_report is not None:
        return newest_report
    return "-", last_assistant


def _sticky_pending(entries: list[dict[str, Any]]) -> tuple[str, str] | None:
    """The newest STICKY report in the window, or None if none is owed.

    Used only when the primary reading is a suppressed ``ERR``. A report that is
    not sticky returns None rather than being surfaced, because a newer
    non-sticky report supersedes whatever preceded it -- this reaches past an
    error row, not past a state the worker has since moved on from.
    """
    for entry in reversed([e for e in entries if str(e.get("role", "")) not in TOOL_ROLES]):
        if entry.get("role") != "assistant":
            continue
        text = _text_of(entry).strip()
        if not text:
            continue
        tag = _proto_tag(text)
        if tag is None or tag in HEARTBEAT_TAGS:
            continue
        return (tag, text) if tag in STICKY_TAGS else None
    return None


def _tail_matches(entries: list[dict[str, Any]], patterns: list[re.Pattern[str]]) -> bool:
    """Does anything the session SAID in this window match one of *patterns*?

    Used for the delivery counters (2c), which are per-session facts: a session
    can be undelivered whatever its tag. Tool rows are skipped as in
    ``_classify``. The whole window is scanned, since a watchdog notice is
    followed by whatever the session did next.

    The walk goes NEWEST first and stops at the first protocol report: a session
    that filed a report since the notice got a turn through, so a healed failure
    does not stay counted for the whole 200 KB window.
    """
    for entry in reversed(entries):
        if str(entry.get("role", "")) in TOOL_ROLES:
            continue
        text = _text_of(entry)
        if not text:
            continue
        if entry.get("role") == "assistant" and _proto_tag(text.strip()) is not None:
            # The boundary is tested BEFORE the patterns, not after. A recovery
            # report that quotes the notice it recovered from -- "WORKING: back up
            # after initialize timed out" -- matches the pattern on its own text,
            # so testing patterns first counts the recovery itself as the failure
            # and pins the session in the undelivered column permanently.
            return False
        if any(rx.search(text) for rx in patterns):
            return True
    return False


def _recorded_proto(handled: dict[str, Any], key: str) -> str | None:
    """The last DISPOSITIONED protocol tag for *key*, or None.

    Read from the single ``settled`` record, which every mark sets or carries
    forward, so a later non-payload disposition (``IDLE``, ``ERR``, ``GONE``)
    cannot erase it; otherwise a finished worker reads like a wedged one.

    A ``{tag, digest, ts}`` entry with no ``settled`` falls back to its own tag
    when that tag is a payload; otherwise None.
    """
    entry = handled.get(key)
    if not isinstance(entry, dict):
        return None
    settled = entry.get("settled")
    if isinstance(settled, dict) and isinstance(settled.get("tag"), str):
        return str(settled["tag"])
    tag = entry.get("tag")
    if isinstance(tag, str) and tag in _PAYLOAD_TAGS:
        # The shipped shape, on the first probe after an upgrade: no ``settled``
        # yet, but the entry's own tag is the last dispositioned report, and when
        # that is a payload it answers the same question. Without this a delivered
        # worker is nudged once during the upgrade -- small, and exactly the harm
        # this field exists to prevent, so it is not worth conceding.
        return tag
    return None


def _stalled_since_disposition(
    handled: dict[str, Any], key: str, index: int | None, idle_secs: int
) -> bool:
    """Has *key* produced nothing at all since the conductor last acted on it?

    Compares the current index with the one stored at the last
    ``--mark-handled``, so it needs no new write: "no message and no tool since
    you last acted on this session". The disposition must also be at least one
    idle budget old, or the tag would fire right after every mark. A key with no
    recorded index cannot be stalled yet.

    BOUND: the index is a COUNT, monotonic only while the transcript only grows.
    A writer that truncates or reuses a transcript path would need a generation
    as well as a position; current writers are append-only JSONL.
    """
    if index is None:
        return False
    entry = handled.get(key)
    if not isinstance(entry, dict):
        return False
    recorded = entry.get("index")
    if not isinstance(recorded, int) or isinstance(recorded, bool) or recorded != index:
        return False
    marked = entry.get("ts")
    return isinstance(marked, (int, float)) and time.time() - marked >= idle_secs


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


def _load_state(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class StateUnreadable(Exception):
    """The state file exists but cannot be read as a handled set."""


def _load_state_strict(path: Path) -> dict[str, Any]:
    """The state for a WRITE: ``{}`` when the file does not exist.

    ``_load_state`` folds every failure into ``{}``, which is right for a read
    whose worst case is one re-fired signal. A mark writes the map it read back
    to disk, so the same fallback would replace every recorded disposition with
    the one being added. Here a present file that cannot be read or is not JSON
    raises instead, so the caller can refuse and leave the file as it is. Parsed
    JSON of the wrong shape holds no handled set to lose, so it reads as ``{}``
    as it does for the patrol, and the mark rewrites it as a valid set.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise StateUnreadable(f"{type(exc).__name__}: {exc.strerror or exc}") from exc
    except UnicodeDecodeError as exc:
        raise StateUnreadable(f"not UTF-8: {exc.reason}") from exc
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        raise StateUnreadable(f"not valid JSON: {exc}") from exc
    return parsed if isinstance(parsed, dict) else {}


def _suppressed(handled: dict[str, Any], key: str, tag: str, digest: str, idle_secs: int) -> bool:
    entry = handled.get(key)
    if not isinstance(entry, dict):
        return False
    settled = entry.get("settled")
    if isinstance(settled, dict) and settled.get("tag") == tag and settled.get("digest") == digest:
        # A PAYLOAD disposition preserved underneath a later condition mark. The
        # handled set holds one entry per key, so marking IDLE or NOPROGRESS on a
        # session would otherwise overwrite the record that its BLOCKED was
        # already answered -- and an answered ruling re-presenting is worse than
        # the stall going unreported, because the conductor re-adjudicates
        # something it has already decided. Payload dispositions never expire, so
        # this needs no clock.
        return True
    if entry.get("tag") != tag or entry.get("digest") != digest:
        return False
    if tag in _EXPIRING_TAGS:
        # These two describe a CONTINUING condition rather than a payload, so a
        # single disposition must not silence them forever: a worker that is
        # still silent, or still producing nothing, has to re-alert. Every other
        # tag stays suppressed while its payload is unchanged, because a report
        # that has been acted on is done.
        marked = entry.get("ts")
        return isinstance(marked, (int, float)) and time.time() - marked < idle_secs
    return True
