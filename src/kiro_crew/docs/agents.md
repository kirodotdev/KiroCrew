# Agents & Configuration

Kiro Crew uses agent JSON configurations for LLM interaction. A configuration can define a model, system prompt, tools, permissions, resources, and MCP servers. The default agent is `kirocrew`; custom agents can be added alongside it.

## Default Agent

The generated default configuration is `~/.kiro/agents/kirocrew.json`. Its shipped defaults include:

- Model: `auto`, which leaves model selection to the configured provider.
- Built-in tools: shell, file, code-search, web, introspection, session, reporting, and tool-search tools.
- `allowedTools` grants for selected safe tools and Kiro Crew MCP operations.
- MCP servers: `kirocrew-cron` and `kirocrew-core`; `kirocrew-computer` is emitted only when computer use is enabled and supported on the current platform.
- A `postToolUse` audit hook for shell calls.

## Switching Agents

### Globally (All Sessions)

```
!agent code-reviewer     # switch to a custom agent
!agent off               # back to default kirocrew
```

### Per-Thread (Slack)

```
!ta set code-reviewer    # this thread uses code-reviewer
!ta off                  # remove thread override
!ta status               # show current thread agent
```

### Per-Tab (Dashboard)

Use the agent selector dropdown in the chat topbar or welcome screen.

### Per-Cron Job

Cron jobs can specify an agent at creation time.

## Built-in Agent Specs

Kiro Crew owns specs named `kirocrew`, `kirocrew-lite`, `kirocrew-conductor`, `kirocrew-pipeline-conductor`, `kirocrew-ledger-conductor`, `kirocrew-worker`, `kirocrew-knowledge`, `kirocrew-research`, and `kirocrew-heartbeat`. The primary and lite specs are required; the others support goal conducting, pipeline fleet supervision, knowledge extraction, research, and heartbeat features.

`kirocrew-conductor` tracks its goal in the work ledger: it mounts `kirocrew-work`, binds each item to a session before seeding it, and settles every completion claim with the acceptance evaluator instead of by reading a transcript. `kirocrew-ledger-conductor` is a deprecated alias of it — the same spec under the flow's old name, kept for one release so a session or cron that names the old string keeps resolving, and removed next release. `kirocrew-worker` is the agent a conductor names for a leaf item — the default toolset plus the two reporting tools.

## Custom Agents

Custom agents are JSON files in `~/.kiro/agents/`. They define their own system prompt, tools, MCP servers, and permissions. To delete a template, remove its JSON file from `~/.kiro/agents/`. There is no in-app delete. A crew still bound to the deleted name does not break: kiro-cli cannot resolve the missing spec and falls back to the default agent spec for that session, so the crew keeps running — with the default prompt and tools instead of the deleted template's. Check a template's bindings and repoint them before removing the file so no crew silently changes behavior.

```json
{
  "name": "code-reviewer",
  "description": "Reviews code changes",
  "model": "claude-opus",
  "prompt": "file:///path/to/prompt.md",
  "tools": ["fs_read", "grep", "glob", "@kirocrew-core"],
  "allowedTools": ["fs_read", "grep", "glob"]
}
```

## Managing Agents

**Agent Capabilities → Agents** shows your agents; select one and open its **Template** pane to see its definition — model, system prompt, skills, tools, and MCP servers. Drop a new JSON file into `~/.kiro/agents/` and it appears automatically. `/agents` redirects to Agent Capabilities.

## Mapping Skills to an Agent

Each agent template can be given its own set of [skills](skills.md). Open **Agent Capabilities → Agents**, select an agent, open its **Template** pane, and use the **Skills** section to add or remove them. Every edit saves immediately.

Under the hood a mapped skill is a `skill://` entry in the agent's `resources`, so kiro-cli loads it natively when the agent starts:

```json
{
  "name": "code-reviewer",
  "resources": [
    "file://.kiro/steering/**/*.md",
    "skill://~/.kiro/skills/prepare-pr/SKILL.md"
  ]
}
```

Resolution rules:

| Agent | Mapping | Skills it sees |
|-------|---------|----------------|
| `kirocrew` | none | the whole catalog (default) |
| `kirocrew` | mapped | only the mapped skills |
| custom | none | none — the agent brings its own |
| custom | mapped | only the mapped skills |

`file://` resources (steering globs) are never touched by the editor, and hand-authored `skill://` entries the editor cannot express — wildcards like `skill://~/.kiro/skills/*/SKILL.md`, or paths outside the known skill roots — are listed read-only and preserved across edits.

## Agent Config Files

| File | Purpose |
|------|---------|
| `src/kiro_crew/config/defaults.json` | Shipped base configuration. A development project can override it with `agents/defaults.json`. |
| `src/kiro_crew/config/prompt.md` | Shipped system prompt. A development project can override it with `agents/prompt.md`. |
| `~/.kiro/crew/agent.json` | Optional user overrides merged on top of defaults. |
| `~/.kiro/crew/prompt.md` | Optional user prompt override, which takes priority over the shipped prompt. |
| `~/.kiro/agents/kirocrew.json` | Installed generated agent configuration. |

## Reinstalling Agent Config

```bash
kirocrew setup --agent-only
```

This regenerates `kirocrew.json` from the current defaults and user overrides.

## Architecture Note

Each agent session runs through the configured ACP backend and has its own system prompt, tools, and MCP servers.
