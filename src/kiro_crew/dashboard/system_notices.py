"""Assistant-role system notices the gateway injects into a slot's feed.

Status reports -- the auto-compaction notices and the session-reload
confirmation -- not real turns. Every scan that walks for "the last real
message" (the conversation floor, the sidebar preview, backfill replay) must
skip them, and the frontend keeps a twin of this set
(``website/src/lib/systemNotice.ts``): a kind skipped on one side but not the
other leaves the sidebar showing notice boilerplate while the chat pane shows
the real turn, or vice versa.
"""

import re

from kiro_crew.constants import (
    SUBAGENT_BATCH_COMPLETION_PREFIX,
    SUBAGENT_COMPLETION_META_KEY,
    SUBAGENT_COMPLETION_PREFIX,
)
from kiro_crew.preview_text import drop_format_chars

SESSION_RELOAD_KIND = "session_reload"

SYSTEM_NOTICE_KINDS: frozenset[str] = frozenset({"compaction", SESSION_RELOAD_KIND})


def is_system_notice(role: object, meta: object) -> bool:
    """True for an assistant-role system notice row.

    ``meta`` is checked with ``isinstance`` rather than the ``or {}`` idiom:
    ``append``'s ``meta: dict | None`` is not enforced at runtime, so a truthy
    non-dict would raise ``AttributeError`` on ``.get``.
    """
    return (
        role == "assistant" and isinstance(meta, dict) and meta.get("kind") in SYSTEM_NOTICE_KINDS
    )


#: The injected workflow-completion envelope (``workflow_inject.py``) is written
#: under the assistant role, like the system notices above, and is likewise
#: status rather than speech -- but only when its HEADER parses. The frontend
#: (``WorkflowCompletionCard.tsx``, ``WF_COMPLETION_RE``) deliberately draws a
#: malformed envelope as visible markdown rather than swallowing it, so a
#: prefix-only test here would hide from the roster a row the chat shows. Same
#: header shape as the frontend regex; ``test/fixtures/crewmate_speech_rows.json``
#: pins the two twins to one verdict per row.
WORKFLOW_COMPLETION_PREFIX = "[Workflow completion event]"
WORKFLOW_COMPLETION_HEADER_RE = re.compile(
    r"^\[Workflow completion event\]\s*\nWorkflow `[^`]+` \(wf_[A-Za-z0-9_]+\) → \*\*[a-z]+\*\*"
)

#: The injected sub-agent completion envelope is written under THREE roles --
#: ``subagent`` (the queue-drained injection), ``assistant`` (the Slack
#: gateway's delivery-timeout and orphan-notify variants) and ``user`` in old
#: scrollback -- so a role gate alone lets the assistant-role copy through as
#: the crewmate's own words. Status, not speech, when it parses the way the
#: frontend card parses it (``subagentCompletion.ts``: the ``meta`` facts the
#: gateway stamps, else the header line); a malformed envelope is drawn there
#: as visible markdown and is therefore quoted here too.
_SUBAGENT_OUTCOMES: frozenset[str] = frozenset({"ok", "failed", "stopped", "interrupted"})
_SUBAGENT_AGENT_HEADER_RE = re.compile(r"^Agent `[^`\n]+`(?: \([^)\n]*\))?([^\n]*)$", re.M)
#: The card refuses a per-agent header with no outcome glyph beside the id
#: ("degrade to normal rendering rather than guessing an outcome"), so a user who
#: pastes `[Subagent completion event]\nAgent \`demo\`` back into the chat is
#: drawn -- and must be quoted here too. Same glyph set as the frontend.
_SUBAGENT_OUTCOME_GLYPH_RE = re.compile("[✅❌⏹⚠]")
_SUBAGENT_WAVE_RE = re.compile(
    r"^Batch results \d+/\d+ — wave finished: \d+ ✅ · \d+ ❌ · \d+ ⏹ of \d+ agents\.", re.M
)
_SUBAGENT_CHUNK_RE = re.compile(
    r"^Batch results \d+/\d+ — \d+ of \d+ delivered, \d+ still running\.", re.M
)


def is_subagent_completion_row(content: object, meta: object) -> bool:
    """True for a sub-agent completion envelope whose header parses.

    Twin of ``isSubagentCompletionMessage`` (``subagentCompletion.ts``): the
    ``meta`` facts win when they name a matching kind, else the wire header is
    matched -- the per-agent ``Agent <id>`` line for a single event, the
    wave / chunk digest line for a batch.
    """
    if not isinstance(content, str):
        return False
    single = content.startswith(SUBAGENT_COMPLETION_PREFIX)
    batch = content.startswith(SUBAGENT_BATCH_COMPLETION_PREFIX)
    if not (single or batch):
        return False
    facts = meta.get(SUBAGENT_COMPLETION_META_KEY) if isinstance(meta, dict) else None
    if isinstance(facts, dict):
        if single and facts.get("kind") == "single":
            agent_id = facts.get("agentId")
            if (
                isinstance(agent_id, str)
                and agent_id
                and facts.get("outcome") in _SUBAGENT_OUTCOMES
            ):
                return True
        if batch and facts.get("kind") == "batch":
            return True
    head = content.split("\n\n", 1)[0]
    if batch:
        return bool(_SUBAGENT_WAVE_RE.search(head) or _SUBAGENT_CHUNK_RE.search(head))
    header = _SUBAGENT_AGENT_HEADER_RE.search(head)
    return header is not None and _SUBAGENT_OUTCOME_GLYPH_RE.search(header.group(1)) is not None


#: Roles whose text is something a person or the agent SAID to the other.
SPEECH_ROLES: frozenset[str] = frozenset({"user", "assistant"})


def is_speech_row(role: object, content: object, meta: object) -> bool:
    """True for a row that is speech: a user or assistant row with visible text
    that is neither a system notice, nor a sub-agent completion envelope, nor
    -- under the assistant role only -- a well-formed workflow-completion
    envelope. The gateway injects workflow envelopes under the assistant role;
    a user who PASTES one has said something, and the chat draws it.

    The one backend spelling of what a crew member's chat DRAWS (crew-mode.md,
    "A crewmate's chat"; the frontend twin is ``isCrewmateSpeech`` in
    ``website/src/components/chat/crewmateBubbles.ts``). The Crew Members
    roster preview reads it so the row beside the chat quotes what the chat
    shows -- a compaction summary or a workflow envelope would otherwise
    overwrite the last thing the member said while the chat hides it.
    """
    if role not in SPEECH_ROLES:
        return False
    if is_system_notice(role, meta):
        return False
    # The say-nothing reply a quiet patrol ends on is a bare U+200B: format
    # characters only, truthy as a string, invisible on screen. Not speech.
    if not (isinstance(content, str) and drop_format_chars(content).strip()):
        return False
    if is_subagent_completion_row(content, meta):
        return False
    return not (
        role == "assistant"
        and content.startswith(WORKFLOW_COMPLETION_PREFIX)
        and WORKFLOW_COMPLETION_HEADER_RE.match(content) is not None
    )
