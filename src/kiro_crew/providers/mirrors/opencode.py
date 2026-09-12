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
one setting ``tool_gate`` demands), and the MCP servers deliberately do NOT travel
there. That channel works, but ``OPENCODE_CONFIG_CONTENT`` MERGES with the project
and user config rather than replacing them, and declaring the same server in both
the config block and the array DOUBLE-MOUNTS it: both children spawn, because the
array does not go through config resolution at all. So the array is the one
channel, and this module is the whole of it.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any, Mapping

from kiro_crew.acp.session_mcp import CONTROL_PLANE_SERVERS, session_mcp_projection
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
        control_plane_identity_env(session_key, channel_id, label="opencode")
        if session_key or channel_id
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


def narrowed_control_plane(disabled_tools: Collection[tuple[str, str]]) -> frozenset[str]:
    """Crew's own control-plane servers whose spec narrows them PER TOOL.

    These are withheld on this transport, and the whole of the argument is that
    codex's exemption for them was never about the wire — it was about having a
    SECOND channel to honour the restriction on.

    ``session_mcp_restricted_servers`` subtracts the control plane, because
    ``managed_mcp_spec_entry`` emits only command/args/env: a ``disabledTools`` on
    those two entries never reaches the element, so withholding on the strength of
    a key that was never delivered would be pure loss. codex completes that argument
    — it keeps the server mounted AND refuses a call to a switched-off tool when the
    adapter asks permission for it, so nothing is lost and nothing is widened.

    Neither half of that completion exists here. This harness emits no
    ``_meta.kiro`` and no ``rawInput.server``/``tool`` — only a fused
    ``<server>_<tool>`` title — so ``AcpClient._deny_spec_disabled_tool`` has no
    identity to match, and whether it asks for an MCP tool call at all under
    ``permission: ask`` is unmeasured. Carrying the exemption across on codex's
    reasoning while dropping the mechanism that made it safe would leave a tool the
    user switched off on the dashboard REACHABLE, on an ordinary path, with nothing
    saying so.

    So the rule this module already applies to a third-party server applies here
    too, in the module's own words: an availability cost is the honest price of a
    deny channel this transport does not have; reachability of a tool the user
    switched off is not. The cost is stated rather than hidden — a session whose
    control plane is narrowed loses that server, and with ``kirocrew-core`` gone it
    cannot report back to its channel at all — and it is paid ONLY by an operator
    who narrowed the control plane deliberately. A default install narrows nothing
    and is untouched, which is why this is not the "present but unusable" shape the
    folder exists to remove: the server is absent and logged, not mounted and mute.

    Derived from the ``(server, tool)`` PAIRS the caller already resolved, never
    from the spec again, so the withhold decision reads the same bytes as the array
    — and it sees a narrowing written to the GLOBAL settings file, which is where
    the dashboard's ordinary tool-off action writes it. Free of I/O.
    """
    return frozenset(server for server, _tool in disabled_tools) & frozenset(CONTROL_PLANE_SERVERS)


def opencode_projection(
    agent: str | None,
    *,
    stub_server_names: Collection[str] = (),
    stub_elements: Collection[Mapping[str, Any]] = (),
    work_dir: object = None,
    session_key: str = "",
    channel_id: str = "",
) -> SessionProjection:
    """The whole opencode array -- spec translation AND pooled stubs.

        The mirror's :meth:`~OpenCodeMirror.session_projection`, as a function so it can
        be called and tested without the class.

        ONE owner for both halves of the array, exactly as codex has. The shared MCP
        gateway's broker stubs carry the SAME name as the spec entry each one rewrites,
        so a stub appended after the projection withheld that name un-withholds it --
        and the stub is the UNRESTRICTED server, the worse of the two. Taking the stub
        ELEMENTS here, beside the stub NAMES the translation already yields to, lets the
        withhold rule run over both halves in one place instead of being re-spelled at
        the call site. Once this backend is in ``MIRRORS``,
        ``AcpClient._pooled_mcp_servers`` returns ``[]`` for it, so this IS the stubs'
        only route in.

        Stubs are appended as given rather than run through :func:`opencode_elements`:
        they are gateway-authored, their env is the broker's own, and none of them is
        Crew's control plane (a session-strict server is never pooled), so there is no
        identity to add. They ARE held to the spec's ``tools`` allowlist, from the same
        parse that filtered the translated half: the overlay is written per agent from
        the global settings file too, so it can carry a stub for a server this agent
        never references, and a stub is that server.

    ``denied_tools`` is deliberately EMPTY, and that is a statement rather than a
        default. The client's per-call refusal (``AcpClient._deny_spec_disabled_tool``)
        identifies an MCP call from codex-acp's ``rawInput = {server, tool}``, which
        opencode does not emit, so pairs returned here could never match -- and a set
        that reads as an enforced restriction while being inert is worse than an empty
        one. The restriction is honoured by WITHHOLDING instead, for Crew's own control
        plane as well as for a third-party server: see :func:`narrowed_control_plane`
        for why codex's exemption does not extend to a transport with no second channel.

        Blocking (parses the agent spec once), so callers run it off the event loop.
    """
    projection = session_mcp_projection(
        agent,
        stub_server_names=stub_server_names,
        work_dir=work_dir,  # type: ignore[arg-type]
    )
    withheld = withheld_servers(projection.restricted) | narrowed_control_plane(
        projection.disabled_tools
    )
    kept: list[dict[str, Any]] = []
    narrowed_plane = narrowed_control_plane(projection.disabled_tools)
    for element in projection.servers:
        name = element.get("name")
        if name in narrowed_plane:
            logger.warning(
                "opencode session MCP: withholding Crew's own %r -- its agent spec (or the "
                "global MCP settings file) switches one of its tools off, and this transport "
                "has NO channel for that restriction: no per-tool deny slot on the element, "
                "and no structured MCP identity on a tool call for the client to refuse by. "
                "Leaving it mounted would make a tool you switched off reachable. This "
                "session runs without that server -- with kirocrew-core withheld it cannot "
                "report back to its channel at all -- and the way to clear it is to stop "
                "narrowing the control plane, or to run this agent on a backend that has a "
                "deny channel",
                name,
            )
            continue
        if name in withheld:
            logger.warning(
                "opencode session MCP: withholding server %r -- this transport cannot deliver "
                "what makes it correct (a per-tool restriction it has no deny channel for, "
                "or the session identity a Crew server binds to), and a mounted server that "
                "cannot work is the defect this projection exists to remove",
                name,
            )
            continue
        kept.append(element)
    out: list[dict[str, Any]] = opencode_elements(
        kept, session_key=session_key, channel_id=channel_id
    )
    for stub in stub_elements:
        if not isinstance(stub, Mapping):
            continue
        name = stub.get("name")
        if name in withheld:
            logger.warning(
                "opencode session MCP: withholding pooled stub %r -- the projection withheld "
                "the server it wraps, and a stub re-adds it unrestricted",
                name,
            )
            continue
        if not projection.allowlist.grants(str(name)):
            logger.info(
                "opencode session MCP: not mounting pooled stub %r -- the agent spec's `tools` "
                "does not reference it, and the allowlist that filtered the translated "
                "half applies to a stub of the same name",
                name,
            )
            continue
        # Through the SAME tag strip as the translated half. A stub is gateway-authored
        # and carries no tag today, so this changes no shipped element -- but the array
        # is one array, and a rule that held for only the half this module builds
        # itself would be false of the other the moment the gateway's entry shape
        # gained one.
        out.append(without_stdio_tag(dict(stub)))
    return SessionProjection(params={"mcpServers": out})


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
                "is Routing.VERIFIED_SEEDED_SETTINGS -- one of the two mechanisms in "
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
                "the restriction is honoured by WITHHOLDING THE SERVER -- for a "
                "third-party server AND for Crew's own control plane, which is "
                "where this backend parts company with codex. opencode's element "
                "schema is {name, command, args, env} with no per-tool slot, and "
                "claude's answer (re-applying it as permissions.deny) needs a "
                "settings file Crew does not write here. So "
                "acp.session_mcp.session_mcp_restricted_servers names the "
                "third-party servers (read from the spec AND the global settings "
                "file, which is where the dashboard's ordinary tool-off action "
                "writes them) and the array omits them. An availability cost is the "
                "honest price of a deny channel this transport does not have; "
                "reachability of a tool the user switched off is not. `disabled` "
                "needs nothing here -- build_agent_config strips a disabled "
                "server's @alias from `tools`, and the allowlist mounts nothing "
                "`tools` does not name. "
                "codex EXEMPTS the control plane from that withholding and this "
                "backend does not, because the exemption was never about the wire: "
                "managed_mcp_spec_entry emits only command/args/env, so the "
                "narrowing reaches no element on either backend -- what made "
                "keeping the server safe on codex is its SECOND channel, refusing "
                "the call at the permission request. Neither half of that exists "
                "here: this harness emits no _meta.kiro and no "
                "rawInput.server/tool (only a fused `<server>_<tool>` title, whose "
                "recognition is a separate change), so "
                "AcpClient._deny_spec_disabled_tool has no identity to match, and "
                "whether it asks permission for an MCP tool call at all is "
                "unmeasured. Carrying the exemption across without the mechanism "
                "would leave a tool the operator switched off on the dashboard "
                "REACHABLE, on an ordinary path, with nothing saying so. So a "
                "narrowed control-plane server is withheld and the cost is stated "
                "rather than hidden -- that session loses the server, and with "
                "kirocrew-core withheld it cannot report back to its channel at all "
                "-- and it is paid ONLY by an operator who narrowed the control "
                "plane deliberately, since a default install narrows nothing. That "
                "is an absent server with a logged reason, not the mounted-and-mute "
                "shape this folder exists to remove. denied_tools is therefore "
                "EMPTY, which is a statement and not a default: pairs returned here "
                "could never match, and a set that reads as an enforced restriction "
                "while being inert is worse than an empty one. The exemption "
                "returns when the two measurements do -- see narrowed_control_plane",
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
                _D.NO_CHANNEL,
                "opencode runs hooks natively (its config schema carries them, and "
                "plugins hook tool execution), and the ACP session/new element set "
                "has no hooks field -- so a user's per-agent hooks block reaches "
                "kiro-cli and no other backend, exactly the gap claude and codex "
                "record. Crew's OWN hooks (hooks.py, fired on ACP tool events) are "
                "unaffected and work on this backend already; this gap is only the "
                "spec block",
                channel="a hooks field on the opencode session/new element set, or "
                "a hooks block in the OPENCODE_CONFIG_CONTENT Crew already seeds "
                "for the permission routing -- that channel MERGES rather than "
                "replaces, so it cannot clobber a user's own hooks, which is why "
                "this needs a decision about precedence rather than a writer",
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
        """The structured face: :func:`opencode_projection`, with ``kwargs`` ignored
        as :meth:`session_params` documents (``permission_surface_owned`` arrives
        there). ``denied_tools`` is empty on this backend by decision, not by
        default -- see the ``disabledTools`` ruling."""
        del kwargs
        return opencode_projection(
            agent,
            stub_server_names=stub_server_names,
            stub_elements=stub_elements,
            work_dir=work_dir,
            session_key=session_key,
            channel_id=channel_id,
        )
