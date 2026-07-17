# MCP Server Architecture

How MCP (Model Context Protocol) servers are configured, probed, loaded,
and distributed across KiroCrew, kiro-cli, and Claude Code.

> **Design invariant:** KiroCrew does NOT write to provider globals
> (`~/.kiro/settings/mcp.json`, `~/.claude.json`) under any normal code
> path. Provider globals are user-owned.  KiroCrew layers its own
> additions via per-agent files it fully owns
> (`~/.kiro/agents/kirocrew.json`, `~/.claude/agents/kirocrew.mcp.json`).
> This keeps KiroCrew-scoped tools out of every interactive kiro-cli and
> Claude Code session the user runs outside KiroCrew.

## Config File Hierarchy

| File | Owner | Purpose | Read by |
|------|-------|---------|---------|
| `~/.kiro/agents/kirocrew.json` | KiroCrew gateway (`rebuild_agent_config`) | Rendered Kiro agent: merged model + tools + MCP servers | kiro-cli when running as kirocrew agent |
| `~/.claude/agents/kirocrew.md` + `~/.claude/agents/kirocrew.mcp.json` | _(removed)_ — the CC agent renderer was deleted with the Claude Code provider; KiroCrew is `kiro-cli`-only | Was the rendered Claude Code agent + MCP registry; no longer written | (no current reader — dormant CC seam only) |
| `~/.kiro/settings/mcp.json` | User | Kiro global MCP servers | kiro-cli for ALL agents (merged into KiroCrew's agent file at render time) |
| `~/.claude.json` (`mcpServers`) | User / Claude Code | Claude Code global MCP servers | Interactive CC sessions (merged into KiroCrew's CC agent file at render time) |
| `~/.kirocrew/mcp.json` | User (dashboard MCP panel) | KiroCrew-specific additions and per-server disables | KiroCrew gateway only |

### Merge Priority (in `rebuild_agent_config()`)

Highest wins at collisions:

1. `~/.kirocrew/mcp.json` — KiroCrew-specific authority (user edits via
   dashboard, merged via `update()` so its fields override)
2. Existing `~/.kiro/agents/kirocrew.json` — loaded as the merge base, so
   any server already present with user customizations (`autoApprove`,
   hand-edits, kiro-cli direct adds) survives the rebuild
3. `~/.kiro/settings/mcp.json` — Kiro global (merged via `setdefault`
   **first**, so Kiro global wins between the two globals)
4. `~/.claude.json` `mcpServers` — Claude Code global (merged via
   `setdefault` **after** Kiro, so it only fills gaps the base/Kiro didn't
   already have; it must **not** shadow a Kiro-global entry)

> **Kiro-first (changed 2026-06):** KiroCrew is ACP/kiro-cli-only, so Kiro
> global now **outranks** the Claude Code global — the reverse of the prior
> "CC wins over Kiro" rule. The CC global is retained only as a gap-filler so
> the Claude Code (or another) provider can be re-enabled later without
> rework. Fully removing the CC scope (`mcp_discovery` `SCOPE_CC_GLOBAL`, the
> dashboard `ccGlobal` toggle, the hidden provider-switch UI) is
> **intentionally deferred** pending a provider-strategy decision; the
> interface code is left intact.
>
> **Resolution-aware fallback:** a server may be defined in several sources
> with different commands. If the merged winner's command does not resolve
> (e.g. a bare command whose binary isn't on the rebuild PATH),
> `rebuild_agent_config` falls back to the same
> server's spec from the other sources (kirocrew > kiro-global > cc-global)
> before dropping it, so one source's unresolvable command can't kill a
> server another source can resolve.

The existing-agent-config layer is what keeps user-added remote servers
(e.g. `kiro-cli mcp add --agent kirocrew --url ...`) and tweaked
autoApprove lists from being wiped on every rebuild.  Servers that were
in the previous rebuild's output but no longer in any source file also
survive — the dashboard Uninstall flow deletes them from the agent file
explicitly (see [Apply Pipeline](#apply-pipeline-post-apimcpapply)).

### What Goes Where

| Server | Belongs in | Reason |
|--------|-----------|--------|
| kirocrew-core | Managed defaults (rendered into both agent files) | Agent-scoped; gateway spawns directly, never in any global |
| kirocrew-cron | Managed defaults (rendered into both agent files) | Same as above |
| slack-mcp | Kiro global OR `~/.kirocrew/mcp.json` | Discovered on-demand when Slack is configured; merged into the agent files at render time |
| User-added servers | Any of: `~/.kiro/settings/mcp.json`, `~/.claude.json`, `~/.kirocrew/mcp.json` | Merged into KiroCrew agent files at render time |

## How MCP Servers Are Probed

Source: `src/kiro_crew/mcp_discovery.py`

### Discovery Flow

```
list_servers()
  ├── _load_agent_config()        → reads ~/.kiro/agents/kirocrew.json mcpServers
  ├── _load_mcp_json_by_source()  → reads all three scope files with provenance:
  │                                  kirocrew-own, kiro-global, cc-global
  ├── _fix_stale_managed_command() → re-resolves kirocrew binary path
  └── merge cached probe results   → overlays last-known status/tools
```

Each returned `McpServerInfo` carries a `presence` dict
(`{kirocrew, kiroGlobal, ccGlobal}`) so the dashboard can render
per-scope badges.

### Probe Mechanism

The dashboard triggers probes via `POST /api/mcp/probe`. For each server:

1. **stdio servers** — spawn the command with MCP `initialize` handshake,
   wait for `tools/list` response (timeout: configurable, default 15s)
2. **HTTP servers** — send HTTP POST to the `url` with MCP `initialize`
3. Results cached for 30 minutes (`_PROBE_TTL_SECS`)

The `GET /api/mcp` handler also kicks off a background re-probe when it
sees a server that isn't in the probe cache yet (e.g. a freshly added
server), so status transitions from "Unknown" to "ok"/"error" on the
next page refresh without waiting out the TTL.

### Binary Resolution for Managed Servers

`_fix_stale_managed_command()` re-resolves the kirocrew binary on every
`list_servers()` call because the stored path may be stale after updates:

1. Try `_resolve_kirocrew_bin()` from `agent.py` (walk up from the
   installed package to find the matching console script)
2. Fall back to `shutil.which("kirocrew")` on the augmented PATH (the
   pip-installed console script)

## How kiro-cli Uses MCP Servers

### Session Startup

When KiroCrew spawns a kiro-cli session:

1. kiro-cli reads `~/.kiro/agents/kirocrew.json`
2. Because `includeMcpJson: false` is set, kiro-cli uses ONLY
   agent-level `mcpServers` (never merges the global a second time)
3. kiro-cli spawns each MCP server as a stdio subprocess
4. Sends `initialize` + `tools/list` to discover available tools
5. Tools become available to the LLM

### Sub-agent MCP Access

Sub-agents (spawned via `spawn_run`) get MCP servers through the same
mechanism — each sub-agent is a separate kiro-cli process that reads
the same `kirocrew.json` config. The gateway does NOT re-spawn MCP
servers per sub-agent; kiro-cli handles its own MCP lifecycle.

Key implication: if a sub-agent needs kirocrew-core tools (`learn_add`,
`spawn_run`, `send_message`), those must be in the agent config that
kiro-cli reads. They are — `kirocrew.json` always contains them.

## How Claude Code Uses MCP Servers (removed)

> **Removed during de-Amazoning — KiroCrew is KiroACP / `kiro-cli`-only**
> (`agent.provider` is fixed to `acp`). The standalone Claude Code provider
> (`providers/claude_code.py`), the CC agent renderer (`install_cc_agent_config`,
> `_apply_cc_provider_defaults`) and the rendered `~/.claude/agents/kirocrew.md`
> + `kirocrew.mcp.json` files were **deleted**. Nothing renders those files;
> there is no Claude Code provider to select. This subsection is retained only as
> a record of the former design.

What the renderer used to do: when the provider was Claude Code, KiroCrew wrote a
`kirocrew.md` agent definition plus a `kirocrew.mcp.json` server registry under
`~/.claude/agents/` and passed the latter to CC via
`--mcp-config ~/.claude/agents/kirocrew.mcp.json`, so the CC session loaded
KiroCrew's scoped server set instead of the user's `~/.claude.json` global.

What remains today:

- The **dormant `ACP_BACKEND_CLAUDE` / `_is_claude` protocol seam** in
  `src/kiro_crew/acp/client.py`, kept inert so an internal companion package can
  re-register a Claude-Code-over-`claude-agent-acp` backend without forking the
  client. The public core never selects it — do not re-add the registration glue
  or a CC agent-file renderer. See
  [`system-specs/features/claude-code-provider.md`](system-specs/features/claude-code-provider.md)
  ("Claude Code Provider — removed") and the repo-root `CLAUDE.md`.
- The **CC-global gap-filler merge**: `~/.claude.json` `mcpServers` is still read
  (lowest priority — `kirocrew > kiro-global > cc-global`) in
  `rebuild_agent_config`, and the dashboard `ccGlobal` toggle / `SCOPE_CC_GLOBAL`
  scope still exist, so a future provider re-enable needs no rework (see the
  "Kiro-first (changed 2026-06)" note above). The merge layer is interface code
  left intact; it does **not** imply a selectable CC provider exists today.

## Agent Config vs Global Config

### The `includeMcpJson` Field

```json
// ~/.kiro/agents/kirocrew.json
{
  "includeMcpJson": false,
  "mcpServers": {
    "kirocrew-core": { "command": "kirocrew", "args": ["mcp-core"] },
    "kirocrew-cron": { "command": "kirocrew", "args": ["mcp-cron"] },
    "slack-mcp":     { "command": "slack-mcp", "args": [...] }
  }
}
```

- `includeMcpJson: false` → kiro-cli uses only the agent file
- Backfilled on every gateway startup by `_refresh_dynamic_fields()`

### Why `includeMcpJson: false` for KiroCrew-Managed Agents

KiroCrew already merges user-added servers from `~/.kiro/settings/mcp.json`
into the agent file at render time. The agent file is the **superset**.
If `includeMcpJson` were `true`, kiro-cli would merge the global a second
time at session start, causing:

- Duplicate entries (same server from both sources)
- Stale paths in global overriding fresh paths from the gateway
- KiroCrew-internal servers leaking into unrelated flows

| Agent type | `includeMcpJson` | Reason |
|------------|-------------------|--------|
| kirocrew (default) | `false` | Gateway merges global → agent; double-merge causes conflicts |
| KiroCrew-managed apps (Mochi, etc.) | `false` | SDK injects needed servers; global merge adds unwanted tools |
| Plain kiro-cli agents (outside KiroCrew) | `true` (kiro-cli default) | No gateway merge — global mcp.json is their only source |

**Rule:** KiroCrew forces `includeMcpJson: false` on every agent it
manages. Standalone kiro-cli agents outside KiroCrew keep kiro-cli's
default (`true`).

### Why KiroCrew Does NOT Write to Globals

Historical context:

1. An early build synced KiroCrew's managed servers into the provider
   global as a safety net. Because `includeMcpJson: false` is respected
   by recent kiro-cli versions, that sync was unnecessary.
2. The sync caused real harm: it polluted every interactive kiro-cli /
   Kiro IDE session with KiroCrew-owned tools, so it was removed
   permanently.
3. The multi-provider refactor extends the same principle to Claude
   Code: KiroCrew **never** writes to `~/.claude.json` either; the CC
   agent file at `~/.claude/agents/kirocrew.mcp.json` is authoritative
   for KiroCrew's CC sessions.

**If kirocrew-core/kirocrew-cron ever appear in either global, it is
legacy pollution from pre-fix builds.** Users can clean it up through the
dashboard MCP panel (Kiro / Claude badge → off → Apply) or via
`kirocrew cli-setup` which invokes the narrowly-scoped
`clean_stale_managed_mcp` migration helper.

## Dashboard MCP Management

The Integrations (MCP) page aggregates servers across all three scope
files and presents a unified view:

### Scope Badges

Each row shows per-scope presence badges (green = enabled, gray = not
enabled):

| Badge | Means | Source of truth |
|-------|-------|-----------------|
| KiroCrew | Server will load in KiroCrew sessions | Effective state after merge, minus explicit `disabled:true` overrides in `~/.kirocrew/mcp.json` |
| Kiro | Server is present in `~/.kiro/settings/mcp.json` | Raw file contents |
| Claude | Server is present in `~/.claude.json` `mcpServers` | Raw file contents |

Clicking a badge **stages** an intent. The page accumulates all staged
changes into a pending set and exposes Apply / Discard at the top. Only
when the user clicks Apply does KiroCrew execute the imperative edits.

### Apply Pipeline (`POST /api/mcp/apply`)

The endpoint takes a batched payload of changes (scope add/remove,
uninstall, per-tool overrides) and applies them atomically:

1. **Uninstalls** first — removes from `~/.kirocrew/mcp.json`,
   `~/.kiro/settings/mcp.json`, and `~/.claude.json`, and also strips
   the entry directly from `~/.kiro/agents/kirocrew.json` and
   `~/.claude/agents/kirocrew.mcp.json` so the additive merge base
   for the subsequent rebuild no longer contains the server
2. **Scope adds** — write the server spec into the target scope file
3. **Scope removes** — strip the server from the target scope file.
   If the server will no longer be inherited into KiroCrew but the user
   kept the KiroCrew badge ON, the full spec is first copied to
   `~/.kirocrew/mcp.json` to preserve inheritance (the **preservation
   rule**)
4. **Per-tool overrides** — update `disabledTools` on the server entry
   in `~/.kirocrew/mcp.json`
5. **Single rebuild** at the end re-renders both agent files from the
   new source-of-truth state

No scope metadata is ever persisted. Apply does one-shot edits and
forgets; state is always re-read from disk on the next page load.
External edits (e.g. `kiro-cli mcp remove <name>`, hand-edits to
`~/.claude.json`) are picked up naturally on the next render.

### What Apply Does NOT Do

- **Does not restart sessions** — scope changes take effect on the next
  session spawn. The separate "Apply & Restart" button in the header
  calls `POST /api/sessions/restart` to drain the warm pool when needed
- **Does not install servers for you** — install a new server by adding
  it to a scope file (`~/.kirocrew/mcp.json` or one of the provider
  globals), then use Discover & Sync. The MCP panel manages what's
  already installed

## AppStore (SDK) MCP Distribution

Implemented via the `managedToolPolicy` field on an app's agent spec.

### How Other Agents Get KiroCrew MCP Servers

Apps built on KiroCrew (Mochi, custom agents) declare dependencies:

```json
// app's agent spec
{
  "tools": ["@kirocrew-core", "@kirocrew-cron", "fs_read", "grep"],
  "managedToolPolicy": {
    "exclude": ["cron_add", "cron_remove"]
  }
}
```

The SDK's `installAgentConfig()`:

1. Reads `kirocrew.json` to get kirocrew-core/kirocrew-cron specs
2. Copies server specs into the app's own agent config file
3. Applies `managedToolPolicy.exclude` as `disabledTools` on injected specs
4. kiro-cli reads the app's agent config and spawns MCP servers

### Enforcement Layers

| Layer | Mechanism | Availability |
|-------|-----------|-------------|
| 1. SDK install | Writes `disabledTools` into agent config | Always (no network) |
| 2. kiro-cli | Reads `disabledTools`, filters before LLM | Always (no network) |
| 3. MCP server | `GET /api/session-tool-policy` filters `tools/list` + `tools/call` | Network-dependent |

Layer 3 is defense-in-depth for non-kiro-cli clients (CC, custom MCP
hosts) that may not read `disabledTools`.

## Startup Sequence

On gateway startup, `rebuild_agent_config()`:

1. Load existing `~/.kiro/agents/kirocrew.json` as base
2. `_refresh_dynamic_fields()` — managed defaults, resolved binary path
3. Merge `~/.kiro/settings/mcp.json` (setdefault — Kiro global, wins
   between the two globals)
4. Merge `~/.claude.json` `mcpServers` (setdefault — CC global, fills gaps
   only; lower priority than Kiro)
5. Merge `~/.kirocrew/mcp.json` (`update`, wins over globals)
6. Re-resolve any per-server skill-directory paths from the local skill
   locations (project `skills/`, `~/.kirocrew/skills`) so they never go
   stale across rebuilds
7. Resolve commands to absolute paths, with a resolution-aware fallback:
   if the winning source's command doesn't resolve, try the same server's
   command from the other sources before dropping it
8. Write `~/.kiro/agents/kirocrew.json`
9. Render `~/.claude/agents/kirocrew.md` + `kirocrew.mcp.json`
   (always, regardless of active provider)

Uninstalls happen out-of-band through `POST /api/mcp/apply` which
explicitly deletes the server from the rendered agent files before
calling `rebuild_agent_config` so the additive merge base no longer
contains the entry.

## Troubleshooting

### "MCP tools not working"

1. Check `~/.kiro/agents/kirocrew.json` has `kirocrew-core`/`kirocrew-cron`
2. Verify `includeMcpJson: false` is set
3. Run `kirocrew doctor` — checks MCP probe status
4. Dashboard → MCP panel shows live probe results
5. For CC sessions, also check `~/.claude/agents/kirocrew.mcp.json`

### "Status column shows Unknown forever"

The handler auto-triggers a probe when it sees a new server in any
config file, but the results only appear on the next refresh. Wait a
few seconds and reload. If it stays "Unknown", the server is failing
to handshake — check the dashboard error text or gateway logs.

### "Tools available in KiroCrew but not in interactive kiro-cli / CC"

This is **correct behavior**. `kirocrew-core`/`kirocrew-cron` are
agent-scoped. They should NOT appear in interactive kiro-cli or
Claude Code sessions. If they do, something wrote them to a provider
global — file a bug.

### "Removed a server from Kiro global but it came back"

Check if the KiroCrew badge is still on. When the user keeps the
server enabled for KiroCrew, the preservation rule copies its config
into `~/.kirocrew/mcp.json` before removing it from the global so the
server stays loaded in KiroCrew sessions.

### "Newly added MCP server but sessions don't pick it up"

Session reset drains the warm pool (pre-spawned processes with old
config). Use Dashboard → Apply & Restart, or `kirocrew config set`
which auto-triggers a restart.
