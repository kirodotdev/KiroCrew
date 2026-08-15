# Kiro Crew behind enterprise MCP governance

Applies when the Kiro account `kiro-cli` is signed in to is an **enterprise**
account — IAM Identity Center (or an external IdP such as Okta / Entra ID
fronting it), or an API key — and an administrator has configured an **MCP
registry**. Personal accounts (Builder ID, social sign-in) are not subject to
organization-level MCP controls and need nothing on this page.

## The symptom

Kiro Crew starts, the dashboard works, chat works — and a large part of the
product is quietly absent. `spawn_run` does nothing, `cron_add` is unavailable,
`learn_add` never saves, the knowledge tools are missing, the research agent has
no tools to work with. Nothing errors. `kirocrew doctor` reports the MCP servers
healthy.

That combination — healthy locally, absent in sessions — is the signature of MCP
governance, because the two checks are measuring different things:

- Kiro Crew's own probe **spawns each server directly** and completes an MCP
  handshake with it. That succeeds regardless of governance.
- `kiro-cli` applies governance **when it assembles a session**, after reading
  the agent spec. Every server it drops there is dropped silently.

## What governance actually does

The administrator sets two things on the Kiro profile (Kiro console → Settings →
Shared settings): an MCP on/off toggle, and an **MCP Registry URL** pointing at a
registry JSON file listing the allow-listed servers.

With a registry URL configured, the client is in **registry access mode**, and
its filter is *symmetric*:

| Access mode | Entries that connect | Entries that are dropped |
|---|---|---|
| registry (a registry URL is set) | only entries carrying `"type": "registry"` that resolve to a catalog entry **of the same name** | everything else |
| non-registry (no registry URL) | ordinary entries | entries carrying `"type": "registry"` |

Two consequences worth internalising:

- The match is on the **`mcpServers` map key**, not on the command, not on a
  registry id. `kirocrew-core` in your spec must be `kirocrew-core` in the
  registry file.
- `"type": "registry"` is **not a transport**. It declares "this entry is a
  pointer into the catalog", and only `env`, `headers` and `timeout` are carried
  over from your entry as overrides. The `command` in a registry-type entry is
  not what launches.

Governance also **fails closed**: if the client cannot reach the governance API,
MCP is disabled entirely rather than falling open.

## Fixing it — two halves, both required

### 1. Declare registry mode on the Kiro Crew side

```bash
kirocrew config set agent.mcp_registry_mode true
kirocrew restart
```

Kiro Crew then stamps `"type": "registry"` on the servers it manages, so they
survive the registry filter. It is an explicit declaration rather than
auto-detection on purpose: the client fetches the toggle and the registry URL
from `GetProfile` at startup and **persists neither**, so nothing on disk
distinguishes a governed account from an ungoverned one. Leave the setting
`false` on a personal account — there the filter inverts and the marked entries
are the ones dropped.

Verify with `kirocrew doctor`, which grows an `MCP Governance (enterprise)`
section whenever the local identity came from Identity Center.

### 2. Have the administrator allow-list the servers

Kiro Crew needs three servers, and they must appear in the registry file under
**exactly** these names:

| Server | What is lost without it |
|---|---|
| `kirocrew-core` | `spawn_run`, `learn_add`, artifacts, knowledge, monitoring — the bulk of the product |
| `kirocrew-cron` | every scheduled job (`cron_add` and the whole cron surface) |
| `kirocrew-computer` | desktop automation (inert unless separately enabled, but still filtered) |

The registry file format is a subset of the MCP registry standard's server
schema. Each entry needs a `packages` entry describing how to launch the server,
and — because all three Kiro Crew servers live behind one package — a
`packageArguments` entry naming the subcommand. For a `pypi` package the client
derives `uvx <identifier> <packageArguments>`, so an entry without the argument
launches `uvx kirocrew` with no subcommand, which prints CLI help instead of
speaking MCP and fails the handshake:

```json
{
  "servers": [
    {
      "name": "kirocrew-core",
      "description": "Kiro Crew orchestration: subagents, memory, artifacts, monitoring",
      "version": "0.3.0",
      "packages": [
        {
          "registryType": "pypi",
          "identifier": "kirocrew",
          "packageArguments": [{ "type": "positional", "value": "mcp-core" }],
          "transport": { "type": "stdio" }
        }
      ]
    },
    {
      "name": "kirocrew-cron",
      "description": "Kiro Crew scheduled jobs",
      "version": "0.3.0",
      "packages": [
        {
          "registryType": "pypi",
          "identifier": "kirocrew",
          "packageArguments": [{ "type": "positional", "value": "mcp-cron" }],
          "transport": { "type": "stdio" }
        }
      ]
    },
    {
      "name": "kirocrew-computer",
      "description": "Kiro Crew desktop automation (macOS, opt-in)",
      "version": "0.3.0",
      "packages": [
        {
          "registryType": "pypi",
          "identifier": "kirocrew",
          "packageArguments": [{ "type": "positional", "value": "mcp-computer" }],
          "transport": { "type": "stdio" }
        }
      ]
    }
  ]
}
```

Set `version` to the Kiro Crew version your fleet runs.

## Known limitation: the registry launches the server, not your install

Kiro Crew's MCP servers are not standalone tools — they are the gateway's own
process, reached through subcommands (`kirocrew mcp-core`, `mcp-cron`,
`mcp-computer`), and they share the gateway's data home and version.

A registry-type entry hands the launch decision to the catalog: the client
resolves the package and, when a locally installed server's version differs from
the registry's, relaunches it at the registry's version. For a `pypi` entry that
means `uvx` fetching Kiro Crew from PyPI into its own ephemeral environment — so
the process serving your MCP tools can be a *different* Kiro Crew from the
gateway serving your dashboard. Your `env` overrides (including `KIROCREW_HOME`)
do flow through, which keeps the data home aligned, but the code does not.

Keep the registry `version` in step with your fleet's installed version. If your
organisation pins Kiro Crew centrally, that pin now governs the MCP side too.

## Version floor

MCP registry governance requires Kiro CLI **1.23** or later (Kiro IDE 0.11.28).
Enforcement in the V2 TUI arrived in **2.2.2**, and **2.6.0** made personal
`mcp.json` servers load alongside registry-managed ones. Kiro Crew's servers
live in an agent spec (`~/.kiro/agents/kirocrew.json`), not in personal
`mcp.json`, so that last change does not exempt them.

## Related

- [../architecture/mcp.md](../architecture/mcp.md) — how Kiro Crew composes the
  agent spec's `mcpServers` map and which files it owns.
- [../../src/kiro_crew/docs/troubleshooting.md](../../src/kiro_crew/docs/troubleshooting.md)
  — the user-facing "MCP tools not working" checklist.
- Kiro's own documentation: `https://kiro.dev/docs/enterprise/governance/mcp/`
  (administrator setup) and `https://kiro.dev/docs/mcp/registry/` (registry mode
  and registry-type overrides).
