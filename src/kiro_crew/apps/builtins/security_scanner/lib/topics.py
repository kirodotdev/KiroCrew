"""Security topics and their per-agent prompt construction.

A *topic* is a narrow security domain scanned by one agent. Per the design
decisions (see the design doc + SECURITY_NOTES.md):

- NO pre-analysis stage and NO file pre-selection — the agent uses its own
  grep/glob/read tools to decide what to look at.
- The agent is handed ONLY its topic's tagged knowledge slice, not the whole
  library, so its context stays focused.

The prompt ends with a strict OUTPUT CONTRACT: a JSON array of findings. The
scan engine parses that contract deterministically (:mod:`lib.scan`), so the
wording here and the parser there are two halves of one interface.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import V1_TOPICS, KnowledgePattern, Suppression


@dataclass
class SecurityTopic:
    id: str
    name: str
    description: str
    tags: list[str] = field(default_factory=list)
    max_findings: int = 20


TOPICS: dict[str, SecurityTopic] = {
    "path-traversal": SecurityTopic(
        id="path-traversal",
        name="Path Traversal & File System",
        description=(
            "Attacker-controlled paths escaping an intended root: os.path.join with "
            "absolute/'..' input, prefix checks that precede symlink resolution, "
            "zip-slip on archive extraction, and any user-influenced filesystem read/write."
        ),
        tags=["fs", "path", "traversal", "symlink", "archive"],
    ),
    "auth-bypass": SecurityTopic(
        id="auth-bypass",
        name="Authentication & Authorization Bypass",
        description=(
            "Token/HMAC comparison timing side-channels, client-asserted identity trusted "
            "without a signed session, authentication-without-authorization (IDOR), and "
            "consent gates that check a token EXISTS rather than that its CONTENT matches."
        ),
        tags=["auth", "session", "token", "authorization", "consent", "timing"],
    ),
    "prompt-injection": SecurityTopic(
        id="prompt-injection",
        name="Prompt Injection & Memory Poisoning",
        description=(
            "User/untrusted text reaching a system or tool prompt without a trust boundary: "
            "stored memory poisoning, untrusted tool/web output treated as instructions, and "
            "injection-to-action / injection-to-exfiltration paths lacking a confirmation gate."
        ),
        tags=["prompt", "memory", "injection", "tools", "exfil"],
    ),
}


def active_topics(topic_ids: list[str] | None = None) -> list[SecurityTopic]:
    """Resolve topic ids to definitions. ``None`` / ``["all"]`` -> the v1 set.
    Unknown ids are ignored (the scan simply doesn't cover them)."""
    if not topic_ids or topic_ids == ["all"]:
        return [TOPICS[t] for t in V1_TOPICS]
    return [TOPICS[t] for t in topic_ids if t in TOPICS]


def build_topic_prompt(
    topic: SecurityTopic,
    knowledge: list[KnowledgePattern],
    suppressions: list[Suppression],
    target_desc: str,
) -> str:
    """Build the focused scanning prompt for one topic agent.

    ``knowledge`` and ``suppressions`` are already the topic's tagged slice
    (from :meth:`KnowledgeStore.for_topic` / ``suppressions_for_topic``).
    """
    know_lines = "\n".join(
        f"- [{p.confidence:.2f}] {p.pattern}"
        + (f"\n    exploit: {p.exploit_template}" if p.exploit_template else "")
        for p in knowledge
    ) or "(no learned patterns yet — reason from the topic description)"

    supp_lines = "\n".join(f"- {s.pattern} (reason: {s.reason})" for s in suppressions) or "(none)"

    return f"""You are a security analyst scanning for **{topic.name}** vulnerabilities.

TARGET: {target_desc}

TOPIC SCOPE:
{topic.description}

KNOWN PATTERNS FOR THIS TOPIC (your knowledge slice — use as starting points, not limits):
{know_lines}

KNOWN FALSE POSITIVES — do NOT report anything matching these:
{supp_lines}

HOW TO WORK:
- Use your own grep/glob/read/code-search tools to find and inspect relevant code.
  There is NO pre-built file list — you decide what to look at.
- Only read the target's source; do NOT modify any file.
- Trace whether attacker-controlled input actually reaches the dangerous sink
  before reporting. Prefer a few well-substantiated findings over many guesses.
- Report at most {topic.max_findings} findings.

OUTPUT CONTRACT (STRICT):
Return ONLY a JSON array (optionally inside a ```json fence). Each element:
{{
  "title": "short title",
  "location": "path/to/file.py:LINE",
  "severity": "critical|high|medium|low|info",
  "description": "why this is exploitable, tracing input -> sink",
  "exploit_suggestion": "how a PoC would demonstrate it (sandbox-only, non-destructive)"
}}
If you find nothing, return []. Do not include prose outside the JSON array."""
