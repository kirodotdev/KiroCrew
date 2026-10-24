"""deploy-web pre-publish content scan.

Deploying makes content world-readable, so before upload the rendered output is
scanned for secrets + internal-data leaks. On any finding the caller
**blocks-and-warns** (shows what/where, requires explicit "publish anyway") —
it never silently redacts. Best-effort detection, not a guarantee.

Reuses the existing credential scan (``security.redaction``, linear on repeated PEM headers)
and adds internal-data heuristics (internal hosts, ARNs, account ids) plus
deploy-specific patterns (private-key headers, GitHub PATs, OpenAI/Stripe keys).
"""
from __future__ import annotations

import bisect
import logging
import re
from dataclasses import dataclass

from kiro_crew.credential_patterns import AWS_KEY_ID, URL_SIGNING_VALUE
from kiro_crew.security.redaction import (
    _credential_matches,
    _pass1_rule,
    redact_credentials_with_records,
)

logger = logging.getLogger(__name__)

# Deploy-specific patterns that MUST be covered even if the canonical set evolves.
# These are a SUPERSET of the canonical set — covers bare 40-char AWS secret
# access keys, PEM private key headers, GitHub PATs, and vendor API keys
# (OpenAI/Stripe). The scan module's pattern set must always
# detect everything that redact_credentials can detect, plus deploy-specific extras.
_DEPLOY_EXTRA = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),             # PEM private key header
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                           # GitHub PAT
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                           # OpenAI/Stripe keys
    # A presigned/signed-link signing value. The key ID is an identifier and ships
    # as a warning, but the signature and session token make the link WORK, so
    # publishing one is a hard credential finding.
    re.compile(URL_SIGNING_VALUE),
]

# Bare 40-char base64 match (potential AWS secret access key). Handled separately
# because it false-positives on SHA-1 hashes and asset digests. Always an
# overridable "info" finding: a labelled secret is a hard finding through the
# canonical records, which read the label.
_BARE_40_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40}(?![A-Za-z0-9+/=])")

# Internal-data heuristics (conservative — block-and-warn, user decides).
_INTERNAL_HOST_RE = re.compile(r"\b[\w.-]+\.(?:amazon\.com|aws\.dev|a2z\.com|amazon\.dev|corp\.amazon\.com)\b",
                               re.IGNORECASE)
_ARN_RE = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:\d{0,12}:[^\s\"'<>]+")
_ACCOUNT_ID_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
# An AWS access key ID is Identifier tier, like an account id: worth a warning
# before content goes world-readable, never a hard block.
_AWS_KEY_ID_RE = re.compile(AWS_KEY_ID)


@dataclass
class Finding:
    kind: str          # "credential" | "internal-host" | "aws-arn" | "aws-account-id" | "aws-key-id"
    snippet: str       # short matched text (truncated)
    line: int          # 1-based line number
    severity: str = ""  # "credential" (hard-block) | "info" (overridable)


# Kinds that represent hard credential findings — override_scan CANNOT clear these.
CREDENTIAL_KINDS: frozenset[str] = frozenset({"credential", "unscanned-large-file"})


def is_credential_finding(f: Finding) -> bool:
    """Return True if the finding is a hard credential/security finding."""
    return f.severity == "credential" or f.kind in CREDENTIAL_KINDS


def _snip(s: str, limit: int = 80) -> str:
    s = s.strip().replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def _mask_credential(matched: str) -> str:
    """Return a masked fingerprint for a credential finding: its rule and length.

    No character of the match is copied out: a match may be a detached part of
    a credential (a signature after ``Signature=``), whose first characters are
    secret too.
    """
    return f"[{_pass1_rule(matched)[0]}]…({len(matched)} chars)"


#: The secret access key the AWS documentation prints in every example.
_AWS_DOC_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfi" + "CYEXAMPLEKEY"

#: A labelled value that documentation writes in place of a secret. Whole-value
#: match only, so a real secret that merely contains one of these words is not
#: one: `your-name` / `YOUR_NAME` (letter words only), `changeme`, `placeholder`,
#: `replace-me`, a run of `x`, and the AWS documentation's example secret
#: (gitleaks and git-secrets allow the same).
_PLACEHOLDER_RE = re.compile(
    r"your(?:[-_][a-z]{1,12}){1,6}|YOUR(?:_[A-Z]{1,12}){1,6}|changeme[0-9]{0,4}"
    r"|placeholder(?:[-_][a-z]{1,12}){0,4}|replace[-_]?me|x{6,64}|X{6,64}"
    r"|" + re.escape(_AWS_DOC_SECRET)
)

#: The record rules a placeholder can stand in: a value behind a label or an
#: ``Authorization`` scheme. A vendor token, a paired key or a signing value is
#: never a placeholder.
_PLACEHOLDER_RULES = frozenset({"secret_label", "authorization_header",
                                "aws_secret_access_key", "aws_session_token"})


def is_documented_placeholder(rule: str, value: str) -> bool:
    """Whether a labelled *value* found by *rule* is a documented placeholder."""
    if rule not in _PLACEHOLDER_RULES:
        return False
    # A Basic header is never a placeholder: ``admin:admin`` and its kind are
    # working factory defaults, not documentation text.
    return _PLACEHOLDER_RE.fullmatch(value) is not None


_MAX_FINDINGS = 500


def _build_line_index(text: str) -> list[int]:
    """Precompute newline-start offsets for O(log n) line lookups via bisect."""
    offsets = [0]
    idx = 0
    while True:
        idx = text.find("\n", idx)
        if idx == -1:
            break
        idx += 1
        offsets.append(idx)
    return offsets


def _line_of_fast(line_offsets: list[int], pos: int) -> int:
    """O(log n) line number lookup using precomputed offsets."""
    import bisect
    return bisect.bisect_right(line_offsets, pos)


def scan_content(text: str) -> list[Finding]:
    """Return all secret/internal-data findings (empty list = clean).

    Uses precomputed newline offsets for O(log n) per-match line lookups
    (replaces the O(n) text.count approach). Capped at _MAX_FINDINGS to
    bound output size on pathological inputs.
    """
    findings: list[Finding] = []
    if not text:
        return findings

    line_offsets = _build_line_index(text)
    credential_spans: list[tuple[int, int]] = []

    def _capped() -> bool:
        return len(findings) >= _MAX_FINDINGS

    # Canonical credential patterns (from kiro_crew.security), scanned in linear time.
    # Hard findings are never stopped by the cap: it bounds only the overridable
    # ones, so a flood of those cannot hide a credential.
    for m in _credential_matches(text):
        credential_spans.append((m.start(), m.end()))
        findings.append(Finding("credential", _mask_credential(m.group(0)),
                                _line_of_fast(line_offsets, m.start()), severity="credential"))

    # Deploy-specific extras (private key header, GitHub PAT, Stripe/OpenAI).
    for pat in _DEPLOY_EXTRA:
        for m in pat.finditer(text):
            if "[REDACTED" in m.group(0):
                continue  # already sanitized: a redaction tag, not a value
            credential_spans.append((m.start(), m.end()))
            findings.append(Finding("credential", _mask_credential(m.group(0)),
                                    _line_of_fast(line_offsets, m.start()), severity="credential"))

    # The core redactor reads more than the fixed spellings above: labelled values
    # (any spelling of the label), a secret beside its key ID, the normalised
    # view, encoded runs, the vendor catalog and every encoding of a signing
    # parameter's name. Reuse its records so each value it would redact is a
    # hard credential finding here too -- publishing a working signed link is
    # one, so the key ID beside it does not soften it.
    # The cap bounds the REPORT, never the verdict: every record is classified,
    # and a hard finding is kept past the cap, so a flood of overridable ones
    # cannot hide it.
    _, _, records = redact_credentials_with_records(text)
    cursor = 0
    # The fixed-pattern spans found above, merged and sorted, for a log-time
    # overlap test (records are disjoint from each other).
    prior: list[tuple[int, int]] = []
    for s, e in sorted(credential_spans):
        if prior and s <= prior[-1][1]:
            prior[-1] = (prior[-1][0], max(prior[-1][1], e))
        else:
            prior.append((s, e))
    prior_starts = [s for s, _ in prior]
    for rec in records:
        placeholder = is_documented_placeholder(rec.rule, rec.value)
        if _capped() and placeholder:
            continue
        pos = text.find(rec.value, cursor)
        if pos < 0:
            pos = text.find(rec.value)
        if pos < 0:
            continue
        start, end = pos, pos + len(rec.value)
        cursor = end
        i = bisect.bisect_left(prior_starts, end)
        if i > 0 and prior[i - 1][1] > start:
            continue
        credential_spans.append((start, end))
        # A record's value has no public prefix to show, so it is described
        # by its rule and length alone; no character of it is copied out.
        # A documented placeholder behind a label is still reported, but as
        # an overridable finding: docs show `api_key: YOUR_API_KEY_HERE`.
        findings.append(Finding("credential-placeholder" if placeholder else "credential",
                                f"[{rec.rule}]…({len(rec.value)} chars)",
                                _line_of_fast(line_offsets, pos),
                                severity="info" if placeholder else "credential"))

    # Bare 40-char base64 match: informational only. A labelled or paired
    # secret is a hard finding through the canonical records above, which read
    # the label itself; a word like ``key =`` on the same line is no label, and
    # a public digest there is not a credential.
    for m in _BARE_40_RE.finditer(text):
        if _capped():
            break
        findings.append(Finding(
            "bare-40-hash",
            _mask_credential(m.group(0)),
            _line_of_fast(line_offsets, m.start()),
            severity="info",
        ))

    if not _capped():
        for m in _INTERNAL_HOST_RE.finditer(text):
            if _capped():
                break
            findings.append(Finding("internal-host", _snip(m.group(0)),
                                    _line_of_fast(line_offsets, m.start()), severity="info"))

    if not _capped():
        for m in _ARN_RE.finditer(text):
            if _capped():
                break
            findings.append(Finding("aws-arn", _snip(m.group(0)),
                                    _line_of_fast(line_offsets, m.start()), severity="info"))

    if not _capped():
        # Account ids not already covered by an ARN match on the same line.
        arn_lines = {f.line for f in findings if f.kind == "aws-arn"}
        for m in _ACCOUNT_ID_RE.finditer(text):
            if _capped():
                break
            ln = _line_of_fast(line_offsets, m.start())
            if ln not in arn_lines:
                findings.append(Finding("aws-account-id", _snip(m.group(0)), ln, severity="info"))

    if not _capped():
        for m in _AWS_KEY_ID_RE.finditer(text):
            if _capped():
                break
            findings.append(Finding("aws-key-id", _snip(m.group(0)),
                                    _line_of_fast(line_offsets, m.start()), severity="info"))

    truncated = _capped()
    findings.sort(key=lambda f: (f.line, f.kind))
    if truncated:
        findings.append(Finding(
            "findings-truncated",
            f"scan stopped at {_MAX_FINDINGS} findings — review the first batch before continuing",
            0,
            severity="info",
        ))
    return findings


def summarize(findings: list[Finding]) -> str:
    """Human-readable one-block summary of findings for the block-and-warn prompt.

    Snippets are passed through credential redaction as defense-in-depth — even
    though _mask_credential is applied at Finding creation time for credential-kind
    findings, this catches any edge case where raw matched content leaks.
    """
    from kiro_crew.security import redact

    if not findings:
        return "No secrets or internal-data patterns detected."
    by_kind: dict[str, int] = {}
    for f in findings:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
    counts = ", ".join(f"{n} {k}" for k, n in sorted(by_kind.items()))
    lines = [f"⚠️ {len(findings)} potential issue(s) before publishing publicly: {counts}.",
             "Review each — publishing makes this content world-readable:"]
    for f in findings[:20]:
        snippet = redact(f.snippet)
        lines.append(f"  • line {f.line} [{f.kind}]: {snippet}")
    if len(findings) > 20:
        lines.append(f"  • … and {len(findings) - 20} more")
    return "\n".join(lines)
