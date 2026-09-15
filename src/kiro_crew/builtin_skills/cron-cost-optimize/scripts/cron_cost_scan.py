#!/usr/bin/env python3
"""cron_cost_scan.py -- read-only cost audit of the cron jobs already registered.

A cron job dispatched to the model pays for its whole injected context on every
wake, whether or not the wake had anything to do. Two cheaper modes already
exist: a ``script`` or ``command`` job runs with no model turn at all, and an
agent job with ``minimal_context`` keeps the model but drops the bulk of the
injected context. Neither reaches a job that was registered before the user knew
about them, which is what this script finds.

It never reads the cron store or the run history itself. Input is the JSON
document ``cron_list`` produces with ``json=true``, fed on stdin (or named with
``--input``, which is the only file this script opens). That is deliberate and it
is the whole design:

  * The job store is only visible to a sandboxed process because ``mcp_cron`` was
    carved out for it. Reading it here directly would bypass the ownership filter
    every cron tool applies, so a non-owner sharing one data home would see every
    participant's job metadata.
  * The run history is masked from every sandboxed shell outright, and on Linux
    the mask is an empty directory -- so a direct read reports zero runs for every
    job and is indistinguishable from a job that has never fired. Only the gateway
    can see it.

The run history is what makes this more than keyword matching. A job's prompt
hints at intent; its recorded runs say what it really did, so "31 of the last 40
runs said the same thing" is evidence rather than inference. When the history
could not be read, this says so and downgrades its own confidence instead of
reporting a job as idle.

Verdicts, one per job:

  already-zero-token       script or command job, nothing to do
  move-to-script           deterministic work, no reasoning needed
  enable-minimal-context   still needs the model, does not need the context
  leave-as-is              genuinely reasons over its input, or unsafe to change

This script never edits a job. Applying a verdict is the user's call, made with
``cron_update`` or the dashboard Schedule page, because both changes alter what
the job can see at run time. See SKILL.md for what each one costs.

Stdlib only, Python 3.9+.

Usage:
  cron_list json=true | cron_cost_scan.py [--min-runs N] [--json]
  cron_cost_scan.py --input payload.json      # a saved payload, for testing
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

#: Runs needed before the history is treated as evidence rather than a hint.
DEFAULT_MIN_RUNS = 5

#: Share of runs that must be no-ops before a job counts as idle.
NOOP_THRESHOLD = 0.8

# ---------------------------------------------------------------------------
# Pattern sets. Each is deliberately narrow: a false "move-to-script" costs the
# user a broken job, so every pattern here has to be a phrase that only shows up
# in work a program can settle.
# ---------------------------------------------------------------------------

#: Work a program can settle with no judgement. Deliberately narrow, and only
#: pluralized where the plural is the natural spelling: broadening this set makes
#: the scan recommend MORE script rewrites, which is the unsafe direction.
DETERMINISTIC_RE = re.compile(
    r"\b("
    r"timestamps?|unchanged|last modified|mtime|newer than|older than|expired|"
    r"disk|disk space|free space|disk usage|inodes?|"
    r"file exists|already exists|is present|is missing|"
    r"http status|status code|response code|[1-5]\d\d response|"
    r"ping|ports?|reachable|is up|is down|responding|"
    r"checksums?|hash|sha256|byte size|file size|line count|row count|"
    r"thresholds?|quota|above \d|below \d|exceeds|greater than|less than|"
    r"count of|number of files|exit code|non-zero exit"
    r")\b",
    re.IGNORECASE,
)

#: Work that needs a model. Any hit blocks a script rewrite outright.
#:
#: Verb stems take a trailing ``\w*`` on purpose. A rewrite that is wrong here
#: fails SILENTLY -- the job stays green and quietly stops doing its work -- so
#: over-matching costs a missed saving while under-matching costs a broken job.
JUDGEMENT_RE = re.compile(
    r"\b("
    r"summari[sz]\w*|summary of|review\w*|draft\w*|compose\w*|write up|write a|"
    r"decide\w*|decision|judge\w*|assess\w*|evaluat\w*|interpret\w*|describe\w*|"
    r"explain\w*|analy[sz]\w*|analysis|investigat\w*|diagnos\w*|root cause|triag\w*|"
    r"recommend\w*|suggest\w*|prioriti[sz]\w*|rank\w*|classif\w*|categori[sz]\w*|"
    r"brainstorm\w*|plan\w*|propos\w*|refactor\w*|implement\w*|fix the|"
    r"repl(?:y|ies)|respond to"
    r")\b",
    re.IGNORECASE,
)

#: Context a minimal-context wake does not inject. Any hit blocks that verdict.
CONTEXT_RE = re.compile(
    r"\b("
    r"memor(?:y|ies)|remember|lessons?|preferences?|steering|knowledge base|"
    r"previous session|past session|prior session|chat history|"
    r"conversation history|my notes|project context|as we discussed"
    r")\b",
    re.IGNORECASE,
)

#: ``$skill`` inline tokens. A minimal-context wake injects no skill at all, so a
#: job that names one this way stops working. Mirrors the core's own token shape.
SKILL_TOKEN_RE = re.compile(r"(?<![\w$])\$([a-z0-9][a-z0-9/_-]*)")

#: A skill named by word, for jobs that spell it out in prose. Plural included:
#: "loads the skills it needs" names one just as much as the singular does, and
#: missing it would clear a job for a mode that injects no skill at all.
SKILL_WORD_RE = re.compile(r"\bskills?\b", re.IGNORECASE)

#: Every blocker above is an English word list, so a prompt written in another
#: language matches NONE of them. That failure is silent and points the wrong
#: way: no match reads as "nothing blocks a cheaper mode", so a Chinese or German
#: "总结并决定" / "fasse zusammen und entscheide" job would be cleared for script
#: mode precisely because the blockers could not see it. The classifier is biased
#: toward the safe direction only for text it can actually read, so a prompt whose
#: judgement-bearing words cannot be matched is treated like a truncated one:
#: unreadable, therefore not judged.
#:
#: The test is coarse on purpose -- enough Latin-script word characters to make an
#: English word list meaningful. A prompt of mostly CJK, Cyrillic, Arabic or other
#: non-Latin script fails it; an English prompt containing a few such characters
#: (a name, a quoted string) still passes.
_LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_NON_LATIN_RE = re.compile(r"[^\x00-\x7F]")


def is_scannable_prose(message: str) -> bool:
    """Whether the English blocker word lists can meaningfully read this prompt.

    False means the verdict must be withheld rather than defaulted to "cheaper
    mode is fine": the blockers did not fail to fire because nothing blocks, they
    failed to fire because they cannot read the language.
    """
    latin = sum(len(m.group(0)) for m in _LATIN_WORD_RE.finditer(message))
    non_latin = len(_NON_LATIN_RE.findall(message))
    if latin == 0:
        return False
    # Non-Latin characters carry whole words, Latin ones carry letters, so a
    # straight character count would favour the Latin side. Weighting the
    # non-Latin count keeps a mostly-CJK prompt with an ASCII tail on the
    # unreadable side, which is the safe direction.
    return latin >= non_latin * 3


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Evidence:
    """What a job's run history says about whether it does any work.

    ``known`` is False when the gateway could not read the history. That is a
    third state, not a zero: a job whose history is unknown has NOT been shown to
    be idle, and every verdict below has to keep the two apart.
    """

    known: bool = True
    runs: int = 0
    failures: int = 0
    distinct_summaries: int = 0
    noop_runs: int = 0
    same_every_run: bool = False

    @classmethod
    def unknown(cls) -> Evidence:
        return cls(known=False)

    @classmethod
    def from_payload(cls, raw: Any) -> Evidence:
        """Build from one job's ``history`` object, or mark it unknown."""
        if not isinstance(raw, dict):
            return cls.unknown()
        return cls(
            known=True,
            runs=int(raw.get("runs") or 0),
            failures=int(raw.get("failures") or 0),
            distinct_summaries=int(raw.get("distinct_summaries") or 0),
            noop_runs=int(raw.get("noop_runs") or 0),
            same_every_run=bool(raw.get("same_every_run")),
        )

    @property
    def noop_ratio(self) -> float:
        return (self.noop_runs / self.runs) if self.runs else 0.0

    def is_evidence(self, min_runs: int) -> bool:
        """True when there are enough recorded runs to trust what they show."""
        return self.known and self.runs >= min_runs

    def says_idle(self, min_runs: int) -> bool:
        """True when the history shows a job that repeats itself or does nothing."""
        if not self.is_evidence(min_runs):
            return False
        return self.same_every_run or self.noop_ratio >= NOOP_THRESHOLD

    def to_dict(self) -> dict[str, Any]:
        if not self.known:
            return {"known": False}
        return {
            "known": True,
            "runs": self.runs,
            "failures": self.failures,
            "distinct_summaries": self.distinct_summaries,
            "noop_runs": self.noop_runs,
            "noop_ratio": round(self.noop_ratio, 3),
            "same_every_run": self.same_every_run,
        }


@dataclass
class Finding:
    """One job's verdict, with the numbers it was derived from."""

    job_id: str
    name: str
    mode: str
    verdict: str
    confidence: str
    reason: str
    schedule: str
    wakes_per_day: float | None
    minimal_context: bool
    persistent_session: bool
    hide_in_chat: bool
    enabled: bool
    evidence: Evidence
    blockers: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    change: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "name": self.name,
            "mode": self.mode,
            "verdict": self.verdict,
            "confidence": self.confidence,
            "reason": self.reason,
            "schedule": self.schedule,
            "wakes_per_day": self.wakes_per_day,
            "minimal_context": self.minimal_context,
            "persistent_session": self.persistent_session,
            "hide_in_chat": self.hide_in_chat,
            "enabled": self.enabled,
            "evidence": self.evidence.to_dict(),
            "blockers": self.blockers,
            "notes": self.notes,
            "change": self.change,
        }


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def load_payload(text: str) -> dict[str, Any]:
    """Parse the ``cron_list json=true`` document. Raises LookupError on anything else.

    The tool answers a plain sentence rather than JSON in three real cases -- an
    empty registry, nothing owned by this session, and an unidentified caller --
    and each is a legitimate outcome the user should read verbatim, not a crash.
    """
    stripped = text.strip()
    if not stripped:
        raise LookupError("no input on stdin; pipe `cron_list json=true` into this script")
    if not stripped.startswith("{"):
        raise LookupError(f"cron_list did not return JSON, it said: {stripped[:400]}")
    try:
        data = json.loads(stripped)
    except ValueError as exc:
        raise LookupError(f"cannot parse the cron_list payload ({exc})") from exc
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise LookupError("the cron_list payload carries no job list")
    return data


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def describe_schedule(job: dict[str, Any]) -> tuple[str, float | None]:
    """Render a schedule and, where it is a fixed interval, its wakes per day."""
    label = str(job.get("schedule") or "unknown")
    secs = job.get("every_secs")
    if isinstance(secs, (int, float)) and secs > 0:
        return label, round(86400.0 / float(secs), 2)
    return label, None


def script_blockers(message: str) -> list[str]:
    """Reasons this job must keep a model, so a script rewrite would break it."""
    out: list[str] = []
    hit = JUDGEMENT_RE.search(message)
    if hit:
        out.append(f"the prompt asks the model to {hit.group(0).lower()}, which code cannot do")
    if SKILL_TOKEN_RE.search(message) or SKILL_WORD_RE.search(message):
        out.append("the prompt drives a skill, which only an agent turn can load")
    if CONTEXT_RE.search(message):
        out.append("the prompt reads injected context a script never receives")
    return out


def minimal_context_blockers(message: str) -> list[str]:
    """Reasons this job needs the full injected context."""
    out: list[str] = []
    if SKILL_TOKEN_RE.search(message) or SKILL_WORD_RE.search(message):
        out.append("a minimal-context wake injects no skill, and this prompt names one")
    hit = CONTEXT_RE.search(message)
    if hit:
        out.append(f"the prompt relies on {hit.group(0).lower()}, which a minimal wake drops")
    return out


def classify(job: dict[str, Any], min_runs: int) -> Finding:
    """Decide one job's verdict from its record and its run history."""
    message = str(job.get("message") or "")
    mode = str(job.get("mode") or "agent")
    schedule, per_day = describe_schedule(job)
    evidence = Evidence.from_payload(job.get("history"))
    minimal = bool(job.get("minimal_context"))
    persistent = bool(job.get("persistent_session"))
    hidden = bool(job.get("hide_in_chat"))

    finding = Finding(
        job_id=str(job.get("id") or ""),
        name=str(job.get("name") or ""),
        mode=mode,
        verdict="leave-as-is",
        confidence="high",
        reason="",
        schedule=schedule,
        wakes_per_day=per_day,
        minimal_context=minimal,
        persistent_session=persistent,
        hide_in_chat=hidden,
        enabled=bool(job.get("enabled", True)),
        evidence=evidence,
    )

    if mode in ("script", "command"):
        finding.verdict = "already-zero-token"
        finding.reason = f"a {mode} job runs with no model turn, so its wakes are already free"
        return finding

    # A prompt that arrived cut short cannot be judged, because every signal this
    # scanner reads is a phrase IN the prompt -- so a judgement verb, a skill
    # token or a memory reference sitting past the cut is invisible, and the
    # verdict would be confident about text it never saw. That is the silent
    # failure this whole classifier is biased against, so refuse rather than
    # guess. Checked before any other signal: nothing below can outrank not
    # having read the input.
    if bool(job.get("message_truncated")):
        finding.verdict = "leave-as-is"
        finding.confidence = "high"
        finding.reason = (
            "the prompt is longer than the scan receives, so a reason to rule out a "
            "cheaper mode could sit past the cut. Read the full prompt with cron_list "
            "ids to judge this one"
        )
        finding.blockers = ["the prompt was truncated before it reached the scan"]
        return finding

    # Same refusal, different reason for not having read the prompt: the blockers
    # are English word lists, so a prompt in another language produces an empty
    # blocker set that means "unreadable", not "unblocked". Checked before any
    # other signal for the same reason as truncation.
    if not is_scannable_prose(message):
        finding.verdict = "leave-as-is"
        finding.confidence = "high"
        finding.reason = (
            "the prompt is not in English, and every blocker this scan applies is an "
            "English word list, so a reason to keep the model would not be detected. "
            "Read the prompt yourself to judge this one"
        )
        finding.blockers = ["the prompt is in a language this scan cannot read"]
        return finding

    blocked = script_blockers(message)
    finding.blockers = list(blocked)
    deterministic = bool(DETERMINISTIC_RE.search(message))
    idle = evidence.says_idle(min_runs)

    if not blocked and (deterministic or idle):
        finding.verdict = "move-to-script"
        finding.change = "set script to a file under the crons directory, and clear the prompt"
        if deterministic and idle:
            finding.confidence = "high"
            finding.reason = (
                f"the work is a mechanical check, and {_idle_phrase(evidence)} "
                "so no wake has needed reasoning"
            )
        elif idle:
            finding.confidence = "medium"
            finding.reason = (
                f"the wording is not obviously mechanical, but {_idle_phrase(evidence)} "
                "so the history says nothing is being reasoned about"
            )
        elif not evidence.known:
            finding.confidence = "low"
            finding.reason = (
                "the work reads as a mechanical check, but the run history could "
                "not be read, so nothing confirms it"
            )
        else:
            finding.confidence = "low"
            finding.reason = (
                "the work reads as a mechanical check, but there is not enough run "
                "history yet to confirm it"
            )
        if persistent:
            finding.notes.append(
                "this job currently carries its previous result into the next prompt for "
                "dedup. A script receives no such carry, so any dedup has to be rewritten "
                "as state the script itself writes and reads."
            )
        return finding

    mc_blocked = minimal_context_blockers(message)
    if not minimal and not mc_blocked:
        finding.verdict = "enable-minimal-context"
        finding.change = "set minimal_context to true"
        finding.confidence = "high" if evidence.is_evidence(min_runs) else "medium"
        if blocked:
            finding.reason = (
                f"this job needs a model because {blocked[0]}, but it does not need the "
                "full injected context"
            )
        else:
            finding.reason = (
                "this job needs a model, but it does not need the full injected context"
            )
        if evidence.known and evidence.noop_ratio >= NOOP_THRESHOLD and not hidden:
            finding.notes.append(
                "most runs report nothing. Setting hide_in_chat to true keeps those out of "
                "the chat without changing what the job can see."
            )
        return finding

    finding.verdict = "leave-as-is"
    finding.blockers = list(dict.fromkeys(blocked + mc_blocked))
    if minimal:
        # Say this first even when a blocker exists. "Nothing to change here" is the
        # fact the user acts on; the blocker only explains why.
        if finding.blockers:
            finding.reason = f"already on minimal context, and {finding.blockers[0]}"
        else:
            finding.reason = "already on minimal context, and the work still needs a model"
    elif finding.blockers:
        finding.reason = finding.blockers[0]
    else:
        finding.reason = "already on the cheapest mode this job can safely use"
    return finding


def _idle_phrase(evidence: Evidence) -> str:
    """Say what the history showed, in runs rather than in adjectives."""
    if evidence.same_every_run:
        return f"all {evidence.runs} recorded runs produced the same result,"
    return f"{evidence.noop_runs} of {evidence.runs} recorded runs found nothing to do,"


def scan(payload: dict[str, Any], min_runs: int) -> list[Finding]:
    """Classify every job in the payload."""
    jobs = [j for j in payload.get("jobs", []) if isinstance(j, dict)]
    return [classify(job, min_runs) for job in jobs]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_ORDER = {
    "move-to-script": 0,
    "enable-minimal-context": 1,
    "leave-as-is": 2,
    "already-zero-token": 3,
}


def render_text(findings: Sequence[Finding], payload: dict[str, Any], min_runs: int) -> str:
    """A report meant to be read by a person and acted on one job at a time."""
    lines: list[str] = []
    ordered = sorted(findings, key=lambda f: (_ORDER.get(f.verdict, 9), f.name))
    counts: dict[str, int] = {}
    for f in ordered:
        counts[f.verdict] = counts.get(f.verdict, 0) + 1

    lines.append(f"Scanned {len(findings)} cron job(s) owned by this session.")
    for verdict in sorted(counts, key=lambda v: _ORDER.get(v, 9)):
        lines.append(f"  {counts[verdict]:>3}  {verdict}")
    # Lead with this, not bury it: without history every verdict below rests on
    # wording alone, and the reader has to know that before acting on one.
    if not payload.get("history_available", True):
        reason = str(payload.get("history_unavailable_reason") or "reason not reported")
        lines.append("")
        lines.append(f"Run history could NOT be read: {reason}.")
        lines.append("Every verdict below therefore rests on the prompt's wording alone.")
    if payload.get("truncated"):
        lines.append("")
        lines.append("More jobs are owned than one payload carries; this is a partial scan.")
    lines.append("")

    for f in ordered:
        head = f"{f.name or '(unnamed)'}  [{f.job_id}]"
        if not f.enabled:
            head += "  (paused)"
        lines.append(head)
        lines.append(f"  mode        {f.mode}, {f.schedule}")
        if f.wakes_per_day is not None:
            lines.append(f"  wakes/day   {f.wakes_per_day:g}")
        ev = f.evidence
        if not ev.known:
            lines.append("  history     could not be read")
        elif ev.runs or ev.failures:
            lines.append(
                f"  history     {ev.runs} run(s), {ev.distinct_summaries} distinct result(s), "
                f"{ev.noop_runs} found nothing to do, {ev.failures} failed"
            )
        else:
            lines.append("  history     none recorded yet")
        lines.append(f"  verdict     {f.verdict} ({f.confidence} confidence)")
        lines.append(f"  because     {f.reason}")
        if f.change:
            lines.append(f"  change      {f.change}")
        for blocker in f.blockers:
            lines.append(f"  blocker     {blocker}")
        for note in f.notes:
            lines.append(f"  note        {note}")
        lines.append("")

    lines.append(
        f"Run history is treated as evidence at {min_runs} or more recorded runs. "
        "Below that a verdict is marked low or medium confidence."
    )
    lines.append(
        "Kiro Crew's own in-code estimate for a minimal-context wake is roughly 200 tokens "
        "against 30,000 to 55,000 for a full one. That range is an estimate written into the "
        "product, not a measurement, so quote it as an estimate."
    )
    lines.append("Nothing was changed. Applying a verdict is a separate, explicit step.")
    return "\n".join(lines)


def render_json(findings: Sequence[Finding], payload: dict[str, Any], min_runs: int) -> str:
    out = {
        "min_runs": min_runs,
        "noop_threshold": NOOP_THRESHOLD,
        "scanned": len(findings),
        "history_available": bool(payload.get("history_available", True)),
        "partial": bool(payload.get("truncated")),
        "findings": [f.to_dict() for f in findings],
    }
    if not out["history_available"]:
        out["history_unavailable_reason"] = payload.get("history_unavailable_reason")
    return json.dumps(out, indent=2, sort_keys=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cron_cost_scan.py",
        description=(
            "Read-only cost audit of registered cron jobs. Reads the JSON that "
            "`cron_list json=true` produces, on stdin. Changes nothing."
        ),
    )
    parser.add_argument(
        "--min-runs",
        type=int,
        default=DEFAULT_MIN_RUNS,
        help=f"Recorded runs needed before history counts as evidence (default {DEFAULT_MIN_RUNS}).",
    )
    parser.add_argument(
        "--input",
        help="Read the payload from this file instead of stdin. For testing.",
    )
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    args = parser.parse_args(argv)

    if args.min_runs < 1:
        parser.error("--min-runs must be at least 1")

    try:
        raw = Path(args.input).read_text(encoding="utf-8") if args.input else sys.stdin.read()
    except OSError as exc:
        print(f"cron_cost_scan: cannot read input ({exc})", file=sys.stderr)
        return 1

    try:
        payload = load_payload(raw)
    except LookupError as exc:
        print(f"cron_cost_scan: {exc}", file=sys.stderr)
        return 1

    findings = scan(payload, args.min_runs)
    if args.json:
        print(render_json(findings, payload, args.min_runs))
    else:
        print(render_text(findings, payload, args.min_runs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
