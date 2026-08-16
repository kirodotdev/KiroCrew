"""Project a Crew agent spec onto KAS's ``ClientCustomAgent`` shape.

kiro-cli reads agent definitions from ``~/.kiro/agents/*.json`` and selects one
with a ``--agent`` flag. KAS has no such flag: it advertises only its own
built-in modes and takes client agents over the wire, as
``_meta.kiro.customAgents`` on ``session/new``. Each injected agent is
registered and then surfaces as a switchable mode, which is what lets the
ordinary ``session/set_mode`` activation work afterwards.

Two properties of KAS's schema drive the mapping and are easy to get wrong:

* ``prompt`` must be resolved content. A ``file://`` URI is the client's job to
  read, so a spec that points at a prompt file has to be inlined here.
* ``tools`` absent means NO tool access, not "all tools" — KAS resolves it as
  ``agent.tools ?? []``. The list is therefore always emitted explicitly, and an
  ambiguous spec fails closed rather than guessing ``*``.

Deliberately NOT projected, each for a reason a reader would otherwise have to
rediscover:

* ``mcpServers`` — Crew injects broker stubs as the session-level ``mcpServers``
  param, and a session-injected server outranks an agent-declared one. Carrying
  them twice risks a double registration. ``@server`` entries in ``tools`` still
  resolve, because KAS tags every MCP tool with ``@<server>`` from the server's
  name regardless of where it was declared.
* ``model`` — the model is set through its own protocol verb, so it has exactly
  one owner rather than being pinned in two places that can disagree.
* ``permissions`` — KAS's inline policy is keyed by ITS capability vocabulary,
  not by tool names, so translating Crew's auto-approve list would mean guessing
  identifiers. A wrong guess either no-ops or over-allows, and over-allowing is
  not a risk worth taking for a convenience feature; omitting the field leaves
  KAS's own default policy in force. Auto-approval parity is a follow-up.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.security import is_sensitive_path

logger = logging.getLogger(__name__)

#: Cap KAS enforces on ``_meta.kiro.customAgents`` (``z.array(...).max(50)``).
KAS_MAX_CUSTOM_AGENTS = 50

_PROMPT_FILE_SCHEME = "file://"

#: Pseudo-filesystems whose contents are process/kernel state, not documents.
_PSEUDO_FS_ROOTS = ("/proc", "/sys", "/dev")

#: Spec keys Crew writes that KAS's ``ClientCustomAgent`` has no home for.
#: Dropped with a named warning so a lost capability shows up in the log instead
#: of being discovered as missing behaviour later.
UNSUPPORTED_SPEC_KEYS = frozenset(
    {
        "allowedTools",
        "hooks",
        "slashCommand",
        "toolsSettings",
    }
)


class KasAgentTranslationError(ValueError):
    """A spec cannot be projected onto KAS's schema at all."""


#: System prompt fed to a prompt-less agent when projecting onto KAS. KAS
#: requires a non-empty prompt where kiro-cli tolerates an empty one, so any
#: agent that ships ``"prompt": ""`` (today only Crew's ``kirocrew-lite``, but
#: the fallback is deliberately not tied to it) would otherwise crash KAS
#: session creation. Deliberately generic and small: prompt-less agents run
#: small system-issued text tasks (titles, summaries, tags, rephrases), so the
#: full orchestration persona in ``prompt.md`` is both wrong and wasteful here.
#: Only the KAS path uses this — ``resolve_prompt`` is called solely from
#: ``build_kas_custom_agents`` — so the kiro-cli path keeps its empty-prompt
#: behaviour (kiro-cli supplies its own default) unchanged.
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


def _is_unsafe_prompt_path(path: Path) -> bool:
    """True if *path* must not be read and inlined into a KAS agent prompt.

    The prompt content is shipped to KAS over the wire, so a spec pointing at a
    credential store or a pseudo-filesystem would exfiltrate it. Blocks the
    credential/governance locations ``is_sensitive_path`` knows, plus ``/proc``,
    ``/sys`` and ``/dev`` (which it does not cover) — ``/proc/<pid>/environ`` is
    the sharp edge, exposing the gateway's own environment.
    """
    if is_sensitive_path(str(path)):
        return True
    posix = path.as_posix()
    return any(posix == root or posix.startswith(root + "/") for root in _PSEUDO_FS_ROOTS)


def resolve_prompt(
    spec: dict[str, Any],
    *,
    agent_id: str,
    agents_dir: Path,
) -> str:
    """Return the spec's prompt as literal text, reading a ``file://`` URI.

    Separated from :func:`to_client_custom_agent` so the projection itself stays
    pure. Because the resolved content is shipped to KAS over the wire, two
    safety rules apply to a ``file://`` URI:

    * A RELATIVE path is anchored to *agents_dir* (where the agent config lives,
      the documented base for ``file://./prompts/x.md``), never the gateway cwd,
      and may not escape it via ``..``.
    * The resolved path must not be a credential/governance location or a
      pseudo-filesystem (see :func:`_is_unsafe_prompt_path`).

    KAS requires a non-empty prompt where kiro-cli tolerates an empty one, so a
    spec with no prompt (Crew's own utility agents such as ``kirocrew-lite``
    ship ``"prompt": ""``) falls back to the small :data:`_KAS_FALLBACK_PROMPT`
    constant instead of crashing the session. The fallback is an inline literal,
    not a file read, so it carries none of the ``file://`` path's exfiltration /
    decode risk. Only KAS reaches this — the kiro-cli path keeps its empty-prompt
    behaviour untouched.
    """
    raw = spec.get("prompt")
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        # Missing or blank — an intentionally prompt-less agent. Substitute the
        # fallback so KAS's non-empty-prompt requirement is met.
        logger.warning(
            "agent %r has no prompt; falling back to the lightweight KAS prompt "
            "(KAS requires a non-empty prompt)",
            agent_id,
        )
        return _KAS_FALLBACK_PROMPT
    if not isinstance(raw, str):
        # A non-string prompt is a malformed spec, not a prompt-less one — fail
        # loud rather than silently running with unrelated fallback text.
        raise KasAgentTranslationError(
            f"agent {agent_id!r} prompt must be a string, got {type(raw).__name__}"
        )
    if not raw.startswith(_PROMPT_FILE_SCHEME):
        return raw
    ref = raw[len(_PROMPT_FILE_SCHEME) :]
    candidate = Path(ref).expanduser()
    if candidate.is_absolute():
        path = candidate.resolve()
    else:
        base = agents_dir.resolve()
        path = (base / ref).resolve()
        if path != base and base not in path.parents:
            raise KasAgentTranslationError(
                f"agent {agent_id!r} relative prompt {ref!r} escapes the agent directory"
            )
    if _is_unsafe_prompt_path(path):
        raise KasAgentTranslationError(
            f"agent {agent_id!r} prompt path {path} is not an allowed location; refusing to inline it"
        )
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise KasAgentTranslationError(
            f"agent {agent_id!r} prompt file {path} is unreadable: {exc}"
        ) from exc
    if not text.strip():
        raise KasAgentTranslationError(f"agent {agent_id!r} prompt file {path} is empty")
    return text


def _project_tools(spec: dict[str, Any], agent_id: str) -> str | list[str]:
    """Resolve the tool allowlist, failing closed when the spec is silent.

    ``"*"`` anywhere in the list is KAS's all-tools literal, which is a
    different type from a list, so it cannot simply be passed through.
    """
    raw = spec.get("tools")
    if raw == "*":
        return "*"
    if isinstance(raw, list):
        entries = [t for t in raw if isinstance(t, str) and t]
        if "*" in entries:
            return "*"
        return entries
    # Absent or malformed: KAS would resolve this to zero tools anyway. Emit that
    # explicitly and say so, rather than inferring an allowlist nobody wrote.
    logger.warning(
        "agent %r declares no usable 'tools' list; sending an empty allowlist, so "
        "it will run with no tool access on KAS",
        agent_id,
    )
    return []


def to_client_custom_agent(
    agent_id: str,
    spec: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    """Project one Crew agent spec onto a KAS ``ClientCustomAgent`` descriptor.

    Pure: *prompt* is already-resolved content (see :func:`resolve_prompt`).
    """
    if not agent_id:
        raise KasAgentTranslationError("agent id must be non-empty")
    if not prompt.strip():
        raise KasAgentTranslationError(f"agent {agent_id!r} prompt is empty")

    dropped = sorted(k for k in UNSUPPORTED_SPEC_KEYS if spec.get(k))
    if dropped:
        logger.warning(
            "agent %r: dropping spec keys with no KAS equivalent: %s",
            agent_id,
            ", ".join(dropped),
        )

    out: dict[str, Any] = {
        "id": agent_id,
        "prompt": prompt,
        "tools": _project_tools(spec, agent_id),
    }

    description = spec.get("description")
    if isinstance(description, str) and description:
        out["description"] = description

    excluded = spec.get("excludedTools")
    if isinstance(excluded, list):
        entries = [t for t in excluded if isinstance(t, str) and t]
        if entries:
            out["excludedTools"] = entries

    include_mcp = spec.get("includeMcpJson")
    if isinstance(include_mcp, bool):
        out["includeMcpJson"] = include_mcp

    resources = spec.get("resources")
    if isinstance(resources, list):
        entries = [r for r in resources if isinstance(r, str) and r]
        if entries:
            out["resources"] = entries

    return out


def load_agent_spec(agents_dir: Path, agent_id: str) -> dict[str, Any]:
    """Read a materialized agent spec.

    Takes the directory explicitly rather than resolving it here so this module
    stays free of :mod:`kiro_crew.agent`, which imports the config loader and
    would form an import cycle.
    """
    path = agents_dir / f"{agent_id}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise KasAgentTranslationError(f"agent spec {path} is unreadable: {exc}") from exc
    except ValueError as exc:
        raise KasAgentTranslationError(f"agent spec {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise KasAgentTranslationError(f"agent spec {path} is not an object")
    return raw


def build_kas_custom_agents(agents_dir: Path, agent_id: str) -> list[dict[str, Any]]:
    """Build the ``_meta.kiro.customAgents`` batch that binds *agent_id* on KAS.

    One entry: KAS registers the injected agent, it then surfaces as a mode, and
    the ordinary ``session/set_mode`` activation can select it. Without this the
    session stays on KAS's own default mode and the operator's prompt and tool
    configuration have no effect.

    A prompt-less spec (e.g. ``kirocrew-lite``) is projected with the small
    :data:`_KAS_FALLBACK_PROMPT` so it satisfies KAS's non-empty-prompt
    requirement instead of crashing the session (see :func:`resolve_prompt`).
    """
    spec = load_agent_spec(agents_dir, agent_id)
    prompt = resolve_prompt(spec, agent_id=agent_id, agents_dir=agents_dir)
    return [to_client_custom_agent(agent_id, spec, prompt)]
