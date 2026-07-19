"""deploy-web pre-publish content scan (design §4.1 / Q4).

Deploying makes content world-readable, so before upload the rendered output is
scanned for secrets + internal-data leaks. On any finding the caller
**blocks-and-warns** (shows what/where, requires explicit "publish anyway") —
it never silently redacts. Best-effort detection, not a guarantee.

Reuses KiroCrew's existing credential regexes (``security.get_credential_patterns()``)
and adds internal-data heuristics (Amazon hosts, ARNs, account ids) plus
deploy-specific patterns (private-key headers, GitHub PATs, OpenAI/Stripe keys).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from kiro_crew.security import get_credential_patterns

logger = logging.getLogger(__name__)

# Canonical credential patterns from the security module (single combined regex).
_CANONICAL_CRED = get_credential_patterns()

# Deploy-specific patterns that MUST be covered even if the canonical set evolves.
# These are a SUPERSET of the canonical set — covers base64-encoded AKIA keys,
# bare 40-char AWS secret access keys, PEM private key headers, GitHub PATs, and
# vendor API keys (OpenAI/Stripe). The scan module's pattern set must always
# detect everything that redact_credentials can detect, plus deploy-specific extras.
_DEPLOY_EXTRA = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),             # PEM private key header
    re.compile(r"ghp_[A-Za-z0-9]{36}"),                           # GitHub PAT
    re.compile(r"sk-[A-Za-z0-9]{20,}"),                           # OpenAI/Stripe keys
    # Base64-encoded AKIA key: the base64 of "AKIA..." starts with QUtJ
    re.compile(r"QUtJ[A-Za-z0-9+/]{12,}={0,2}"),                 # base64-encoded AKIA
]

# Bare 40-char base64 match (potential AWS secret access key). Handled separately
# because it false-positives on SHA-1 hashes and asset digests. Only classified as
# "credential" severity when the same line contains an AWS secret context indicator;
# otherwise downgraded to overridable "info" severity.
_BARE_40_RE = re.compile(r"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40}(?![A-Za-z0-9+/=])")
_AWS_CONTEXT_RE = re.compile(
    r"(?i)(aws|secret|access|key|token|credential)[_-]?(access)?[_-]?(key|id)?\s*[:=]"
)

# Internal-data heuristics (conservative — block-and-warn, user decides).
_INTERNAL_HOST_RE = re.compile(r"\b[\w.-]+\.(?:amazon\.com|aws\.dev|a2z\.com|amazon\.dev|corp\.amazon\.com)\b",
                               re.IGNORECASE)
_ARN_RE = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:\d{0,12}:[^\s\"'<>]+")
_ACCOUNT_ID_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")


@dataclass
class Finding:
    kind: str          # "credential" | "internal-host" | "aws-arn" | "aws-account-id"
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
    """Return a masked fingerprint for a credential finding.

    Never exposes the raw matched text — only the first 4 chars + length.
    """
    return f"{matched[:4]}…({len(matched)} chars)"


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


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


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

    def _capped() -> bool:
        return len(findings) >= _MAX_FINDINGS

    # Canonical credential patterns (from kiro_crew.security).
    for pat in _CANONICAL_CRED:
        for m in pat.finditer(text):
            if _capped():
                break
            findings.append(Finding("credential", _mask_credential(m.group(0)),
                                    _line_of_fast(line_offsets, m.start()), severity="credential"))
        if _capped():
            break

    # Deploy-specific extras (private key header, GitHub PAT, Stripe/OpenAI).
    if not _capped():
        for pat in _DEPLOY_EXTRA:
            for m in pat.finditer(text):
                if _capped():
                    break
                findings.append(Finding("credential", _mask_credential(m.group(0)),
                                        _line_of_fast(line_offsets, m.start()), severity="credential"))
            if _capped():
                break

    # Bare 40-char base64 match — downgraded to "info" unless the same line
    # contains an AWS secret context indicator (e.g. aws_secret_access_key=...).
    if not _capped():
        lines = text.split("\n")
        for m in _BARE_40_RE.finditer(text):
            if _capped():
                break
            line_num = _line_of_fast(line_offsets, m.start())
            line_text = lines[line_num - 1] if line_num <= len(lines) else ""
            if _AWS_CONTEXT_RE.search(line_text):
                severity = "credential"
            else:
                severity = "info"
            findings.append(Finding(
                "credential" if severity == "credential" else "bare-40-hash",
                _mask_credential(m.group(0)),
                line_num,
                severity=severity,
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
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    if not findings:
        return "No secrets or internal-data patterns detected."
    by_kind: dict[str, int] = {}
    for f in findings:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
    counts = ", ".join(f"{n} {k}" for k, n in sorted(by_kind.items()))
    lines = [f"⚠️ {len(findings)} potential issue(s) before publishing publicly: {counts}.",
             "Review each — publishing makes this content world-readable:"]
    for f in findings[:20]:
        snippet, _ = redact_credentials(f.snippet)
        snippet, _ = redact_exfiltration_urls(snippet)
        lines.append(f"  • line {f.line} [{f.kind}]: {snippet}")
    if len(findings) > 20:
        lines.append(f"  • … and {len(findings) - 20} more")
    return "\n".join(lines)
