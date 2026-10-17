"""opencode's agent-config mirror (``opencode acp``).

The wire face only, and it exists because the reason opencode had no projection
was WRONG. The previous declaration read the harness's ``initialize`` result --
``mcpCapabilities: {"http": true, "sse": true}`` -- as evidence that the
``session/new`` ``mcpServers`` array could not carry a stdio server. It carries
them fine. ACP's own ``McpCapabilities`` schema has exactly two boolean fields,
``http`` and ``sse``, and no ``stdio`` field at all, so a conforming agent CANNOT
advertise stdio and ``{"http": true, "sse": true}`` is what full support looks
like. The absence proved nothing, and reading it as a refusal left every opencode
session with none of Crew's own tools -- no ``spawn_run``, no ``cron_add``, no
``send_message`` -- on a harness that is selectable, working in every visible
respect.

Measured against opencode 1.18.30 rather than re-inferred
(``docs/request-for-change/rfc-agent-config-mirror.md``, and the ratchet in
``test/test_opencode_session_mcp.py``):

* The element ``acp/session_mcp.py::acp_server_element`` already emits --
  ``{"name", "command", "args", "env": [{"name","value"}], "type": "stdio"}`` --
  is ACCEPTED, the child is spawned, its tools are listed and the element's ``env``
  reaches it. Dropping the ``type`` key gives a byte-identical result.
* ``type: "stdio"`` is TOLERATED AND DISCARDED, not honoured -- and this
  projection DROPS IT anyway rather than relying on that. The adapter's ACP union
  is ``http`` and ``sse`` as tagged members plus an untagged stdio member, and zod
  strips unknown keys, so the tag fails the two tagged members and matches the
  untagged one, which has no ``type`` field. But the mapping function then
  discriminates on the mere PRESENCE of ``type``, so a tag that SURVIVED would take
  the element down the remote branch and throw on its ``headers``. Sending the tag
  therefore makes every session start depend on one third-party implementation
  detail -- a schema switched to passthrough or strict -- and on this harness the
  penalty is not one missing server but ``-32602`` for the whole ``session/new``.
  Removing it costs nothing (measured byte-identical with and without) and removes
  the dependence, so :func:`opencode_elements` removes it.
* ``env`` and ``args`` are REQUIRED arrays, and a malformed element fails the
  WHOLE ``session/new`` with ``-32602`` -- not just that server. That is why
  ``acp_server_element``'s skip-and-log discipline is load-bearing here rather than
  merely tidy, and it is the opposite of codex, where a malformed stdio element is
  dropped and the request succeeds.
* ``http`` and ``sse`` elements are BOTH accepted, so this backend needs no
  analogue of codex's :func:`~kiro_crew.providers.mirrors.codex.drop_unadvertised_transports`.
  One caveat worth stating rather than discovering: the adapter maps ``sse`` onto
  its own ``remote`` kind, which is streamable HTTP, and it has no SSE-specific
  path -- so an SSE-only server mounted here is spoken to over the wrong shape.
  That is the harness's mapping, not something a projection can correct.
* The MCP child INHERITS the ambient environment (133 variables in the probe); it
  is not ``env_clear()``ed the way codex's is. The element ``env`` is still the
  right carrier for ``KIROCREW_SESSION_KEY``, but for a different reason -- it is
  what makes the value THIS session's rather than the gateway's.

Crew writes no opencode config file of its own beyond the permission routing
(``AcpClient._opencode_routing_config`` seeds ``OPENCODE_CONFIG_CONTENT`` with the
one setting ``tool_gate`` demands, plus one ``deny`` rule per switched-off MCP tool
-- see :func:`opencode_projection`), and the MCP servers deliberately do NOT travel
there. That channel works, but ``OPENCODE_CONFIG_CONTENT`` MERGES with the project
and user config rather than replacing them, and declaring the same server in both
the config block and the array DOUBLE-MOUNTS it: both children spawn, because the
array does not go through config resolution at all. So the array is the one
channel, and this module is the whole of it.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection
from typing import Any, Mapping

from kiro_crew.acp.session_mcp import (
    CONTROL_PLANE_SERVERS,
    SessionMcpProjection,
    session_mcp_projection,
)
from kiro_crew.acp_backends import ACP_BACKEND_OPENCODE
from kiro_crew.providers.mirrors.base import (
    AgentConfigMirror,
    Concern,
    Disposition,
    Ruling,
    SessionProjection,
)
from kiro_crew.providers.mirrors.identity import (
    control_plane_identity_env,
    with_env,
    withheld_servers,
)

logger = logging.getLogger(__name__)

_D = Disposition

#: The tag ``acp.session_mcp.acp_server_element`` puts on a stdio element. ACP v1
#: makes stdio the UNTAGGED member, so this is the one transport whose tag carries
#: no information -- and the one this projection removes. Named rather than spelled
#: inline so the pop and the prose cannot drift onto different strings.
_STDIO_TAG = "stdio"


def without_stdio_tag(element: dict[str, Any]) -> dict[str, Any]:
    """*element* with a ``type: "stdio"`` tag removed, as a copy.

    ONE owner, because BOTH halves of the array need it and they arrive by
    different routes: the translated half from ``acp_server_element``, and the
    shared gateway's broker stubs from ``mcp_gateway.session_servers``. Emitting
    the tag on one and not the other would make the module's stated behaviour true
    of whichever half a reader happened to check.

    Why the tag goes at all: the adapter tolerates it only because zod strips
    unknown keys before its union matches the untagged stdio member, and its
    ACP-to-native mapping then branches on whether ``type`` is PRESENT -- so a tag
    that survived would be routed as a remote server and throw on its absent
    ``headers``. Only the stdio tag; ``http`` and ``sse`` need theirs, because for
    them the tag is what the union matches on.
    """
    if element.get("type") != _STDIO_TAG:
        return element
    out = dict(element)
    out.pop("type", None)
    return out


def opencode_elements(
    elements: list[dict[str, Any]],
    *,
    session_key: str = "",
    channel_id: str = "",
    session_token: str = "",
) -> list[dict[str, Any]]:
    """Apply opencode's identity rule to an already-translated array.

    **The identity env goes on Crew's OWN CONTROL PLANE and nowhere else.**
    ``KIROCREW_SESSION_KEY`` is the credential Crew's internal API authenticates a
    session-directive claim with, so it may ride only an element Crew itself
    derived. ``kirocrew-core`` and ``kirocrew-cron`` are exactly that: the shared
    translation REPLACES them from ``managed_mcp_spec_entry``, so their command,
    args and env are Crew's own by construction and no part of the hand-editable
    spec reaches the child. Everything else in the array gets NO Crew identity,
    because an element the spec describes is one whose command, args and env the
    spec chose -- handing it this session's credential would let a hand-edited line
    drive the session it was mounted into.

    The carriage is needed for a DIFFERENT reason than codex's, and the difference
    is worth keeping straight because it decides what happens if the rule is
    dropped. codex-rs ``env_clear()``s its stdio children, so without the element
    ``env`` codex's control plane has no session key at all. opencode's children
    inherit the ambient environment, so a gateway-level ``KIROCREW_SESSION_KEY``
    would reach them -- and it would be the WRONG one, the gateway's rather than
    this session's. Dropping the rule here therefore mis-scopes rather than
    empties, which is the harder failure to notice.

    **The stdio ``type`` tag is REMOVED, and that is not tidiness.** The adapter
    tolerates it only because zod strips unknown keys before its union matches the
    untagged stdio member -- and its ACP-to-native mapping then branches on whether
    ``type`` is PRESENT, so a tag that survived would be routed as a remote server
    and throw on its absent ``headers``. Keeping it would make every opencode
    session start depend on that stripping behaviour, and the penalty here is the
    whole ``session/new`` failing with ``-32602`` rather than one server going
    missing. Measured byte-identical with and without
    (``test_real_opencode_acp_accepts_the_crew_stdio_element`` drives both shapes),
    so the safe form is free. Only the stdio tag goes: ``http`` and ``sse`` elements
    need theirs, because for them the tag is what the adapter's union matches on.

    **No name folding, and that is a measured absence rather than an omission.**
    codex registers a client element under
    ``name.replace(whitespace, "_")``, so its mirror has to reproduce the fold or
    its own session report names a server that does not exist. opencode registers
    the name as given (``mcp.add({directory, name, config})``); the sanitising it
    does do is on the TOOL id it shows the model
    (``sanitize(server) + "_" + sanitize(tool)``), which is a display spelling this
    projection does not compare against. With no fold there is no collision this
    projection could manufacture, and the translated half is already keyed by name.

    Pure and in-memory.
    """
    identity = (
        control_plane_identity_env(
            session_key, channel_id, label="opencode", session_token=session_token
        )
        if session_key or channel_id or session_token
        else {}
    )
    out: list[dict[str, Any]] = []
    for element in elements:
        if not isinstance(element, dict):
            continue
        # The shared translator emits the tag for claude, which needs it; this
        # harness discards it, and depending on that discarding is what the
        # docstring above declines to do.
        element = without_stdio_tag(dict(element))
        if identity and str(element.get("name") or "") in CONTROL_PLANE_SERVERS:
            element = with_env(element, identity)
        out.append(element)
    return out


#: Characters the harness keeps in a tool id; every other one becomes ``_``. Read
#: from opencode 1.18.30's own MCP tool registration, which names a tool
#: ``sanitize(server) + "_" + sanitize(tool)`` with exactly this class. The result
#: can never hold ``*`` or ``?``, so a deny rule built from it is a literal key in
#: the harness's wildcard rule matcher and cannot widen into a pattern.
_TOOL_ID_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")

#: Tool ids the harness's tool filter re-maps to a builtin permission before it
#: looks a rule up (``edit``/``write``/``apply_patch`` -> ``edit``, the three MCP
#: resource tools -> ``read``). An MCP tool whose fused id lands on one of these is
#: judged by the builtin's rule, not by a deny Crew writes under its id, so no rule
#: of Crew's can switch it off. Its server is withheld instead.
_REMAPPED_TOOL_IDS = frozenset(
    {
        "edit",
        "write",
        "apply_patch",
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "read_mcp_resource",
    }
)


def opencode_tool_id(server: str, tool: str) -> str:
    """The id this harness gives *tool* on *server*, which its permission rules key on.

    Two different pairs can fuse to one id (``a_b``/``c`` and ``a``/``b_c``). A deny
    for one then denies the other too. That is the safe direction: it can only switch
    off more than the spec asked, never less.
    """
    return f"{_TOOL_ID_UNSAFE.sub('_', server)}_{_TOOL_ID_UNSAFE.sub('_', tool)}"


def opencode_deny_rules(disabled_tools: Collection[tuple[str, str]]) -> tuple[str, ...]:
    """The tool ids Crew seeds as ``deny`` for *disabled_tools*, sorted and unique.

    An id the harness re-maps to a builtin is left out, because a rule under it would
    not be the rule the harness reads (see :data:`_REMAPPED_TOOL_IDS`). Free of I/O.
    """
    ids = {opencode_tool_id(server, tool) for server, tool in disabled_tools}
    return tuple(sorted(ids - _REMAPPED_TOOL_IDS))


def place_single_binary_array(
    projection: SessionMcpProjection,
    *,
    label: str,
    unhonoured: frozenset[str],
    stub_elements: Collection[Mapping[str, Any]] = (),
    session_key: str = "",
    channel_id: str = "",
    session_token: str = "",
) -> list[dict[str, Any]]:
    """The ``mcpServers`` array for a single-binary harness, both halves of it.

    Shared by opencode and goose, which differ only in HOW a per-tool restriction is
    honoured, never in how the array is placed. *unhonoured* is the set of narrowed
    servers whose restriction this session cannot put in force; they are withheld
    whole, which is the only faithful action left for them. Crew's identity-bound
    servers are withheld from the spec-described half for the reason
    :func:`~kiro_crew.providers.mirrors.identity.withheld_servers` gives.

    ONE owner for both halves. The shared MCP gateway's broker stubs carry the SAME
    name as the spec entry each one rewrites, so a stub appended after this withheld
    that name would un-withhold it, and the stub is the unrestricted server. A stub
    the spec narrows but does not withhold is fine to mount: the per-tool rule keys on
    the server NAME, which the stub shares. Stubs are held to the spec's ``tools``
    allowlist from the same parse, and go through the same stdio tag strip as the
    translated half.
    """
    withheld = withheld_servers(frozenset()) | unhonoured
    kept: list[dict[str, Any]] = []
    for element in projection.servers:
        name = element.get("name")
        if name in unhonoured:
            logger.warning(
                "%s session MCP: withholding server %r -- one of its tools is switched off "
                "and this session cannot put that per-tool rule in force (see the earlier "
                "warning for why), so mounting it would make the tool reachable. This "
                "session runs without that server%s",
                label,
                name,
                (
                    "; with kirocrew-core withheld it cannot report back to its channel"
                    if name in CONTROL_PLANE_SERVERS
                    else ""
                ),
            )
            continue
        if name in withheld:
            logger.warning(
                "%s session MCP: withholding server %r -- it is one of Crew's own servers, "
                "and an element the agent spec describes cannot carry this session's "
                "identity",
                label,
                name,
            )
            continue
        kept.append(element)
    out: list[dict[str, Any]] = opencode_elements(
        kept, session_key=session_key, channel_id=channel_id, session_token=session_token
    )
    for stub in stub_elements:
        if not isinstance(stub, Mapping):
            continue
        name = stub.get("name")
        if name in withheld:
            logger.warning(
                "%s session MCP: withholding pooled stub %r -- the projection withheld "
                "the server it wraps, and a stub re-adds it unrestricted",
                label,
                name,
            )
            continue
        if not projection.allowlist.grants(str(name)):
            logger.info(
                "%s session MCP: not mounting pooled stub %r -- the agent spec's `tools` "
                "does not reference it, and the allowlist that filtered the translated "
                "half applies to a stub of the same name",
                label,
                name,
            )
            continue
        out.append(without_stdio_tag(dict(stub)))
    return out


def opencode_projection(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    stub_elements: Collection[Mapping[str, Any]] = (),
    work_dir: object = None,
    session_key: str = "",
    channel_id: str = "",
    session_token: str = "",
    denies_in_force: Collection[str] | None = None,
) -> SessionProjection:
    """The whole opencode array -- spec translation AND pooled stubs -- plus its deny rules.

    The mirror's :meth:`~OpenCodeMirror.session_projection`, as a function so it can
    be called and tested without the class.

    A narrowed server stays MOUNTED. Each switched-off tool becomes a ``deny`` rule
    under the harness's own tool id (:func:`opencode_deny_rules`), returned as
    ``harness_deny_rules`` for the client to seed after its ``"*": "ask"``. Measured on
    opencode 1.18.30, such a rule removes that one tool from the model's tool list,
    and the server's other tools stay listed and still ask per call. This holds for
    Crew's own control plane and a third-party server alike, and for a pooled stub,
    because the rule keys on the server name the stub shares.

    *denies_in_force* is the set of rule ids the client found IN FORCE in the
    harness's resolved config, or ``None`` before it has seeded any (the spawn path's
    first call, whose rules ARE what gets seeded). A narrowed server with a
    switched-off tool whose id is not in that set is withheld whole instead. Two
    cases land there: the harness re-maps the id to a builtin rule
    (:data:`_REMAPPED_TOOL_IDS`), and a rule the read-back found outranked -- the
    harness lets the LAST matching rule win, and a lower config source that already
    names the same tool keeps its earlier place, behind the seed's ``"*"``.

    ``denied_tools`` stays EMPTY. The client's per-call refusal needs a structured
    ``(server, tool)`` on the call, and this harness sends only a fused title, so
    pairs here could never match.

    Blocking (parses the agent spec once), so callers run it off the event loop.
    """
    projection = session_mcp_projection(
        agent,
        stub_server_names=stub_server_names,
        work_dir=work_dir,  # type: ignore[arg-type]
    )
    rules = opencode_deny_rules(projection.disabled_tools)
    in_force = frozenset(rules if denies_in_force is None else denies_in_force)
    unhonoured = frozenset(
        server
        for server, tool in projection.disabled_tools
        if opencode_tool_id(server, tool) not in in_force
    )
    for server in sorted(unhonoured):
        logger.warning(
            "opencode session MCP: a switched-off tool on %r has no deny rule in force on "
            "this session -- either its tool id is one the harness judges by a builtin "
            "rule, or a lower opencode config source names that tool and outranks Crew's "
            "seed. Remove that tool's key from your opencode config to restore per-tool "
            "deny for it",
            server,
        )
    out = place_single_binary_array(
        projection,
        label="opencode",
        unhonoured=unhonoured,
        stub_elements=stub_elements,
        session_key=session_key,
        channel_id=channel_id,
        session_token=session_token,
    )
    return SessionProjection(
        params={"mcpServers": out},
        disabled_servers=projection.disabled_servers,
        # Only the names actually withheld for a restriction: every other narrowed
        # server is mounted, with its rule in force.
        restricted_servers=unhonoured,
        unhonoured_servers=unhonoured,
        harness_deny_rules=rules,
        derived_spec_snapshot=projection.derived_spec_snapshot,
        zero_tools=projection.zero_tools,
        agent_spec=projection.agent_spec,
    )


class OpenCodeMirror(AgentConfigMirror):
    """Projects the agent spec onto ``opencode acp``."""

    backend = ACP_BACKEND_OPENCODE

    def rulings(self) -> Mapping[Concern, Ruling]:
        return {
            Concern.MCP_SERVERS: Ruling(
                _D.DELIVERED,
                "the session/new + session/load mcpServers array, translated by "
                "acp.session_mcp.session_mcp_servers with NO new translator and "
                "then narrowed by this module two ways. The channel was declared "
                "no-channel until this mirror, on the reading that opencode's "
                "initialize advertises mcpCapabilities of http and sse and 'no "
                "stdio' -- which is not a refusal: ACP's McpCapabilities schema "
                "has only those two boolean fields and no stdio field at all, so a "
                "conforming agent cannot advertise stdio and that answer is what "
                "FULL support looks like. Measured against opencode 1.18.30: the "
                "element Crew already emits is accepted, the child is spawned, its "
                "tools are listed and the element env reaches it. The narrowing: "
                "(1) a third-party server whose spec narrows it per tool is "
                "omitted rather than forwarded un-narrowed -- see DENIED_TOOLS; "
                "(2) Crew's own control plane carries KIROCREW_SESSION_KEY (plus "
                "CHANNEL_ID, BOUND_PORT and any KIROCREW_HOME override) on the "
                "element, and ONLY kirocrew-core and kirocrew-cron do, because "
                "those are the two entries the translation replaces from the "
                "managed source so no part of a hand-editable spec reaches a child "
                "holding that credential. Here the carriage is about SCOPE rather "
                "than about survival: this harness's MCP children inherit the "
                "ambient environment, so without the element a control-plane child "
                "would pick up the gateway's key instead of this session's. Every "
                "OTHER managed Crew server is withheld rather than mounted "
                "credential-less, since it would answer not_bound to every call, "
                "and that set is DERIVED as the managed servers minus the control "
                "plane (providers.mirrors.identity.identity_bound_crew_servers). "
                "The array and the withhold set come from ONE parse of the spec "
                "(session_mcp_projection), so a narrowing that lands between two "
                "reads of a user-writable file cannot leave a narrowed server "
                "mounted un-narrowed. NO transport filter, unlike codex: http and "
                "sse elements are both accepted here, so nothing needs dropping -- "
                "with the caveat that the adapter maps sse onto its own `remote` "
                "kind, which is streamable HTTP, so an SSE-only server is spoken "
                "to over the wrong shape. Unlike claude the array is NOT "
                "conditional on Crew owning a permission file: opencode's routing "
                "is Routing.VERIFIED_SEEDED_SETTINGS -- a mechanism in "
                "tool_gate.ENFORCED_ROUTINGS -- seeded on OPENCODE_CONFIG_CONTENT "
                "and READ BACK from the harness's own config resolution before the "
                "first prompt, so a session that cannot establish the asking "
                "posture is refused rather than run",
            ),
            Concern.TOOL_ALLOWLIST: Ruling(
                _D.TRANSLATED,
                "`tools` is not sent; it is applied during translation as the "
                "allowlist deciding which servers enter the array -- the "
                "translated half AND the pooled broker stubs, from the same parse "
                "-- so a server the spec declares but never references is not "
                "mounted here either, which is kiro-cli parity. A missing or "
                "non-list `tools` is an EMPTY allowlist, not 'no filter', for the "
                "same reason. It carries the same residual claude and codex have: "
                "an `@server/tool` grant narrows to one tool on kiro-cli but "
                "mounts the whole server here, because the tool set is not "
                "knowable without connecting",
            ),
            Concern.DENIED_TOOLS: Ruling(
                _D.TRANSLATED,
                "into a per-tool `deny` rule in the harness's own `permission` "
                "config, which Crew already seeds on OPENCODE_CONFIG_CONTENT for the "
                'permission routing: `{"*": "ask", "<server>_<tool>": '
                '"deny"}`, the tool id spelled the way the harness names an MCP '
                "tool (opencode_tool_id). Measured on opencode 1.18.30, the rule "
                "removes that one tool from the model's tool list, while the server's "
                "other tools stay listed and still ask per call -- so a narrowed "
                "server stays MOUNTED, Crew's own control plane included, and "
                "per_tool_deny is settings-file. The rule is held to the harness's "
                "resolved config, not to the seed: the harness lets the LAST matching "
                "rule win, and a lower source that already names the same tool keeps "
                "its earlier place behind the seed's `*`, which turns the deny into an "
                "ask. The routing read-back evaluates every rule the way the harness "
                "does, and a server whose rule did not come out in force is withheld "
                "whole, with a warning naming the cause. An id the harness re-maps to "
                "a builtin rule is withheld the same way. denied_tools stays EMPTY: "
                "the harness sends only a fused `<server>_<tool>` title on a call, so "
                "the client has no structured pair to refuse by",
            ),
            Concern.AUTO_APPROVE: Ruling(
                _D.WITHHELD,
                "opencode's nearest equivalent is its own `permission` setting, "
                "and Crew already writes the one value the host gate needs there "
                "(ask) and verifies it by reading the harness's resolved config "
                "back. Translating autoApprove would pre-approve the call INSIDE "
                "the harness, which then never sends session/request_permission -- "
                "skipping Crew's permission gate, its governance ceiling and its "
                "SEL audit. This is the harness whose asking is the reason it is "
                "offered at all, so every MCP call must reach the host gate",
            ),
            Concern.MODEL: Ruling(
                _D.DELIVERED,
                "not through this mirror: opencode is in "
                "ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION, so the resolved model is "
                "pushed with session/set_config_option after session/new. Named "
                "here rather than left out so a reader does not read this mirror's "
                "silence as the model being dropped",
            ),
            Concern.MODEL_ALLOWLIST: Ruling(
                _D.WITHHELD,
                "the direction is reversed on this backend, as on codex: opencode "
                "advertises its own model list as a session/new configOptions "
                "select and that advertised list is the only source of ids "
                "set_config_option accepts, so Crew CAPTURES the advertised set "
                "(ACP_BACKENDS_ADVERTISED_MODEL_SELECTION) instead of sending one. "
                "Projecting the spec's availableModels here would offer ids the "
                "harness refuses",
            ),
            Concern.PERMISSION_MODE: Ruling(
                _D.WITHHELD,
                "opencode has a `permission` setting and Crew writes it -- but it "
                "writes the FIXED value tool_gate demands (ask), not the mode the "
                "spec asked for, and then reads the harness's own resolved config "
                "back to check no higher-precedence source overrode it. Honouring "
                "a spec-requested mode would let an agent file widen an opencode "
                "session past the one boundary that makes this harness offerable. "
                "A deliberate override, not a dropped setting",
            ),
            Concern.PROMPT: Ruling(
                _D.WITHHELD,
                "not a mirror concern on any backend: the prompt reaches every "
                "harness as ordinary prompt text in the [AGENT SYSTEM PROMPT] "
                "context block, which is backend-agnostic and already works",
            ),
            Concern.RESOURCES: Ruling(
                _D.WITHHELD,
                "same as PROMPT -- steering files are injected as context text, "
                "not projected into a backend's config",
            ),
            Concern.HOOKS: Ruling(
                _D.TRANSLATED,
                "the ACP session/new element set has no hooks field, so the harness never "
                "receives the spec's hooks block, and Crew's turn loop runs it instead "
                "(agent_sdk/spec_hooks.py, ACP_BACKENDS_CREW_FIRES_SPEC_HOOKS), as it does "
                "for KAS. That is sound here because the ask permission Crew seeds and "
                "reads back makes every tool call arrive as a permission request, which "
                "is where a PreToolUse hook runs and can block. A tool matcher is written "
                "in kiro-cli's names; the tool name opencode states as the first "
                "tool_call frame's title is mapped back to them "
                "(acp/harness_tool_names.py), so execute_bash meets opencode's bash. "
                "This covers the chat, subagent and task-runner turn loops; a "
                "channel-agent turn runs no script hooks on any backend",
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
        session_token: str = "",
        **kwargs: object,
    ) -> dict[str, object]:
        """The wire face: the ``mcpServers`` array for this opencode session.

        ``permission_surface_owned`` is accepted and IGNORED (it arrives in
        ``kwargs``), which is the documented behaviour for a mirror outside
        claude's class. The flag exists because claude's permission surface is a
        file Crew may not own, so a pre-approved tool there never sends
        ``session/request_permission`` and Crew's gate never fires. opencode has no
        such file in play: its routing is seeded on ``OPENCODE_CONFIG_CONTENT`` and
        then READ BACK from the harness's own config resolution, so a session whose
        resolved value is not the required one is refused before its first prompt.
        Failing closed on the flag here would withhold every Crew tool from every
        opencode session on the strength of a condition that does not describe this
        backend.

        ``session_key`` and ``channel_id`` are the caller's, because a mirror
        cannot discover them. On this harness they scope rather than supply: the
        MCP child inherits the ambient environment, so without them a
        control-plane child would come up holding the GATEWAY's session key
        instead of this session's.

        ``work_dir`` is the session's project checkout and is required for
        CORRECTNESS, not convenience: kiro-cli resolves ``--agent`` against
        ``<work_dir>/.kiro/agents`` as well as the user level, so omitting it makes
        a project-only agent read as "no spec" and drops the ``tools`` allowlist
        that spec declared.

        ``stub_elements`` are the shared gateway's broker stubs for this session,
        which the caller holds (it owns the overlay) and this mirror places, so the
        withhold rule covers both halves of the array -- see
        :func:`opencode_projection`.

        Blocking -- it reads the agent spec. The caller warms this on the opencode
        spawn path and serves the shared ``session/new`` call site from that cache
        (harness-parity H13).
        """
        stubs = kwargs.get("stub_elements") or ()
        return self.session_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stubs if isinstance(stubs, (list, tuple)) else (),
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
            session_token=session_token,
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
        session_token: str = "",
        **kwargs: object,
    ) -> SessionProjection:
        """The structured face: :func:`opencode_projection`.

        ``harness_denies_in_force`` arrives in ``kwargs``: the rule ids the client's
        read-back found in force, or ``None`` before any were seeded. Every other
        keyword in ``kwargs`` is ignored, as :meth:`session_params` documents
        (``permission_surface_owned`` arrives there)."""
        in_force = kwargs.get("harness_denies_in_force")
        return opencode_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stub_elements,
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
            session_token=session_token,
            denies_in_force=(
                frozenset(str(rule) for rule in in_force)
                if isinstance(in_force, (set, frozenset, list, tuple))
                else None
            ),
        )
