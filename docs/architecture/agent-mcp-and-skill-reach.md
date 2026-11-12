# Agent MCP servers and skills: who gets what

Which MCP servers and skills each of the four Crew-written agents mounts, and
whether an MCP server or skill the user installs later reaches it. The four are
the Ultra Mode conductor and worker, and a crewmate's (mate's) conductor and
worker. Mechanism detail lives in [crew-mode](../system-specs/modules/crew-mode.md)
and [mcp.md](mcp.md); this page is the summary table and where each answer is
decided.

The rule behind this table is a recorded decision that future changes must
follow: [conductors stay simple, new MCP servers and skills go to workers](../decisions/2026-11-04-conductors-stay-simple-and-new-abilities-go-to-workers.md).
A change that adds capability to a conductor, or binds Ultra Mode or mates back
to custom agents, needs an explicit owner decision.

| Agent | MCP servers | New server from the MCP page? | Skills | New skill? |
|---|---|---|---|---|
| Ultra Mode conductor (`kirocrew-conductor`) | Crew's own: `kirocrew-core`, `kirocrew-dashboard`, `kirocrew-work` | No | Its fixed mapped list | No |
| Ultra Mode worker (`kirocrew-worker`) | Same as the default `kirocrew` agent | Yes | Whole catalog (or the default agent's mapped set) | Yes |
| Mate conductor (`kirocrew-mate-conductor`) | In its spec: `kirocrew-core`, `kirocrew-cron`, `kirocrew-guide`, `kirocrew-mate`, `kirocrew-work`. Per session: `kirocrew-dashboard`, `kirocrew-panel` | No | Its mapped skills; this release also the user's (deprecated) | This release: yes, with a notice. Next: no |
| Mate worker (`kirocrew-mate-worker`) | Platform servers + that mate's own additions | Only when added to that mate | Worker base + that mate's own additions | Only when added to that mate |

## Ultra Mode conductor

- **MCP servers.** Every conductor spec's `mcpServers` is rebuilt to exactly
  the Crew servers on every install pass
  (`_conductor_mcp_servers` in `src/kiro_crew/agent_materialization/conductor_agents.py`),
  so a server installed on the MCP page does not reach it. The one way to add
  a server is an approved per-template addition, kept outside the spec file in
  `src/kiro_crew/template_mcp_additions.py` and merged ADD-only by
  `_merge_template_additions` in the same installer.
- **Skills.** `_conductor_spec` maps the skills its prompt names
  (`goal-conductor`, `dashboard`, `widgets`, `living-ui`, `artifacts`) with
  `skill_resources.map_prompt_skills`. A mapped agent loads only its mapped
  skills (`agent_skill_globs` in
  `src/kiro_crew/agent_discovery_parts/skill_scope.py`), so a new skill does
  not reach it.

## Ultra Mode worker

- **MCP servers.** The worker mirrors the default agent's `tools`,
  `allowedTools`, `excludedTools`, `mcpServers` and `model`
  (`src/kiro_crew/agent_materialization/worker_agent.py`), so a server installed
  on the MCP page reaches it as it reaches `kirocrew`.
- **Skills.** It carries the default agent's `skill://` mappings
  (`_mirror_worker_skill_resources` in the same file). With none it is a
  whole-catalog template (`WHOLE_CATALOG_TEMPLATES` in
  `src/kiro_crew/agent_discovery_parts/skill_scope.py`) and sees every skill in
  the catalog, a newly installed one included.

## Mate conductor

- **MCP servers.** `_install_mate_conductor_agent` in
  `src/kiro_crew/agent_materialization/service_agents.py` keeps only the
  servers `mate_worker.conductor_mcp_servers` names (the gateway's own
  `agent._MANAGED_MCP_SERVERS`, in `src/kiro_crew/mate_worker.py`), turns
  `includeMcpJson` off and never reads the mate's own file. A user server
  reaches the mate's worker only.
- **Which servers, by name.** `agent._MANAGED_MCP_SERVERS`
  (`src/kiro_crew/agent.py`) lists `kirocrew-core`, `kirocrew-cron`,
  `kirocrew-computer`, `kirocrew-dashboard`, `kirocrew-work`,
  `kirocrew-crew-log`, `kirocrew-debug`, `kirocrew-panel`, `kirocrew-guide`,
  `kirocrew-mate` and `kirocrew-secrets`. The spec is filtered from the default
  template, which skips the `opt_in` entries (`kirocrew-dashboard`,
  `kirocrew-work`, `kirocrew-crew-log`, `kirocrew-debug`, `kirocrew-panel`,
  `kirocrew-secrets`) and `kirocrew-computer` while its `spec_gate` is closed.
  So the spec carries:
  - `kirocrew-core`, `kirocrew-cron`, `kirocrew-guide`, `kirocrew-mate`;
  - `kirocrew-work`, added by `_mount_work_ledger`.
- **Mounted per session, not in the spec.** For a member session key only,
  `src/kiro_crew/acp/client_mcp_array.py` appends two servers named in
  `src/kiro_crew/members_parts/member_sessions.py`:
  - `kirocrew-dashboard` (`MEMBER_DISPATCH_SERVER`), switched by
    `agent.member_dispatch`;
  - `kirocrew-panel` (`MEMBER_PANEL_SERVER`), switched by `agent.crew_panel`.
    It carries `dashboard_fields`, `dashboard_write`, `dashboard_save` and
    `dashboard_apply` (`src/kiro_crew/mcp_panel.py`), which run the mate's
    Dashboard tab.
- **The two conductors differ today.** The mate conductor's spec has
  `kirocrew-cron`, `kirocrew-guide` and `kirocrew-mate`, which the Ultra Mode
  conductor's lacks. The Ultra Mode conductor's spec has `kirocrew-dashboard`,
  which the mate conductor gets only per session. Ultra Mode's list is built
  by `_conductor_mcp_servers`
  (`src/kiro_crew/agent_materialization/conductor_agents.py`); the mate's by
  `_install_mate_conductor_agent`
  (`src/kiro_crew/agent_materialization/service_agents.py`).
- **Skills.** Where its spec mounts the work ledger, the same installer maps
  `goal-conductor`, `dashboard` and the skills the widget pointer in its
  `prompt.md` contract leads to (`skill_resources.WIDGET_POINTER_SKILLS`). The
  intended scope is that mapping alone, as on the Ultra Mode conductor. This
  release is a one-release deprecation:
  `skill_scope.MATE_CONDUCTOR_INHERITS_USER_SKILLS` is on, so
  `skill_scope.mate_conductor_scope` still adds the user's `~/.kiro/skills` and a
  workspace `.kiro/skills`, on kiro-cli and on KAS (`agent_skill_globs`;
  `_spec_globs` in `src/kiro_crew/acp/kas_skill_scope.py`). No session-start
  notice names these skills: a skill to keep is added in the mate's Abilities.
  The next release turns
  the flag off and removes it; `test/test_mate_conductor_agent.py` and
  `test/test_mate_conductor_parity.py` already pin that scoped behavior with the
  flag off.

## Mate worker

- **MCP servers and skills.** The base is the source-defined
  `kirocrew-mate-worker` spec (`_install_mate_worker_agent` in
  `src/kiro_crew/agent_materialization/service_agents.py`). At spawn the mate's
  own file, `~/.kiro/mates/agents/<id>.json`, is layered on top:
  `mate_worker.worker_additions` reads its `mcpServers`, `resources` and
  `skills`, and `mate_worker.merge_worker_spec` adds them to the base, then
  applies the governance ceiling (`src/kiro_crew/mate_worker.py`). A server the
  file declares is callable only when the file's `tools` select it (`@server`,
  `@server/tool`, or `*`; `mate_worker.server_selectors`); one declared with no
  selector is mounted with no tool, and the capability catalog counts it as one
  the worker lacks.
- **How a new server or skill gets there.** Only by adding it to that mate:
  in chat, when the mate asks with `request_capabilities` and the user applies
  the card (`src/kiro_crew/mcp_mate.py`), or in the mate's **Abilities**
  section. That section's **Comes with** band is the worker base and is
  read-only; only the **Added to** band, the mate's own file, can change
  (`src/kiro_crew/mate_capabilities.py`). Installing a server on the MCP page,
  or a skill in `~/.kiro/skills`, does not add it to any mate's file.
- **When the mate chose no skills.** Neither the base nor the mate's file then
  maps a `skill://` entry, and the answer depends on the harness
  (`agent_skill_globs` and `session_skill_globs` in
  `src/kiro_crew/agent_discovery_parts/skill_scope.py`):
  - on kiro-cli, the worker gets the trees kiro-cli lets an agent inherit:
    `~/.kiro/skills` and a trusted workspace `.kiro/skills`
    (`inherited_skill_globs`). Crew's other skill folders are not in that scope.
  - when neither tree exists, or on any other harness, the scope is empty and
    `kirocrew-mate-worker` is a whole-catalog template, so it sees the whole
    catalog.

## New chats and open chats

A session mounts its servers and skills when it starts, so an addition always
reaches a **new** chat. An open chat needs more:

- **MCP page writes.** On kiro-cli the running session picks up the changed
  spec through its own file watcher; on KAS Crew re-sends the agent at the next
  turn boundary; other harnesses either relaunch with the conversation kept or
  are reset (`ACP_BACKENDS_MCP_CONFIG_HOT_RELOAD`,
  `ACP_BACKENDS_MCP_WIRE_REINJECT`, `ACP_BACKENDS_MCP_RELAUNCH_RELOAD` in
  `src/kiro_crew/agent_sdk/backends_mcp.py`). A remote OAuth server also needs
  a drained warm pool ([connecting-remote-oauth-mcp-server](../guides/connecting-remote-oauth-mcp-server.md)).
- **A mate's additions.** The gateway reloads that mate's running worker with
  the conversation kept, once it is idle
  (`src/kiro_crew/dashboard/crewmate_worker_reload.py`).
- **Skills elsewhere.** An open chat's skill list was built at its start. Use
  the tab menu's **Reload session**
  (`reload_slot_session` in `src/kiro_crew/dashboard/chat_api/session_relaunch.py`), or
  start a new chat.
