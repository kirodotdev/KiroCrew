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

``permissions`` IS projected, and is the one field that changes behaviour rather
than just describing it. KAS's policy is keyed by its own capability vocabulary
instead of by tool name, so it is not a rename of Crew's ``allowedTools`` — see
:mod:`kiro_crew.acp.kas_permissions` for the mapping and for why an entry it
cannot classify is left to prompt. Omitting the field is not the neutral choice
it looks like: with no policy, KAS resolves every request to ``ask``, so a spec
that auto-approves a dozen tools on kiro-cli would prompt for all of them here.
It is derived from ``allowedTools`` and from nothing else: a ``permissions``
block already in the spec is NOT relayed, because ``allowedTools`` is the only
auto-approve input Crew's governance ceiling has filtered.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from kiro_crew.acp.kas_permissions import allowed_tools_to_permissions
from kiro_crew.platform.governance import may_skip_gate_now
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Cap KAS enforces on ``_meta.kiro.customAgents`` (``z.array(...).max(50)``).
KAS_MAX_CUSTOM_AGENTS = 50

_PROMPT_FILE_SCHEME = "file://"

#: Pseudo-filesystems whose contents are process/kernel state, not documents.
_PSEUDO_FS_ROOTS = ("/proc", "/sys", "/dev")

#: Spec keys with no slot in KAS's ``ClientCustomAgent`` wire schema.
#:
#: "No slot on the wire" is NOT "no such capability in KAS" — conflating the two
#: is what kept ``hooks`` written off as unsupported. KAS runs pre/post-tool-use
#: hooks natively and loads them from an agent profile ON DISK (it even accepts
#: Crew's object form), so what is lost here is a delivery path, not a feature:
#: an agent injected over the wire cannot carry them.
#:
#: ``allowedTools`` is deliberately NOT in this set. It has no slot either, but
#: :mod:`kiro_crew.acp.kas_permissions` translates it into ``permissions``, so
#: the capability survives under another name.
UNSUPPORTED_SPEC_KEYS = frozenset(
    {
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


def _ceiling_permitted(allowed_tools: Any, agent_id: str) -> list[str]:
    """``allowedTools`` with every entry the governance ceiling withholds removed.

    The five writers of an ``allowedTools`` list already consult the ceiling when
    they WRITE, so on a freshly rebuilt spec this changes nothing. It is here
    because projection READS a file, and the file can predate the ceiling that now
    governs it: a spec written on an ungoverned host, restored from a backup, or
    edited by hand carries grants nobody ever cleared. Re-asking at the moment of
    projection is the same shape as the final sanitizer pass ``rebuild_agent_config``
    runs over ``mcpServers`` — the last chance to withhold, taken deliberately.

    ``may_skip_gate_now`` fails closed (an unreadable ceiling withholds), which is
    the direction that matters: what is dropped here keeps prompting.
    """
    if not isinstance(allowed_tools, list):
        return []
    permitted: list[str] = []
    withheld: list[str] = []
    for raw in allowed_tools:
        if not isinstance(raw, str) or not raw.strip():
            continue
        entry = raw.strip()
        if may_skip_gate_now(entry):
            permitted.append(entry)
        else:
            withheld.append(entry)
    if withheld:
        names = ", ".join(sorted(withheld))
        logger.info(
            "agent %r: the governance ceiling withholds auto-approval for %s; "
            "projecting no rule for them, so they keep prompting",
            agent_id,
            names,
        )
        # A withhold is a permission DECISION, and the other writers that reach
        # this state — app-agent materialization, the host shared-MCP sync,
        # doctor's auto-fix — all record it in the security event log. Projection
        # is the fourth, and the only one whose input is a file it did not write,
        # so a stale grant is likelier to be withheld HERE than anywhere else;
        # leaving it at a log line would make the most likely case the one with no
        # audit trail. Never fail the projection on an audit error: the withhold
        # itself has already happened and is the safe direction.
        try:
            sel().log_api_access(
                caller="system",
                operation="mcp_auto_approve_withheld",
                outcome="ok",
                source="kas_agent_projection",
                resources=(
                    f"{names} projected without auto-approve "
                    f"(governance ceiling) for agent {agent_id or '?'}; "
                    "calls go through the approval gate"
                ),
            )
        except Exception:  # noqa: BLE001 — audit must not break projection
            logger.debug("SEL audit unavailable for KAS projection withhold", exc_info=True)
    return permitted


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
        # Says WHY the key is dropped, because the previous wording ("no KAS
        # equivalent") reads as "KAS cannot do this" and sent readers looking for
        # a missing feature instead of a missing wire field. Debug, not warning:
        # this fires on every session/new with a constant payload, so at WARNING
        # it drowns the log without ever telling anyone something new.
        logger.debug(
            "agent %r: spec keys the customAgents wire schema cannot carry, "
            "so an injected agent runs without them: %s",
            agent_id,
            ", ".join(dropped),
        )

    out: dict[str, Any] = {
        "id": agent_id,
        "prompt": prompt,
        "tools": _project_tools(spec, agent_id),
    }

    # Derived from `allowedTools` and from nothing else. A `permissions` block
    # sitting in the spec is deliberately NOT forwarded, even though it is already
    # in KAS's vocabulary and forwarding it would be one line: it has not passed
    # Crew's governance ceiling, so projecting one would hand any editor of the
    # file a grant the ceiling never saw — and an auto-approved call never reaches
    # Crew's permission callback, so the deny-list and the audit trail are skipped
    # with it. One governed input, one derivation.
    #
    # A hand-written block is not ignored, just not Crew's to relay: it lives in
    # the profile on disk, which the backend reads itself when Crew is not
    # injecting an agent over the wire.
    permissions = allowed_tools_to_permissions(
        _ceiling_permitted(spec.get("allowedTools"), agent_id), agent_id=agent_id
    )
    if permissions:
        out["permissions"] = permissions

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
