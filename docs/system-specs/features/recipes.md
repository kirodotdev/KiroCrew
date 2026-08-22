# Recipes seams

Kiro Crew does not ship a recipes implementation. This page documents the two
seams core provides so an **edition-supplied app** can implement one, and states
what deliberately does not exist here.

A "recipe" is an app-declared, per-user installable: a Slack channel with agent
routing, or a customizable cron job. Discovery, prompting, the install ledger,
channel lifecycle, and cleanup all live in the app. Core owns only the shared
vocabulary and the one operation an app cannot perform from its own process.

## Seam 1: the `recipes` manifest vocabulary

`AppManifest` parses and validates a `recipes` block so a recipe-declaring app
validates identically in every edition, including this one:

```json
{
  "name": "my-app",
  "agents": ["agents/triage.json"],
  "recipes": {
    "slack": [
      {
        "name": "triage-room",
        "description": "Triage inbox",
        "channelNamePart": "triage",
        "agent": "triage",
        "activation": "mention"
      }
    ],
    "crons": [
      {
        "name": "daily-digest",
        "description": "Daily digest",
        "schedule": "0 9 * * MON-FRI",
        "agent": "triage"
      }
    ]
  }
}
```

Types are `SlackRecipe`, `CronRecipe`, `RecipesConfig`, and `RecipeDependencies`
in `kiro_crew.apps.manifest`; `AppManifest._validate_recipes` enforces
kebab-case names, the `channelNamePart` length cap, the activation enum, and the
`schedule` / `every_secs` exclusivity. Nothing in core reads this block; it is
inert, validated data here.

### Recipes that bring their own agent

A recipe's `agent` is resolved per turn by
`kiro_crew.config.loader.resolve_agent_bindings`, which accepts either a Kiro
Crew alias (a `config.agents` key) or a **materialized kiro agent**. An app that
ships `agents: ["agents/triage.json"]` gets that file registered into the kiro
agents directory by `apps.bridges.register_app` on install and enable, under a
namespaced filename while the config keeps its bare `name`. Both spellings
resolve, so the recipe references either `triage` or `my-app--triage`. An
implementing app should verify resolution (`ResolvedBindings.requested_resolved`)
before it routes a channel, so it never points a channel at an agent that will
silently fall back to the default.

## Seam 2: `PUT /api/slack/channels/{channel_id}/routing`

Writes (or removes) a channel's agent / activation routing, then refreshes the
gateway's in-memory routing table.

```json
{ "agent": "triage", "activation": "mention",
  "owner": { "app": "my-app", "name": "triage-room" } }
```

Teardown uses the same endpoint:

```json
{ "remove": true }
```

Response `{"ok": true, "changed": ["agent"], "routing_refreshed": bool}`.

Why this is the only Slack seam: the app does its own Slack API calls (create,
archive, invite, set purpose) with the bot token it reads from the credential
store, so channel lifecycle needs no core endpoint. The one thing an app cannot
do from its own process is reach into the running gateway to make a routing
change take effect. Routing also lives in `config.json`, whose write lock is an
in-process `asyncio.Lock`; if the app wrote `config.json` itself it would race
the gateway's other config writers with no shared OS lock. Funneling the write
through this endpoint keeps the gateway the single writer and removes the race.

When the Slack orchestrator is not bound (gateway down, or Slack disabled) the
durable write still lands and `routing_refreshed` is `false`; the change applies
at next boot. The optional `owner` block records provenance under `_owner` so an
app can find and remove its own entries at uninstall.

## Reaching the seams from an app

Apps authenticate with their per-app secret at `POST /api/apps/{name}/token`,
receive an app-scoped token carrying a verified identity, and must declare the
paths they call in manifest `permissions.api`, which `token_auth` enforces as a
prefix allowlist. An implementing app declares:

```json
{ "permissions": { "api": ["/api/slack/channels/", "/api/apps/my-app/"],
                   "cron": true } }
```

Cron jobs come from `permissions.cron` plus `apps.cron_sdk`; the app's own REST
surface and UI come from manifest `backend.routes` and `ui.entry`; its MCP tools
come from manifest `mcpServers`. None of those need a recipes-specific seam.

## Security note: the token is not contained

The app reads the bot token from the credential store and calls Slack directly.
This is not a security boundary: an app backend is a same-UID subprocess with
full filesystem access, so it could read the token regardless. The routing
endpoint exists for correctness (single config writer, in-memory refresh), not
containment. The real control against a hostile app is **admission** (app
signing / review in `apps/admission.py`) plus, if it is ever needed, sandboxing
the app backend, neither of which these endpoints attempt.

## What core does not provide

- **No recipe installer, ledger, discovery, or cleanup.** App territory.
- **No channel create / archive / invite endpoint.** The app does these itself
  with the bot token.
- **No package-registry install.** `CapabilityManager`
  (`platform/interfaces.py`) exposes `install_agent` / `install_skill`, but the
  public default reports `available() == False`, so `/api/capability/*` answers
  503. An app must ship agents in-tree via manifest `agents`.
