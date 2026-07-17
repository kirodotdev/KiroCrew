"""deploy-web pre-publish content scan (design §4.1 / Q4).

Deploying makes content world-readable, so before upload the rendered output is
scanned for secrets + internal-data leaks. On any finding the caller
**blocks-and-warns** (shows what/where, requires explicit "publish anyway") —
it never silently redacts. Best-effort detection, not a guarantee.

Reuses KiroCrew's existing credential regexes (``security.get_credential_patterns()``)
and adds internal-data heuristics (Amazon hosts, ARNs, account ids).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:  # reuse the canonical credential patterns (public accessor)
    from kiro_crew.security import get_credential_patterns

    _CRED = get_credential_patterns()
except ImportError:  # pragma: no cover - defensive
    logger.warning(
        "deploy-web: could not import credential patterns from kiro_crew.security; "
        "pre-publish credential scanning is DISABLED (internal-data heuristics still run)."
    )
    _CRED = []

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


def _snip(s: str, limit: int = 80) -> str:
    s = s.strip().replace("\n", " ")
    return s if len(s) <= limit else s[:limit] + "…"


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def scan_content(text: str) -> list[Finding]:
    """Return all secret/internal-data findings (empty list = clean)."""
    findings: list[Finding] = []
    if not text:
        return findings

    for pat in _CRED:
        for m in pat.finditer(text):
            findings.append(Finding("credential", _snip(m.group(0)), _line_of(text, m.start())))

    for m in _INTERNAL_HOST_RE.finditer(text):
        findings.append(Finding("internal-host", _snip(m.group(0)), _line_of(text, m.start())))

    for m in _ARN_RE.finditer(text):
        findings.append(Finding("aws-arn", _snip(m.group(0)), _line_of(text, m.start())))

    # Account ids not already covered by an ARN match on the same line.
    arn_lines = {f.line for f in findings if f.kind == "aws-arn"}
    for m in _ACCOUNT_ID_RE.finditer(text):
        ln = _line_of(text, m.start())
        if ln not in arn_lines:
            findings.append(Finding("aws-account-id", _snip(m.group(0)), ln))

    findings.sort(key=lambda f: (f.line, f.kind))
    return findings


def summarize(findings: list[Finding]) -> str:
    """Human-readable one-block summary of findings for the block-and-warn prompt."""
    if not findings:
        return "No secrets or internal-data patterns detected."
    by_kind: dict[str, int] = {}
    for f in findings:
        by_kind[f.kind] = by_kind.get(f.kind, 0) + 1
    counts = ", ".join(f"{n} {k}" for k, n in sorted(by_kind.items()))
    lines = [f"⚠️ {len(findings)} potential issue(s) before publishing publicly: {counts}.",
             "Review each — publishing makes this content world-readable:"]
    for f in findings[:20]:
        lines.append(f"  • line {f.line} [{f.kind}]: {f.snippet}")
    if len(findings) > 20:
        lines.append(f"  • … and {len(findings) - 20} more")
    return "\n".join(lines)
