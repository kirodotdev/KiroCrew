"""Pure-function helpers: dedup, verdict, and exception matching.

Every function here is deterministic and touches no I/O. Kept in its own
module so the tests are fast and can be reasoned about without running
the LLM pipeline.
"""

from __future__ import annotations

import re

from kiro_crew.apps.builtins.writing_review import Finding

# High severity outranks medium outranks low outranks advisory. Missing
# severities fall back to advisory (0) so a corrupt finding never
# accidentally beats a real one during dedup.
_SEVERITY_RANK: dict[str, int] = {
    "high": 3,
    "medium": 2,
    "low": 1,
    "advisory": 0,
}


def dedup_findings(findings: list[Finding]) -> list[Finding]:
    """Same-scanner LLM-verbosity safety net; cross-scanner overlaps pass through.

    A single scanner call occasionally emits multiple findings at the
    same ``(section, paragraph)`` -- LLM verbosity where two candidate
    framings for the same underlying issue both make it into the JSON
    envelope. The collapse key ``(scanner, section, paragraph)`` folds
    those into one card carrying the higher-severity framing.

    Cross-scanner overlaps at the same location deliberately survive
    this pass and continue on to ``_cross_validate_findings``. Two
    different scanners flagging the same paragraph is the input signal
    the synthesis pass needs to tag findings as ``redundant`` (same
    root cause, collate onto the primary) or ``conflicts`` (real
    disagreement, both surface with the ``Scanners disagree`` pill).
    Collapsing them here would suppress that signal at source and
    render both downstream code paths unreachable -- the earlier
    location-only key did exactly that, which is what M2 fixes.
    """
    highest_by_scanner_and_location: dict[tuple[str, str, int], Finding] = {}
    for finding in findings:
        collapse_key = (finding.scanner, finding.section, finding.paragraph)
        existing_finding = highest_by_scanner_and_location.get(collapse_key)
        if existing_finding is None:
            highest_by_scanner_and_location[collapse_key] = finding
            continue
        incoming_rank = _SEVERITY_RANK.get(finding.severity, 0)
        existing_rank = _SEVERITY_RANK.get(existing_finding.severity, 0)
        if incoming_rank > existing_rank:
            highest_by_scanner_and_location[collapse_key] = finding
    return list(highest_by_scanner_and_location.values())


def compute_verdict(findings: list[Finding]) -> str:
    """Reduce a finding list to a single overall verdict.

    * ``"red"``    -> at least one high-severity finding.
    * ``"yellow"`` -> only medium findings.
    * ``"green"``  -> only low/advisory findings, or none at all.
    """
    if not findings:
        return "green"
    severity_set = {finding.severity for finding in findings}
    if "high" in severity_set:
        return "red"
    if "medium" in severity_set:
        return "yellow"
    return "green"


def match_additional_context(
    findings: list[Finding], additional_context_notes: list[str]
) -> list[Finding]:
    """Drop findings whose issue text shares a distinctive word with any note.

    The ``additional_context_notes`` list holds short notes the author
    supplies before the review (for example, ``"FY2025 numbers are
    correct"`` or ``"written for the CFO"``). The intent is to suppress
    findings that argue with something the author has already ruled on
    -- one class of "additional context" is the classical "don't-flag
    exception". Matching therefore has to be tolerant of paraphrase:
    the note rarely repeats the finding's exact wording.

    The rule below is deliberately conservative:

    1. Tokenise each note into words, lower-cased and stripped of
       punctuation.
    2. Discard short (< 4 char) and stopword tokens; what remains is
       the note's "distinctive vocabulary" -- product names, numbers,
       jargon, proper nouns.
    3. A finding drops out when any distinctive token appears in its
       issue text.

    This is a substring match on tokens, not full-string equality, so
    the note ``"FY2025 numbers are correct"`` suppresses findings that
    only mention ``"FY2025"``. Full-string matches (author copies the
    finding text verbatim) still work because every long token is
    shared.

    Notes that are purely framing (``"written for the CFO"``) rarely
    match any finding vocabulary and pass through untouched -- which is
    the intended behaviour: they belong in the scanner prompt as
    context, not as suppression rules.
    """
    if not findings or not additional_context_notes:
        return list(findings)

    distinctive_tokens = _collect_distinctive_tokens(additional_context_notes)
    if not distinctive_tokens:
        return list(findings)

    return [
        finding
        for finding in findings
        if not _issue_mentions_any_token(finding.issue, distinctive_tokens)
    ]


# Small, generic stopword set. Kept literal (no NLTK dependency) so the
# behaviour is auditable and does not shift with a library upgrade.
_ADDITIONAL_CONTEXT_STOPWORDS: frozenset[str] = frozenset(
    {
        "about",
        "after",
        "again",
        "also",
        "always",
        "and",
        "any",
        "are",
        "because",
        "been",
        "being",
        "both",
        "but",
        "correct",
        "could",
        "did",
        "does",
        "done",
        "each",
        "even",
        "ever",
        "every",
        "false",
        "fine",
        "from",
        "good",
        "has",
        "have",
        "having",
        "here",
        "how",
        "into",
        "just",
        "know",
        "like",
        "made",
        "make",
        "many",
        "more",
        "most",
        "much",
        "must",
        "need",
        "never",
        "next",
        "not",
        "now",
        "only",
        "over",
        "same",
        "seem",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "true",
        "under",
        "until",
        "very",
        "want",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "why",
        "will",
        "with",
        "would",
        "wrong",
        "your",
    }
)

_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9]{4,}")


def _collect_distinctive_tokens(additional_context_notes: list[str]) -> set[str]:
    distinctive_tokens: set[str] = set()
    for note_text in additional_context_notes:
        if not note_text:
            continue
        for token_match in _TOKEN_PATTERN.finditer(note_text.lower()):
            token = token_match.group(0)
            if token not in _ADDITIONAL_CONTEXT_STOPWORDS:
                distinctive_tokens.add(token)
    return distinctive_tokens


def _issue_mentions_any_token(issue_text: str, distinctive_tokens: set[str]) -> bool:
    issue_lower = issue_text.lower()
    return any(token in issue_lower for token in distinctive_tokens)
