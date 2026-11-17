"""What a session delivered, set beside what it cost.

The Spend tab ranks conversations by credits. This module reads the same
conversations' own transcripts for the outcomes a person weighs that spend
against: commits, changed lines and pull requests, and how many of those pull
requests were merged. Nothing here runs git or calls a provider. Every figure
is read from text the gateway already holds. A tool row's command is the
``command`` argument the call was made with, or its title when it has none:

* a **commit** is counted when a shell tool row ran ``git commit`` and git's own
  summary line (``[branch abc1234] subject``) is in that row's output, with the
  ``N insertions(+), M deletions(-)`` line that follows it giving the changed
  lines. A quiet commit (``-q``) prints no summary line, so it is not counted:
  what else the row printed cannot say which commit, if any, this call made. An
  amend, a failed commit, a preview (``--dry-run`` and the output formats that
  imply it) and any other output are not counted, nor is a command left with a
  quote open, whose end cannot be read. Commands are read by the shared shell
  reader, so quoting is the shell's and a here-document body is data;
* a **pull request** is counted when a shell tool row ran ``gh pr create`` or
  ``glab mr create`` and its output carries the new change's URL on a line of
  its own, parsed by the same validator the session chips use. An output that
  says the pull request already exists names one this call did not create, so
  it counts nothing;
* a pull request is **merged** when the gateway's cached chip status for that URL
  says so. That cache is read, never refreshed, so a pull request nobody has
  looked at yet is "unchecked", not "open".

Rows come through the transcript's derivation seam: an incognito or temporary
session, or one whose line cannot be read, gives no figure at all, and neither does
one whose older transcripts of the same tab include such a line. The response asks
again, through ``publication_hold``, about every chain that gave a figure, and
holds all of them until its bytes are built, so a session tightened after its
scan is dropped and none can be tightened while the answer is being built.

Every rule above fails toward counting less. A figure here is never a guess that
something happened.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from typing import Any

from aiohttp import web

from kiro_crew.dashboard.chat_utils import slot_transcript_key
from kiro_crew.dashboard.handlers import usage as _usage
from kiro_crew.dashboard.handlers.source_providers import (
    get_cached_check_status,
    is_owner_dashboard_request,
    parse_source_url,
)
from kiro_crew.history_derivation import (
    TranscriptBusy,
    TranscriptWithheld,
    transcript_withholds_derivation,
)
from kiro_crew.history_files import transcript_lock_stems
from kiro_crew.security import shell_commands as _shell_commands
from kiro_crew.security import shell_normalizer as _shell
from kiro_crew.tool_call_title import parse_tool_args

logger = logging.getLogger(__name__)

# How many of the costliest conversations are read. The rows arrive ranked by
# credits, so the cut drops the cheapest first. Reading is bounded work on every
# poll of the Spend tab, not a scan of every transcript on disk.
OUTCOME_TOP_CONVOS = 50

# A changed transcript is read again at most this often. The costliest sessions
# are the live ones, so their files change between polls of the Spend tab (every
# few seconds), and a long session's transcript runs to hundreds of megabytes:
# rereading it on every poll would hold a worker thread for seconds each time.
_REREAD_SECS = 60.0

# A tool row's output is read up to here, and no further: the summary lines this
# looks for open an output, and a pasted log should not set the cost of a poll.
_OUTPUT_READ_CHARS = 65536
_OUTPUT_READ_LINES = 400
# Room for one commit's summary and the statistics line that follows it.
_COMMIT_CHUNK_CHARS = 4000
# A command is scanned a character at a time; a longer one (a file written through
# a here-document) is not read and counts nothing.
_COMMAND_READ_CHARS = 16384

# What ends one simple command and starts the next.
_PIPE = "|"
_AND = "&&"
# ``git`` options placed before the subcommand that take the next word as their value.
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
# ``git commit`` short options whose value is the rest of the word, or the next word.
_COMMIT_VALUE_LETTERS = frozenset("mFCct")
# Short options whose value, when given, is attached: the rest of the word.
_COMMIT_ATTACHED_LETTERS = frozenset("Su")
# Long options that take the next word as their value when given no ``=``.
_COMMIT_VALUE_LONG = frozenset(
    "message file author date reuse-message reedit-message fixup squash template"
    " cleanup trailer pathspec-from-file".split()
)
# Options that make ``git commit`` only show what it would commit: ``--dry-run``
# and the output formats git documents as implying it (``-z`` implies
# ``--porcelain``). Git takes any unambiguous prefix of a long option, and an
# ambiguous one fails, so every prefix of these names makes no commit.
_PREVIEW_LONG = ("dry-run", "short", "porcelain", "long", "null")
_PREVIEW_LETTER = "z"
_HEX = frozenset("0123456789abcdef")
_DIGITS = frozenset("0123456789")
_INSERTED = ("insertion(+)", "insertions(+)")
_DELETED = ("deletion(-)", "deletions(-)")

# (kind, epoch, a, b, key): ``("commit", ts, added, removed, sha)`` or
# ``("pr", ts, 0, 0, identity)``.
Event = tuple[str, float, int, int, str]

_MEMO_MAX = 128
# A transcript's events are held only up to this many, and all entries together up
# to the total, the oldest entry going first; a transcript with more is read again
# on each poll instead of being held.
_MEMO_EVENTS_MAX = 2048
_MEMO_EVENTS_TOTAL = 16384
# An entry keeps the chain of transcripts it was read from, so a hit can ask each
# of them the withheld question. A longer chain (a tab whose transcripts all share
# one id) is not held: every poll reads it again through the derivation seam.
_MEMO_CHAIN_MAX = 64
# The most transcripts one response holds at once. Every chain that gave an
# outcome is held while the response is built, so this bounds the locks a poll
# takes; a response over it sends no outcome rather than hold an unbounded set.
_PUBLICATION_HOLD_MAX = 256
# Transcript key -> (file mtime, when it was read, its events, every transcript the
# chained read covered).
_memo: OrderedDict[str, tuple[float, float, tuple[Event, ...], tuple[str, ...]]] = OrderedDict()
_memo_lock = threading.Lock()


def _command_of(row: dict, meta: dict) -> str | None:
    """What a tool row ran: the ``command`` argument of the call, else its title.

    A long shell title can arrive cut short, which hides an ``--amend`` or what
    follows the commit; the call's own arguments hold the whole command.
    """
    args = parse_tool_args(meta.get("input"))
    command = args.get("command") if args else None
    if isinstance(command, str) and command.strip():
        return command
    title = row.get("content")
    return title if isinstance(title, str) else None


def _split_commands(command: str) -> tuple[list[tuple[list[str], str]], bool]:
    """*command* as its simple commands: each one's words and the separator after it.

    Read by the shared shell reader, :func:`security.shell_commands.read_commands`,
    with each command's words as the shell hands them to its program, the way the
    batch guard reads a command line. Quoting is the shell's, a redirection and its
    target are not words, and a here-document body is data. A command with no words
    (a newline after an operator, the ``&`` of ``|&``) is passed over, and a ``;`` or
    newline ending the last command joins nothing. The flag is False when a quote is
    left open, as in a title cut short.
    """
    text = _shell._fold_line_continuations(command) if "\\\n" in command else command
    read = _shell_commands.read_commands(text, nested=False, heredocs=True)
    commands: list[tuple[list[str], str]] = []
    for cmd in read:
        if cmd.argv:
            words = _shell._shell_tokens(" ".join(text[w.start : w.end] for w in cmd.argv))
            commands.append((words, cmd.separator))
    if commands and commands[-1][1] in (";", "\n"):
        commands[-1] = (commands[-1][0], "")
    tail = read[-1].steps
    return commands, not tail or tail[-1].state == 0


def _is_program(word: str, name: str) -> bool:
    return word == name or word.endswith("/" + name)


def _commit_call(words: list[str]) -> tuple[bool, bool] | None:
    """``(amend, preview)`` for a ``git commit`` call; None when *words* is not one.

    A preview (:data:`_PREVIEW_LONG`, ``-z``) makes no commit. Option values are
    skipped so a message is not read as options, and ``--`` ends the options.
    """
    start = next((k + 1 for k, w in enumerate(words) if _is_program(w, "git")), None)
    if start is None:
        return None
    sub = start
    while sub < len(words) and words[sub].startswith("-"):
        sub += 2 if words[sub] in _GIT_VALUE_OPTIONS else 1
    if sub >= len(words) or words[sub] != "commit":
        return None
    amend = preview = False
    rest = iter(words[sub + 1 :])
    for word in rest:
        if word == "--":
            break
        if word.startswith("--"):
            name, eq, _ = word[2:].partition("=")
            if not name:
                continue
            preview = preview or any(full.startswith(name) for full in _PREVIEW_LONG)
            amend = amend or "amend".startswith(name)
            if not eq and name in _COMMIT_VALUE_LONG:
                next(rest, None)
            continue
        if len(word) < 2 or not word.startswith("-"):
            continue
        for k, letter in enumerate(word[1:], 1):
            preview = preview or letter == _PREVIEW_LETTER
            if letter in _COMMIT_ATTACHED_LETTERS:
                break
            if letter in _COMMIT_VALUE_LETTERS:
                if k == len(word) - 1:
                    next(rest, None)
                break
    return amend, preview


def _summary_sha(line: str) -> str | None:
    """The sha of git's ``[branch abc1234] subject`` line, or None for any other line.

    ``[main (root-commit) abc1234] first`` counts too: the sha is the last word
    before the first ``]``.
    """
    close = line.find("]")
    if not line.startswith("[") or close < 0:
        return None
    prefix, space, sha = line[1:close].rpartition(" ")
    if not space or not 1 <= len(prefix) <= 200 or not _is_sha(sha):
        return None
    return sha


def _is_sha(text: str) -> bool:
    return 7 <= len(text) <= 40 and set(text) <= _HEX


def _commit_events(ts: float, output: str) -> Iterable[Event]:
    headers: list[tuple[int, int, str]] = []
    start = 0
    while start < len(output):
        end = output.find("\n", start)
        end = len(output) if end < 0 else end
        sha = _summary_sha(output[start:end])
        if sha is not None:
            headers.append((start, end, sha))
        start = end + 1
    for i, (_, line_end, sha) in enumerate(headers):
        stop = headers[i + 1][0] if i + 1 < len(headers) else len(output)
        added, removed = (0, 0) if line_end >= stop else _stats_after(output, line_end + 1, stop)
        yield ("commit", ts, added, removed, sha)


def _stats_after(output: str, start: int, stop: int) -> tuple[int, int]:
    """Lines added and removed, read from git's statistics line alone.

    It follows the summary line, after any `` Author:`` and `` Date:`` lines git
    prints there, so neither the subject nor a later command's output is read.
    """
    chunk = output[start : min(stop, start + _COMMIT_CHUNK_CHARS)]
    for line in chunk.split("\n", 3)[:3]:
        stats = _stats_of(line.rstrip())
        if stats is not None:
            return stats
        if not line.startswith((" Author:", " Date:")):
            break
    return 0, 0


def _stats_of(line: str) -> tuple[int, int] | None:
    """`` 2 files changed, 10 insertions(+), 3 deletions(-)``; either count may be absent."""
    head, *rest = line.split(", ")
    if not head.startswith(" ") or not head.endswith(" changed"):
        return None
    if _count(head[1 : -len(" changed")], ("file", "files")) is None:
        return None
    added = _count(rest[0], _INSERTED) if rest else None
    rest = rest[1:] if added is not None else rest
    removed = _count(rest[0], _DELETED) if rest else None
    rest = rest[1:] if removed is not None else rest
    return None if rest else (added or 0, removed or 0)


def _count(part: str, units: tuple[str, ...]) -> int | None:
    """N from ``N <unit>`` when the unit is one of *units*, else None."""
    number, space, unit = part.partition(" ")
    if not space or not number or not set(number) <= _DIGITS or unit not in units:
        return None
    return int(number)


def _pr_verbs(words: list[str]) -> Iterable[str]:
    """The verb of each ``gh pr`` / ``glab mr`` call in *words* (``create``, ``view``)."""
    for k in range(len(words) - 2):
        program, noun, verb = words[k : k + 3]
        if (_is_program(program, "gh") and noun == "pr") or (
            _is_program(program, "glab") and noun == "mr"
        ):
            if verb[:1].isascii() and verb[:1].islower():
                yield verb


def _created_alone(commands: list[tuple[list[str], str]]) -> bool:
    """Whether a pull request URL the command printed can only be the one it created.

    The create must be the command's one ``gh pr`` / ``glab mr`` call, and only
    ``&&`` or a pipe may follow it: after ``||``, ``;``, ``&`` or a newline another
    command can print an existing pull request's URL when the create failed.
    """
    calls = [(k, verb) for k, (words, _) in enumerate(commands) for verb in _pr_verbs(words)]
    if len(calls) != 1 or calls[0][1] != "create":
        return False
    return all(op in (_AND, _PIPE, "") for _, op in commands[calls[0][0] :])


def _pr_events(ts: float, output: str) -> Iterable[Event]:
    # A create for a branch that already has an open pull request prints that one's URL.
    if "already exists" in output.lower():
        return
    for line in output.splitlines()[:_OUTPUT_READ_LINES]:
        text = line.strip()
        if not text.startswith("https://") or " " in text:
            continue
        try:
            ref = parse_source_url(text)
        except ValueError:
            continue
        if ref.kind == "change":
            yield ("pr", ts, 0, 0, ref.url)


def outcome_events(rows: Iterable[Any]) -> tuple[Event, ...]:
    """The commits and pull requests one transcript's tool rows prove, in order.

    One linear pass. A row without a parseable time or a text output is skipped,
    and so is a command past :data:`_COMMAND_READ_CHARS` or with a quote left
    open, whose end cannot be read.
    """
    found: list[Event] = []
    for row in rows:
        if not isinstance(row, dict) or row.get("role") != "tool":
            continue
        meta = row.get("meta")
        if not isinstance(meta, dict):
            continue
        command = _command_of(row, meta)
        output = meta.get("output")
        if command is None or not isinstance(output, str) or len(command) > _COMMAND_READ_CHARS:
            continue
        # Only a command naming a commit or a create is scanned; most are neither.
        if not ("commit" in command and "git" in command) and "create" not in command:
            continue
        commands, complete = _split_commands(command)
        calls = [c for c in (_commit_call(words) for words, _ in commands) if c is not None]
        # An amend's summary line names the commit it rewrote, so a row with one
        # counts none of its lines; a row whose only commits are previews made none.
        is_commit = any(not preview for _, preview in calls) and not any(
            amend for amend, _ in calls
        )
        is_pr = _created_alone(commands)
        if not complete or not (is_commit or is_pr):
            continue
        ts = _usage._parse_row_ts(str(row.get("ts") or ""))
        if ts is None:
            continue
        text = output[:_OUTPUT_READ_CHARS]
        if is_commit:
            found.extend(_commit_events(ts, text))
        if is_pr:
            found.extend(_pr_events(ts, text))
    return tuple(found)


def _events_of(
    key: str,
    read_rows: Callable[[str], tuple[list[dict] | None, tuple[str, ...]]],
    signature: Callable[[str], float | None],
    now: Callable[[], float],
    withheld: Callable[[str], bool] | None = None,
) -> tuple[tuple[Event, ...], tuple[str, ...]]:
    """A transcript's events and the chain they were read from, reread only when
    its file changed, and then not more often than every ``_REREAD_SECS``.

    *read_rows* returns the rows and every transcript key the chained read
    covered. A transcript *withheld* names, or one *read_rows* refuses, has none,
    and what the memo held for it goes: a line tightened to incognito keeps its
    file's mtime, so the memo alone would go on serving what the transcript held
    before. Before the memo is served, each transcript it was read from is asked
    the same question, so an older transcript of the same tab tightened after
    the read takes its rows' figures with it. A transcript with more events than
    :data:`_MEMO_EVENTS_MAX`, or read from a chain longer than
    :data:`_MEMO_CHAIN_MAX`, is not held at all.
    """
    if withheld is not None and withheld(key):
        _forget(key)
        return (), ()
    stamp = signature(key)
    read_at = now()
    hit = None
    if stamp is not None:
        with _memo_lock:
            hit = _memo.get(key)
            if hit is not None and (hit[0] == stamp or read_at - hit[1] < _REREAD_SECS):
                _memo.move_to_end(key)
            else:
                hit = None
    if hit is not None:
        if withheld is not None and any(withheld(k) for k in hit[3] if k != key):
            _forget(key)
            return (), ()
        return hit[2], hit[3]
    try:
        rows, chain = read_rows(key)
    except TranscriptWithheld:
        _forget(key)
        return (), ()
    except Exception:
        return (), ()
    events = outcome_events(rows or ())
    covered = tuple(chain) or (key,)
    if stamp is not None:
        with _memo_lock:
            if len(events) > _MEMO_EVENTS_MAX or len(covered) > _MEMO_CHAIN_MAX:
                _memo.pop(key, None)
                return events, covered
            _memo[key] = (stamp, read_at, events, covered)
            _memo.move_to_end(key)
            while len(_memo) > _MEMO_MAX or _memo_events() > _MEMO_EVENTS_TOTAL:
                _memo.popitem(last=False)
    return events, covered


def _forget(key: str) -> None:
    with _memo_lock:
        _memo.pop(key, None)


def _memo_events() -> int:
    """Events the memo holds across its entries; the caller holds ``_memo_lock``."""
    return sum(len(entry[2]) for entry in _memo.values())


def fold_outcome(
    events: Iterable[Event],
    *,
    start: float,
    end: float,
    credits: float,
    merged_of: Callable[[str], bool | None] | None,
) -> dict[str, Any] | None:
    """One session's outcome inside ``[start, end)``, or None when it delivered nothing.

    ``merged_of`` answers "was this pull request merged" from cached status, or
    None for a URL it has not seen; passing None for the callable itself omits
    the merge figures altogether.
    """
    commits: dict[str, tuple[int, int]] = {}
    prs: dict[str, None] = {}
    for kind, ts, added, removed, key in events:
        if ts < start or ts >= end:
            continue
        if kind == "commit":
            commits.setdefault(key, (added, removed))
        else:
            prs.setdefault(key)
    if not commits and not prs:
        return None
    out: dict[str, Any] = {
        "commits": len(commits),
        "lines_added": sum(a for a, _ in commits.values()),
        "lines_removed": sum(r for _, r in commits.values()),
        "prs": len(prs),
    }
    if merged_of is not None and prs:
        states = [merged_of(url) for url in prs]
        merged = sum(1 for s in states if s is True)
        out["merged"] = merged
        out["unchecked"] = sum(1 for s in states if s is None)
        if merged:
            out["per_merged"] = credits / merged
    return out


def with_outcomes(
    cost: dict[str, Any],
    *,
    read_rows: Callable[[str], tuple[list[dict] | None, tuple[str, ...]]],
    signature: Callable[[str], float | None],
    transcript_key: Callable[[str], str],
    merged_of: Callable[[str], bool | None] | None,
    start: float,
    end: float = math.inf,
    places: int = 1,
    now: Callable[[], float] = time.monotonic,
    withheld: Callable[[str], bool] | None = None,
    contributors: dict[tuple[str, str], tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """*cost* with an ``outcome`` on each of its costliest conversations that has one.

    The rows are copied, never written into: *cost* is the memoised object
    ``cost_breakdown`` hands every caller. ``outcome_scan`` names how many
    conversations were read, so a row without an ``outcome`` is told apart from
    one that was not read. A window in which nothing was delivered comes back as
    the same object, so its payload is what it was before this module.
    *contributors*, when given, maps each delivering row's slot and transcript to
    the chain its figures came from, so the response can revalidate them
    (:func:`respond_with_session_outcomes`).
    """
    conversations = cost.get("conversations")
    if not conversations:
        return cost
    transcripts = cost.get("transcripts") or {}
    rows: list[dict[str, Any]] = []
    delivered = False
    for i, row in enumerate(conversations):
        if i >= OUTCOME_TOP_CONVOS:
            rows.extend(conversations[i:])
            break
        slot = str(row.get("slot") or "")
        try:
            key = transcripts.get(slot) or transcript_key(slot)
        except Exception:
            key = ""
        outcome = None
        chain: tuple[str, ...] = ()
        if key:
            events, chain = _events_of(key, read_rows, signature, now, withheld)
            outcome = fold_outcome(
                events,
                start=start,
                end=end,
                credits=float(row.get("credits") or 0.0),
                merged_of=merged_of,
            )
        if outcome is None:
            rows.append(row)
            continue
        if "per_merged" in outcome:
            outcome["per_merged"] = round(outcome["per_merged"], places)
        delivered = True
        if contributors is not None:
            contributors[(slot, key)] = chain
        rows.append({**row, "outcome": outcome})
    if not delivered:
        return cost
    return {
        **cost,
        "conversations": rows,
        "outcome_scan": min(len(conversations), OUTCOME_TOP_CONVOS),
    }


def _cached_merge_state(url: str) -> bool | None:
    """Whether the chip cache holds *url* as merged; None when it holds no state."""
    state = (get_cached_check_status(url) or {}).get("state")
    return None if not state else state == "merged"


async def attach_session_outcomes(
    request: Any,
    cost: dict[str, Any],
    window: _usage.TimeWindow,
    contributors: dict[tuple[str, str], tuple[str, ...]] | None = None,
) -> dict[str, Any]:
    """:func:`with_outcomes` for one ``/api/telemetry/startup`` request.

    Best effort, like every other block of that response: a transcript store that
    cannot be read leaves the spend view as it was. Merge state is the gateway's
    cached provider status, so only the owner is given it, as with the session
    chips. Rows come through the transcript's derivation seam, so an incognito or
    temporary session, or one whose line cannot be read, is given no outcome.
    """
    try:
        log = getattr(request.app["state"], "conversation_log", None)
        if log is None:
            return cost
        try:
            owner = is_owner_dashboard_request(request)
        except Exception:
            owner = False
        return await asyncio.to_thread(
            with_outcomes,
            cost,
            read_rows=log.derive_messages_chained_full_with_keys,
            signature=log.session_mtime,
            transcript_key=slot_transcript_key,
            merged_of=_cached_merge_state if owner else None,
            start=window.start,
            end=math.inf if window.rolling else window.end,
            places=4 if cost.get("unit") == "usd" else 1,
            withheld=lambda key: transcript_withholds_derivation(log, key),
            contributors=contributors,
        )
    except Exception:
        logger.debug("session outcome attachment failed", exc_info=True)
        return cost


def _without_outcomes(block: dict[str, Any], refused: set[str]) -> dict[str, Any]:
    rows = block.get("conversations") or []
    if not any("outcome" in row and row.get("slot") in refused for row in rows):
        return block
    kept = [
        {k: v for k, v in row.items() if k != "outcome"} if row.get("slot") in refused else row
        for row in rows
    ]
    return {**block, "conversations": kept}


def _payload_without(payload: dict[str, Any], refused: set[str]) -> dict[str, Any]:
    """*payload* with the outcomes of *refused* slots dropped from both spend units."""
    cost = payload.get("cost")
    if not refused or not isinstance(cost, dict):
        return payload
    cost = _without_outcomes(cost, refused)
    if isinstance(cost.get("usd"), dict):
        cost = {**cost, "usd": _without_outcomes(cost["usd"], refused)}
    return {**payload, "cost": cost}


def _held_members(contributors: dict[tuple[str, str], tuple[str, ...]]) -> list[str] | None:
    """Every transcript the contributing chains cover, sorted; None past the bound."""
    members: set[str] = set()
    for (_slot, transcript), chain in contributors.items():
        members.update(chain or (transcript,))
        if len(members) > _PUBLICATION_HOLD_MAX:
            return None
    return sorted(members)


def _respond_under_holds(
    log: Any, payload: dict[str, Any], contributors: dict[tuple[str, str], tuple[str, ...]]
) -> web.Response:
    """Build the response while every contributing chain is held; runs off the loop.

    The union of the chains' lock stems is taken first, in one sorted acquire, so
    this takes locks in the same order as every other single acquire. Each chain
    is then entered through ``publication_hold`` in sorted order (its locks are
    already held, so this only revalidates it) and kept until the bytes are
    built. A refused chain loses its outcome; a busy union loses every outcome.
    """
    every = {slot for slot, _ in contributors}
    members = _held_members(contributors)
    if members is None:
        return web.json_response(_payload_without(payload, every))
    stems = {stem for key in members for stem in transcript_lock_stems(key)}
    try:
        with log.derivation_hold(stems), contextlib.ExitStack() as holds:
            refused: set[str] = set()
            for (slot, transcript), chain in sorted(contributors.items()):
                try:
                    holds.enter_context(
                        log.publication_hold(transcript, expected_keys=chain or None)
                    )
                except Exception:
                    refused.add(slot)
            return web.json_response(_payload_without(payload, refused))
    except TranscriptBusy:
        logger.debug("session outcome holds busy; no outcome is sent", exc_info=True)
    return web.json_response(_payload_without(payload, every))


async def respond_with_session_outcomes(
    request: Any,
    payload: dict[str, Any],
    contributors: dict[tuple[str, str], tuple[str, ...]],
) -> web.Response:
    """The ``/api/telemetry/startup`` response, built while its outcomes' chains are held.

    The scan let go of each transcript once it was read, and the handler awaits
    other work before it answers, so a session tightened in that gap (a channel's
    ``!incognito``) must not be published. Off the loop, every chain that gave an
    outcome is held and revalidated through ``publication_hold``, a refused one is
    dropped from both spend units, and the response bytes are built before any
    hold is let go. When the holds cannot be taken, every outcome is dropped.
    """
    if not contributors:
        return web.json_response(payload)
    try:
        log = request.app["state"].conversation_log
        return await asyncio.to_thread(_respond_under_holds, log, payload, contributors)
    except Exception:
        logger.debug("session outcome revalidation failed", exc_info=True)
    return web.json_response(_payload_without(payload, {slot for slot, _ in contributors}))
