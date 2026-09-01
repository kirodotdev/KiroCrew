"""Claude Code's agent-config mirror (``claude-agent-acp``).

The only backend that needs BOTH faces:

* **Wire** — MCP servers arrive as the ``mcpServers`` parameter of
  ``session/new`` / ``session/load``. The adapter reads no agent file, so that
  array is the session's entire MCP surface: empty means the harness works and
  every Crew tool is absent, with no error.
* **File** — ``<work_dir>/.claude/settings.local.json``, which the adapter loads
  itself, carries the model allowlist, the resolved model, the permission mode
  and the deny rules derived from ``disabledTools``.

The translation itself lives in :mod:`kiro_crew.acp.session_mcp` (spec entry ->
array element, ``tools`` allowlist, registry filter) and in ``AcpClient``'s
settings writer. This module is the mirror: it declares what happens to every
concern and routes the two faces. That split matches KAS, where
:mod:`kiro_crew.acp.kas_permissions` stays beside the mirror as its translation
helper rather than being inlined into it.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Mapping

from kiro_crew.acp.session_mcp import session_mcp_servers
from kiro_crew.acp_backends import ACP_BACKEND_CLAUDE
from kiro_crew.providers.mirrors.base import (
    AgentConfigMirror,
    Concern,
    Disposition,
    Ruling,
)

logger = logging.getLogger(__name__)

_D = Disposition


class ClaudeCodeMirror(AgentConfigMirror):
    """Projects the agent spec onto claude-agent-acp."""

    backend = ACP_BACKEND_CLAUDE

    def rulings(self) -> Mapping[Concern, Ruling]:
        return {
            Concern.MCP_SERVERS: Ruling(
                _D.DELIVERED,
                "the session/new + session/load mcpServers array, translated by "
                "acp.session_mcp.session_mcp_servers. The adapter reads no agent "
                "file, so this array is the session's whole MCP surface. Delivered "
                "ONLY when Crew authored settings.local.json: that file is this "
                "backend's permission surface, and a tool Crew cannot gate must "
                "not be handed to the session at all",
            ),
            Concern.TOOL_ALLOWLIST: Ruling(
                _D.TRANSLATED,
                "`tools` is not sent; it is applied HERE as the allowlist deciding "
                "which servers enter the array. A missing or non-list `tools` is an "
                "empty allowlist, matching kiro-cli, which mounts a server only "
                "when tools names it. Residual asymmetry: an `@server/tool` grant "
                "narrows to one tool on kiro-cli but mounts the whole server here, "
                "because the tool set is not knowable without connecting",
            ),
            Concern.DENIED_TOOLS: Ruling(
                _D.TRANSLATED,
                "`mcpServers.<name>.disabledTools` becomes permissions.deny rules "
                "(`mcp__<server>__<tool>`) in settings.local.json. It cannot ride "
                "along in the mcpServers array, and dropping a restriction while "
                "still forwarding the server it narrows would widen the surface",
            ),
            Concern.AUTO_APPROVE: Ruling(
                _D.WITHHELD,
                "translating autoApprove would pre-approve a call inside the "
                "adapter, which then never calls back to the host — so it would "
                "skip Crew's permission gate, governance ceiling and SEL audit. "
                "Every MCP call must reach the host gate",
            ),
            Concern.MODEL: Ruling(
                _D.DELIVERED,
                "settings.local.json `model` when the session pinned one, plus the "
                "session/set_config_option verb. DEFAULT_MODEL ('auto') is omitted "
                "so the adapter picks the allowlist head",
            ),
            Concern.MODEL_ALLOWLIST: Ruling(
                _D.DELIVERED,
                "settings.local.json `availableModels` from model_registry. Not "
                "cosmetic: the adapter merges availableModels across every settings "
                "source, so a user ~/.claude carrying a short list collapses a "
                "versioned [1m] id back to the 200K window",
            ),
            Concern.PERMISSION_MODE: Ruling(
                _D.DELIVERED,
                "settings.local.json permissions.defaultMode when the session asked "
                "for one; omitted otherwise so the adapter keeps its own default "
                "rather than Crew asserting one",
            ),
            Concern.PROMPT: Ruling(
                _D.WITHHELD,
                "not a mirror concern on any backend: the prompt reaches every "
                "harness as ordinary prompt text in the [AGENT SYSTEM PROMPT] "
                "context block, which is backend-agnostic and already works",
            ),
            Concern.RESOURCES: Ruling(
                _D.WITHHELD,
                "same as PROMPT — steering files are injected as context text, not "
                "projected into a backend's config",
            ),
            Concern.HOOKS: Ruling(
                _D.NO_CHANNEL,
                "the spec's `hooks` block is executed by the harness, and Claude "
                "Code runs hooks natively — but nothing writes them today, so a "
                "user's per-agent hooks reach kiro-cli and no other backend. Crew's "
                "OWN hooks (hooks.py, fired on ACP tool events) are unaffected and "
                "work on every backend; this gap is only the spec block",
                channel="settings.local.json `hooks`, the file Crew already creates "
                "for this backend under create-or-decline",
            ),
        }

    def session_params(
        self,
        agent: str | None,
        *,
        stub_server_names: Collection[str] = (),
        permission_surface_owned: bool = False,
        work_dir: object = None,
        **kwargs: object,
    ) -> dict[str, object]:
        """The wire face: the ``mcpServers`` array for this session.

        ``stub_server_names`` are the pooled broker stubs the caller appends
        separately, and they must be excluded here rather than translated: a stub
        wraps — and is keyed by — the same name as the agent-spec entry it rewrites,
        so translating both halves would put two elements with one ``name`` into a
        single array. Either the raw entry shadows the stub and the session bypasses
        the broker, or both register and every pooled backend runs twice (#927).

        **``permission_surface_owned`` is a precondition, not an option, and it
        defaults to withholding.** Crew's gate fires on
        ``session/request_permission``; a tool pre-approved in ``permissions.allow``
        never sends one, so Crew sees the ``tool_call`` notification only after the
        fact and cannot withhold it. The seed Crew authors carries
        ``defaultMode`` plus the spec's ``permissions.deny``, which is what puts a
        session under the gate — but the writer is create-or-decline, so a project
        that already has its own ``settings.local.json`` gets no seed and Crew
        governs nothing there. Handing THAT session the array would deliver
        ``spawn_run``, ``cron_add``, ``send_message`` and every configured server
        into a permission surface Crew does not control.

        So the array is withheld unless Crew authored the file. The cost is
        stated rather than hidden: such a session runs with no Crew MCP tools,
        which is exactly how every claude session behaved before this array
        existed, so nothing regresses — it simply does not gain tools Crew could
        not take back.

        ``work_dir`` is the session's project checkout, and it is required for
        CORRECTNESS rather than convenience: kiro-cli resolves ``--agent`` against
        ``<work_dir>/.kiro/agents`` as well as the user level, so omitting it makes a
        project-only agent read as "no spec" and drops the ``tools`` allowlist that
        spec declared. A mirror cannot discover the checkout on its own, so the
        caller passes it.

        Blocking — it reads the agent spec. The caller warms this on the spawn path
        and serves the shared ``session/new`` call site from that cache (H13).
        """
        if not permission_surface_owned:
            logger.warning(
                "session MCP: withholding the whole mcpServers array -- Crew does not own "
                "this session's settings.local.json, so a permissions.allow entry there "
                "could pre-approve a Crew tool and skip session/request_permission "
                "entirely. This session runs without Crew's MCP tools; remove or rename "
                "the project's own .claude/settings.local.json to restore them.",
            )
            return {"mcpServers": []}
        return {
            "mcpServers": session_mcp_servers(
                agent,
                stub_server_names=stub_server_names,
                work_dir=work_dir,  # type: ignore[arg-type]
            )
        }
