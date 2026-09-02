"""Shift handover digest — what the incoming responder needs to know.

Hand-maintained handover documents are the common practice, and they are among the
most-used artifacts a rotation has: at shift change the incoming responder does not want
a list of every incident, they want the small number of things that will actually page
them, plus what to do about each.

Such a document costs hours of upkeep and goes stale between edits. Everything in it that
is *generic* is already data this app owns:

- **Recurring patterns ranked by frequency** — the core section of any such digest, and
  structurally identical to the ledger ranked by ``use_count``. An entry used 9× is by
  definition what keeps happening.
- **What is open right now**, and specifically what is *waiting on a person* — the one
  thing that does not survive a shift change on its own.
- **Which sources are actually watching**, because an unconfigured source is a blind
  spot the incoming responder inherits without being told.

What is deliberately NOT here: rosters, per-person assignments, ticket ids, runbook
links. Those are the parts of a real handover doc that are organization-specific, and
inventing a schema for them would be guessing at a stranger's org. The digest is a
synthesis of observed behavior, not a CMDB.

**This is a read-only projection.** It stores nothing and decides nothing — every
number comes from the ledger, the dispatch index, or the live registry. A stale
handover is worse than none, so it is computed at request time rather than cached.

See ``docs/system-specs/modules/ops-mission-control.md`` § Shift handover.
"""

from __future__ import annotations

import logging
from typing import Any

from kiro_crew.apps.builtins.ops_mission_control.backend import ledger, store
from kiro_crew.apps.builtins.ops_mission_control.backend.models import (
    STATUS_ESCALATED,
    STATUS_NEEDS_HUMAN,
    Incident,
)

logger = logging.getLogger(__name__)

#: Patterns listed in the digest. The point is the SHORT list of what keeps
#: happening — a full ledger dump is the thing a responder already will not read.
MAX_PATTERNS = 8

#: An entry must have been used at least this often to count as "recurring". Used
#: once is an incident; used twice is a pattern.
MIN_USES_TO_RECUR = 2

#: Characters of pattern/fix text in the digest, so one verbose entry cannot crowd
#: out the rest of the list.
MAX_TEXT_CHARS = 400


def _clip(text: str, limit: int = MAX_TEXT_CHARS) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def recurring_patterns() -> list[dict[str, Any]]:
    """Ledger entries that keep coming back, most frequent first.

    Ranked by ``use_count`` because that is the only honest frequency signal the app
    has — it counts times a real signal matched this fingerprint, not times somebody
    thought the entry was important.
    """
    entries = [e for e in ledger.read_entries() if e.use_count >= MIN_USES_TO_RECUR]
    entries.sort(key=lambda e: (-e.use_count, e.pattern))
    return [
        {
            "pattern": _clip(e.pattern),
            "fix": _clip(e.fix),
            "uses": e.use_count,
            #: Times the fix was cited and the failure came back. In the digest because
            #: the digest's whole job at shift change is "what will page you and what to
            #: do about it" — and "the obvious answer to this one has already failed
            #: twice" is the single most valuable thing the incoming responder can be
            #: told about a recurring pattern. Without it they reach for the top-ranked
            #: fix precisely because it recurs.
            "misses": e.miss_count,
            "confidence": e.confidence,
            "trust": e.trust,
            # A proven entry can be applied directly; anything weaker is a hypothesis,
            # and saying so is the difference between a useful digest and one that gets
            # someone to apply the wrong fix confidently.
            #
            # Delegated to the ledger's own predicate rather than restating
            # "verified/high" — which is exactly what this line used to do, and it went
            # stale the moment the bar gained a use-count floor and a miss ceiling. A
            # digest that disagrees with the engine about what counts as proven tells a
            # responder to trust an entry the agent itself would not.
            "proven": ledger.entry_unlocks_fast_path(e),
            #: Louder than "not proven", and a different fact: this fix was tried and
            #: the failure returned. An untested hypothesis is worth more than a
            #: refuted one, so the digest must not render them the same.
            "demoted": ledger.is_demoted(e),
            "last_used": e.last_used,
        }
        for e in entries[:MAX_PATTERNS]
    ]


def _incident_row(inc: Incident) -> dict[str, Any]:
    return {
        "id": inc.incident_id,
        "title": _clip(inc.signal.title, 160),
        "status": inc.status,
        "blocked_reason": inc.blocked_reason,
        "severity": inc.signal.severity,
        "source": inc.signal.source,
        "age_from": inc.claimed_at,
        # WHAT claimed it. At a shift change "the agent picked this up" and "the outgoing
        # responder picked this up by hand" imply different next steps: the second means a
        # person already judged it worth attention, which is context the incoming reader
        # cannot recover from the incident itself. "" for anything claimed before the field
        # existed, and the UI must render that as unrecorded rather than inventing a path.
        "claimed_by": inc.claimed_by,
        "updated_at": inc.updated_at,
        "has_diagnosis": bool(inc.diagnosis),
    }


def open_work() -> dict[str, Any]:
    """What is still live, split by whether it needs a person.

    ``waiting_on_you`` is the section that matters at shift change: an incident parked
    on an approval or a question does not make progress on its own, so an outgoing
    responder's unanswered prompt becomes the incoming one's first job. Derived from
    ``blocked_reason`` (which slot_watch reconciles from the live chat) rather than
    from status alone, because "needs human" alone does not say whether a click or a
    decision is wanted.
    """
    incidents = store.open_incidents()
    waiting = [i for i in incidents if i.blocked_reason]
    # Escalated is a TERMINAL status, so it is deliberately absent from
    # ``open_incidents`` — the app no longer owns that work. It still belongs in a
    # handover though: "we passed this to another owner" is exactly the kind of thing
    # that gets lost at shift change, and the incoming responder may be the one who
    # has to chase it. Read from the index rather than the open set, and keep it out
    # of the ``progressing`` remainder below (which counts open work only).
    escalated = [i for i in store.read_index().values() if i.status == STATUS_ESCALATED]
    escalated.sort(key=lambda i: i.updated_at, reverse=True)
    # Needs-human WITHOUT a blocked reason and WITHOUT a diagnosis is the quiet
    # failure mode: the agent stopped and recorded nothing, so there is no thread to
    # pick up. Surfaced separately because it needs a human to restart, not answer.
    stalled = [
        i
        for i in incidents
        if i.status == STATUS_NEEDS_HUMAN and not i.blocked_reason and not i.diagnosis
    ]
    return {
        "total_open": len(incidents),
        "waiting_on_you": [_incident_row(i) for i in waiting],
        "escalated": [_incident_row(i) for i in escalated],
        "stalled_without_diagnosis": [_incident_row(i) for i in stalled],
        # Open work that needs nobody right now. Escalated is NOT subtracted: it is
        # not in ``incidents`` at all (terminal), so subtracting it would undercount
        # — and could go negative once several incidents were escalated.
        "progressing": len(incidents) - len(waiting) - len(stalled),
    }


def coverage(providers: list[dict[str, Any]]) -> dict[str, Any]:
    """Which signal sources are actually watching, and which are blind spots.

    An unconfigured source is silence that looks like health. The incoming responder
    inherits that blind spot, so name it explicitly rather than reporting a count of
    what happens to be on.
    """
    signal_sources = [p for p in providers if "signal" in (p.get("roles") or [])]
    watching = [p["display_name"] for p in signal_sources if p.get("configured")]
    blind = [p["display_name"] for p in signal_sources if not p.get("configured")]
    return {
        "watching": sorted(watching),
        "not_configured": sorted(blind),
        "any_watching": bool(watching),
    }


def build(providers: list[dict[str, Any]], rotation: dict[str, Any]) -> dict[str, Any]:
    """Assemble the digest. Pure projection — reads state, writes nothing."""
    work = open_work()
    patterns = recurring_patterns()
    cover = coverage(providers)
    return {
        "open_work": work,
        "recurring_patterns": patterns,
        "coverage": cover,
        "autonomy": {
            "mode": rotation.get("mode", ""),
            "rules": rotation.get("rules", 0),
            "on_shift": rotation.get("on_shift"),
        },
        "headline": _headline(work, patterns, cover, rotation),
    }


def _headline(
    work: dict[str, Any],
    patterns: list[dict[str, Any]],
    cover: dict[str, Any],
    rotation: dict[str, Any],
) -> str:
    """One sentence for someone who reads nothing else.

    Ordered by what would hurt most to miss: no coverage at all beats everything (the
    board looks calm because nothing is watching), then work parked on a person, then
    the ordinary case.
    """
    if not cover["any_watching"]:
        return (
            "No signal source is configured — the board is quiet because nothing is "
            "being watched, not because nothing is wrong."
        )
    waiting = len(work["waiting_on_you"])
    stalled = len(work["stalled_without_diagnosis"])
    parts: list[str] = []
    if waiting:
        parts.append(f"{waiting} incident(s) waiting on you")
    if stalled:
        parts.append(f"{stalled} stopped with no diagnosis")
    if work["escalated"]:
        parts.append(f"{len(work['escalated'])} escalated")
    if not parts:
        base = f"Nothing is waiting on you; {work['progressing']} in progress"
    else:
        base = "Start here: " + ", ".join(parts)
    if patterns:
        base += f". {len(patterns)} recurring pattern(s) known"
    if str(rotation.get("mode", "")) == "observe":
        base += ". Autonomy is observe — nothing will be written to any provider"
    return base + "."


def render_text(digest: dict[str, Any]) -> str:
    """Plain-text digest, for Slack or a terminal.

    Kept next to the data so the two cannot drift, and text rather than Block Kit
    because this is also what an agent pastes into a handover thread.
    """
    out: list[str] = ["Shift handover", "", digest["headline"], ""]

    work = digest["open_work"]
    if work["waiting_on_you"]:
        out.append("Waiting on you:")
        for row in work["waiting_on_you"]:
            reason = row["blocked_reason"].replace("_", " ") or row["status"]
            out.append(f"  • {row['id']} [{reason}] {row['title']}")
        out.append("")
    if work["stalled_without_diagnosis"]:
        out.append("Stopped with no diagnosis (needs a restart, not an answer):")
        for row in work["stalled_without_diagnosis"]:
            out.append(f"  • {row['id']} {row['title']}")
        out.append("")
    if work["escalated"]:
        out.append("Escalated:")
        for row in work["escalated"]:
            out.append(f"  • {row['id']} {row['title']}")
        out.append("")

    if digest["recurring_patterns"]:
        out.append("What keeps happening (most frequent first):")
        for pat in digest["recurring_patterns"]:
            mark = "proven" if pat["proven"] else f"{pat['confidence']}/{pat['trust']}"
            # A refuted fix is called out IN the pasted text, not only in the dashboard.
            # This text is what lands in the handover thread, and it is where the incoming
            # responder reads "this keeps happening" — the one place a fix that has already
            # failed must not appear as the answer with nothing said about it.
            if pat["misses"]:
                mark += f", failed {pat['misses']}×"
            out.append(f"  • {pat['uses']}× [{mark}] {pat['pattern']}")
            out.append(f"      fix: {pat['fix']}")
        out.append("")

    cover = digest["coverage"]
    out.append(f"Watching: {', '.join(cover['watching']) or 'nothing'}")
    if cover["not_configured"]:
        out.append(f"Not configured (blind spots): {', '.join(cover['not_configured'])}")
    return "\n".join(out)
