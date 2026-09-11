"""Which backend has a mirror, and — as a first-class entry — which has none.

The point of a registry rather than a lookup that returns ``None`` on a miss is
that **absence has to be a statement**. A backend with no entry here fails the
parity test; a backend that genuinely needs no projection says so, with its
reason, in :data:`NO_MIRROR`. That is the difference between "declared not to
need one" and "nobody got round to it", which is the distinction whose absence
let the same missing-tools defect ship twice.
"""

from __future__ import annotations

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

#: Backends whose spec projection lives in this folder.
MIRRORS: dict[str, type[AgentConfigMirror]] = {
    ACP_BACKEND_CLAUDE: ClaudeCodeMirror,
    ACP_BACKEND_CODEX: CodexMirror,
}

#: Backends that deliberately have no mirror, and why. Read as a claim to be
#: checked, not as a backlog: each of these is a decision.
NO_MIRROR: dict[str, str] = {
    ACP_BACKEND_KIRO: (
        "kiro-cli is handed --agent and reads ~/.kiro/agents/<name>.json itself, so "
        "the spec needs no projection at all. Its only native-config write is the "
        "<work_dir>/.kiro/settings/cli.json overlay (providers/acp.py "
        "_write_cli_overlay / _write_tool_search_overlay) carrying model, effort and "
        "tool-search settings — a small overlay rather than a projection, which is "
        "why folding it into this folder is a separate decision and not assumed here"
    ),
    ACP_BACKEND_KAS: (
        "KAS has the most complete projection of any backend (acp/kas_agents.py + "
        "acp/kas_permissions.py: prompt inlined from file://, tools always explicit, "
        "mcpServers minus broker stubs, permissions derived from allowedTools through "
        "KAS's own capability vocabulary) — it simply has not moved into this folder "
        "yet. Tracked as the next PR in the mirror stack; NOT a claim that it needs "
        "no mirror"
    ),
    ACP_BACKEND_OPENCODE: (
        "opencode is offered on a public build and serves sessions today, and its "
        "mirror is unwritten for a reason that is not scheduling: the channel a "
        "projection would travel down does not reach Crew's own tools. Its "
        "initialize result advertises mcpCapabilities of http and sse and no stdio, "
        "so the session/new mcpServers array cannot carry the stdio servers a "
        "projection would put in it, which is why opencode is outside "
        "ACP_BACKENDS_SESSION_MCP_ARRAY. What it reads instead is its own config "
        "file's mcp block, and writing that would mean writing into a checked-out "
        "repository -- the thing this harness's routing seed deliberately avoids by "
        "travelling in the child's environment. So an opencode session carries the "
        "shared gateway's broker stubs when _pooled_mcp_servers appends them and no "
        "Crew tools at all when that gateway is off. Writing a mirror here needs the "
        "transport question answered "
        "first (an http or sse broker endpoint, or a config channel that is not the "
        "user's repo); NOT a claim that it needs no mirror"
    ),
}


def mirror_for(backend: str) -> AgentConfigMirror | None:
    """The mirror for *backend*, or ``None`` when it declares it needs none.

    Raises for a backend that is in neither map: an unregistered backend is the
    failure this module exists to catch, so it is loud rather than silently
    mirror-less.
    """
    cls = MIRRORS.get(backend)
    if cls is not None:
        return cls()
    if backend in NO_MIRROR:
        return None
    raise KeyError(
        f"backend {backend!r} has no agent-config mirror and no NO_MIRROR entry — "
        "add one of the two; see providers/mirrors/README.md"
    )
