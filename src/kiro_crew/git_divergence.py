"""Ahead/behind divergence counting against an upstream ref.

Several gateway surfaces ask "how far is this checkout from its upstream"
before acting on the answer, and two of them gate actions that are hard to
undo: the CLI's hard-reset recovery path, and the update-check verdict that
the unattended auto-apply path reads. Each surface previously re-derived the
same fragile details by hand — the three-dot range, ``--left-right``'s
left-is-ahead semantics, the two-token split, the ``int()`` conversion, the
subprocess timeout, and what an unreadable result means — so the copies
agreeing was luck rather than structure. This module is the one owner of the
COUNTING. Every caller keeps its own POLICY about what the counts mean,
because the surfaces intentionally disagree: the update check offers only a
fast-forward, the apply precondition refuses only true divergence, the CLI
refuses to reset even an ahead-only checkout, and the papyrus status panel
merely displays the distance.

The one property no caller may lose: **failure is never (0, 0)**. A count
that cannot be read returns :class:`DivergenceUnreadable`, a distinct type,
because ``(0, 0)`` also means "in sync" and a sentinel pair would silently
convert a fail-closed gate into a fail-open one. Callers decide explicitly
what an unreadable count means for them; the gates guarding destructive
actions refuse.

Three call shapes are deliberate, not redundancy:

* :func:`count_divergence` — async, for handlers running on the event loop.
* :func:`count_divergence_sync` — blocking, for the one-shot ``kirocrew
  update`` CLI path that runs with no event loop.
* :func:`divergence_count_args` + :func:`parse_divergence_counts` — the raw
  argv and the parser, for a caller that must spawn through its own hardened
  runner (papyrus routes every git call through its sandbox chokepoint with
  config-override pins, and counting through this module's spawns would
  bypass that). The primitives keep the fragile details here even when the
  spawn cannot be.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass
from pathlib import Path

from kiro_crew.subprocess_utf8 import UTF8_TEXT

#: Wall-clock ceiling for the count. ``rev-list`` walks local objects only —
#: no network — so this bounds a wedged filesystem or a pathological repo,
#: not a slow remote.
DIVERGENCE_TIMEOUT_SEC = 10.0

#: ``DivergenceUnreadable.reason`` values. Callers that present failures
#: differently (an HTTP surface mapping a timeout to a different status than
#: a bad count; the CLI choosing which message to print) branch on these
#: rather than re-deriving the failure class.
UNREADABLE_TIMEOUT = "timeout"
UNREADABLE_GIT_FAILED = "git_failed"
UNREADABLE_UNPARSEABLE = "unparseable"


@dataclass(frozen=True)
class DivergenceCounts:
    """How far HEAD is from the upstream, in commits, both directions."""

    #: Commits reachable from HEAD only — local work the upstream lacks.
    ahead: int
    #: Commits reachable from the upstream only — remote work HEAD lacks.
    behind: int


@dataclass(frozen=True)
class DivergenceUnreadable:
    """The count could not be determined.

    A distinct type rather than a sentinel pair: gates that guard destructive
    actions must refuse on an unreadable count, and a result the caller
    cannot read ``.ahead`` off makes "treat failure as in sync"
    unrepresentable rather than merely discouraged.
    """

    #: One of the ``UNREADABLE_*`` constants.
    reason: str
    #: Operator-facing context: git's own error text, or the unparseable
    #: output. Empty when the failure carries no message worth printing.
    detail: str = ""


def divergence_count_args(upstream: str) -> list[str]:
    """The ``git`` arguments (without the binary) that count HEAD vs *upstream*.

    The three-dot range with ``--count --left-right`` prints
    ``"<ahead>\\t<behind>"``: left counts commits reachable from HEAD only,
    right those reachable from *upstream* only. *upstream* is a caller choice
    because the surfaces genuinely differ — ``@{u}``/``@{upstream}`` where
    the tracked upstream is the comparison, ``origin/<branch>`` where the
    exact ref a reset targets is.
    """
    return ["rev-list", "--count", "--left-right", f"HEAD...{upstream}"]


def parse_divergence_counts(output: str) -> DivergenceCounts | None:
    """Parse ``rev-list --count --left-right`` output, ``None`` when unreadable.

    Exactly two whitespace-separated integer tokens are accepted. Anything
    else — an empty read, an error message, a truncated line — is ``None``,
    never a pair of zeros.
    """
    try:
        ahead_text, behind_text = output.split()
        return DivergenceCounts(int(ahead_text), int(behind_text))
    except ValueError:
        return None


async def count_divergence(
    repo: str | Path, upstream: str, *, timeout: float = DIVERGENCE_TIMEOUT_SEC
) -> DivergenceCounts | DivergenceUnreadable:
    """Count HEAD's divergence from *upstream* in *repo*, off the event loop.

    stderr is discarded: the async callers are HTTP handlers whose responses
    carry their own machine-readable codes, so git's message has no reader.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *divergence_count_args(upstream),
            cwd=repo,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except OSError as exc:
        # The spawn itself failed — git not on PATH, the repo path gone, or a
        # permission refusal. Naming it keeps "failure is DivergenceUnreadable"
        # exhaustive: without this, spawn errors would be a third, unnamed
        # exit that every caller degrades through differently.
        return DivergenceUnreadable(UNREADABLE_GIT_FAILED, detail=str(exc))
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.communicate()
        return DivergenceUnreadable(UNREADABLE_TIMEOUT, detail=f"timed out after {timeout:g}s")
    if proc.returncode != 0:
        return DivergenceUnreadable(UNREADABLE_GIT_FAILED)
    text = out.decode(errors="replace")
    counts = parse_divergence_counts(text)
    if counts is None:
        return DivergenceUnreadable(UNREADABLE_UNPARSEABLE, detail=text.strip())
    return counts


def count_divergence_sync(
    repo: str | Path, upstream: str, *, timeout: float = DIVERGENCE_TIMEOUT_SEC
) -> DivergenceCounts | DivergenceUnreadable:
    """Blocking :func:`count_divergence`, for CLI paths with no event loop.

    Never call this from a coroutine or an event-loop callback — the gateway
    runs everything on one loop, and a blocked loop freezes every session.
    stderr IS captured here: the sync caller is an operator at a terminal,
    and git's own message is the most useful thing to show them.
    """
    try:
        result = subprocess.run(
            ["git", *divergence_count_args(upstream)],
            cwd=repo,
            capture_output=True,
            timeout=timeout,
            **UTF8_TEXT,
        )
    except subprocess.TimeoutExpired:
        return DivergenceUnreadable(UNREADABLE_TIMEOUT, detail=f"timed out after {timeout:g}s")
    except OSError as exc:
        # Same exhaustiveness as the async path: a spawn failure (git missing,
        # repo path gone, permissions) is an unreadable count, not a traceback.
        return DivergenceUnreadable(UNREADABLE_GIT_FAILED, detail=str(exc))
    if result.returncode != 0:
        return DivergenceUnreadable(
            UNREADABLE_GIT_FAILED,
            detail=result.stderr.strip() or result.stdout.strip(),
        )
    counts = parse_divergence_counts(result.stdout)
    if counts is None:
        return DivergenceUnreadable(UNREADABLE_UNPARSEABLE, detail=result.stdout.strip())
    return counts
