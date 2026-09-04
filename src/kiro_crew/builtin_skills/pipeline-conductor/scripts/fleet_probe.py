#!/usr/bin/env python3
"""Batch fleet probe — the deterministic half of the pipeline conductor's patrol.

One invocation answers, for every watched worker session, "does anything need
judgment this cycle?" — plus host posture — so a quiet patrol cycle costs one
script call and a couple of output lines instead of N transcript reads.

Usage:
    python3 fleet_probe.py --config <probe-config.json>
    python3 fleet_probe.py --config <probe-config.json> --mark-handled KEY TAG DIGEST

Config (JSON):
    {
      "sessions": ["dashboard_chat-601-1788099254", ...],   # slot keys to watch
      "idle_alert_secs": 900,      # silent longer than this -> IDLE alert
      "tail_bytes": 200000,        # per-session transcript PARSE cap
      "load_alert_per_cpu": 1.5,   # 1-min loadavg / cpu above this -> hot
      "err_res": [...],            # extra error-tail regexes (optional)
      "banned_process_res": [...], # cmdline regexes for banned ops (optional)
      "init_timeout_res": [...],   # initialize-timeout tails (optional)
      "watchdog_res": [...],       # turn-ended-by-stall-watchdog tails (optional)
      "fleet_worktrees": [...]     # absolute roots this fleet owns (optional)
    }

Every regex key is validated at load time: a bad pattern is malformed config
(exit 2 with the offending pattern), never a crash mid-cycle. ``tail_bytes``
caps how much of a transcript is PARSED, not how much is read -- the whole file
is read either way, which is what makes the tail index monotonic for free.

Paths are DERIVED, never configurable -- the same containment rule for every
location this script touches, because its config is authored by a no-write
agent and a config-chosen path would quietly widen what an approved run can
reach:

  * transcripts are read from ``<data home>/sessions`` only, where the data
    home is ``$KIROCREW_HOME`` (else ``~/.kiro/crew``) -- the gateway this
    conductor belongs to, not an arbitrary directory;
  * the handled-set state file is ALWAYS ``<config path>.state.json``;
  * the banned-process scan reads ``/proc`` (``$KIROCREW_PROBE_PROC_ROOT``
    exists for the test harness).

A session key that does not match a transcript stem directly is also tried as
``dashboard_<key>`` (and with ``:`` as ``_``): ``session_create`` answers slot
keys while the store prefixes the surface, and a raw key must not read as a
missing session -- GONE triggers reclaim, and a false GONE is how an active
item gets duplicate-dispatched.

The data home is ``$KIROCREW_HOME`` when set, else ``~/.kiro/crew`` — the same
resolution every pipeline script uses.

Output (text, one line per FIRING signal; suppressed sessions print nothing):
    🔔 <session-key> <age>s <TAG> i=<index> d=<digest>

``i=`` counts the rows that SESSION PRODUCED -- its own messages and tool calls,
never an inbound nudge, inject or user row, because a supervisor's own nudge
landing in the file must not read as the worker making progress. It is a
monotonic per-session position counted from the start of the file, not from the
window, so it cannot saturate once a transcript passes ``tail_bytes``. An
unchanged count since the conductor last acted is *no progress*, whether or not
a turn is open, and that is the discriminator a self-deadlocked worker cannot
fake -- the probe makes that comparison itself and fires ``NOPROGRESS`` rather
than leaving two numbers for someone to diff. It is a position, so it carries no
transcript content.

Metadata only, by design: transcript-derived text never appears in the output,
so no private session content crosses into the caller's context whatever keys
the config watches. Content, when a ruling needs it, is read through the
workspace-authorized session tools.
    BANNED pid=<pid> rule=<regex> cwd=fleet|unknown
    OK <n> watched, <m> fired | load/cpu <x> (<posture>) | mem <G>G
       | banned <k> | foreign <k> | deliver init-timeout <a>, watchdog <b>

``banned`` counts fleet-owned matches only -- a banned command shape running in
an unrelated checkout on the same host is somebody else's business, and counting
it made the conductor stop a worker that was not the offender. Those are
summarised as ``foreign`` and not printed. ``deliver`` is the honest admission
instrument: load and memory can both read healthy while the fleet cannot
deliver, so sessions whose tail carries an initialize timeout or a
stall-watchdog turn end are counted every cycle, fired or not.

Tags: the worker protocol words (``WORKING/PR/GREEN/BLOCKED/STANDDOWN/
PROPOSAL``) -- recognised in their protocol form, ``<WORD>:``, so prose that
merely opens with one is not a report -- plus ``ERR`` for an error/throttle
tail, ``IDLE`` for silence past the threshold, ``TERMINAL`` for a session whose
last dispositioned report ENDED its assignment (``GREEN``/``STANDDOWN``/
``PROPOSAL``) and which has since written unprefixed text, ``NOPROGRESS`` for a
session that has produced nothing since the conductor last acted on it, and
``-`` when a tail carries no tag
(never fires on its own). Tool rows never classify: a protocol word or an error
phrase inside a tool card is quoted text, not a report.

The tag is the newest REPORT, not the newest message, because ``BLOCKED`` is
STICKY: a probe samples rather than subscribes, and the protocol requires a
blocked worker to keep reporting status, so its own next message would displace
the only thing a newest-message classifier reads. A heartbeat (``WORKING``) and
unprefixed text leave a sticky report standing; any other report supersedes it.
``TERMINAL`` and sticky ``BLOCKED`` are kept distinct on purpose -- one says
close me, the other says a ruling is owed.

Leading markdown decoration is stripped before the tag is matched, because the
match is anchored at position zero and ``**BLOCKED:**`` puts an asterisk there.
Emphasis, blockquote arrows, heading hashes and list markers all count. The
normalisation applies to the MATCHED text only: the digest is still computed over
what the worker wrote, and the anchor survives, so a bolded protocol word
mid-sentence is still not a report.

THE HANDLED SET replaces the overnight run's hand-grown ``grep -vE`` exclusion
pipe. Every fired line carries a ``d=<digest>`` field; ``--mark-handled KEY TAG
DIGEST`` records exactly that digest into the state file (compare-and-set: if
the tail moved on since the probe, the mark is REFUSED with exit 3 so the
caller re-probes instead of suppressing a payload nobody read). A signal is
then suppressed while (tag, digest) both still match. A new payload under the
same tag re-fires (a second GREEN with a new PR number is a new signal).
``IDLE`` marks expire after another ``idle_alert_secs``
so a nudged-but-still-silent worker re-alerts instead of vanishing.

A handled entry also records the tail ``index`` at the moment of the mark, and
the last dispositioned PAYLOAD report as ``settled`` (its tag AND digest) -- one
field, written on every mark and carried forward when the mark is not itself a
payload, so no later disposition can erase the fact that a report was already
answered. Neither field takes part in the digest, and a state file written before
they existed still suppresses exactly as it did; a legacy ``proto`` field, which
recorded only the tag, is still read so an in-place upgrade keeps its terminal
reading.

Deliberately boring properties, do not weaken:
  * No subprocess, ever. Reads transcripts, ``/proc`` and ``loadavg`` directly.
  * The only write is the probe's own state file, atomically, and only on
    ``--mark-handled``.
  * A per-session problem (unreadable file, malformed line) degrades that one
    row, never the cycle. Exit 0 when the probe ran; 2 on malformed config.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
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


#: Error shapes observed in real worker tails during the 2026-08-30 fleet run.
DEFAULT_ERR_RES = (
    r"Bedrock is throttling",
    r"dispatch failure",
    r"initialize timed out",
)

#: Banned-operation cmdline shapes: a pytest whose worker count nobody CHOSE,
#: and a bare full-suite vitest with no file arguments.
#:
#: "Nobody chose" is the honest statement of what this rule catches, and it is
#: not the same as "too many". This comment used to say a bare pytest forks one
#: worker per core because of the repo's ``-n auto`` addopts. That premise is
#: wrong: ``setup.cfg`` documents that ``auto`` is bounded by the rootdir
#: conftest's ``pytest_xdist_auto_num_workers`` hook, which sizes the pool by
#: available memory and by what concurrent runs on the host already hold, and
#: that "an explicit ``-n <N>`` bypasses the budget". So on THIS repo the
#: explicit spelling is the one that can outgrow the host, and ``auto`` is the
#: one that cannot.
#:
#: The rule's sense is deliberately left as it stands, because changing which
#: shapes it flags changes what the conductor stops mid-turn across a whole
#: fleet, and that is not a comment's decision to make. What it costs is stated
#: plainly instead: ``-n 4``, ``-n=4``, ``-n4``, ``-n0`` and
#: ``--numprocesses=4`` all read as bounded, ``-n auto`` and a bare pytest do
#: not. ``-n0`` is the repo's own documented override and is genuinely
#: in-process, so the safest form a worker can run is also a passing one.
DEFAULT_BANNED_RES = (
    r"\bpytest\b(?!.*(?:-n|--numprocesses)\s*=?\s*\d)",
    r"\bvitest\b\s+run\s*$",
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
#: ``GREEN`` is the one that matters most and was missing from the first version
#: of this set, which is worth recording because it made the fix cover only its
#: rare cases: ``GREEN`` is the literal exit condition in every worker's contract
#: ("report GREEN and stop"), so the most common terminal state in the fleet aged
#: into IDLE and the conductor nudged workers that had already delivered -- the
#: exact harm this set exists to remove. ``PR`` is deliberately NOT here: opening
#: a pull request is a milestone the work continues past, and a worker that has
#: only reported ``PR`` still owes the conductor a green.
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


def _norm_path(path: str) -> str:
    """A compare-ready spelling of *path*.

    Two spellings of one directory must not read as two directories, or a
    fleet-owned process is filed as somebody else's and its banned run goes
    unreported. Three normalisations, all of them load-bearing on Windows, where
    this ran green on Linux and misfiled every match:

    * the extended-length prefix. ``os.readlink`` can answer ``\\\\?\\D:\\...``,
      which no configured root will ever spell, so a literal prefix comparison
      fails on a path that does match.
    * case and separator. ``normcase`` folds both, since ``D:/a`` and ``d:\\a``
      are the same directory there and only one of them is what the config says.
    * short (8.3) names. Left to ``os.path.realpath`` in the caller's fallback,
      because expanding them requires touching the filesystem and this half must
      stay a pure string operation for the unreadable-cwd case.
    """
    if path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normcase(os.path.normpath(path))


def _under(child: str, root: str) -> bool:
    """Is *child* the directory *root* or inside it?

    The boundary test is a separator, not a bare prefix: without it a sibling
    worktree named ``wt-a-old`` is swallowed by ``wt-a`` and its runs are
    attributed to the wrong owner.
    """
    return child == root or child.startswith(root.rstrip(os.sep) + os.sep)


def _program_path(cmd: str) -> str:
    """The program path out of a joined cmdline, or ``""``.

    ``/proc/<pid>/cmdline`` is world-readable where the ``cwd`` and ``exe``
    symlinks are not -- both of those need the same access a debugger would, so
    they fail for another user's process while the cmdline still reads. That
    asymmetry is the only reason this fallback is worth having.

    Reading argv is not the same as PRINTING it. A secret can ride in an
    argument, so the command line is still never emitted -- but the program PATH
    is structural, so it can be compared for a decision and dropped. The next
    reader will otherwise assume argv was excluded from being read at all.
    """
    return cmd.split(" ", 1)[0] if cmd else ""


def _venv_root(program: str) -> str | None:
    """The virtualenv a program path belongs to, or None if it is not in one.

    A venv is created inside one checkout and belongs to it, so its interpreter
    path attributes the process. A system or shim interpreter attributes nothing:
    every checkout on the host shares it.
    """
    parent = os.path.dirname(os.path.dirname(program))
    if not parent:
        return None
    # bin/python on POSIX, Scripts/python.exe on Windows; pyvenv.cfg sits beside
    # both, so the marker is checked rather than either layout being assumed.
    return parent if os.path.isfile(os.path.join(parent, "pyvenv.cfg")) else None


def _program_class(cmd: str, fleet: list[str]) -> str:
    """The ownership class implied by the program path alone, for use when the
    cwd could not be read. See ``_owner_class`` for why the two directions of
    this comparison do not carry the same weight.

    Every uncertain answer is ``unknown``, never ``fleet``. In the conductor's
    action table ``fleet`` is the one class that STOPS a session while ``unknown``
    only re-injects the directive, so the safe direction for a wrong answer is
    toward not enforcing. A root that reaches here without being comparable
    therefore widens nothing.
    """
    if not fleet:
        return "unknown"
    program = _program_path(cmd)
    if not program:
        return "unknown"
    try:
        literal = _norm_path(program)
        real = _norm_path(os.path.realpath(program))
        for root in fleet:
            if _under(literal, _norm_path(root)) or _under(
                real, _norm_path(os.path.realpath(root))
            ):
                return "fleet"
        venv = _venv_root(program)
    except (OSError, ValueError):
        # ValueError is the embedded-NUL case that validation refuses at load
        # time; this is the belt to that braces, and it fails toward not stopping.
        return "unknown"
    if venv is None:
        # A system or shim interpreter. Every checkout on the host shares it, so
        # it says nothing about ownership and must not be read as a denial.
        return "unknown"
    return "foreign"


def _owner_class(proc_entry: Path, fleet: list[str], cmd: str = "") -> str:
    """``fleet``, ``foreign`` or ``unknown`` for the process at *proc_entry*.

    ``/proc/<pid>/cwd`` is a symlink to the working directory, so the link TARGET
    is the answer. When it cannot be read -- a process that exited between the
    scan and the read, or one owned by another user -- the program path is asked
    instead, because in both attributable BANNED lines observed in the field the
    cwd was the field that failed while the cmdline survived. A cwd-only
    classifier would have returned ``unknown`` for two processes that could be
    PROVEN not to be the fleet's, and an unknown match makes the conductor act.

    The two signals do NOT get the same authority, and the difference is load
    bearing. A program path UNDER a fleet worktree is conclusive: nothing outside
    that checkout runs its interpreter. A program path outside one is only
    conclusive when it is a venv interpreter, which belongs to whichever checkout
    created it. A system or shim interpreter attributes nothing -- and that is the
    fleet's own case, not a hypothetical: a fleet worktree here has no ``.venv``
    and its workers run a global ``python3`` shim, so treating "not under a fleet
    worktree" as ``foreign`` would classify a real banned run INSIDE the fleet as
    somebody else's and never print it. That is the exact harm 2d exists to
    prevent, so the non-match stays ``unknown``.

    An empty or unset ``fleet_worktrees`` declares no scope, and scoping against
    an empty set would classify every match as ``foreign`` and mute the banned
    signal entirely -- a failure the conductor cannot see. Unscoped therefore
    means ``unknown``: every match is still reported and still counted, which is
    exactly the pre-2d behaviour.

    The comparison gets a second chance through ``realpath`` because a match
    missed is a banned run inside the fleet reported as somebody else's: it
    absorbs a symlinked worktree root and a Windows short (8.3) name, either of
    which spells the same directory a way the literal form does not.
    """
    try:
        target = os.readlink(proc_entry / "cwd")
    except OSError:
        # Usually a process that exited mid-scan; on a shared host also every
        # OTHER user's process, whose /proc entry this uid cannot follow.
        #
        # Asking WHO owns it would let most of the second group be summarised as
        # `foreign` instead of reported as `unknown`, and that refinement was
        # tried and withdrawn. It needs the current uid, and this file is a
        # standalone script run by a bare interpreter -- it imports nothing from
        # the package, so it cannot route through ``platform_compat``, and
        # ``os.getuid`` is POSIX-only: absent on Windows, where its absence
        # raises AttributeError rather than OSError and takes the whole scan
        # down. The exchange is a fail-open reading for a crash on one platform,
        # to buy quieter output on a path that is already correct. The PROGRAM
        # path is a different trade: cmdline is world-readable, so it costs no
        # new primitive and no portability risk, and it is asked below.
        return _program_class(cmd, fleet)
    if not fleet:
        return "unknown"
    try:
        literal = _norm_path(target)
        if any(_under(literal, _norm_path(root)) for root in fleet):
            return "fleet"
        real = _norm_path(os.path.realpath(target))
        if any(_under(real, _norm_path(os.path.realpath(root))) for root in fleet):
            return "fleet"
    except (OSError, ValueError):
        # Uncertain ownership answers `unknown`, never `fleet`: `fleet` is the one
        # class that stops a session, so a comparison that cannot be completed
        # must not promote a process into it.
        return "unknown"
    return "foreign"


def _host_lines(cfg: dict[str, Any]) -> tuple[list[str], str]:
    """Banned-process lines plus the host summary fragment."""
    banned_res = [
        re.compile(rx) for rx in cfg.get("banned_process_res") or list(DEFAULT_BANNED_RES)
    ]
    fleet = [p for p in cfg.get("fleet_worktrees") or []]
    lines: list[str] = []
    # /proc, with an env seam for the test harness only -- not a config key,
    # for the same containment reason as the other paths.
    proc_root = Path(os.environ.get("KIROCREW_PROBE_PROC_ROOT") or "/proc")
    banned = 0
    foreign = 0
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmd = (
                    (entry / "cmdline")
                    .read_bytes()
                    .replace(b"\0", b" ")
                    .decode("utf-8", "replace")
                    .strip()
                )
            except OSError:
                continue
            if cmd:
                matched = next((rx.pattern for rx in banned_res if rx.search(cmd)), None)
                if matched is None:
                    continue
                # A banned SHAPE is only a banned OPERATION when the fleet owns
                # it. The same unbounded pytest run in an unrelated checkout is
                # this machine's business, and counting it made the conductor
                # stop a worker that was not the offender -- so the cwd decides
                # which counter it lands in, and only fleet-owned or unreadable
                # matches are printed at all.
                cwd_class = _owner_class(entry, fleet, cmd)
                if cwd_class == "foreign":
                    foreign += 1
                    continue
                banned += 1
                # pid + WHICH RULE fired + the cwd class is everything the
                # conductor needs (stop the owner, re-seed with the directive).
                # The argv is deliberately not echoed: a command line can carry
                # credentials or presigned URLs, and this line lands in the
                # conductor's model context.
                lines.append(f"BANNED pid={entry.name} rule={matched} cwd={cwd_class}")
    per_cpu = None
    if hasattr(os, "getloadavg"):
        try:
            per_cpu = os.getloadavg()[0] / max(os.cpu_count() or 1, 1)
        except OSError:
            per_cpu = None
    mem_gb = None
    meminfo = proc_root / "meminfo"
    try:
        for line in meminfo.read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                mem_gb = int(line.split()[1]) / 1_048_576
                break
    except (OSError, ValueError, IndexError):
        mem_gb = None
    hot = per_cpu is not None and per_cpu > float(cfg.get("load_alert_per_cpu", 1.5))
    load_part = (
        f"load/cpu {per_cpu:.2f} ({'hot' if hot else 'ok'})"
        if per_cpu is not None
        else "load/cpu n/a"
    )
    mem_part = f"mem {mem_gb:.0f}G" if mem_gb is not None else "mem n/a"
    return lines, f"{load_part} | {mem_part} | banned {banned} | foreign {foreign}"


def _sessions_dir() -> Path:
    """DERIVED, never configurable: this gateway's own session store."""
    return data_home() / "sessions"


def _transcript_path(sessions_dir: Path, key: str) -> Path | None:
    """The transcript file for ``key``, or None when no safe transcript exists.

    ``session_create`` answers a slot key while the store prefixes the surface
    (``dashboard_<slot>.jsonl``) and colon-form session keys use ``:`` where
    the filename uses ``_``. A raw key must not read as a missing session:
    GONE triggers reclaim, and a false GONE is how an active item gets
    duplicate-dispatched. The first SAFE existing candidate wins.

    Keys are validated against ``_KEY_RE`` before this is called, and an
    existing candidate is returned only if it resolves to a file directly
    under ``sessions_dir`` -- both halves of one rule: a key is a filename
    stem, never a path. A candidate that exists but resolves elsewhere (a
    symlink out of the store) is treated as MISSING, never returned: None is
    the answer, and None reads as GONE.
    """
    candidates = (key, f"dashboard_{key}", key.replace(":", "_"))
    root = sessions_dir.resolve()
    for candidate in candidates:
        path = sessions_dir / f"{candidate}.jsonl"
        if path.exists() and path.resolve().parent == root:
            return path
    return None


def _handled_of(state: dict[str, Any]) -> dict[str, Any]:
    """The handled map, tolerating a corrupted state file: anything that is
    not a dict reads as empty (worst case a handled signal re-fires once),
    never as a crashed patrol."""
    handled = state.get("handled")
    return handled if isinstance(handled, dict) else {}


def run_probe(cfg: dict[str, Any], state_path: Path) -> int:
    sessions: list[str] = list(cfg.get("sessions") or [])
    sessions_dir = _sessions_dir()
    idle_secs = int(cfg.get("idle_alert_secs", 900))
    tail_bytes = int(cfg.get("tail_bytes", 200_000))
    err_res = [re.compile(rx) for rx in (list(DEFAULT_ERR_RES) + list(cfg.get("err_res") or []))]
    init_res = [
        re.compile(rx) for rx in cfg.get("init_timeout_res") or list(DEFAULT_INIT_TIMEOUT_RES)
    ]
    watchdog_res = [re.compile(rx) for rx in cfg.get("watchdog_res") or list(DEFAULT_WATCHDOG_RES)]
    handled = _handled_of(_load_state(state_path))

    fired = 0
    init_timeouts = 0
    watchdogs = 0
    for key in sessions:
        path = _transcript_path(sessions_dir, key)
        age: int | None = None
        if path is not None:
            try:
                age = int(time.time() - path.stat().st_mtime)
            except OSError:
                age = None
        if path is None or age is None:
            # GONE flows through the same suppression as every other tag: an
            # acted-on GONE (item reclaimed, mark-handled) must not re-fire
            # every cycle until the key is dropped from the watch list.
            tag, tail, age_text, index = "GONE", "transcript missing", "?", None
        else:
            entries, index = _tail_entries(path, tail_bytes)
            tag, tail = _classify(entries, err_res)
            # Counted for every watched session, fired or not: an undelivered
            # session is a fleet fact, not a per-tag one.
            init_timeouts += 1 if _tail_matches(entries, init_res) else 0
            watchdogs += 1 if _tail_matches(entries, watchdog_res) else 0
            if tag not in _FIRING:
                # A worker that filed a terminal report and then wrote one
                # unprefixed line is FINISHED. Ageing it into IDLE says the
                # opposite, and the two readings call for opposite actions
                # (close the item vs. nudge or reclaim it), so TERMINAL takes
                # precedence over the clock.
                #
                # ``tag == "-"`` is load-bearing: the non-firing set holds BOTH
                # ``-`` and ``WORKING``, so without it a worker that stood down,
                # was re-seeded, and is now reporting ``WORKING:`` would read as
                # finished and have its live work closed. WORKING is a protocol
                # message and means active work; only an unprefixed tail can
                # inherit a terminal disposition. A WORKING tail that then goes
                # silent still ages into IDLE, which is the correct nudge.
                if tag == "-" and _recorded_proto(handled, key) in TERMINAL_TAGS:
                    tag = TERMINAL_TAG
                elif age > idle_secs:
                    tag = IDLE_TAG
                elif _stalled_since_disposition(handled, key, index, idle_secs):
                    # Ranked BELOW the clock deliberately. A cold transcript is
                    # already fully described by IDLE, whose action -- nudge, then
                    # the intervention ladder -- is the right one. What IDLE
                    # cannot see is the session held WARM by traffic it never
                    # answers: inbound nudges keep the mtime fresh while nothing
                    # comes out. That is the case this tag exists for, and its
                    # action is different: check the EFFECT rather than liveness.
                    tag = NOPROGRESS_TAG
            if tag not in _FIRING:
                continue
            age_text = str(age)
        digest = _digest(f"{tag}:{tail}")
        if _suppressed(handled, key, tag, digest, idle_secs):
            # An answered ERR must not bury a ruling nobody has answered. The
            # error branch outranks the sticky walk, which is right on the first
            # cycle -- a crashing session is the more urgent reading -- but the
            # error row stays LAST for as long as the session is wedged, so the
            # ERR is re-classified and re-suppressed every cycle and the BLOCKED
            # underneath it is never reached. The deferral this was documented as
            # costing (one cycle) was in fact unbounded.
            surfaced = False
            if tag == "ERR":
                pending = _sticky_pending(entries)
                if pending is not None:
                    sticky_digest = _digest(f"{pending[0]}:{pending[1]}")
                    if not _suppressed(handled, key, pending[0], sticky_digest, idle_secs):
                        tag, digest = pending[0], sticky_digest
                        surfaced = True
            if not surfaced:
                # The report itself is dealt with. Whether anything has come OUT
                # of the session since is a different question, and the
                # interesting case for it is exactly here: the ruling was
                # delivered, the tag went quiet, and the worker then produced
                # nothing at all. Without this the named tag masks the stall for
                # as long as it stays suppressed.
                # A finished worker produces nothing BY DEFINITION, so absence of
                # output is not a stall. This guard is separate from the
                # terminal/idle ladder above because that ladder is only reached
                # for a NON-firing tag: a delivered worker whose `GREEN:` is still
                # the newest row in the window keeps a firing tag, so it arrives
                # here instead and would be reclassified as stalled -- then re-fire
                # every cycle, since this tag expires. That is the harm TERMINAL
                # exists to remove, so it is refused on both paths.
                if (
                    tag == NOPROGRESS_TAG
                    or _recorded_proto(handled, key) in TERMINAL_TAGS
                    or not _stalled_since_disposition(handled, key, index, idle_secs)
                ):
                    continue
                tag = NOPROGRESS_TAG
                digest = _digest(f"{tag}:{tail}")
                if _suppressed(handled, key, tag, digest, idle_secs):
                    continue
        fired += 1
        # Metadata ONLY: key, age, tag, index, digest. Transcript-derived text is
        # deliberately never printed -- the conductor's action table is
        # tag-keyed, and content, when a ruling needs it, is read through the
        # workspace-authorized session tools, not through this script. That
        # keeps the probe's output free of private session text no matter
        # which keys an (agent-authored) config watches. The index is a line
        # POSITION, so it carries no content either.
        index_text = "?" if index is None else str(index)
        print(f"🔔 {key:<28} {age_text:>5}s {tag:<9} i={index_text} d={digest}")

    banned_lines, host = _host_lines(cfg)
    for line in banned_lines:
        print(line)
    print(
        f"OK {len(sessions)} watched, {fired} fired | {host} | "
        f"deliver init-timeout {init_timeouts}, watchdog {watchdogs}"
    )
    return 0


def mark_handled(cfg: dict[str, Any], state_path: Path, key: str, tag: str, digest: str) -> int:
    if not _KEY_RE.fullmatch(key):
        print(f"malformed key {key!r}: keys are stems, never paths", file=sys.stderr)
        return 2
    tail_bytes = int(cfg.get("tail_bytes", 200_000))
    err_res = [re.compile(rx) for rx in (list(DEFAULT_ERR_RES) + list(cfg.get("err_res") or []))]
    path = _transcript_path(_sessions_dir(), key)
    index: int | None = None
    if path is not None and path.exists():
        entries, index = _tail_entries(path, tail_bytes)
        current_tag, tail = _classify(entries, err_res)
        # A signal the conductor cannot mark is worse than no signal at all. When
        # the probe reaches PAST a suppressed ERR to surface the sticky ruling
        # underneath it, the digest it prints is over the RULING -- but the error
        # row is still last here, so classifying again yields the ERR payload and
        # the compare-and-set below refuses the mark. The ruling would then fire
        # every cycle, undismissable. Resolving the same payload the probe printed
        # keeps the two halves of the protocol agreeing on what is being marked.
        if tag != current_tag and tag in STICKY_TAGS:
            pending = _sticky_pending(entries)
            if pending is not None and pending[0] == tag:
                tail = pending[1]
        del current_tag  # the digest is keyed on the CALLER's tag, like the probe's
    else:
        tail = "transcript missing"  # mirror run_probe's GONE payload exactly
    current = _digest(f"{tag}:{tail}")
    if current != digest:
        # Compare-and-set: a new same-tag payload arrived between the probe
        # and this mark. Digesting what is there NOW would suppress a signal
        # nobody has read -- refuse, so the caller re-probes and acts on the
        # payload that actually exists.
        print(
            f"refused: {key} payload changed since the probe (re-probe and act on it)",
            file=sys.stderr,
        )
        return 3
    state = _load_state(state_path)
    handled = _handled_of(state)
    state["handled"] = handled
    entry: dict[str, Any] = {
        "tag": tag,
        "digest": current,
        "ts": int(time.time()),
    }
    if index is not None:
        entry["index"] = index
    # The last dispositioned PROTOCOL tag survives a later non-protocol
    # disposition, because "this worker filed a terminal report" and "this
    # worker went quiet" are different facts and the second must not erase the
    # first. Carried forward from the previous entry when this mark is not
    # itself a protocol tag.
    # ONE field records the last payload disposition, and it is written on EVERY
    # mark: set when this mark IS a payload, carried forward when it is not.
    #
    # An earlier version wrote two fields for this -- `proto` for the terminal
    # reading and `settled` for the suppression -- and gated the second on the
    # condition tags alone. That left the same data loss reachable one door down:
    # an `ERR` disposition on a session whose `BLOCKED` was answered overwrote the
    # answer, and once a heartbeat stopped the error row being last, the answered
    # ruling presented again. The rule is not "condition marks preserve payloads"
    # but "a mark that is not itself a payload cannot erase one", so the carry is
    # unconditional and the two fields collapse into this one.
    previous = handled.get(key)
    previous = previous if isinstance(previous, dict) else {}
    prior_tag, prior_digest = previous.get("tag"), previous.get("digest")
    if tag in _PAYLOAD_TAGS:
        entry["settled"] = {"tag": tag, "digest": digest}
    elif (
        isinstance(prior_tag, str) and prior_tag in _PAYLOAD_TAGS and isinstance(prior_digest, str)
    ):
        # The previous mark WAS the payload. Reading it off the entry keeps both
        # halves, which matters for a state file written before ``settled``
        # existed: that shape records an answered payload as the entry itself, and
        # suppression needs the digest as well as the tag. Carrying only the
        # legacy tag presented every answered ruling again on the first cycle
        # after an upgrade.
        entry["settled"] = {"tag": prior_tag, "digest": prior_digest}
    elif isinstance(previous.get("settled"), dict):
        entry["settled"] = previous["settled"]
    handled[key] = entry
    state["updated_at"] = int(time.time())
    _atomic_write(state_path, json.dumps(state, indent=1, sort_keys=True) + "\n")
    print(f"handled {key} {tag}")
    return 0


def _config_error(cfg: dict[str, Any]) -> str | None:
    """The first problem with a parsed config, or None. Typed misconfiguration
    is malformed config (exit 2 with a message), never an uncaught crash."""
    for key in (
        "sessions",
        "err_res",
        "banned_process_res",
        "init_timeout_res",
        "watchdog_res",
        "fleet_worktrees",
    ):
        value = cfg.get(key)
        if value is not None and (
            not isinstance(value, list) or any(not isinstance(item, str) for item in value)
        ):
            return f"{key} must be a list of strings"
    for item in cfg.get("sessions") or []:
        if not _KEY_RE.fullmatch(item):
            return f"session key {item!r} is not a plain key (keys are stems, never paths)"
    # A relative worktree root would be compared against an absolute
    # /proc/<pid>/cwd target and could never match, so every banned run inside
    # the fleet would be filed as foreign and go unreported. Say so at load time
    # rather than silently muting the signal.
    for item in cfg.get("fleet_worktrees") or []:
        if "\0" in item:
            # A NUL can never appear in a real path, so this entry could only ever
            # fail to match -- and it fails LOUDLY: the path calls raise
            # ValueError, not OSError, so it escapes the scan's exit-race handling
            # and takes the whole cycle down, losing every other session's reading
            # with it. Same reasoning as the relative-path check below: an entry
            # that cannot match is malformed config, said at load time.
            return "fleet_worktrees entry contains a NUL byte"
        if not os.path.isabs(item):
            return f"fleet_worktrees entry {item!r} must be an absolute path"
        # A root that matches EVERYTHING is the dangerous direction, and this key
        # is read from a config an agent authors -- so it is a trust boundary, not
        # a typo class. ``cwd=fleet`` is the one class that stops a session, so a
        # root of ``/`` turns the ownership guard into a false-stop generator
        # against unrelated processes on the same host: precisely the harm that
        # scoping the scan was introduced to prevent, reintroduced through config.
        # Refused rather than narrowed, because silently ignoring an entry would
        # leave the conductor believing a scope it does not have.
        norm = _norm_path(item)
        store = _norm_path(str(_sessions_dir()))
        # BOTH spellings are judged, because the classifier compares both. Its
        # realpath second chance exists so a symlinked worktree still matches --
        # which means a root spelled as a symlink to `/` passes a literal-only
        # check and then matches every cwd on the host. The widening comes back
        # through the door the convenience opened, so validation follows it.
        try:
            candidates = {norm, _norm_path(os.path.realpath(item))}
        except (OSError, ValueError):
            return f"fleet_worktrees entry {item!r} cannot be resolved"
        for candidate in candidates:
            if candidate == _norm_path(os.path.dirname(candidate) or candidate):
                return (
                    f"fleet_worktrees entry {item!r} resolves to a filesystem root "
                    "and matches everything"
                )
            if _under(store, candidate):
                # One level up from the same widening: the session store is the
                # conductor's own data directory, never a worktree, so a root that
                # contains it makes the conductor and every sibling process read as
                # a fleet worker eligible to be stopped.
                return f"fleet_worktrees entry {item!r} contains the session store"
    for key in ("idle_alert_secs", "tail_bytes", "load_alert_per_cpu"):
        value = cfg.get(key)
        if value is None:
            continue
        # bool is an int subclass, and JSON permits NaN/Infinity: neither is a
        # usable threshold, and int(NaN) raises -- reject both up front.
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value != value
            or value in (float("inf"), float("-inf"))
            or value < 0
        ):
            return f"{key} must be a finite non-negative number"
    for rx in (
        list(cfg.get("err_res") or [])
        + list(cfg.get("banned_process_res") or [])
        + list(cfg.get("init_timeout_res") or [])
        + list(cfg.get("watchdog_res") or [])
    ):
        try:
            re.compile(rx)
        except re.error as exc:
            return f"bad regex {rx!r}: {exc}"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument(
        "--mark-handled",
        nargs=3,
        metavar=("KEY", "TAG", "DIGEST"),
        help="record the fired signal as handled; DIGEST is the d= field of the"
        " fired line, and a stale digest is refused (exit 3)",
    )
    args = parser.parse_args(argv)
    config_path = Path(args.config)
    try:
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("config must be a JSON object")
    except (OSError, ValueError) as exc:
        print(f"malformed config: {exc}", file=sys.stderr)
        return 2
    problem = _config_error(cfg)
    if problem is not None:
        print(f"malformed config: {problem}", file=sys.stderr)
        return 2
    # Derived, never configurable -- see the module docstring: a config-chosen
    # destination would make this no-write agent's one approved writer an
    # arbitrary-path file replacer.
    state_path = Path(f"{config_path}.state.json")
    if args.mark_handled:
        return mark_handled(cfg, state_path, *args.mark_handled)
    return run_probe(cfg, state_path)


if __name__ == "__main__":
    sys.exit(main())
