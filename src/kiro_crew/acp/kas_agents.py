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
stubs. ``@server`` entries in ``tools`` do resolve wherever the server was
declared, so carrying the servers twice would risk a double registration — but
that only arises for a STUBBED server, and stubs are opt-in per server
(``mcp_gateway.stub_servers``, empty by default). With nothing stubbed the
session-level param is an empty array, so omitting the block leaves a KAS session
holding ``tools: ["@kirocrew-core", ...]`` and no definition of what
``kirocrew-core`` is — refs naming nothing, and every Crew tool silently absent.
kiro-cli does not have this problem: it reads the spec off disk itself via
``--agent``.

Filtering by the stub set keeps the no-double-registration guarantee (a stubbed
server is still declared exactly once, by the injection that outranks this block)
while never leaving the session with nothing. Two fields are dropped on the way
through, and a muted or registry-governed entry is not declared at all — see
:func:`_project_mcp_servers`. The runtime then carries the ACTIVE
agent's projected managed entries in the session-level array itself
(:func:`hoist_managed_servers`), the declaration site the captured 2.18.0 release
honours over a same-named global or workspace server and every probed release
reports as the session's own.

``model`` is deliberately NOT projected: the model is set through its own
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

import logging
import os
from pathlib import Path
from typing import Any

from kiro_crew.acp.kas_permissions import allowed_tools_to_permissions
from kiro_crew.agent_discovery import (
    AmbiguousAgentSpecError,
    read_agent_spec_strict,
    spec_by_declared_name,
)
from kiro_crew.agent_spec_format import agent_spec_candidates
from kiro_crew.mcp_cleanup import (
    KIROCREW_BIN_MCP_SERVERS,
    MCP_REGISTRY_TYPE,
    mcp_entry_is_muted,
    mcp_entry_is_registry_governed,
)
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

#: Per-server keys KAS's wire schema ACCEPTS and then throws away, so projecting
#: one is indistinguishable from omitting it. ``ClientAgentMcpServerSchema``
#: declares every name below, and then ``mapClientMcpServers`` rebuilds each
#: entry from ``command``/``args``/``env``/``timeout`` for a stdio server or
#: ``url``/``headers``/``env``/``timeout`` for a remote one — nothing else
#: survives the rebuild.
#:
#: ``type`` is absent from this tuple because it is lost one step EARLIER and for
#: a different reason: the schema has no slot for it at all, so the unknown key
#: is stripped before the mapper runs. The consequence of that is the registry
#: filter in :func:`_project_mcp_servers`, not this tuple.
#:
#: Only ``disabled`` is acted on. It is the one whose loss INVERTS the user's
#: decision — a muted server that arrives unmuted launches — and omitting the
#: declaration is a faithful way to express it. The others are restrictions with
#: no Crew-side carrier: ``disabledTools`` would need a per-tool exclusion
#: grammar this projection cannot verify against the backend, ``cwd`` changes
#: where the server runs and not whether it runs, and ``autoApprove`` is already
#: dropped upstream of here for its own reason. They are named so an operator
#: reading the log learns the restriction had no effect.
#:
#: TODO(kiro-agent): copy these through in ``mapClientMcpServers`` (and add
#: ``type`` to ``ClientAgentMcpServerSchema``); the handling here then becomes a
#: no-op rather than something to unpick.
_KAS_DISCARDED_ENTRY_KEYS = ("autoApprove", "cwd", "disabled", "disabledTools")

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


def _registry_governed() -> bool:
    """Whether the operator declared this install registry-governed.

    Deferred, and reaching for a private name on purpose: this has to be the SAME
    answer :mod:`kiro_crew.acp.session_mcp` acts on rather than a second reading
    of the same config, because two readings can disagree and a ceiling that
    disagrees with itself is not a ceiling. That module's wrapper also fixes the
    direction an unreadable config is read in (governed, not ungoverned), which is
    the half most easily got backwards. The import is call-time because it pulls
    :mod:`kiro_crew.agent` and the config loader in behind it, which this
    projection leaf stays off (see :data:`MANAGED_MCP_SERVER_NAMES`) — the same
    reason ``_MEMBER_DASHBOARD_GRANTS`` is imported where it is used.

    Not on the event loop, and not by luck: the one production route into this
    module is ``KasHarness.session_extras``, which builds the whole payload inside
    ``await asyncio.to_thread(_build)``. A config read here costs that thread, not
    every other session's turn — the same place ``require_fresh_derived_spec`` and
    the ceiling's ``may_skip_gate_now`` already read from.
    """
    from kiro_crew.acp.session_mcp import _registry_mode

    return _registry_mode()


def _project_mcp_servers(
    spec: dict[str, Any],
    agent_id: str,
    stub_server_names: frozenset[str],
    session_key: str = "",
) -> dict[str, dict[str, Any]]:
    """The spec's ``mcpServers``, minus stubbed names and minus two field classes.

    Five subtractions, each load-bearing:

    * **stubbed names** — those arrive as the session-level ``mcpServers`` param,
      which outranks an agent-declared entry. Emitting both is the double
      registration this block exists to avoid. Applied LAST: the withholds below
      are decisions about whether the server may run at all, and a name handed to
      the injection escapes every one of them.
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

    * **a muted server** — an entry carrying ``disabled: true`` is not declared at
      all. KAS's wire schema accepts the field and its mapper then drops it
      (:data:`_KAS_DISCARDED_ENTRY_KEYS`), so projecting a muted entry launches
      the very server the user silenced. Omitting the declaration is the only
      Crew-side way to say "do not launch this" that the backend cannot discard,
      and it says the same thing: a server KAS was never told about does not run.
      The mute is the user's own decision about a server they can un-mute, so
      honouring it costs no capability — unlike the credential strip above, it has
      no residue.
    * **a registry-governed entry** — see :func:`_registry_governed`. The filter
      mirrors kiro-cli's, which is SYMMETRIC: in registry mode an entry survives
      only by resolving its ``"type": "registry"`` marker against the admin's
      catalog, and OUTSIDE registry mode a marked entry is the one that is
      dropped. So a non-managed entry is withheld when registry mode is on
      (marked or not) and also when it is marked while the mode is off. Both
      cases are ones KAS would get wrong rather than merely differently: it has no
      slot for ``type``, so it sees every entry as unmarked — dropping all of them
      under an admin's catalog, and mounting a marked one outside it that kiro-cli
      would have dropped. Withholding here makes the first case diagnosable and
      the second correct.

    Crew's OWN managed servers are exempt from that filter and keep their marker,
    which is the same exemption :mod:`kiro_crew.acp.session_mcp` makes for the
    control plane and for the same reason: they are the host's own processes, and
    a session that loses them cannot report back to its channel at all. On today's
    KAS the marker does not reach the registry filter, so under registry mode the
    host drops them anyway and the session comes up with no Crew tools — which is
    why that case is WARNED about once rather than left silent, and why the
    exemption is still right: the day the wire carries ``type``, a governed
    session keeps its control plane with no further change here.

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
    registry_mode = _registry_governed()
    warned_about_the_marker = False
    for name, entry in servers.items():
        if not isinstance(name, str) or not name or not isinstance(entry, dict):
            continue
        managed = name in MANAGED_MCP_SERVER_NAMES
        if mcp_entry_is_muted(entry):
            # ``mcp_entry_is_muted`` is the shared launch-decision reading, so the
            # gateway rewriter cannot wrap an entry this function would withhold --
            # a wrapped name arrives as a stubbed name and is subtracted above,
            # before any check here. A non-boolean is not coerced and not forwarded
            # either: ``disabled: z.boolean()`` fails the wire schema, and a client
            # agent that fails it is dropped WHOLE
            # (``validateAndConvertClientCustomAgents`` logs
            # ``client.agent.drop.invalid`` and moves on), so Crew injects its one
            # agent and the session silently runs on KAS's default mode instead.
            disabled = entry.get("disabled")
            logger.info(
                "agent %r: not declaring MCP server %r — %s",
                agent_id,
                name,
                (
                    "the entry is disabled, and the customAgents wire schema "
                    "accepts that flag and then discards it, so a declared entry "
                    "would launch the server anyway"
                    if disabled is True
                    else "its 'disabled' value is not a boolean, so it is read as a "
                    "mute rather than coerced; forwarding it would fail the wire "
                    "schema and cost the session the whole injected agent"
                ),
            )
            continue
        marked = mcp_entry_is_registry_governed(entry)
        if not managed and (registry_mode or marked):
            logger.info(
                "agent %r: withholding MCP server %r — %s",
                agent_id,
                name,
                (
                    "registry mode is on and the wire schema has no slot for the "
                    "registry marker, so the host's catalog filter drops this "
                    "entry whether or not it is marked"
                    if registry_mode
                    else "registry mode is off and the entry carries the registry "
                    "marker, so kiro-cli drops it too"
                ),
            )
            continue
        if managed and registry_mode and not warned_about_the_marker:
            # Deliberately NOT conditional on the entry carrying the marker. A
            # spec materialized while the mode was off has unmarked managed
            # entries, and that install loses its control plane in exactly the
            # same way — requiring the stamp would keep the one case that cannot
            # self-diagnose silent.
            warned_about_the_marker = True
            logger.warning(
                "agent %r: registry mode is on, and this backend's customAgents "
                "wire schema has no slot for the %r marker Crew stamps on its own "
                "MCP servers. The host therefore sees them as unmarked and its "
                "catalog filter drops them, so this session starts with no Crew "
                "control plane: spawn_run, cron_add, learn_add, artifacts, "
                "knowledge and monitoring are all absent, with no error from the "
                "host. Nothing on this side can carry the marker; the workaround "
                "is `kirocrew config set agent.mcp_registry_mode false` on an "
                "install whose profile is not actually registry-governed.",
                agent_id,
                MCP_REGISTRY_TYPE,
            )
        if name in stub_server_names:
            # LAST of the subtractions, deliberately. A stubbed name is declared by
            # the session-level injection instead of here, and that path answers
            # none of the questions above -- so a name subtracted before them is a
            # server admitted without them. The gateway declines to wrap a muted or
            # catalog-governed entry, which keeps such a name out of this set in the
            # first place; this ordering is what makes the answer here true on its
            # own rather than by trusting that. It is the order
            # :func:`kiro_crew.acp.session_mcp.session_mcp_servers` uses.
            continue
        projected = {k: v for k, v in entry.items() if k not in _WRAPPER_MARKERS}
        projected.pop("autoApprove", None)
        discarded = [k for k in _KAS_DISCARDED_ENTRY_KEYS if projected.get(k)]
        if discarded:
            # Read off the PROJECTED entry, not the spec's: a key this function
            # already removed never reaches the backend, so naming it here would
            # report Crew's own subtraction as the backend's. Debug, because the
            # server is still declared and still runs — this explains a
            # restriction that had no effect, not a lost capability. ``disabled``
            # cannot appear: that entry returned above.
            logger.debug(
                "agent %r: MCP server %r declares %s, which the customAgents wire "
                "schema accepts and then discards, so the restriction has no "
                "effect on this backend.",
                agent_id,
                name,
                "/".join(discarded),
            )
        withheld = _withhold_credential_fields(projected, managed=managed)
        if managed:
            # Native MCP children do not inherit the gateway's environment.
            # Take the live listener from the gateway, never from an editable
            # spec or process discovery.
            bound_port = os.environ.get("KIROCREW_BOUND_PORT", "")
            if (
                1 <= len(bound_port) <= 5
                and bound_port.isascii()
                and bound_port.isdecimal()
                and 0 < int(bound_port) < 65536
            ):
                projected.setdefault("env", {})["KIROCREW_PORT"] = bound_port
            if session_key:
                projected.setdefault("env", {})["KIROCREW_SESSION_KEY"] = session_key
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
    session_key: str = "",
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

    mcp_servers = _project_mcp_servers(spec, agent_id, stub_server_names, session_key)
    if mcp_servers:
        out["mcpServers"] = mcp_servers

    return out


def load_agent_spec(agents_dir: Path, agent_id: str) -> dict[str, Any]:
    """Read a materialized agent spec.

    Takes the directory explicitly rather than resolving it here so this module
    stays free of :mod:`kiro_crew.agent`, which imports the config loader and
    would form an import cycle.

    A spec that DECLARES ``name == agent_id`` wins, found through
    :func:`kiro_crew.agent_discovery.spec_by_declared_name`, and
    ``<agent_id>.json`` or ``<agent_id>.md`` (the markdown form, frontmatter
    plus a body that is the prompt -- see :mod:`kiro_crew.agent_spec_format`)
    is read only when no spec declares the id. That is the
    order :func:`kiro_crew.agent.agent_spec_path` and the documented resolution
    convention use, and it is what keeps a misnamed ``<agent_id>.json`` that
    declares some other agent from being projected under this id, with that
    other agent's tools and prompt, while the spec that does declare the id
    sits beside it unread. Two specs declaring *agent_id* are refused, as
    :func:`kiro_crew.agent.agent_spec_path` refuses them: which is live is
    undefined, and picking either would project an agent the operator did not
    name.

    The scan's parsed spec is returned as is: it was read under the hardened
    reader's guards, labelled ``kas_agent_projection`` so a denial is
    attributed to the projection, and reopening the file it came from would
    read it a second time with none of them. The fallback read of
    ``<agent_id>.json`` (or ``.md``) goes through
    :func:`kiro_crew.agent_discovery.read_agent_spec_strict`, the same guards
    with the failure class kept; a spec declaring no name at all, or a name
    other than its stem, reaches the projection only through it.

    The scan and the fallback read raise :class:`KasAgentTranslationError` on
    an ``OSError`` for the same reason: every caller of this module handles the
    translation error, not an ``OSError``. On 3.12 ``Path.glob`` propagates one
    from the ``is_dir`` probe it runs on the directory itself (3.13 and 3.14
    run no such probe), and the strict reader raises one for a file it cannot
    resolve or open on every supported version, so an unsearchable agents dir
    reaches this function as an ``OSError`` and the conversion is what makes
    the failure uniform.
    """
    candidates = agent_spec_candidates(agents_dir, agent_id)
    path = candidates[0]
    try:
        declared = spec_by_declared_name(
            agents_dir, agent_id, operation="kas_agent_projection", source="unknown"
        )
    except AmbiguousAgentSpecError as exc:
        raise KasAgentTranslationError(str(exc)) from exc
    except OSError as exc:
        raise KasAgentTranslationError(f"agent spec {path} is unreadable: {exc}") from exc
    if declared is not None:
        return declared
    # ``<id>.json`` first: beside an ``<id>.md`` twin the JSON wins, the same
    # rule the directory scan applies (see ``agent_spec_format``).
    present = [p for p in candidates if p.is_file()]
    if present:
        path = present[0]
    try:
        # The hardened reader, not a bare ``read_text``: the agents directory is
        # user-writable, so a symlink here must not be followed to a sensitive
        # target or an oversized file slurped into the projection.
        raw = read_agent_spec_strict(path, operation="kas_agent_projection", source="unknown")
    except OSError as exc:
        raise KasAgentTranslationError(f"agent spec {path} is unreadable: {exc}") from exc
    except ValueError as exc:
        raise KasAgentTranslationError(f"agent spec {path} is not a valid spec: {exc}") from exc
    if not isinstance(raw, dict):
        raise KasAgentTranslationError(f"agent spec {path} is not an object")
    return raw


def build_kas_custom_agents(
    agents_dir: Path,
    agent_id: str,
    spec: dict[str, Any],
    *,
    stub_server_names: frozenset[str] = frozenset(),
    member_dispatch: bool = False,
    session_key: str = "",
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

    *spec* is REQUIRED and positional, and this function performs no read of its
    own. Its answer becomes the session's whole tool surface, so it has to be
    built from the spec the caller verified under the freshness gate -- reading the
    file here would be a SECOND read, milliseconds later, and a revocation landing
    in between would be projected as though it had been checked. A defaulted
    parameter that fell back to :func:`load_agent_spec` would restore exactly that
    hole for any caller that forgot to pass one, which is why there is no default.
    *agents_dir* stays for :func:`resolve_prompt`, which anchors a ``file://``
    prompt URI and reads a different artifact than the spec.
    """
    prompt = resolve_prompt(spec, agent_id=agent_id, agents_dir=agents_dir)
    return [
        to_client_custom_agent(
            agent_id,
            spec,
            prompt,
            stub_server_names=stub_server_names,
            member_dispatch=member_dispatch,
            session_key=session_key,
        )
    ]


#: The keys a projected stdio declaration may carry and still be reproduced
#: exactly by :func:`kiro_crew.acp.session_mcp.acp_server_element`. Anything
#: else on a managed entry is a user customization with no session-level
#: carrier -- ``disabledTools`` (a user guard), ``timeout`` -- so an entry
#: carrying one stays in the block. ``disabled`` is absent from that list of
#: examples for a reason worth stating: a disabled entry never reaches this
#: function, because :func:`_project_mcp_servers` declines to declare it at all.
_HOISTABLE_ENTRY_KEYS = frozenset({"command", "args", "env", "type"})


def hoist_managed_servers(
    custom_agents: list[dict[str, Any]] | None,
    agent_id: str,
    session_servers: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
    """Carry the ACTIVE agent's managed declarations in the session-level array.

    Captured released kiro-cli 2.18.0 honours a session-level ``mcpServers``
    entry over a same-named global or workspace ``mcp.json`` server on
    ``session/new`` and ``session/load`` alike, while an agent-block declaration
    loses to the global one there, and it stamps no status provenance. The
    retained 2.20.0 capture proves a session-level injection reports
    ``origin: client`` and reaches readiness; its same-name collision behaviour
    was not probed. The session-level array is therefore the one declaration site
    whose connected report is positively the session's own on both, and Crew's
    managed servers -- the ones whose env carries this session's key -- belong
    there rather than in the block, exactly as the member dispatch server already
    travels.

    Pure and non-mutating: returns a new agents list (the active descriptor
    shallow-copied with the hoisted names removed from ``mcpServers``) and a new
    array (the caller's entries first, then the hoisted elements by name). The
    entries hoisted are the ALREADY projected ones -- credential fields withheld,
    ``autoApprove`` dropped, ``KIROCREW_PORT``/``KIROCREW_SESSION_KEY`` applied
    -- so no spec is re-read and the source snapshot is untouched.

    What is NOT hoisted, each deliberately:

    * a name the caller's array already carries -- a broker stub or the member
      dispatch entry is authoritative and a name must appear once;
    * a non-managed server -- third-party declarations are not this seam's;
    * an inactive agent's block -- it must not widen the active session's tool
      surface or carry another identity's key;
    * an entry with a key outside :data:`_HOISTABLE_ENTRY_KEYS`, a ``type``
      other than ``stdio``, or no usable command -- a restriction, a registry
      marker or a malformed entry keeps the block path, where it is honoured.

    The agent's ``tools`` / ``excludedTools`` / ``permissions`` are untouched:
    ``@server`` refs resolve wherever the server was declared, which is the same
    property ``member_dispatch`` already relies on for ``@kirocrew-dashboard``.
    """
    if not custom_agents:
        # The kiro path (no wire payload) returns here without importing the
        # agent/config translation machinery below as a side effect.
        return custom_agents, session_servers
    from kiro_crew.acp.session_mcp import acp_server_element

    taken = {
        str(entry.get("name"))
        for entry in session_servers
        if isinstance(entry, dict) and entry.get("name")
    }
    hoisted: list[dict[str, Any]] = []
    out_agents: list[dict[str, Any]] = []
    for descriptor in custom_agents:
        declared = descriptor.get("mcpServers") if descriptor.get("id") == agent_id else None
        if not isinstance(declared, dict):
            out_agents.append(descriptor)
            continue
        remaining: dict[str, Any] = {}
        for name, entry in declared.items():
            keep = True
            if (
                name in MANAGED_MCP_SERVER_NAMES
                and name not in taken
                and isinstance(entry, dict)
                and set(entry) <= _HOISTABLE_ENTRY_KEYS
                and entry.get("type", "stdio") == "stdio"
            ):
                element = acp_server_element(name, entry)
                if element is not None:
                    hoisted.append(element)
                    taken.add(name)
                    keep = False
            if keep:
                remaining[name] = entry
        if len(remaining) == len(declared):
            out_agents.append(descriptor)
            continue
        copied = dict(descriptor)
        if remaining:
            copied["mcpServers"] = remaining
        else:
            del copied["mcpServers"]
        out_agents.append(copied)
    if not hoisted:
        return custom_agents, session_servers
    hoisted.sort(key=lambda element: element["name"])
    return out_agents, [*session_servers, *hoisted]
