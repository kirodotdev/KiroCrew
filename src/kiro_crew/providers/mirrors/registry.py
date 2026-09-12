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


#: Backends whose spec projection lives in this folder.
MIRRORS: dict[str, type[AgentConfigMirror]] = {
    ACP_BACKEND_CLAUDE: ClaudeCodeMirror,
    ACP_BACKEND_CODEX: CodexMirror,
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
    ),
    ACP_BACKEND_CODEX: McpProjection(
        kind=ProjectionKind.MIRROR,
        reason="codex.py — the wire face alone. Crew writes no codex file, so the "
        "session/new array is this backend's whole MCP channel",
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
        kind=ProjectionKind.NO_CHANNEL,
        reason=(
            "opencode's initialize result advertises mcpCapabilities of http and sse and "
            "no stdio, so the session/new mcpServers array cannot carry the stdio servers "
            "a projection would put in it — which is why it is outside "
            "ACP_BACKENDS_SESSION_MCP_ARRAY. What it reads instead is its own config "
            "file's mcp block, and writing that would mean writing into a checked-out "
            "repository, the thing this harness's routing seed deliberately avoids by "
            "travelling in the child's environment. The shared MCP gateway does not "
            "reach it either: _pooled_mcp_servers does append broker stubs for a "
            "backend outside MIRRORS, but a stub is shaped as a stdio element too "
            "(mcp_gateway.session_servers._acp_server_entry emits command/args/env), so "
            "it lands in the very array this harness advertises no transport for. An "
            "opencode session therefore holds none of Crew's own tools, gateway on or "
            "off"
        ),
        channel=(
            "an http or sse MCP endpoint the shared gateway serves, addressable from the "
            "session/new array this harness does accept — or a config channel Crew owns "
            "that is not the operator's checkout"
        ),
        tracking="docs/request-for-change/rfc-agent-config-mirror.md#5-migration",
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
