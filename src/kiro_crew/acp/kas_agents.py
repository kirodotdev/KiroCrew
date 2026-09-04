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

``mcpServers`` IS projected, minus the names that arrive as session-level broker
stubs. It was previously omitted on the reasoning that ``@server`` entries in
``tools`` resolve wherever the server was declared and that carrying the servers
twice risks a double registration. The first half is true; the second described a
case that only arises for a STUBBED server, and stubs are opt-in per server
(``mcp_gateway.stub_servers``, empty by default). With nothing stubbed the
session-level param is an empty array, so omitting the block left a KAS session
holding ``tools: ["@kirocrew-core", ...]`` and no definition of what
``kirocrew-core`` is — refs naming nothing, and every Crew tool silently absent.
kiro-cli never had this: it reads the spec off disk itself via ``--agent``.

Filtering by the stub set keeps the original reason intact (a stubbed server is
still declared exactly once, by the injection that outranks this block) while
removing the case where the omission left the session with nothing. Two fields
are dropped on the way through — see :func:`_project_mcp_servers`.

``model`` is still deliberately NOT projected: the model is set through its own
protocol verb, so it has exactly one owner rather than being pinned in two places
that can disagree.

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
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
from kiro_crew.platform.governance import may_skip_gate_now
from kiro_crew.security import is_sensitive_path
from kiro_crew.sel import sel

logger = logging.getLogger(__name__)

#: Cap KAS enforces on ``_meta.kiro.customAgents`` (``z.array(...).max(50)``).
KAS_MAX_CUSTOM_AGENTS = 50

_PROMPT_FILE_SCHEME = "file://"

#: Kiro Crew's OWN managed MCP servers — the ones whose ``env`` may retain a
#: single Crew-authored key through projection (see :func:`_project_mcp_servers`).
#:
#: Imported from :mod:`kiro_crew.mcp_cleanup`, which already pins this set to
#: ``agent._MANAGED_MCP_SERVERS`` with a ratchet test and imports nothing heavier
#: than ``config.paths`` — so this projection leaf stays off the config-loader /
#: aiohttp import chain without spelling the four names a third time.
MANAGED_MCP_SERVER_NAMES = frozenset(KIROCREW_BIN_MCP_SERVERS)

#: Fields that carry a secret. ``env`` "routinely holds tokens and API keys"
#: (``mcp_gateway.session_servers``) and a remote entry's ``headers`` can hold a
#: static ``Authorization`` value, so neither crosses the wire intact.
_CREDENTIAL_BEARING_FIELDS = ("env", "headers")

#: The ONLY env key that survives for one of Crew's own managed servers. It pins
#: the data home, so dropping it would have the shims read a different one than
#: the gateway — that is the whole reason managed ``env`` is not simply withheld.
#:
#: Everything else is withheld even on a managed server. "Crew authored this
#: server" is not "Crew authored every key now in its env": the entry lives in a
#: user-editable agent file, so a hand-added secret is reachable under a managed
#: name and would otherwise be the one credential path left onto the wire.
_MANAGED_ENV_KEYS_KEPT = frozenset({"KIROCREW_HOME"})

#: Crew-internal bookkeeping on a rewritten entry. Never belongs on the wire: an
#: unknown field can fail a strict schema, and it means nothing to the backend.
_WRAPPER_MARKERS = ("_kirocrew_mcp_gateway_wrapped", "_mc_mcp_gateway_wrapped")

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


def _project_mcp_servers(
    spec: dict[str, Any],
    agent_id: str,
    stub_server_names: frozenset[str],
) -> dict[str, dict[str, Any]]:
    """The spec's ``mcpServers``, minus stubbed names and minus two field classes.

    Three subtractions, each load-bearing:

    * **stubbed names** — those arrive as the session-level ``mcpServers`` param,
      which outranks an agent-declared entry. Emitting both is the double
      registration this block was originally omitted to avoid.
    * **``autoApprove``** — an auto-approved MCP tool is approved by the host and
      emits no permission request, so ``hooks.on_tool_call`` (the always-on deny
      floor, the sensitive-path check, the governance ceiling) never runs for it.
      ``agent.py`` states the rule for Crew's own servers ("DELIBERATELY NO
      ``autoApprove`` KEY, and none may ever be added"); relaying one copied from
      a spec would grant through this path what that rule refuses on the other,
      and on KAS there is no wire slot for hooks at all. Auto-approve reaches KAS
      only as ``permissions``, derived from the ceiling-filtered ``allowedTools``.
    * **``env`` and ``headers``** — projection puts these on the wire, and a
      declared server's env routinely holds tokens. Every server is filtered; the
      classes differ only in what survives. A server Crew did not author loses
      both fields outright. One of Crew's OWN managed servers keeps exactly
      ``KIROCREW_HOME`` out of its env and nothing else, because that key is the
      only reason managed env is projected at all: without it the shims read a
      different data home than the gateway. A managed entry still lives in a
      user-editable agent file, so a hand-added key under a managed name is
      withheld like any other.

    A non-managed server therefore starts without its credentials and may fail to
    authenticate — which is still strictly better than today, where it does not
    start at all. The drop is logged with KEY NAMES ONLY so an operator can see
    why, without the value reaching a log.

    Note what this canNOT reach: ``command``, ``args`` and ``url`` are how the
    server is launched or addressed, so a secret embedded THERE (an ``--api-key``
    argv, a signed query string) still crosses the wire. Stripping them would not
    withhold a credential, it would unmake the server — the exact "declared but
    absent" state this function exists to end — so the residue is accepted and
    stated rather than papered over.
    """
    servers = spec.get("mcpServers")
    if not isinstance(servers, dict):
        return {}

    out: dict[str, dict[str, Any]] = {}
    for name, entry in servers.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            continue
        if name in stub_server_names:
            continue
        projected = {k: v for k, v in entry.items() if k not in _WRAPPER_MARKERS}
        projected.pop("autoApprove", None)
        managed = name in MANAGED_MCP_SERVER_NAMES
        withheld = _withhold_credential_fields(projected, managed=managed)
        if withheld:
            logger.info(
                "agent %r: not relaying %s for MCP server %r — the field can carry "
                "a credential. The server is still declared; it may need its "
                "credentials supplied another way.",
                agent_id,
                "/".join(withheld),
                name,
            )
        out[name] = projected
    return out


def _withhold_credential_fields(
    projected: dict[str, Any],
    *,
    managed: bool,
) -> list[str]:
    """Strip credential-bearing fields from one projected server entry in place.

    Returns the FIELD NAMES something was withheld from, for the caller's log —
    never a value, and never the withheld env keys, since a key name in a
    third-party server's env is itself operator-supplied.

    *managed* keeps ``_MANAGED_ENV_KEYS_KEPT`` alive in ``env``; everything else
    goes either way, ``headers`` included. A managed server has no legitimate
    ``headers`` (all four are local stdio processes), so retaining it would only
    forward whatever a hand edit put there.
    """
    withheld: list[str] = []
    if projected.get("headers"):
        projected.pop("headers", None)
        withheld.append("headers")

    env = projected.get("env")
    if not env:
        projected.pop("env", None)
        return withheld
    if not managed or not isinstance(env, dict):
        # Non-managed, or a malformed env that cannot be filtered key-by-key.
        projected.pop("env", None)
        withheld.append("env")
        return withheld

    kept = {k: v for k, v in env.items() if k in _MANAGED_ENV_KEYS_KEPT}
    if len(kept) != len(env):
        withheld.append("env")
    if kept:
        projected["env"] = kept
    else:
        projected.pop("env", None)
    return withheld


def to_client_custom_agent(
    agent_id: str,
    spec: dict[str, Any],
    prompt: str,
    *,
    stub_server_names: frozenset[str] = frozenset(),
    member_dispatch: bool = False,
) -> dict[str, Any]:
    """Project one Crew agent spec onto a KAS ``ClientCustomAgent`` descriptor.

    Pure: *prompt* is already-resolved content (see :func:`resolve_prompt`).

    *stub_server_names* are the servers that will arrive as the session-level
    ``mcpServers`` param and must not also be declared here — see
    :func:`_project_mcp_servers`. The default is empty, which is correct for a
    caller with no shared gateway: nothing is stubbed, so nothing is subtracted.

    *member_dispatch* widens the projection for a crew member's DM session:
    ``@kirocrew-dashboard`` joins ``tools`` (the server itself arrives as a
    session-level entry, but KAS grants only what ``tools`` names), and the
    member's approval-free dashboard verbs join the ``allowedTools`` input
    BEFORE the governance ceiling filter — the conductor grant set plus the
    write verbs the server-side ``created_by`` ownership fence bounds, passed
    through the same ceiling every other grant crosses.
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
    allowed_tools_input = spec.get("allowedTools")
    if member_dispatch:
        # The dashboard server arrives as a session-level entry; naming it in
        # ``tools`` is what grants its tools (KAS resolves ``tools ?? []``).
        # ``"*"`` already covers it.
        tools = out["tools"]
        if isinstance(tools, list) and "@kirocrew-dashboard" not in tools:
            out["tools"] = [*tools, "@kirocrew-dashboard"]
        # circular import: agent imports the config loader, which sits below
        # this module; resolved at call time like the other heavy seams here.
        from kiro_crew.agent import _MEMBER_DASHBOARD_GRANTS

        base_allowed = allowed_tools_input if isinstance(allowed_tools_input, list) else []
        merged = list(base_allowed)
        merged.extend(g for g in _MEMBER_DASHBOARD_GRANTS if g not in merged)
        allowed_tools_input = merged

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
        _ceiling_permitted(allowed_tools_input, agent_id), agent_id=agent_id
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

    mcp_servers = _project_mcp_servers(spec, agent_id, stub_server_names)
    if mcp_servers:
        out["mcpServers"] = mcp_servers

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


def build_kas_custom_agents(
    agents_dir: Path,
    agent_id: str,
    *,
    stub_server_names: frozenset[str] = frozenset(),
    member_dispatch: bool = False,
) -> list[dict[str, Any]]:
    """Build the ``_meta.kiro.customAgents`` batch that binds *agent_id* on KAS.

    One entry: KAS registers the injected agent, it then surfaces as a mode, and
    the ordinary ``session/set_mode`` activation can select it. Without this the
    session stays on KAS's own default mode and the operator's prompt and tool
    configuration have no effect.

    A prompt-less spec (e.g. ``kirocrew-lite``) is projected with the small
    :data:`_KAS_FALLBACK_PROMPT` so it satisfies KAS's non-empty-prompt
    requirement instead of crashing the session (see :func:`resolve_prompt`).

    *stub_server_names* is forwarded to :func:`_project_mcp_servers`; the caller
    holds the gateway overlay this session will inject from, so it is the only
    layer that can answer which names are stubbed.
    """
    spec = load_agent_spec(agents_dir, agent_id)
    prompt = resolve_prompt(spec, agent_id=agent_id, agents_dir=agents_dir)
    return [
        to_client_custom_agent(
            agent_id,
            spec,
            prompt,
            stub_server_names=stub_server_names,
            member_dispatch=member_dispatch,
        )
    ]
