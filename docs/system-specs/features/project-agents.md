# Project Agents

**Status:** Draft
**Author:** madbajaj
**Date:** 2026-06-21

---

## 1. Problem

kiro-cli supports per-project agent configs in `.kiro/agents/`. A developer can write a `dev.json` agent tailored to their codebase — with the right steering files, MCP tools, and system prompt — and kiro-cli picks it up automatically when the project directory is the working directory.

KiroCrew's dashboard only shows global agents (`~/.kiro/agents/`). Users with project-specific agents must manually copy configs globally or context-switch manually. This is friction.

---

## 2. Goals

1. Make project agents discoverable through the dashboard.
2. Show project agents alongside global agents in every agent selection surface.
3. Switching to a project agent automatically sets the session's working directory.
4. Users can understand which agent belongs to which project.

## 3. Non-Goals

- Cross-machine registry sync — project paths are machine-local absolute paths.
- Per-project-agent config customization from the dashboard (workspace/memory store overrides).
- Creating or editing project agent files from the dashboard — users manage files directly.
- Persistent agent channels (deferred, DQ-B5b).

---

## 4. Overview

Project agents are discovered via two mechanisms:

1. **User-initiated scan** — user clicks "Scan Projects" and provides a root directory. KiroCrew walks the tree looking for `.kiro/agents/*.json` files and registers found projects.
2. **Project switch** — when a user sets a session's project path (chat, CLI `--cwd`, or folder `project_dir`), KiroCrew immediately reads that project's `.kiro/agents/` and registers it.

Discovered project agents are stored in a persistent registry (`~/.kirocrew/project_agents.json`) and shown in the agent picker alongside global agents. The picker always shows the complete arsenal — current project's agents are selectable, other projects' agents are visible but grayed out.

**Why a persistent registry?** The picker must show your complete agent arsenal at all times, regardless of which session is currently active. Lazy discovery (reading only the active session's project dir) would mean agents from other projects disappear when you switch sessions — an inconsistent picker that breaks the "complete arsenal" mental model. The registry is the display cache that makes all registered project agents always visible.

**Why other-project agents are shown but grayed?** Hiding them entirely causes "where are my agents?" confusion — users with 10 registered projects need to know those agents exist. Graying communicates "available, but needs a context switch to use" without cluttering the active portion of the picker.

---

## 5. User Flows

### Flow 1: First-time setup

1. User has `/projects/myrepo/.kiro/agents/dev.json`
2. User opens the KiroCrew dashboard
3. User clicks "Scan Projects" on the /agents or /capabilities page
4. User enters `~/projects` (or browses to it)
5. KiroCrew finds `dev.json`, registers it
6. `dev` appears in the agent dropdown

### Flow 2: Natural discovery on project switch

1. User opens a chat session
2. User sets project path to `/projects/myrepo`
3. KiroCrew immediately discovers agents in that project
4. `dev` appears in the agent dropdown for that session

### Flow 3: Switching to an agent from a different project

1. User has `dev` in ProjectA (current) and `dev` in ProjectB (different)
2. Dropdown shows both: ProjectA's `dev` is selectable; ProjectB's `dev` is grayed out
3. User clicks ProjectB's `dev` → modal: "This agent belongs to ProjectB. Switch your project to use it?"
4. User clicks **[Switch to ProjectB & use]** → session cwd changes to ProjectB, agent switches to ProjectB's `dev`
5. Alternatively, user clicks **[Cancel]** → nothing changes

### Flow 4: Cron job with a project agent

1. User creates a cron job, selects `dev (ProjectA)` from the picker
2. Cron job stores binding: `agent=dev, project=/projects/ProjectA`
3. At run time, session starts with cwd = ProjectA and agent = ProjectA's `dev`

### Flow 5: Agent config changes on disk

1. User edits `/projects/myrepo/.kiro/agents/dev.json` — adds a new MCP tool
2. Next time a session starts with `dev`, kiro-cli reads the file and picks up the change
3. No manual sync needed

---

## 6. Agent Sources

| Source | Location | Who creates | `source` value |
|--------|----------|-------------|----------------|
| `kirocrew` | `~/.kiro/agents/kirocrew*.json` | KiroCrew installer | `"kirocrew"` |
| `aim` | `~/.kiro/agents/<Pkg>-<agent>.json` | AIM installer | `"aim"` |
| `builtin` | `~/.kiro/agents/<agent>.json` | User | `"builtin"` |
| **`project`** | `<project>/.kiro/agents/<agent>.json` | User / project repo | `"project"` |

Global sources live in `~/.kiro/agents/` and are synced into `config.json`. Project agents are discovered dynamically and stored only in the registry — never in `config.json`.

---

## 7. Data Model

### Registry (`~/.kirocrew/project_agents.json`)

```json
{
  "/abs/path/ProjectA": {
    "name": "ProjectA",
    "state": "ok",
    "agents": [
      {"file": "dev.json", "agent_name": "dev"},
      {"file": "architect.json", "agent_name": "architect"}
    ]
  },
  "/abs/path/OldProject": {
    "name": "OldProject",
    "state": "not_found",
    "agents": [
      {"file": "dev.json", "agent_name": "dev"}
    ]
  }
}
```

- `name` — basename of the project path, used for display
- `state` — `"ok"` (path exists) or `"not_found"` (path missing on disk). Entries are never silently deleted — `not_found` keeps the agent visible in the picker so the user knows it exists and can fix the path.
- `agents` — list of `{file, agent_name}` entries. `agent_name` is the `name` field inside the JSON (not the filename), cached here so the picker renders without any filesystem reads.
- Registry is the display cache — no filesystem reads needed at picker render time
- `state` and `agent_name` refresh at: gateway restart, manual rescan, project switch

**Why store `agent_name` in the registry?** Without caching the name, every picker render would require reading every registered agent file (N projects × M agents per project file reads per dropdown open). The registry acts as the cache, trading staleness risk (user edits the `name` field without rescanning) for zero I/O at display time.

**Why never delete entries?** Silent deletion means agents disappear without explanation. A `not_found` state keeps the agent visible with a warning badge, giving the user the information they need to act (rescan, fix path, or ignore).

### Agent Identity

An agent is uniquely identified by `(name, project_path)`:
- Global agent: `(name, "")` — empty string sentinel for "no project context"
- Project agent: `(name, "/absolute/project/path")`

The empty string sentinel (rather than null) is used consistently across all binding storage (cron jobs, channels, config) for simple equality checks without null handling.

### Bindings (cron, channel)

Every surface that stores an agent binding persists `(agent_name, project_path)`:
- `CronJob.project_path: str = ""` — empty = global, set = project agent from that path
- `ChannelConfig.project_path: str = ""` — same semantics

**Why not store project agents in `config.json`?** Config uses agent `name` as key — two `dev` agents from different projects would collide. Config is machine-agnostic by design; project paths are machine-local absolute paths and would be dead entries on any other machine. The registry exists precisely to handle project agents' ephemeral, machine-local lifecycle.

---

## 8. Discovery

### Scan

`POST /api/agents/rescan` accepts a root path. KiroCrew walks the directory tree looking for `.kiro/agents/*.json` files:

- Writes each found project to the registry incrementally (not buffered — agents appear in the picker as they're found, and a cancelled/truncated scan still preserves all discovered agents)
- Prunes: hidden dirs (except `.kiro`), `node_modules`, `build`, `vendor`, `.cargo`, `Library`, `Pods`, and other common large trees
- Depth limit: 8 levels. Entry cap: 50,000 entries
- Does not follow symlinks — logs `WARNING` with the real path and a hint to scan it directly
- `~/.kiro` is never a project — the guard runs before any registration

### Auto-register on project switch

When a user sets a session's project path (via dashboard, CLI `--cwd`, Slack `!project`, or folder `project_dir`):

1. `auto_register_project(project_path)` runs synchronously before the response returns
2. Reads `{project}/.kiro/agents/*.json` directly (no deep walk)
3. Registers found agents in the registry
4. If the dir has no `.kiro/agents/`, skips silently — the session switch still succeeds
5. If registration fails (e.g. disk error), logs `WARNING` — the session switch still succeeds

**Why synchronous?** If auto-register ran as a background task, a user could set a project and immediately click a project agent — the agent wouldn't be in the registry yet and the switch would fail. Running synchronously guarantees the registry is populated before the response returns, eliminating the timing race.

**Why does session-set succeed even if registration fails?** The session's working directory is set correctly regardless. A registry failure degrades the UX (project agents don't appear in picker) but doesn't break the session. Failing the project-set on a registry error would be a worse trade-off.

---

## 9. Agent Picker UX

### Sort order

1. Current session's project agents (selectable, sorted alphabetically by name)
2. Global agents (selectable, sorted alphabetically by name)
3. Other registered project agents (grayed out, sorted by folder name then agent name)

### Grayed agents

Other-project agents and `not_found` agents are visible but non-selectable:
- **Other-project** (`state: ok`, different project): click → modal "Switch to [ProjectB] to use this agent?" with **[Switch & Use]** and **[Cancel]**
- **Not found** (`state: not_found`): click → "Project path not found. Rescan to restore." Badge shows `project (FolderName) ⚠`

### Filter

Text filter matches both agent name and project folder name. Matching text is highlighted in results.

### Empty state

When no project agents are registered, a subtle CTA appears: "Scan projects to discover more →" (links to /agents page).

---

## 10. Security Invariants

### `~/.kiro` is a protected path

`~/.kiro` is never treated as a project directory. Guards are enforced at:
- `scan_directory()` — prunes `~/.kiro` from the walk
- `auto_register_project()` — rejects paths inside `~/.kiro`
- `_load_project_agents()` — ignores registry keys inside `~/.kiro`
- `POST /api/agents/rescan` — rejects scan roots inside `~/.kiro`

### Agent binding validation

Every surface that stores `(agent_name, project_path)` must enforce:
- `project_path = ""` → global agent only
- `project_path` set → project agent from that path only
- Global agent + non-empty `project_path` is invalid — rejected at both API and UI layers

### Registry write integrity

All writes to `project_agents.json` must be atomic and use the JSON library (no string manipulation). Concurrent writes from within the same gateway process are safe.

On `JSONDecodeError` (corrupt registry): log `WARNING`, return `{}`, surface UI notification prompting rescan.

---

## 11. APIs

| Endpoint | Purpose |
|----------|---------|
| `POST /api/agents/rescan` | Scan a directory tree; write found agents to registry incrementally |
| `GET /api/agents` | Merged agent list for dashboard UI (includes project agents) |
| `GET /api/agents/installed` | All agents for /agents and /capabilities pages |
| `GET /api/agents/detail/{name}?project_path=` | Agent config file; project registry checked first when `project_path` given; hard-404 if not found |
| `POST /api/agents/sync` | Sync AIM/global agents into config — must pass `include_project=False` |
| `POST /api/chat/slots/{slot}/agent` | Switch agent; no `project_path` = global intent (registry not consulted) |
| `POST /api/chat/slots/{slot}/project` | Set project path; triggers `auto_register_project` synchronously |

### `list_agents()` callers requiring `include_project=False`

- `_do_agents_sync` — syncs global agents into `config.json` only
- `generate_conductor_skill` — generates static conductor skill; project agents are session-contextual

---

## 12. Backwards Compatibility

- Existing `list_agents()` callers now receive project agents by default (`include_project=True`). Callers that need global-only must explicitly pass `include_project=False` (see §11).
- Existing `CronJob` entries default `project_path=""` on load — global agent, no behavior change.
- Existing `ChannelConfig` entries default `project_path=""` on load — global agent, no behavior change.
- `config.json` is never modified to add project agents — existing config is untouched.

---

## 13. Surfaces

### Agent selection surfaces (all support `(agent_name, project_path)` binding)

| Surface | Route | Component |
|---------|-------|-----------|
| Chat dropdown | `/chat` | `AgentDropdownList` |
| Cron new/edit | `/schedule` | `AgentSelector` via `JobForm` |
| Cron overview | Overview → Cron tab | `AgentSelector` via `CronTab` |
| Channel settings | `/channels` | `AgentSelector` via `ChannelPage` |
| Projects page | `/projects` | `AgentSelector` via `ProjectsPage` |
| Schedule page | `/schedule` | `AgentSelector` via `SchedulePage` |
| Settings default agent | Settings → Overview | `AgentSelector` via `KiroCrewCfgTab` — project agents hidden; note shown; API rejects |

### Discovery surfaces

| Location | Route | Component |
|----------|-------|-----------|
| Platform → Agents | `/agents` | `KiroCrewAgentsPage` |
| Capabilities → Agents | `/capabilities?tab=agents` | `AgentsPage` |

### Display surfaces

| Surface | Route | Shows project agents? |
|---------|-------|-----------------------|
| Platform → Agents | `/agents` | ✅ With state badge (⚠ for not_found) |
| Capabilities → Agents | `/capabilities?tab=agents` | ✅ With state badge (⚠ for not_found) |
| Settings → Overview | `/settings` | ❌ Default agent config only (always global) |

---

## 14. Folder Integration

`ChatFolder` already has `default_agent?: string` and `project_dir?: string`. These interact with project agents as follows:

- When a session is created in a folder, `slot.project` is set from `folder.project_dir`, which triggers `auto_register_project`
- `folder.default_agent` is resolved against `project_dir` first (project agent wins on name collision), then falls back to global
- Setting `folder.project_dir` alone does not trigger registration — only session creation does

---

## 15. Error Handling Summary

| Scenario | Behavior |
|----------|----------|
| `POST /api/agents/rescan` finds 0 agents | 200 with `{"discovered": 0, "agents": [...]}` |
| `auto_register_project` fails | Log WARNING; session-set succeeds; agents won't appear until next rescan |
| Registry corrupted (JSONDecodeError) | Log WARNING; return `{}`; UI notification prompts rescan |
| Agent file missing at launch | Silent fallback to default agent (matches global agent behavior); WARNING logged |
| `project_path` not a directory | 400 |
| Agent not found at `project_path` | 404 with full slot rollback |
| Global agent + non-empty `project_path` in binding | 400 |

---

## 16. Spec Update Checklist

When this lands, update:
- `docs/system-specs/modules/learn-cron-dashboard.md` — `CronJob.project_path` schema addition
- `docs/system-specs/modules/config.md` — `ChannelConfig.project_path` + `ChatFolder` resolution behavior
- `docs/system-specs/modules/slack-gateway.md` — `!project` command triggers auto-register

---

## 17. Related

- **Mesh-975** — core task
- **Mesh-2200** — Slack parity follow-up
- **CR-282242202** — implementation CR (covers chat dropdown + all AgentSelector surfaces)
