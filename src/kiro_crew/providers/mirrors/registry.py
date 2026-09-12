"""Which backend projects the agent spec how — as a typed declaration, per backend.

The point of a registry rather than a lookup that returns ``None`` on a miss is
that **absence has to be a statement**. A backend with no entry here fails the
parity test; a backend that genuinely needs no projection says so, in a record a
test can read.

A free-text reason was not enough to carry that distinction. "Declared not to
need one" and "nobody got round to it" were both spelled as a paragraph of prose
under one name, so a selectable backend could sit there with an explanation of
why its projection had not been written and every check stayed green. That is the
structural reason the same missing-tools defect shipped on four harnesses in a
row. :class:`McpProjection` replaces the paragraph with a KIND, and the kinds
that are not finished states carry the fields that make them addressable — what
would have to exist, and where the work is tracked.

The declaration lives here rather than in :mod:`kiro_crew.agent_sdk.backends`,
which owns the capability sets, for one mechanical reason: that module is a leaf
that imports neither ``kiro_crew.acp`` nor ``kiro_crew.providers``, and
``config.loader`` reaches it from inside ``KiroCrewConfig.load()``. A table
naming this folder's mirror classes would put that cycle back. The projection
vocabulary (:class:`~kiro_crew.providers.mirrors.base.Concern`,
:class:`~kiro_crew.providers.mirrors.base.Disposition`) already lives in this
folder, so the declaration that selects between them belongs beside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from kiro_crew.acp_backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
)
from kiro_crew.providers.mirrors.base import AgentConfigMirror
from kiro_crew.providers.mirrors.claude_code import ClaudeCodeMirror
from kiro_crew.providers.mirrors.codex import CodexMirror
from kiro_crew.providers.mirrors.opencode import OpenCodeMirror


class ProjectionKind(str, Enum):
    """How one backend's MCP surface is reached from the agent spec.

    Closed, and an enum rather than a bare ``Literal`` for the same reason
    :class:`~kiro_crew.providers.mirrors.base.Disposition` is one: the members are
    read by name at call sites and by value in the doc tables, and a misspelled
    string in a declaration must fail at import rather than resolve to a kind
    nothing handles.
    """

    #: The backend reads ``~/.kiro/agents/<name>.json`` itself. There is nothing
    #: to project, so the spec's servers reach the session by construction.
    NATIVE = "native"
    #: A mirror in this folder projects it. Must have a class in :data:`MIRRORS`.
    MIRROR = "mirror"
    #: Crew projects it, from a module outside this folder. A real projection with
    #: a real channel, named by ``projection`` so a reader can go and read it.
    EXTERNAL = "external"
    #: No transport this backend advertises can carry Crew's servers. The only
    #: kind under which a session legitimately holds none of Crew's tools, and the
    #: only one that has to name what would have to exist for that to change.
    NO_CHANNEL = "no-channel"


class PerToolDeny(str, Enum):
    """How far a backend can honour the spec's per-TOOL MCP restriction.

    ``mcpServers.<name>.disabledTools`` is a RESTRICTION a user writes, and the
    dashboard's ordinary tool-off action writes it. How much of it survives the trip
    to a backend is a property of the transport, and the three members below are the
    three states that exist -- each one observable in what the projection returns,
    which is what lets a test hold the declaration to the behaviour rather than to
    its own prose.

    This is a DECLARATION, not a requirement. Per-tool MCP deny is not something
    every provider can offer, and this folder's thesis is that an honest "this
    transport cannot carry it" beats a projection that quietly drops it. What a
    reader needs is to know WHICH of the three they are getting before a session
    runs.

    Three members rather than the one flag its shipped reader branches on
    (``cli_doctor`` acts on ``WHOLE_SERVER`` alone), and the reason is the drift
    test rather than a future renderer: the three states are told apart by two
    INDEPENDENT observations -- whether a narrowed server stays mounted, and whether
    the projection hands the client a deny set -- so the test catches claude
    silently ceasing to write its deny rules and codex silently ceasing to hand over
    its pairs. A boolean cannot express either: both would read false and stay
    false while the behaviour changed underneath. A reader that renders all three
    (the per-provider ability card) is a consumer this serves, not the reason it has
    three members.
    """

    #: The restriction reaches the harness as a per-tool rule in a file Crew writes,
    #: so the narrowed server stays MOUNTED and the harness itself refuses the tool.
    SETTINGS_FILE = "settings-file"
    #: No per-tool slot on the wire, but the backend asks permission per MCP call
    #: with an identity Crew can match, so Crew refuses the call itself. The narrowed
    #: server may stay mounted where that channel is complete.
    PER_CALL = "per-call"
    #: No channel at all -- neither a rule the harness reads nor a per-call identity
    #: Crew can match. The only faithful action is WITHHOLDING the whole server, so
    #: the restriction costs availability rather than being silently dropped.
    WHOLE_SERVER = "whole-server"


@dataclass(frozen=True)
class McpProjection:
    """One backend's declared answer to "how do Crew's MCP servers get here?".

    ``reason`` is required for every kind. The two kinds that are not finished
    states additionally have to be ADDRESSABLE, and the constructor enforces it
    rather than a reviewer: a ``no-channel`` names the ``channel`` that would have
    to exist and the ``tracking`` that carries the decision, and an ``external``
    names the ``projection`` module a reader goes to. That is the whole difference
    between this record and the prose it replaces — a paragraph can explain a gap
    without ever giving it an address, and a gap with no address is
    indistinguishable from a decision.
    """

    kind: ProjectionKind
    reason: str
    #: ``no-channel`` only: the delivery path that would carry Crew's servers.
    channel: str = ""
    #: ``no-channel`` and ``external`` only: issue URL or repo-relative doc anchor.
    tracking: str = ""
    #: ``external`` only: the dotted module path holding the projection.
    projection: str = ""
    #: ``mirror`` only: how far this backend honours a per-TOOL MCP restriction.
    #: Required there and refused elsewhere, because a mirror is what performs the
    #: projection and so is the only kind that can answer -- a ``native`` backend
    #: reads the spec itself, and an ``external`` projection's answer belongs with
    #: its own module when that moves into this folder (RFC section 5).
    per_tool_deny: PerToolDeny | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("an McpProjection needs a reason")
        if self.kind is ProjectionKind.NO_CHANNEL:
            if not self.channel.strip():
                raise ValueError(
                    "a no-channel projection must name the channel that would carry "
                    "Crew's servers — an unaddressed gap reads as a decision"
                )
            if not self.tracking.strip():
                raise ValueError(
                    "a no-channel projection must name its tracking issue or doc "
                    "anchor — 'not written yet' is not a kind"
                )
        elif self.channel:
            raise ValueError("channel is only meaningful for a no-channel projection")
        if self.kind is ProjectionKind.EXTERNAL:
            if not self.projection.strip():
                raise ValueError("an external projection must name the module that holds it")
            if not self.tracking.strip():
                raise ValueError(
                    "an external projection must name the tracking issue or doc "
                    "anchor for folding it into providers/mirrors/"
                )
        elif self.projection:
            raise ValueError("projection is only meaningful for an external projection")
        if self.kind in (ProjectionKind.NATIVE, ProjectionKind.MIRROR) and self.tracking:
            raise ValueError("tracking is only meaningful for a kind that is not a finished state")
        if self.kind is ProjectionKind.MIRROR and self.per_tool_deny is None:
            raise ValueError(
                "a mirror must declare how far it honours a per-tool MCP restriction "
                "(per_tool_deny) — leaving it unsaid puts a reader back to inferring "
                "it from source, which is the state this record replaced"
            )
        if self.kind is not ProjectionKind.MIRROR and self.per_tool_deny is not None:
            raise ValueError("per_tool_deny is only meaningful for a mirror projection")


#: Backends whose spec projection lives in this folder.
MIRRORS: dict[str, type[AgentConfigMirror]] = {
    ACP_BACKEND_CLAUDE: ClaudeCodeMirror,
    ACP_BACKEND_CODEX: CodexMirror,
    ACP_BACKEND_OPENCODE: OpenCodeMirror,
}

#: Every backend this build can spell, and how its MCP surface is reached.
#:
#: Read as a claim to be checked, not as a backlog. The parity test holds every
#: selectable backend to exactly one entry here, so a new harness cannot reach the
#: dashboard switch without one of these four answers being written down.
PROJECTIONS: dict[str, McpProjection] = {
    ACP_BACKEND_KIRO: McpProjection(
        kind=ProjectionKind.NATIVE,
        reason=(
            "kiro-cli is handed --agent and reads ~/.kiro/agents/<name>.json itself, so "
            "the spec needs no projection at all. Its only native-config write is the "
            "<work_dir>/.kiro/settings/cli.json overlay (providers/acp.py "
            "_write_cli_overlay / _write_tool_search_overlay) carrying model, effort and "
            "tool-search settings — a small overlay rather than a projection, which is "
            "why folding it into this folder is a separate decision and not assumed here"
        ),
    ),
    ACP_BACKEND_CLAUDE: McpProjection(
        kind=ProjectionKind.MIRROR,
        reason="claude_code.py — both faces: the session/new mcpServers array and "
        "<work_dir>/.claude/settings.local.json",
        # The narrowed server stays MOUNTED: session_mcp_deny_rules re-expresses
        # disabledTools as permissions.deny rules in the settings file the adapter
        # reads, so the restriction survives as a per-tool rule.
        per_tool_deny=PerToolDeny.SETTINGS_FILE,
    ),
    ACP_BACKEND_CODEX: McpProjection(
        kind=ProjectionKind.MIRROR,
        reason="codex.py — the wire face alone. Crew writes no codex file, so the "
        "session/new array is this backend's whole MCP channel",
        # No wire slot and no file of Crew's, but codex asks session/request_permission
        # per MCP call carrying rawInput.server/tool, so the CLIENT refuses a
        # switched-off tool (AcpClient._deny_spec_disabled_tool). Complete for Crew's
        # control plane, which is why that stays mounted; a third-party server is
        # withheld instead, since its readOnlyHint tools are approved inside codex
        # without ever asking.
        per_tool_deny=PerToolDeny.PER_CALL,
    ),
    ACP_BACKEND_KAS: McpProjection(
        kind=ProjectionKind.EXTERNAL,
        reason=(
            "KAS has the most complete projection of any backend — prompt inlined from "
            "file://, tools always explicit, mcpServers minus broker stubs, permissions "
            "derived from allowedTools through KAS's own capability vocabulary — and it "
            "travels as _meta.kiro.customAgents on session/new rather than as an "
            "mcpServers array. A real projection down a real channel, so this is not a "
            "gap: what is outstanding is only WHERE the code sits, and the RFC schedules "
            "that as a pure relocation of its own so a live harness's projection is not "
            "moved and changed in one diff"
        ),
        projection="kiro_crew.acp.kas_agents",
        tracking="docs/request-for-change/rfc-agent-config-mirror.md#5-migration",
    ),
    ACP_BACKEND_OPENCODE: McpProjection(
        kind=ProjectionKind.MIRROR,
        reason="opencode.py -- the wire face alone, and the entry that shows why a "
        "no-channel claim has to be measured rather than inferred. It was declared "
        "no-channel on the reading that opencode's initialize advertises "
        "mcpCapabilities of http and sse and NO stdio, so the session/new array "
        "could not carry Crew's stdio servers. That inference was wrong: ACP's "
        "McpCapabilities schema has exactly two boolean fields, http and sse, and "
        "no stdio field at all, so a conforming agent cannot advertise stdio and "
        "that answer is what FULL support looks like. Driven against opencode "
        "1.18.30, the element acp.session_mcp.acp_server_element already emits is "
        "accepted, the child is spawned, its tools are listed and the element's env "
        "reaches it. The same entry also contradicted itself -- its last sentence "
        "said the array carries the shared gateway's broker stubs, which are stdio "
        "elements too -- and both halves could not be true. Crew writes no opencode "
        "MCP config: OPENCODE_CONFIG_CONTENT (which Crew does seed, for the "
        "permission routing alone) MERGES rather than replaces, and declaring one "
        "server in both that block and the array double-mounts it, so the array is "
        "this backend's whole MCP channel",
        # WHOLE-SERVER ONLY, and declared rather than enforced. There is no per-tool
        # slot on the element and no file of Crew's, and unlike codex there is no
        # per-call fallback either: this harness emits no _meta.kiro and no
        # rawInput.server/tool -- only a fused `<server>_<tool>` title -- so
        # AcpClient._deny_spec_disabled_tool has nothing to match and the mirror
        # returns an empty deny set rather than pairs that could never fire. A
        # narrowed server is therefore withheld whole, Crew's own control plane
        # included (providers/mirrors/opencode.py::narrowed_control_plane), which is
        # where this backend parts company with codex. Per-tool MCP deny is not a
        # requirement on every provider; what this field owes a reader is that they
        # are getting the whole-server form here BEFORE a session runs.
        per_tool_deny=PerToolDeny.WHOLE_SERVER,
    ),
}


def projection_for(backend: str) -> McpProjection:
    """*backend*'s declared MCP projection.

    Raises for a backend with no entry: an undeclared backend is the failure this
    module exists to catch, so it is loud rather than silently projection-less.
    """
    declared = PROJECTIONS.get(backend)
    if declared is None:
        raise KeyError(
            f"backend {backend!r} has no agent-config mirror and no PROJECTIONS entry — "
            "add one of the two; see providers/mirrors/README.md"
        )
    return declared


def has_mirror(backend: str) -> bool:
    """Does *backend*'s projection live in THIS folder?

    The one bit of provenance a caller cannot recover from a projected array
    alone: "no mirror registered" and "a mirror that dropped this server" are
    different problems with different remedies, and so is "a projection that
    lives elsewhere". False rather than raising for an unknown backend, because
    the callers are diagnostics.
    """
    return backend in MIRRORS


def mirror_for(backend: str) -> AgentConfigMirror | None:
    """The mirror for *backend*, or ``None`` when its projection is not in this folder.

    Raises for a backend that is in neither table: see :func:`projection_for`.
    ``None`` covers all three of the other kinds, and a caller that needs to tell
    them apart asks :func:`projection_for` — which is the whole reason the kind is
    typed rather than inferred from this returning ``None``.
    """
    cls = MIRRORS.get(backend)
    if cls is not None:
        return cls()
    projection_for(backend)
    return None
