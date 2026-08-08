"""Feed exploit verdicts back into the stores — the self-improving loop.

Given a finding and the evidence from running its PoC:

- **EXPLOITED** -> advance the finding to ``exploited`` and *learn* a generalized
  pattern from it (source ``self-discovered``), so future scans of the same
  class start with this knowledge.
- **BLOCKED** (PoC ran, target was safe) -> advance to ``blocked`` and add a
  *suppression* so the same false positive is not re-reported, and nudge the
  false-positive rate of any matching learned patterns.
- **TIMEOUT / ERROR** -> record the evidence on the finding but learn nothing —
  an inconclusive run must not teach the scanner anything.

Everything here is append-with-audit (SECURITY_NOTES.md #7): patterns and
suppressions are added, never silently deleted.
"""
from __future__ import annotations

from .exploit import BLOCKED, EXPLOITED, ExploitEvidence
from .findings import FindingsStore
from .knowledge import KnowledgeStore
from .models import Finding, KnowledgePattern, Suppression


def apply_verdict(
    evidence: ExploitEvidence,
    finding: Finding,
    knowledge: KnowledgeStore,
    findings: FindingsStore,
) -> None:
    """Persist the evidence on the finding and update knowledge per the verdict."""
    ev_text = evidence.summary() + ("\n" + evidence.output if evidence.output else "")

    if evidence.verdict == EXPLOITED:
        findings.set_status(finding.id, "exploited", evidence=ev_text)
        pattern_text = _generalize(finding)
        knowledge.learn(
            KnowledgePattern(
                topic=finding.topic,
                pattern=pattern_text,
                tags=[finding.topic],
                exploit_template=finding.exploit_suggestion or "",
                confidence=0.9,  # a confirmed exploit is high-confidence
                source="self-discovered",
            )
        )
    elif evidence.verdict == BLOCKED:
        findings.set_status(finding.id, "blocked", evidence=ev_text)
        knowledge.suppress(
            Suppression(
                topic=finding.topic,
                pattern=finding.title,
                reason=f"PoC ran and target was safe ({evidence.summary()})",
                tags=[finding.topic],
            )
        )
        # Nudge FP rate on any learned pattern for this topic whose text overlaps
        # the finding — soft signal that this pattern class over-fires here.
        for p in knowledge.for_topic(finding.topic):
            if _overlaps(p.pattern, finding.title):
                knowledge.record_false_positive(p.id)
    else:
        # TIMEOUT / ERROR — record evidence, learn nothing (inconclusive).
        findings.set_status(finding.id, finding.status, evidence=ev_text)


def _generalize(finding: Finding) -> str:
    """Turn a specific finding into a reusable pattern sentence. The location is
    intentionally dropped — a pattern is a CLASS, not a single file:line."""
    base = finding.title.strip()
    if finding.description:
        base = f"{base} — {finding.description.strip()}"
    return base


def _overlaps(pattern_text: str, title: str) -> bool:
    """Cheap lexical overlap: do the title's significant words appear in the
    pattern text? Avoids a heavyweight similarity dependency for a soft signal."""
    words = {w for w in title.lower().split() if len(w) > 4}
    if not words:
        return False
    ptext = pattern_text.lower()
    hits = sum(1 for w in words if w in ptext)
    return hits >= max(1, len(words) // 2)
