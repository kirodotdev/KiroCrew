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

`kirocrew-conductor` tracks its goal in the work ledger: it mounts `kirocrew-work`, binds each item to a session before seeding it, and settles every completion claim with the acceptance evaluator instead of by reading a transcript. `kirocrew-ledger-conductor` is a deprecated alias of it — the same spec under the flow's old name, kept for one release so a session or cron that names the old string keeps resolving, and removed next release. `kirocrew-worker` is the agent a conductor names for a leaf item — the default agent's own resolved toolset plus the two reporting tools. Its `tools`, `allowedTools`, `excludedTools`, `mcpServers` and `model` are mirrored from `kirocrew.json` at every gateway start, and re-checked before each worker session starts: a worker never runs on a mirror older than your default agent, so revoking an app takes its server off the worker without a reboot. If that re-derive cannot be done the dispatch is refused rather than run on a stale spec. One thing is subtracted rather than inherited: a worker does not auto-approve cron scheduling (`cron_add`, `cron_update`, `cron_secret_request`), because a recurring job outlives the one item it was dispatched for. The tools stay mounted, so those calls go through the approval gate instead. An opt-in server you mount on the default agent is not mirrored either: an opt-in set is assigned per agent, so `kirocrew-dashboard` on your own agent does not put `session_send` on every worker you dispatch. In short, `kirocrew-worker` is `kirocrew` + `@kirocrew-work` − cron scheduling − the opt-in sets nobody assigned to it.

## Custom Agents

Custom agents are JSON or markdown files in `~/.kiro/agents/` (or a project's `.kiro/agents/`). They define their own system prompt, tools, MCP servers, and permissions. To delete a template, remove its file from `~/.kiro/agents/`. There is no in-app delete. A crew still bound to the deleted name does not break: kiro-cli cannot resolve the missing spec and falls back to the default agent spec for that session, so the crew keeps running — with the default prompt and tools instead of the deleted template's. Check a template's bindings and repoint them before removing the file so no crew silently changes behavior.

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

### Markdown agents

The same agent can be one markdown file, the form kiro-cli v3 (KAS) and Kiro IDE read: YAML frontmatter carries the fields, the body is the system prompt.

```markdown
---
name: code-reviewer
description: Reviews code changes
tools: ["read"]
---

# Code reviewer

You review code changes. Keep every answer short.
```

Rules that decide whether a `.md` file is an agent:

- It must open with `---` on the first line and close the frontmatter with a line that is exactly `---`. A markdown file without that fence (a `README.md`, notes) is not an agent and is never listed.
- The frontmatter must be a YAML mapping. Nested fields (`mcpServers`, `permissions`) work as in JSON.
- The body is the prompt. A frontmatter `prompt` field is used only when the body is empty.
- `<name>.json` and `<name>.md` side by side is the same agent twice. In Kiro Crew the JSON file wins and the markdown file is not read; the gateway log warns which file is shadowed. kiro-cli v3's own on-disk loader resolves the pair the other way round (checked against the loader bundled with kiro-cli 2.21.4: it reads files in name order and the later `.md` overwrites), so keep one file per name to get the same agent everywhere. If you kept a JSON copy as a workaround, delete one of the two.
- `tools: read, write` (a comma-separated string, which kiro-cli v3 also accepts) is read as the list `["read", "write"]`.
- The frontmatter must be a JSON-shaped document: YAML anchors and aliases (`&name` / `*name`) are refused (repeat the value instead), an unquoted date stays the text you typed, and a value with no JSON form (`!!binary`, `!!set`, a non-string key, `.inf`) makes the file unreadable as a spec, with the offending key named in the log.

What differs from a JSON agent:

| | JSON | Markdown |
|---|---|---|
| Listed in pickers, model resolution, MCP gateway stubs, Connections census | yes | yes |
| Runs on the `kas` backend | yes | yes |
| Runs on the `kiro` (kiro-cli) backend | yes | no: kiro-cli loads JSON only, so an agent that has no JSON spec in either the project's `.kiro/agents` or `~/.kiro/agents` does not become the session's active agent, and the session is refused at start with a message naming the markdown file. Switch `agent.acp_backend` to `kas` or add a JSON spec in either scope. |
| Edited by Kiro Crew (Template pane PATCH, `kirocrew agent reset-model`, bookkeeping migration, fork refresh) | yes | no: the file is read-only to Kiro Crew. Edit the frontmatter yourself. The dashboard answers `409 markdown_spec_readonly`. |

## Managing Agents

**Agent Capabilities → Agents** shows your agents; select one and open its **Template** pane to see its definition — model, system prompt, skills, tools, and MCP servers. Drop a new JSON or markdown file into `~/.kiro/agents/` and it appears automatically. `/agents` redirects to Agent Capabilities.

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
