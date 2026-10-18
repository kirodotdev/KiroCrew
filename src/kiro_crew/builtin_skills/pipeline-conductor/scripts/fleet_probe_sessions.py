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

#: A worker report is ``<WORD>:`` — the colon IS the protocol, not decoration.
#: Matching a bare word boundary instead reads ordinary prose as a report:
#: measured over the 60 most recent transcripts on this host, 20 of the 94
#: assistant rows that matched ``^<WORD>\b`` were not reports at all (13 of them
#: opened with a bare ``PR #<n>``), so one row in five carried a fabricated tag.
#:
#: Built FROM ``PROTO_TAGS`` rather than spelling the six words a second time,
#: because two copies of one list diverge. Longest first, so a word that is a
#: prefix of another is never tried after it: ``PR`` and ``PROPOSAL`` share two
#: characters, and while Python's alternation backtracks and would match either
#: way, ordering by length makes that correctness independent of backtracking.
PROTO = re.compile(r"^(" + "|".join(sorted(PROTO_TAGS, key=lambda w: (-len(w), w))) + r")\s*:")

#: Markdown decoration a report may be wearing at position zero: emphasis and
#: strikethrough (``*``, ``_``, ``~``), code ticks, heading hashes, blockquote
#: arrows, and list markers (``-``, ``*``, ``+``, or a number with ``.``/``)``),
#: in any combination and with any whitespace between them.
_LEADING_DECOR = re.compile(r"^(?:[\s>#*_~`+-]|\d+[.)])+")


def _proto_tag(text: str) -> str | None:
    """The protocol tag *text* reports, or None -- decoration and all.

    ``PROTO`` is anchored at position zero, so ANY leading decoration defeats it:
    ``**BLOCKED:**`` puts an asterisk where the tag has to be, the match fails,
    and the line falls through as no-prefix. On a fresh transcript IDLE does not
    fire either, so the report is not delayed or suppressed -- it is silent.

    That is worth normalising rather than legislating, because emphasis is
    ordinary formatting habit rather than a protocol violation, and a convention
    the worker has to remember fails exactly when the worker is under pressure --
    which is when escalations get written. The robust half has to be the reader.

    WHICH TEXT IS NORMALISED, precisely, because the difference is observable:
    only the text this function MATCHES against. Callers keep the raw text for
    everything else, so the digest is still computed over what the worker
    actually wrote and a decorated report keeps a stable, distinct identity.

    The anchor survives normalisation: decoration is stripped from the FRONT, so
    a bolded protocol word mid-sentence still does not set a tag. This is not a
    substring search, and turning it into one would tag any message that
    mentioned a report.
    """
    match = PROTO.match(_LEADING_DECOR.sub("", text, count=1))
    return match.group(1) if match else None


#: Roles whose rows are TOOL ACTIVITY, never a worker's own protocol message.
#: This is the transcript's OWN discriminator -- the writers tag a tool card with
#: its role (``history_consolidation._TOOL_ROLES`` is the same set) and the
#: presentation class is not persisted at all, so ``role`` is the only field that
#: separates the two. A tool row's content is a glyph plus the tool title, so a
#: protocol word inside one is quoted text: 87 of 2,590 measured tool rows carry
#: one. Excluding them costs no error signal either -- across those same
#: transcripts every ``initialize timed out`` / stall-watchdog / throttle line
#: landed on an ``error``, ``assistant``, ``inject``, ``user`` or ``nudge`` row
#: and not one landed on a tool row.
TOOL_ROLES = frozenset({"tool", "tool_call", "tool_result"})

#: The roles the SESSION itself produces: its own messages and its tool activity.
#: Everything else in a transcript arrives from outside -- ``nudge`` and
#: ``inject`` recovery notices, ``user`` turns, the metadata header -- and must
#: not count as the session having spoken. Byte needles rather than parsed rows
#: because the index is counted over the whole file, and the role field is spelled
#: the way this package's writers emit it (``json.dumps`` default separators).
#:
#: Anchored at a LINE boundary, so only a row's own opening can be counted and
#: never anything inside another row's content. Valid JSONL already prevents the
#: obvious version of that -- ``json.dumps`` escapes the inner quotes, so a
#: message quoting ``{"role": "assistant"`` is stored as ``{\"role\": ...`` and
#: does not match -- but the anchor makes the count correct without depending on
#: that argument, and covers a torn line at the window edge too.
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
#: ``GREEN`` is the member that matters most, and a set without it covers only
#: the rare cases: ``GREEN`` is the literal exit condition in every worker's
#: contract ("report GREEN and stop"), so omitting it ages the fleet's most
#: common terminal state into IDLE and has the conductor nudge workers that
#: already delivered -- the exact harm this set exists to remove. ``PR`` is
#: deliberately NOT here: opening a pull request is a milestone the work
#: continues past, and a worker that has only reported ``PR`` still owes the
#: conductor a green.
TERMINAL_TAGS = frozenset({"GREEN", "STANDDOWN", "PROPOSAL"})

#: Reports that keep their meaning until the conductor ACTS on them.
#:
#: A probe samples; it does not subscribe. So it can only ever see a session's
#: newest message, and a state that was overwritten between two samples is not
#: suppressed or deferred -- it is never observed at all. ``BLOCKED`` is exactly
#: the state that gets overwritten, because the protocol requires a blocked
#: worker to keep reporting status, so its own next message displaces the only
#: place a sampling probe looks. The debt then exists on both sides and is
#: visible to neither: the worker holds position waiting for a ruling, and the
#: conductor never learned it owes one.
#:
#: Sticky is what survives the sample. A sticky report is not cleared by a
#: heartbeat or by unprefixed text; only another real report clears it, and the
#: conductor's own act of delivering the ruling (``--mark-handled``) is what
#: quiets it.
#:
#: Deliberately NOT merged with ``TERMINAL_TAGS``: ``TERMINAL`` means close me,
#: sticky ``BLOCKED`` means a ruling is owed. Those call for opposite actions, so
#: collapsing them would trade one invisible obligation for a wrong one.
#:
#: KNOWN BOUND, stated rather than left to be discovered: stickiness reaches only
#: as far back as the parse window. A blocked worker that heartbeats long enough
#: eventually pushes its own report past ``tail_bytes`` (200 KB), and the ruling
#: debt goes invisible again -- the same loss class this fixes, deferred rather
#: than closed. Raising ``tail_bytes`` buys proportionally more time; making it
#: unconditional would mean parsing every transcript in full on every cycle.
#:
#: The durable handled set cannot close it, which is worth spelling out because it
#: looks like it should. ``proto`` there is written ONLY by ``--mark-handled``, so
#: it exists exactly when the ruling has already been delivered -- and in that
#: case the signal is correctly suppressed by digest, so reading it would change
#: nothing. In the case the bound actually bites, an UNDISPOSITIONED report ageing
#: out, there is no mark and therefore no recorded tag to read. Sourcing
#: stickiness from the state file would either be a no-op or re-fire answered
#: rulings forever, so the transcript stays the source of truth and the bound
#: stays honest.
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


def _atomic_write(path: Path, payload: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, path)
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
    means the session has not spoken, whether or not a turn is open, and that is
    the one thing a self-deadlocked worker cannot fake. Two properties follow,
    and each of them is a way the obvious implementation gets it wrong.

    It counts from the START of the file, not the start of the window. A
    window-relative count saturates the moment a transcript passes ``tail_bytes``
    (200 KB by default) and then stays frozen while the session talks -- reading,
    at exactly the sizes real worker sessions reach, as the deadlock it exists to
    detect.

    It counts only rows the session PRODUCED -- its own messages and its tool
    activity -- never inbound ones. A transcript holds nudges, injected recovery
    notices and user turns as well, so counting every row means the conductor's
    OWN nudge advances the index of the worker it just nudged, and a wedged
    session reads as progress precisely when the conductor pokes it. Counting
    tool rows IS deliberate: a session running tools is working even while it is
    silent.

    Both halves are byte-level counts over bytes this read already had in hand,
    so neither costs a second pass or a JSON parse of the prefix. The needle is
    the role field as this package's writers emit it (``json.dumps`` default
    separators); a writer that changed that spelling would understate the index,
    which is why the round-trip test pins it against the real writer rather than
    a fixture.

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

    Classification reads only what the session SAID -- tool rows are dropped
    first, so neither half can be driven by tool text. The tag half already
    looked at ``role == "assistant"`` alone, but the error half read the last
    entry of ANY role, and a tool row is the last row on roughly one transcript
    in ten (6 of 60 measured), so an error pattern quoted in a tool title used
    to raise ERR on a healthy worker.

    The tag is the newest REPORT rather than the newest message, and a sticky
    report outlives the messages that follow it. Reading only the newest message
    makes a sampling probe structurally unable to see a state its own protocol
    guarantees will be overwritten -- see ``STICKY_TAGS``. The returned tail is
    the sticky report's OWN text, which is what keeps its digest stable while the
    worker goes on filing heartbeats: the signal fires once, is suppressed by the
    ruling, and re-fires only if the worker files a genuinely new one.

    ERR still takes precedence, because an errored session must be resumed before
    anything it said can be acted on. That defers a sticky report by one cycle at
    most: the ERR is marked, the tail is unchanged, and the sticky report is what
    the next probe classifies.
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

    Used for the delivery counters (2c), which are per-session facts, not
    per-tag ones: a session can be counted as undelivered while its tag is
    something else entirely, which is the whole point -- load and memory read
    healthy while the fleet cannot deliver.

    Tool rows are skipped for the same reason ``_classify`` skips them, and the
    measurement backs it: over the 60 most recent transcripts on the development
    host, every initialize-timeout and stall-watchdog line landed on an
    ``error``, ``assistant``, ``inject``, ``user`` or ``nudge`` row and not one
    landed on a tool row. The whole window is scanned, not just the last row --
    a watchdog notice is followed by whatever the session did next, so reading
    only the final row would miss nearly all of them.

    "Currently" is load-bearing, and the walk goes NEWEST first for it. Scanning
    the window for ANY historical match counts a failure the session has since
    recovered from, and because the window is 200 KB one healed init-timeout keeps
    that session in the undelivered column until it scrolls out. A counter that
    only ratchets up stops being an admission instrument and becomes a permanent
    accusation, so the walk stops at the first protocol report: a session that has
    filed a report since the notice evidently got a turn through, whatever else is
    wrong with it. Only a match reached BEFORE any report is outstanding.
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

    Read out of the single ``settled`` record, which every mark either sets or
    carries forward, so a later non-payload disposition (an ``IDLE`` nudge, an
    ``ERR``, a ``GONE`` reclaim) cannot erase it. Without that, a finished worker
    read identically to a wedged one: the handled set keeps one entry per key,
    so the terminal report was overwritten by the very next tag.

    None on a state file written before the field existed. The shipped shape is
    ``{tag, digest, ts}``, and the first mark after an upgrade recovers a terminal
    reading from the entry's own tag when that tag is a payload -- which is the
    only recovery available, because the older writer had already overwritten any
    earlier report. That is the loss this field exists to stop, so it cannot also
    be undone retroactively.
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

    This is the comparison the index exists for, done HERE rather than delegated.
    Printing ``i=`` and expecting someone to diff two cycles by eye is a decision
    living in prose: nothing enforces it, so it may simply never happen. The probe
    holds both numbers, so it can answer the question instead of posing it.

    The recorded side is the index stored at the last ``--mark-handled``, which
    makes the claim precise and useful: not "quiet for a while" but "has emitted
    no message and run no tool since you last acted on this session". That is why
    it needs no new write and does not weaken the one-writer rule -- the only
    write is still the mark.

    The disposition must also be at least one idle budget old. Without that the
    tag fires on the cycle immediately after EVERY mark, since a session that just
    filed a report and was acted on has, trivially, produced nothing in the
    seconds since -- which would put a spurious line on every watched key every
    cycle and bury the real ones.

    A key with no recorded index has no prior observation to compare against, so
    it cannot be stalled yet. Absent on state written before ``index`` existed,
    which reads the same way.

    BOUND -- the identity is a COUNT, so it is only monotonic while the transcript
    only grows. If a runtime is introduced that truncates or rotates a transcript
    in place, the count restarts and can land on a value already recorded here,
    and this reads a re-started session as one that has produced nothing. Nothing
    in the current writers does that -- transcripts are append-only JSONL and the
    reader takes a tail -- so the count is sufficient today and a rotation counter
    would be state carried for a case that does not exist. The condition to watch
    for is the arrival of a writer that reuses a transcript path; that is when the
    identity needs a generation as well as a position.
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
