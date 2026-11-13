"""The prompt text a KAS agent is handed: the ``prompt`` spelling, the ``file://`` path, and lite's brief.

Split out of :mod:`kiro_crew.acp.kas_agents`, which reads a ``file://`` prompt and
ships the text over the wire (:func:`kiro_crew.acp.kas_agents.resolve_prompt`).
Which resolved paths may be read is :mod:`kiro_crew.acp.kas_prompt_paths`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from kiro_crew.agent_files import LITE_AGENT_FILENAME

_PROMPT_FILE_SCHEME = "file://"


def spec_prompt(spec: dict[str, Any]) -> Any:
    """*spec*'s prompt as written: ``prompt``, else kiro-cli's ``systemPrompt`` alias.

    kiro-cli reads both spellings into one field (``#[serde(rename = "prompt",
    alias = "systemPrompt")]``); ``prompt`` wins when both are set.
    """
    raw = spec.get("prompt")
    return spec.get("systemPrompt") if raw is None else raw


def prompt_file_path(ref: str, agents_dir: Path) -> Path:
    """The resolved file a ``file://`` *ref* names, a relative one anchored to *agents_dir*.

    ``$VAR`` / ``${VAR}`` and ``~`` are expanded first, as kiro-cli's v2 engine
    expands them (``file://$HOME/p.md``); an unset variable is left as written, so
    the path does not exist. Only a *ref* that is not already absolute is expanded:
    an absolute one is used as written, so a checked path handed on by the
    project-checkout rule (``acp.harness.kas.resolve_projected_spec``) cannot be
    changed by the gateway's environment after it was checked.

    Raises ``OSError`` or ``RuntimeError`` when the path cannot be expanded
    (``~nosuchuser``) or resolved (a symlink loop).
    """
    candidate = Path(ref)
    if not candidate.is_absolute():
        candidate = Path(os.path.expandvars(ref)).expanduser()
    return (candidate if candidate.is_absolute() else agents_dir / candidate).resolve()


def prompt_unread_notice(agent: str, paths: list[Path]) -> str:
    """The session-start notice for a ``file://`` prompt the session runs without, or ``""``."""
    if not paths:
        return ""
    return (
        f"ℹ Agent {agent}'s prompt file {paths[0]} is missing, unreadable or empty, "
        "so this session runs on the backend's default prompt. Point the agent's "
        "prompt at a readable file to use your own."
    )


#: The prompt Crew's own background agent ``kirocrew-lite`` gets on KAS. It is the
#: ONLY agent that gets it (:data:`_KAS_FALLBACK_PROMPT_AGENTS`): lite runs small
#: system-issued text tasks (titles, summaries, tags, rephrases), so a short
#: "do exactly the request, nothing else" brief keeps those replies to their
#: format. Every other prompt-less spec -- a user's, an app's, a crewmate's
#: worker -- is a user-chosen agent, and on kiro-cli such a spec runs with
#: kiro-cli's own default prompt and nothing added. KAS behaves the same way when
#: the projected ``prompt`` is ``""``: the field is required on the wire (an
#: entry without it is refused as ``_kiro/customAgent/not_found`` and the session
#: falls back to KAS's ``vibe`` mode), but an empty string registers the agent
#: and its prompt adds nothing to KAS's own base prompt, as a non-empty one is
#: layered on top of it (measured live on kiro-cli 2.35.1). So those agents send
#: ``""`` and this text, which tells the model it has no tools, never reaches an
#: agent that has them.
_KAS_FALLBACK_PROMPT = """\
You are a Kiro Crew lightweight background worker. You are dispatched by the
system — never by a human in a chat — to perform one small, self-contained text
task per request: naming or summarizing a conversation, classifying or tagging
content, rephrasing a line, suggesting a short label, and similar. The specific
task is fully described in each request.

- Do exactly what the request asks, and only that. Treat its stated output
  format as binding: if it asks for a single line, a length limit, or JSON,
  return exactly that — no preamble, no explanation, no markdown fences unless
  the request asks for them.
- Be concise and deterministic. Prefer the shortest correct answer; add no
  commentary, caveats, or follow-up questions.
- You have no tools and touch no external state. Work only from the text in the
  request. If it is empty or unintelligible, return a minimal safe default (an
  empty string or a generic label) rather than guessing at length.
- This is not a conversation: no user to address, no session to remember. Each
  request stands alone.
"""

#: The agents that get :data:`_KAS_FALLBACK_PROMPT` when their spec has no
#: prompt: Crew's own background agent, by its id (``agent_files.LITE_AGENT_FILENAME``).
_KAS_FALLBACK_PROMPT_AGENTS = frozenset({Path(LITE_AGENT_FILENAME).stem})
