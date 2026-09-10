"""Codex's agent-config mirror (``codex-acp``).

The wire face only. ``codex-acp`` loads its own ``~/.codex/config.toml`` and Kiro
Crew never writes there (create-or-decline: that file is the operator's), so the
``session/new`` / ``session/load`` ``mcpServers`` array is the ONLY channel Crew
has onto a codex session. An empty array is therefore not a neutral default but
the whole defect ``providers/mirrors/README.md`` exists for: codex is in
``BASELINE_SELECTABLE_BACKENDS``, so a public build would serve a harness with no
``spawn_run``, no ``cron_add`` and no ``send_message`` — working in every visible
respect, with every Crew tool silently absent.

The translation is claude's (:func:`kiro_crew.acp.session_mcp.session_mcp_servers`):
the agent spec is the single source of truth, the ``tools`` allowlist decides
which servers enter the array, and the registry ceiling and control-plane
re-derivation apply unchanged. What is codex-specific is what this module adds on
top, and both rules were MEASURED against a real ``codex-acp`` rather than
inferred (``test/test_codex_session_mcp.py::test_real_codex_acp_accepts_the_crew_stdio_element``):

1. **An element whose transport the agent did not advertise fails the WHOLE
   request.** ``codex-acp`` 1.11.0 answers ``session/new`` with ``-32600 Invalid
   request`` / *"Codex doesn't support MCP SSE transport protocol"*, so one such
   entry costs the session every other server too. The filter reads the
   advertisement from THIS session's ``initialize``
   (``agentCapabilities.mcpCapabilities``, which 1.11.0 answers as ``{"acp":
   false, "http": true, "sse": false}``) rather than from a constant, so a
   version that gains or loses a transport needs no edit here — see
   :func:`drop_unadvertised_transports`. Stdio is exempt because ACP states
   every agent MUST support it.
2. **The child MCP process inherits almost nothing**, so Crew's control plane
   carries its identity on the element -- and ONLY the control plane does, because
   an element the spec describes is one whose command, args and env the spec chose.
   ``codex-rs``'s stdio launcher runs ``Command::env_clear()`` and then re-adds an
   ALLOWLIST
   (``rmcp-client/src/stdio_server_launcher.rs``, ``utils.rs::DEFAULT_ENV_VARS``:
   ``HOME``, ``LOGNAME``, ``PATH``, ``SHELL``, ``USER``, ``LANG``, ``LC_ALL``,
   ``TERM``, ``TMPDIR``, ``TZ``, plus the custom-CA keys) on top of the entry's
   own ``env`` map. So ``KIROCREW_SESSION_KEY`` does NOT reach the child by
   process inheritance the way it does under claude-agent-acp: it has to ride the
   element, or Crew's own control plane comes up unable to name the session it
   belongs to.

The SCOPE of that first rule is as load-bearing as the rule, because the wide
reading of it argues for projecting nothing at all. A malformed stdio element —
one missing ``command``, or an array member that is not an object — does NOT fail
``session/new``: the request succeeds and the bad element is dropped. ``sse`` is
the only fatal shape, and it fails with ``-32600`` rather than the ``-32602`` an
unadvertised transport invites you to assume.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection
from typing import Any, Mapping

from kiro_crew.acp.session_mcp import CONTROL_PLANE_SERVERS, session_mcp_projection
from kiro_crew.acp_backends import ACP_BACKEND_CODEX
from kiro_crew.mcp_cleanup import KIROCREW_BIN_MCP_SERVERS
from kiro_crew.providers.mirrors.base import (
    AgentConfigMirror,
    Concern,
    Disposition,
    Ruling,
    SessionProjection,
)

logger = logging.getLogger(__name__)

_D = Disposition

#: The transport every ACP agent MUST support, so it needs no advertisement. An
#: element with no ``type`` at all is this one: ACP v1 spells ``McpServer`` as
#: ``serde(tag = "type")`` with the stdio variant as the untagged fallback.
_ACP_BASELINE_TRANSPORT = "stdio"


def drop_unadvertised_transports(
    elements: list[dict[str, Any]], advertised: Mapping[str, Any] | None
) -> list[dict[str, Any]]:
    """Keep only elements whose transport THIS session's agent advertised.

    Reads the fact rather than remembering it. ``codex-acp`` answers ``initialize``
    with ``agentCapabilities.mcpCapabilities`` — ``{"acp": false, "http": true,
    "sse": false}`` on 1.11.0 — and ACP's own contract is that a client does not
    send a transport the agent did not claim. Encoding one version's answer as a
    constant would make a released adapter that gains ``sse``, or drops ``http``,
    silently wrong here; asking the session removes the whole class.

    Why this is not tidiness: an unsupported element does not degrade to "that one
    server is missing". codex-acp answers ``-32600 Invalid request`` for the WHOLE
    ``session/new``, so one bad element costs the session every other server — and
    the array is the only channel Crew has onto that session.

    **An unknown advertisement keeps stdio only.** The fail-safe direction, and not
    an arbitrary one: ACP requires every agent to support stdio, so it is the one
    transport that cannot be refused, while anything else with no positive claim
    behind it risks the whole request. Callers pass what ``initialize`` returned; an
    empty mapping means the handshake has not been read yet.

    Pure and in-memory by construction — its caller is the shared ``session/new``
    site, which must add no scheduling or failure point to any backend's
    construction path (harness-parity H13).
    """
    claims = advertised or {}
    out: list[dict[str, Any]] = []
    for element in elements:
        transport = str(element.get("type") or _ACP_BASELINE_TRANSPORT)
        if transport == _ACP_BASELINE_TRANSPORT or claims.get(transport) is True:
            out.append(element)
            continue
        logger.warning(
            "codex session MCP: dropping server %r — this session's agent did not advertise "
            "the %r transport (mcpCapabilities=%r), and codex-acp answers session/new with "
            "-32600 for the WHOLE request on one such entry, which would cost this session "
            "every other server",
            element.get("name"),
            transport,
            dict(claims),
        )
    return out


def _identity_env(session_key: str, channel_id: str) -> dict[str, str]:
    """The env Crew's own control plane needs, resolved for this session.

    Every value here is something a claude MCP child gets for free by inheriting
    the adapter's process environment and a codex one does not (``env_clear`` plus
    an allowlist, see the module docstring). Resolved live rather than read from the
    spec, exactly as ``managed_mcp_spec_entry`` resolves the command.

    ``KIROCREW_BOUND_PORT`` for the same reason ``members.member_dispatch_session_server``
    carries it: without the port the child falls through to the run marker, whose
    check needs ``find_listening_pids`` (``lsof``), which sees no listener from
    inside a sandbox's user namespace -- so the child dials the default port and
    every call is a connection refused on a gateway bound anywhere else.

    Fail-soft throughout: a config-plane failure must not fail a spawn, and no home
    override is exactly the state a default install is in.
    """
    # circular import: agent's module graph is heavy (it imports config), and
    # port_resolution reaches config.loader, whose provider-backend path imports
    # members. Both resolved at call time, as session_mcp.py resolves them.
    from kiro_crew.agent import _managed_mcp_env
    from kiro_crew.port_resolution import resolve_serving_port

    env: dict[str, str] = {}
    try:
        env.update(_managed_mcp_env())
    except Exception:  # pragma: no cover - defensive; the helper is fail-soft
        logger.warning("codex session MCP: could not resolve the managed home", exc_info=True)
    if session_key:
        env["KIROCREW_SESSION_KEY"] = session_key
    if channel_id:
        env["KIROCREW_CHANNEL_ID"] = channel_id
    try:
        env["KIROCREW_BOUND_PORT"] = str(resolve_serving_port())
    except Exception:  # pragma: no cover - defensive
        logger.warning("codex session MCP: could not resolve the serving port", exc_info=True)
    return env


def _with_env(element: dict[str, Any], extra: Mapping[str, str]) -> dict[str, Any]:
    """*element* with *extra* merged into its ACP array-of-pairs ``env``.

    Later wins, so a value resolved here replaces a stale one the entry carried --
    the same precedence ``managed_mcp_spec_entry`` applies to the command.
    """
    pairs: list[dict[str, str]] = [
        p for p in element.get("env") or [] if isinstance(p, dict) and p.get("name") not in extra
    ]
    pairs.extend({"name": k, "value": v} for k, v in extra.items())
    out = dict(element)
    out["env"] = pairs
    return out


def _identity_bound_crew_servers() -> frozenset[str]:
    """Crew's own managed servers that mounting would leave UNUSABLE on codex.

    Every managed server minus the control plane. The control plane is the part
    :func:`codex_elements` rebuilds with this session's identity; everything else
    reaches codex from the agent spec unreplaced, so it would come up bound to no
    session and answer ``not_bound`` to every call. That present-but-unusable shape
    is the defect this whole folder exists to kill, so those names are withheld and
    the absence is logged.

    DERIVED, not enumerated. An earlier revision spelled the three names out with a
    comment saying they were "``agent._MANAGED_MCP_SERVERS`` minus the control
    plane" -- and a hand-copy of a subtraction drifts in the bad direction here: a
    server added to the managed set later would miss this one, mount, and answer
    ``not_bound``, reintroducing by omission the very defect above.

    The managed set is read from :mod:`kiro_crew.mcp_cleanup`, which a ratchet test
    already pins equal to ``agent._MANAGED_MCP_SERVERS`` and which imports nothing
    heavier than ``config.paths`` -- so this leaf stays off ``agent``'s import graph
    without spelling the names again, exactly as ``acp.kas_agents`` reads it.
    """
    return frozenset(KIROCREW_BIN_MCP_SERVERS) - frozenset(CONTROL_PLANE_SERVERS)


def codex_withheld_servers(restricted: frozenset[str]) -> frozenset[str]:
    """Every server name this transport must not mount for a session.

    ONE owner for the question, because two consumers ask it: the projection, and
    the pooled-broker append. A stub the shared MCP gateway wraps carries the SAME
    name as the entry it rewrites, so a name withheld from the projection and then
    re-added as a stub is un-withheld -- and the stub is the UNRESTRICTED server,
    which is the worse of the two.

    Two reasons a name lands here, and they share a shape: this transport cannot
    deliver the thing that makes the server correct.

    * its spec narrows it per tool and codex has no deny channel
      (:func:`~kiro_crew.acp.session_mcp.session_mcp_restricted_servers`);
    * it is one of Crew's own identity-bound servers, which cannot be handed this
      session's credential on an element the spec describes
      (:func:`_identity_bound_crew_servers`).

    ``restricted`` comes from the caller's own parse: the projection derives the
    translation and this set from ONE read of the spec, so a spec gaining
    ``disabledTools`` between two reads cannot yield a withhold set from the old
    bytes applied to a translation of the new ones. Resolving it here would be
    exactly that second read, which is why this takes the set and not the agent.

    Free of I/O; the caller has already paid for the parse.
    """
    return frozenset(restricted) | _identity_bound_crew_servers()


def codex_name(name: str) -> str:
    """*name* spelled the way codex-acp will register it.

    codex-acp runs ``name.replace(|c: char| c.is_whitespace(), "_")`` on every
    element it accepts (``codex_agent.rs``, both the Stdio and Http arms), so
    emitting the raw name would make Crew's own session report -- and the roster it
    compares against -- name a server that does not exist under that spelling.

    **Per character, and that is the whole of it.** An earlier revision folded with
    ``"_".join(name.split())``, which additionally collapses runs of whitespace and
    strips the ends: that maps ``"kirocrew-core "`` onto ``"kirocrew-core"`` where
    codex maps it to ``"kirocrew-core_"``. Since codex registers by name with
    ``insert``, the collapsing form MANUFACTURED a collision with the control plane
    that codex's own rule cannot produce -- ``kirocrew-core`` contains no ``_``, so
    the only name that folds onto it is itself, and that name the translation
    replaces from the managed source. Matching the adapter exactly is what makes the
    hazard structurally absent rather than guarded against.
    """
    return re.sub(r"\s", "_", name)


def codex_elements(
    elements: list[dict[str, Any]],
    *,
    session_key: str = "",
    channel_id: str = "",
) -> list[dict[str, Any]]:
    """Apply codex's spelling and identity rules to a translated array.

    The transport rule is NOT here: it depends on what this session's agent
    advertised, which is not known until ``initialize``, so it lives in
    :func:`drop_unadvertised_transports` and runs at the ``session/new`` call site.

    **The identity env goes on Crew's OWN CONTROL PLANE and nowhere else.**
    ``KIROCREW_SESSION_KEY`` is the credential Crew's internal API authenticates a
    session-directive claim with, so it may ride only an element Crew itself
    derived. ``kirocrew-core`` and ``kirocrew-cron`` are exactly that: the shared
    translation REPLACES them from ``managed_mcp_spec_entry``, so their command,
    args and env are Crew's own by construction and no part of the hand-editable
    spec reaches the child. Their names are matched before folding, and folding
    cannot produce them from anything else (see :func:`codex_name`).

    Everything else in the array gets NO Crew identity, and that is a deliberate
    line rather than an omission: an element the spec describes is one whose
    command, args and env the spec chose, so handing it this session's credential
    would let a hand-edited line drive the session it was mounted into.

    Crew's own identity-bound servers therefore do not appear at all rather than
    appearing unusable -- :func:`codex_withheld_servers` drops them upstream of
    this function. A codex session has a working control plane and no session-bound
    work ledger, the same line ``ACP_BACKENDS_MEMBER_DISPATCH`` already draws for
    session control.

    A folded name is still claimed only once, first writer keeping it: two spec
    entries CAN legitimately fold together (``"my server"`` and ``"my_server"``),
    codex would let the second silently take the first's slot, and a session whose
    roster does not match what registered is worth a warning either way. With no
    credential on a spec element this is an ordinary naming clash, not a privilege
    question.
    """
    identity = _identity_env(session_key, channel_id) if session_key or channel_id else {}
    claimed: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        raw_name = str(element.get("name") or "")
        # Matched BEFORE folding: the control plane's own names contain no
        # whitespace, so this is the same test either way -- doing it on the raw
        # name says so, and leaves no reading in which a folded name qualifies.
        is_control_plane = raw_name in CONTROL_PLANE_SERVERS
        folded = codex_name(raw_name)
        element = dict(element)
        element["name"] = folded
        if identity and is_control_plane:
            element = _with_env(element, identity)
        if folded in claimed:
            logger.warning(
                "codex session MCP: dropping a second server that folds to the name %r "
                "(raw name %r) -- codex registers by name, so keeping both would let one "
                "silently take the other's slot",
                folded,
                raw_name,
            )
            continue
        order.append(folded)
        claimed[folded] = element
    return [claimed[name] for name in order]


def codex_projection(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    stub_elements: Collection[Mapping[str, Any]] = (),
    work_dir: object = None,
    session_key: str = "",
    channel_id: str = "",
) -> SessionProjection:
    """The whole codex array -- spec translation AND pooled stubs -- plus the deny set.

    The mirror's :meth:`~CodexMirror.session_projection`, as a function so it can be
    called and tested without the class. ``denied_tools`` is the client obligation
    :class:`~kiro_crew.providers.mirrors.base.SessionProjection` describes: the
    ``(server, tool)`` pairs this session's spec switched off, which the client
    refuses when codex asks permission for them
    (``AcpClient._deny_spec_disabled_tool``).

    ONE owner for both halves of the array. The shared MCP gateway's broker stubs
    carry the SAME name as the spec entry each one rewrites, so a stub appended
    after the projection withheld that name un-withholds it -- and the stub is the
    UNRESTRICTED server, the worse of the two. Taking the stub ELEMENTS here, beside
    the stub NAMES the translation already yields to, lets the withhold rule run
    over both halves in one place instead of being re-spelled at the call site.

    Stubs are appended as given rather than run through :func:`codex_elements`: they
    are gateway-authored, their env is the broker's own, and none of them is Crew's
    control plane (a session-strict server is never pooled), so there is no identity
    to add and no name the fold could collide with. They ARE held to the spec's
    ``tools`` allowlist, from the same parse that filtered the translated half: the
    overlay is written per agent from the global settings file too, so it can carry
    a stub for a server this agent never references, and a stub is that server.

    ``denied_tools`` is derived on the same parse as the array, so it cannot name a
    tool on a spec revision the array never saw. Server names are folded the way
    codex folds them (:func:`codex_name`), because the identity codex reports on a
    tool call -- ``rawInput.server`` -- is the REGISTERED spelling, and comparing a
    folded name against an unfolded one would silently never match.

    Blocking (parses the agent spec once), so callers run it off the event loop.
    """
    projection = session_mcp_projection(
        agent,
        stub_server_names=stub_server_names,
        work_dir=work_dir,  # type: ignore[arg-type]
    )
    withheld = codex_withheld_servers(projection.restricted)
    kept: list[dict[str, Any]] = []
    for element in projection.servers:
        name = element.get("name")
        if name in withheld:
            logger.warning(
                "codex session MCP: withholding server %r -- this transport cannot deliver "
                "what makes it correct (a per-tool restriction it has no deny channel for, "
                "or the session identity a Crew server binds to), and a mounted server that "
                "cannot work is the defect this projection exists to remove",
                name,
            )
            continue
        kept.append(element)
    out: list[dict[str, Any]] = codex_elements(kept, session_key=session_key, channel_id=channel_id)
    for stub in stub_elements:
        if not isinstance(stub, Mapping):
            continue
        name = stub.get("name")
        if name in withheld:
            logger.warning(
                "codex session MCP: withholding pooled stub %r -- the projection withheld "
                "the server it wraps, and a stub re-adds it unrestricted",
                name,
            )
            continue
        if not projection.allowlist.grants(str(name)):
            # The overlay is written per agent from the GLOBAL settings file as well
            # as the agent's own spec, so it can carry a stub for a server this
            # agent's ``tools`` never references. The translated half was filtered
            # by that allowlist; a stub is the same server under the same name and
            # is held to the same allowlist, from the same parse.
            logger.info(
                "codex session MCP: not mounting pooled stub %r -- the agent spec's `tools` "
                "does not reference it, and the allowlist that filtered the translated "
                "half applies to a stub of the same name",
                name,
            )
            continue
        out.append(dict(stub))
    denied = frozenset((codex_name(server), tool) for server, tool in projection.disabled_tools)
    return SessionProjection(params={"mcpServers": out}, denied_tools=denied)


class CodexMirror(AgentConfigMirror):
    """Projects the agent spec onto codex-acp."""

    backend = ACP_BACKEND_CODEX

    def rulings(self) -> Mapping[Concern, Ruling]:
        return {
            Concern.MCP_SERVERS: Ruling(
                _D.DELIVERED,
                "the session/new + session/load mcpServers array, translated by "
                "acp.session_mcp.session_mcp_servers and then narrowed by this "
                "module three ways. (1) An entry whose transport THIS session's "
                "agent did not advertise is dropped (drop_unadvertised_transports, "
                "reading initialize's mcpCapabilities), because codex-acp answers "
                "-32600 for the WHOLE request rather than skipping one server. "
                "(2) A third-party server whose spec narrows it per tool is "
                "omitted, not forwarded un-narrowed, and a narrowed CONTROL-PLANE "
                "tool is refused at the approval request instead -- see "
                "DENIED_TOOLS. (3) Crew's own control "
                "plane carries KIROCREW_SESSION_KEY on the element, because codex-rs "
                "launches a stdio server with env_clear() plus an allowlist and "
                "inherits nothing; that credential rides ONLY kirocrew-core and "
                "kirocrew-cron, the two entries the translation replaces from the "
                "managed source, so no part of a hand-editable spec reaches a child "
                "holding it. Every OTHER managed Crew server is withheld rather than "
                "mounted credential-less, since it would answer not_bound to every "
                "call, and that set is DERIVED as the managed servers minus the "
                "control plane -- see _identity_bound_crew_servers. The array and "
                "the withhold set come from ONE parse of the spec "
                "(session_mcp_projection), so a narrowing that lands between two "
                "reads of a user-writable file cannot leave a narrowed server "
                "mounted un-narrowed. Unlike claude this array is NOT conditional on Crew "
                "owning a permission file: codex's routing is `Routing.SESSION_CONFIG`, "
                "the one mechanism in tool_gate.ENFORCED_ROUTINGS, so a session "
                "that cannot arm mode=read-only is refused before its first prompt "
                "rather than running unasked",
            ),
            Concern.TOOL_ALLOWLIST: Ruling(
                _D.TRANSLATED,
                "`tools` is not sent; it is applied during translation as the "
                "allowlist deciding which servers enter the array, so a server the "
                "spec declares but never references is not mounted here either -- "
                "kiro-cli parity. It carries the same residual claude has: an "
                "`@server/tool` grant narrows to one tool on kiro-cli but mounts "
                "the whole server here, because the tool set is not knowable "
                "without connecting",
            ),
            Concern.DENIED_TOOLS: Ruling(
                _D.TRANSLATED,
                "the restriction is honoured by WITHHOLDING THE SERVER. codex-rs has "
                "per-server tool narrowing (McpServerConfig.disabled_tools) but "
                "codex-acp's build_session_config hardcodes it to None for a "
                "client-provided server, so the ACP element has no slot it can ride "
                "in -- and claude's answer, re-applying it as permissions.deny, needs "
                "a settings file codex does not have. Forwarding the server without "
                "its narrowing was the earlier reading of that, recorded as an "
                "addressed no-channel gap; it is not addressed, it is a restriction "
                "dropped, and the dashboard writes disabledTools on an ordinary "
                "tool-off action. So acp.session_mcp.session_mcp_restricted_servers "
                "names those servers and the array omits them. An availability cost "
                "is the honest price of a deny channel this transport does not have; "
                "reachability of a tool the user switched off is not. Crew's own "
                "CONTROL PLANE is the one exception, and it is honoured differently "
                "rather than dropped: withholding kirocrew-core would leave the "
                "session unable to report back at all, so it stays mounted and the "
                "client refuses a call to a switched-off tool when codex asks "
                "permission for it (AcpClient._deny_spec_disabled_tool, on the (server, "
                "tool) pairs codex_projection derives from the same parse -- read from "
                "the spec AND the global settings file, the only place the dashboard's "
                "tool-off action writes them for a managed server). "
                "That channel is complete for the control plane because its tools "
                "carry no annotations and codex prompts for every un-annotated MCP "
                "call under mode=read-only (codex-rs requires_mcp_tool_approval); "
                "it is NOT complete for a third-party server, whose readOnlyHint "
                "tools codex approves internally without asking, which is why "
                "those are withheld whole. `disabled` "
                "needs nothing here -- build_agent_config strips a disabled server's "
                "@alias from `tools`, and the allowlist mounts nothing `tools` does "
                "not name",
            ),
            Concern.AUTO_APPROVE: Ruling(
                _D.WITHHELD,
                "codex's nearest equivalents -- McpServerConfig's "
                "`default_tools_approval_mode` and a trusted entry in the "
                "operator's own config.toml -- both pre-approve the call INSIDE "
                "codex, which then never sends session/request_permission. That "
                "would skip Crew's permission gate, its governance ceiling and its "
                "SEL audit, and this is the harness whose asking is the reason it "
                "is offered at all. Every MCP call must reach the host gate",
            ),
            Concern.MODEL: Ruling(
                _D.DELIVERED,
                "not through this mirror: codex is in "
                "ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION, so the resolved model is "
                "pushed with session/set_config_option('model', ...) after "
                "session/new. Named here rather than left out so a reader does not "
                "read this mirror's silence as the model being dropped",
            ),
            Concern.MODEL_ALLOWLIST: Ruling(
                _D.WITHHELD,
                "the direction is reversed on this backend: codex-acp advertises "
                "its own model list as a session/new configOptions select, and that "
                "list is the ONLY source of ids set_config_option accepts -- the "
                "static registry has no codex provider, and kiro's catalog names "
                "models codex refuses with a bare -32602. So Crew CAPTURES the "
                "advertised set into the `codex` registry namespace "
                "(ACP_BACKENDS_ADVERTISED_MODEL_SELECTION) instead of sending one. "
                "Projecting the spec's availableModels here would offer ids that "
                "kill the session",
            ),
            Concern.PERMISSION_MODE: Ruling(
                _D.WITHHELD,
                "codex-acp has a `mode` selector and Crew writes it -- but it "
                "writes the FIXED value tool_gate demands (mode=read-only), not the "
                "mode the spec asked for. Honouring a spec-requested mode would let "
                "an agent file widen a codex session past the one boundary that "
                "makes this harness offerable, and the assertion is per session "
                "rather than seeded to a file precisely so nothing can inherit a "
                "looser one. A deliberate override, not a dropped setting",
            ),
            Concern.PROMPT: Ruling(
                _D.WITHHELD,
                "not a mirror concern on any backend: the prompt reaches every "
                "harness as ordinary prompt text in the [AGENT SYSTEM PROMPT] "
                "context block, which is backend-agnostic and already works",
            ),
            Concern.RESOURCES: Ruling(
                _D.WITHHELD,
                "same as PROMPT -- steering files are injected as context text, not "
                "projected into a backend's config",
            ),
            Concern.HOOKS: Ruling(
                _D.NO_CHANNEL,
                "codex runs hooks natively (codex-rs app-server-protocol ships "
                "HookEventName, ConfiguredHookMatcherGroup and hooks/list), and "
                "codex-acp's build_session_config carries no hooks field -- so a "
                "user's per-agent hooks block reaches kiro-cli and no other "
                "backend, exactly the gap claude records. Crew's OWN hooks "
                "(hooks.py, fired on ACP tool events) are unaffected and work on "
                "this backend already; this gap is only the spec block",
                channel="a hooks field on the codex-acp session/new element set, or "
                "a Crew-owned CODEX_HOME overlay under create-or-decline -- Crew "
                "writes no codex file today, which is why this needs a decision "
                "rather than a writer",
            ),
        }

    def session_params(
        self,
        agent: str | None,
        *,
        stub_server_names: Collection[str] = (),
        work_dir: object = None,
        session_key: str = "",
        channel_id: str = "",
        **kwargs: object,
    ) -> dict[str, object]:
        """The wire face: the ``mcpServers`` array for this codex session.

        ``permission_surface_owned`` is accepted and IGNORED (it arrives in
        ``kwargs``), which is the documented behaviour for a mirror outside
        claude's class. The flag exists because claude's permission surface is a
        file Crew may not own, so a pre-approved tool there never sends
        ``session/request_permission`` and Crew's gate never fires. Codex has no
        such file in play: its routing is asserted per session over
        ``session/set_config_option`` and is the one mechanism in
        ``tool_gate.ENFORCED_ROUTINGS``, so a session that cannot arm
        ``mode=read-only`` is REFUSED rather than run. Failing closed on the flag
        here would withhold every Crew tool from every codex session on the
        strength of a condition that does not describe this backend.

        ``session_key`` and ``channel_id`` are the caller's, because a mirror
        cannot discover them, and they are not decoration: a codex stdio child
        starts from ``env_clear()`` plus an allowlist, so without them Crew's own
        control plane comes up with no session to act on, and the out-of-band
        session-directive path (``dashboard/directive_queue``) -- the one that
        carries ``monitor_start`` and friends on a backend emitting no
        ``_meta.kiro`` -- has nothing to claim against.

        ``work_dir`` is the session's project checkout and is required for
        CORRECTNESS, not convenience: kiro-cli resolves ``--agent`` against
        ``<work_dir>/.kiro/agents`` as well as the user level, so omitting it makes
        a project-only agent read as "no spec" and drops the ``tools`` allowlist
        that spec declared.

        ``stub_elements`` are the shared gateway's broker stubs for this session,
        which the caller holds (it owns the overlay) and this mirror places, so the
        withhold rule covers both halves of the array -- see :func:`codex_projection`.

        Blocking -- it reads the agent spec. The caller warms this on the codex
        spawn path and serves the shared ``session/new`` call site from that cache
        (H13). The client calls :meth:`session_projection`, which also carries the
        deny set derived on the same parse; this method IS that projection's
        ``params``, so the two faces cannot drift.
        """
        stubs = kwargs.get("stub_elements") or ()
        return self.session_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stubs if isinstance(stubs, (list, tuple)) else (),
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
        ).params

    def session_projection(
        self,
        agent: str | None,
        *,
        stub_server_names: Collection[str] = (),
        stub_elements: Collection[Mapping[str, Any]] = (),
        work_dir: object = None,
        session_key: str = "",
        channel_id: str = "",
        **kwargs: object,
    ) -> SessionProjection:
        """The structured face: :func:`codex_projection`, with ``kwargs`` ignored as
        :meth:`session_params` documents (``permission_surface_owned`` arrives there)."""
        del kwargs
        return codex_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stub_elements,
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
        )
