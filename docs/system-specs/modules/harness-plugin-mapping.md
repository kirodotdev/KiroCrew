# Harness plugin mapping

How the plugin models of four other agent harnesses land on Kiro Crew's extension
points and on the [contribution protocol](contribution-protocol.md).

[harness-parity.md](harness-parity.md) states the rule for agent harnesses: an added
harness adapts itself to the seams the Kiro harness already runs through. This file
applies the same rule one layer up, to plugins: a foreign plugin model adapts itself
to the extension points and the protocol Kiro Crew already has, and the protocol does
not grow a branch per runtime. Where a foreign model needs something the protocol
lacks, that is a diff to the contract (section 6), not a local exception.

The harnesses are Codex (Rust), openclaw (TypeScript), hermes (Python) and grok-build
(Rust). pi-mono (TypeScript) is included where it decides a boundary. Codex and
openclaw were read from local clones; hermes, grok-build and pi-mono are from the
architecture cards written this week and are marked `[card]` where a claim rests only
on the card.

## 1. The four buckets

Every foreign contribution kind lands in exactly one:

- **(a) existing extension point** — an `app.json` field a Kiro Crew app already
  declares today, consumed by `kiro_crew.apps.bridges`. Named per row.
- **(b) contribution protocol** — needs sections 3-5 of the contract: read a unit's
  log, append namespaced events, publish a projection.
- **(c) config-only conversion** — a converter rewrites their manifest into `app.json`
  plus copied resources. No foreign code runs.
- **(d) not mappable** — agent-loop internals, in-process service registries, and
  client UI runtimes. Reason given per row.

Two mechanisms carry (a) and (b):

- **converter** — reads their package, writes ours. Nothing foreign executes, ever.
- **runtime adapter** — an app whose backend process hosts their plugin runtime and
  implements their service surface over our surfaces (contract section 8). Their
  plugin code is unmodified and runs in that process, never in the gateway.

A kind is only as good as its mechanism: `(a) via runtime adapter` means the target
field exists but a foreign process has to be alive to fill it.

## 2. What Kiro Crew already has

The mapping targets, from this tree (`src/kiro_crew/apps/manifest.py`, consumed at
`src/kiro_crew/apps/bridges.py`):

| Target | `app.json` field | Consumer |
|---|---|---|
| Skills | `skills` (list of paths) | `bridges.register_app`, symlinked into `config_dir()/skills` |
| MCP servers | `mcpServers` (object) | `bridges._register_mcp_servers` |
| Agents | `agents` (list of paths) | `bridges`, materialized into the kiro agents dir |
| Cron jobs | `crons` (list of objects) | `bridges.register_app_crons` |
| Command rows | `contributes.commands` | Command Bar, see [command-bar.md](command-bar.md) |
| Panel tabs / pages | `contributes.panelTabs`, `ui.pages` | dashboard, declarative |
| HTTP surface | `backend.entryPoint`, `backend.routes`, `permissions.api` | app backend + gateway proxy |
| Process hooks | `backend.hooks.{routes,on_startup,on_shutdown}` | app backend lifecycle |
| App-scoped state | `data/` under the app's install dir | `manager.app_data_dir` |
| Events between apps | `permissions.events` | `apps.event_bus` |

Two facts about this table decide several rows below.

**There is no app-declarable agent hook.** `backend.hooks` are Python callables inside
the app's own process; they are not agent lifecycle hooks. The agent hook surface is
`agent.kiro_hooks` plus autoimport from the kiro hooks dir (`kiro_crew.agent`), it
accepts five events -- `preToolUse`, `postToolUse`, `userPromptSubmit`, `agentSpawn`,
`stop` -- and each entry is `{command, matcher}` where `command` must be an absolute
path to an existing file with no shell metacharacters. It is operator config, not an
app contribution, so no app can add to it by being installed.

**App backends are eager.** `start_enabled_app_backends` runs at gateway startup
(`dashboard/server.py`), so every enabled app with a backend costs a process at boot
whether or not anything calls it. Every runtime adapter in this file pays that cost.

## 3. The matrix

### 3.1 Codex

Packaging unit: a plugin directory whose manifest is `plugin.json` at the root (when
its `$schema` names the agent-plugins.org 1.0.0 schema) or the first hit among the
vendor-prefixed `.<vendor>-plugin/plugin.json` locations. Fields are camelCase, every
declared path must start with `./`, and every resolved resource is validated to live
under the package root. In-process extensions are Rust contributor traits collected by
`ExtensionRegistryBuilder`.

| Their kind | Bucket | Target and mechanism |
|---|---|---|
| `skills` (path or list; a dir per skill holding `SKILL.md`) | (a)+(c) | `skills`. Pure converter. |
| `mcpServers` (path to an MCP config file, or inline object) | (a)+(c) | `mcpServers`. Pure converter -- for a server whose command is a bare name or an absolute path. A server whose `command`, `args` or `cwd` is package-relative is REFUSED: conversion does not preserve the package root, and the program is not a declared resource so it is not copied. Half the servers in the published corpus are that shape. |
| `interface.displayName`, `.shortDescription`, `.developerName` | (c) | `displayName`, `description`, `author`. Pure converter. |
| `interface` icons, `logo`, `screenshots`, `defaultPrompt`, `brandColor` | (c) partial | No installed-app manifest field. Carried in `extra` as provenance; nothing renders them. |
| `keywords` | (c) | `tags`. Pure converter. |
| `hooks` -- `command` handlers | (d) today | 5 of their 11 event names exist on the agent hook surface, but that surface is operator config and takes an absolute path to a metacharacter-free file, so a shell-line command needs materializing as a script the operator installs. No app can declare it. Diff D1. |
| `hooks` -- `mcp_tool`, `prompt`, `agent` handlers | (d) | Handler kinds with no Kiro Crew counterpart; `prompt` and `agent` are loop-internal. |
| `hooks` -- `PermissionRequest`, `Pre/PostCompact`, `Session*`, `Subagent*` | (d) | No app-facing event for these. Kiro Crew's own PreToolUse gate (`agent_sdk/tool_gate.py`) is core and not app-extensible. Diff D2. |
| `apps` (connectors, a single path) | (d) | A foreign connector concept with no Kiro Crew equivalent. |
| Contributor traits: thread/turn/tool lifecycle, prompt/context, config, MCP-server, skill-invocation | (d) | Agent-loop internals. They run inside their own agent process; hosting them would not put them in our loop. |
| Contributor traits: token-usage, turn-item | (b) | Observation only. An adapter that hosts them can append their output as namespaced events and publish a projection. Runtime adapter. |
| `ApprovalReviewContributor` decision chain | (d) | Their tool gate, inside their loop. |

### 3.2 openclaw

Packaging unit: an extension directory with `openclaw.plugin.json` (id, `cliCommands`,
`enabledByDefault`, `activation.{onStartup,onCommands,onCapabilities}`, `contracts`,
`uiHints`, JSON-Schema `configSchema`) plus a TypeScript entry built by
`definePluginEntry({ id, name, register(api) })`. Host capability arrives as roughly 70
deep subpath imports off the plugin SDK plus an injected `api`.

| Their kind | Bucket | Target and mechanism |
|---|---|---|
| `skills` | (a)+(c) | `skills`. Pure converter. |
| cron jobs | (a)+(c) | `crons`. Pure converter when the schedule and payload are declarative; runtime adapter when the job body is plugin code. |
| tools (`api.registerTool`) | (a) via runtime adapter | The adapter serves them as one MCP server declared in `mcpServers`. |
| HTTP routes (`api.registerHttpRoute`) | (a) via runtime adapter | The adapter's own backend, reached through the gateway app proxy under `permissions.api`. |
| gateway RPC methods (`api.registerGatewayMethod`) | (a) via runtime adapter | Same backend; their WS RPC shape becomes HTTP routes on the adapter. Not a wire-compatible mapping. |
| control-UI descriptors (tabs, widgets) | (a) shell + (b) data | `contributes.panelTabs` or `ui.pages` for the shell; the values behind a widget are a projection published under section 5 and rendered by the section 7 declarative schema. Runtime adapter for the data, declarative for the shell. |
| channels (Slack, Telegram, ...) | (d) | Kiro Crew's channel layer is core and not app-extensible. |
| model and capability providers (LLM, TTS, STT, image, embedding, web-search) | (d) | The provider registry is core; `harness-parity` H14 keeps provider capability declared on `LLMProvider`, not probed off an adapter. |
| CLI command groups (`api.registerCli`) | (d) | No app-contributed CLI surface exists. |
| services (`api.registerService`) | (d) | In-process service registry. |
| hooks (`api.on`) | (d) today | Same gap as the Codex hook rows. Diff D2. |
| `activation.{onStartup,onCommands,onCapabilities}` | (d), and a gap in ours | Kiro Crew has no lazy activation: backends are eager. Their plugin declares when it should wake; ours cannot. Diff D3. |
| declared-surface hash + `diffDeclaredSurfaceWidening` re-consent | -- | Not a contribution kind. It is the mechanism our contract's "widening a declaration is a new consent" sentence leaves unspecified. Diff D4. |
| per-plugin state quota keyed `(plugin_id, namespace, entry_key)` | -- | Our quota is per unit per day only. Diff D5. |

### 3.3 hermes

`[card]` Python 3.11. A plugin is a Python module exposing `register(ctx)`, where `ctx`
is a `PluginContext` with `register_tool`, `register_hook`, `register_cli_command`,
`register_memory_provider`. Discovery is `importlib` by file path plus pip entry
points. Unload runs through a `ReplacementCoordinator` that models ownership
generations so a slot restores its correct predecessor when plugins unload in
arbitrary order.

| Their kind | Bucket | Target and mechanism |
|---|---|---|
| `register_tool` | (a) via runtime adapter | The adapter serves the registered tools as one MCP server in `mcpServers`. Cheapest adapter of the four: same language, same process model, and `ctx` is four methods. |
| `register_hook` -- `pre_tool_call`, `post_tool_call`, `pre_llm_call`, `post_llm_call` | (d) today | No app-facing turn or tool event stream, and the gate is core. Diff D2. |
| `register_hook` -- `on_session_start`, `on_session_end` | (b) once a session unit exists | Observation only. With a `session` unit kind these become a subscribe-and-fold, no loop access. Diff D2. |
| `register_cli_command` | (d) | No app-contributed CLI surface. |
| `register_memory_provider`, model providers | (d) | Core provider registry; H14. |
| platform adapters (`plugins/platforms/<name>/adapter.py`) | (d) | Channel layer is core. |
| `plugin_data_dir()` / `plugin_db()`, install dir kept read-only | (a) already satisfied | Our installed app already gets a `data/` dir preserved across update and uninstall. |
| `ReplacementCoordinator` generations | -- | Not a contribution kind. It is the missing half of our section 6: we specify teardown on disable but not replacement of a live contributor. Diff D6. |

### 3.4 grok-build

`[card]` Rust. A plugin is a *directory* discovered from a user, project or
`--plugin-dir` scope, bundling skills, agents (personas), MCP server configs, hooks and
LSP configs; `PluginRegistry` is the single source of truth, rebuilt by a reload
command. In-process extensions are `Arc<dyn Contributor>` trait objects frozen into an
immutable registry. Trust comes from the discovery scope: user and CLI plugins are
auto-trusted, project plugins need an explicit grant in a `TrustStore` before any
executable operation runs.

| Their kind | Bucket | Target and mechanism |
|---|---|---|
| bundled skills | (a)+(c) | `skills`. Pure converter. |
| bundled agents (personas) | (a)+(c) | `agents`. Pure converter. |
| bundled MCP server configs | (a)+(c) | `mcpServers`. Pure converter. |
| bundled hooks | (d) today | Same gap as the Codex hook rows. Diff D1, D2. |
| bundled LSP configs | (d) | No LSP surface. |
| `CommandContributor` (a `/command`) | (a) | `contributes.commands` is the declarative equivalent for a command row. Pure converter when the command's action is a prompt; runtime adapter when its body is plugin code. |
| `TurnLifecycleContributor`, `SessionLifecycleContributor`, `TurnInputContributor` | (d) | Agent-loop internals. Their own rule is that a contributor never owns loop control, which is the same boundary our section 5 draws by folding outside the gateway. |
| `TrustStore` per-origin trust grading | -- | Not a contribution kind. Our `installed.json` already records an `origin`; the protocol ignores it. Diff D7. |
| queue rows with a monotonic `version`, stale edit is a no-op | -- | Convergent with our section 5 `seq` / `409 stale_seq`. No diff; evidence the rule is right. |

### 3.5 pi-mono, for one boundary

`[card]` Extensions are TypeScript files under a project dir, loaded through a jiti
fork, receiving an `ExtensionAPI`; they are fully trusted arbitrary code with the same
reach as core, and the only enforcement point is a `beforeToolCall` hook an app may
supply. Two rows matter here:

| Their kind | Bucket | Target and mechanism |
|---|---|---|
| slash commands | (a) | `contributes.commands`. |
| message renderers, keybindings | (d) | Client UI runtime. Our section 7 forbids contributor code in the browser and gives a declarative schema instead; this is the model that shows why -- a renderer is arbitrary code in the surface that displays every other unit. |

## 4. What converts and what needs a process

The split is not per harness. It is per kind, and it falls the same way in all four:

- **The resource-bundle half converts.** Skills, MCP server configs, agent or persona
  files, declarative cron entries and command rows are files with a schema. All four
  harnesses ship them inside the plugin package, and a converter can rewrite the
  package into an `app.json` plus copied resources with no foreign code anywhere. This
  is (a)+(c) and it is most of what a real plugin in these ecosystems carries.
- **The in-process-code half needs a runtime adapter.** Tools, hook callbacks, services
  and gateway methods are functions in their runtime. Hosting them means a process:
  one adapter app per foreign model, its plugin code unmodified, reaching our
  surfaces through MCP, the app proxy and sections 3-5.
- **Three kinds are not mappable at all** and should stay that way: agent-loop
  contributors, the provider registry, and client-side UI code. Each is a seam
  `harness-parity` already closed for harnesses, and opening it for plugins would
  reopen it for harnesses.

One consequence worth stating plainly: **the hook half of every foreign model is
currently unmappable**, and it is the single largest hole. Codex declares 11 lifecycle
events, hermes 6, openclaw and grok-build their own; not one of them can be carried by
an installed app, because the only agent hook surface we have is operator config with
its own command-shape rules. Diffs D1 and D2 exist to close that, and until they do,
any claim that a foreign plugin "works" through an adapter must exclude its hooks.

## 5. The proof

[plugin-import.md](plugin-import.md) specifies the converter built against this matrix:
it reads a manifest-declared plugin package (the format in section 3.1, which
grok-build's bundle also fits) and emits an installable Kiro Crew app. It covers the
(a)+(c) rows -- skills, MCP servers, identity fields -- enforces the same root
containment the source format does, and *reports* every kind it could not map with the
bucket and reason from this file, including the hook rows. It runs no foreign code.

A converter was chosen over a runtime adapter because it exercises both halves of what
this workstream had to prove -- that our existing extension points already receive
foreign contributions, and that the conversion needs no foreign runtime -- with no
process to supervise. The hermes adapter is the cheapest runtime adapter of the four
(same language, four-method `ctx`), and it is the right second proof; it is not this
one, because its value depends on Diff D2, which is not decided yet.

It was measured against the plugin directory published at `github.com/openai/plugins`
(tree `d416fd5`): all 62 packages converted, none was refused, none emitted an invalid
app manifest, 501 skills and 4 MCP servers reached an extension point, 4 further MCP
servers were refused as package-relative, 36 connector declarations and 62 presentation
blocks were reported unmapped, and there were zero warnings -- meaning no field any
published manifest declares was dropped without being named.

Three of those packages were then converted, installed and enabled in an isolated pod,
picked to cover the three outcomes end to end: one whose bare-command server registered
into the agent config as `<app>:<server>` (the [app-kit-platform.md](app-kit-platform.md)
§1 path) beside 9 skills; one whose 14 skills mapped while its package-relative server
was refused; and the one package in the corpus that declares `hooks`, reported as
declared-with-no-events.

Two platform findings came out of that run, neither a converter question:

- An app MCP server with an `http` url is not registered at all when the app has no
  live backend port (`_register_mcp_servers` skips and scrubs it, `_live_port_for`
  returns `None`). The fail-safe exists for a url pointing at the app's OWN backend,
  whose illustrative port would otherwise be a dead address that breaks every session
  -- but it also drops an EXTERNAL endpoint that has no relationship to any backend
  port. A converted package whose server is a hosted `https` endpoint therefore
  installs clean with its tools missing, and the only trace is one INFO line.
- The dashboard app card reads `mcpServers` from the MANIFEST, so it shows a server
  the agent config does not carry. The two surfaces disagree by construction.

## 6. Proposed diffs to the contract

Proposals, not applied. Each names the section it changes.

**D1 -- an app-declarable agent hook.** Add a contribution kind for the five agent hook
events, so an app can carry hooks instead of asking an operator to edit config. The
existing command-shape rule (absolute path, no shell metacharacters) stays; the app's
install dir supplies the path. Without this, every foreign hook is operator work.
Changes: a new manifest field plus a consumer in `bridges`, outside the event protocol.

**D2 -- a `session` unit kind.** Section 1 says the protocol is written for any kind and
that today there is one, `member`. Adding `session` -- turn and tool lifecycle as events
on a session's log -- turns the observation-only half of every foreign hook model into a
subscribe-and-fold, with no loop access and no gate change. Decision to make: whether
tool-call events are readable per section 2's "reading is granted per kind", given a
tool call names paths and arguments.

**D3 -- declared activation.** Backends are eager. Let a contributor declare when it
should be started (on a unit kind first appearing, on a command, on first request) so N
installed adapters do not cost N processes at boot. Changes: the manifest and
`start_enabled_app_backends`, not the protocol wire.

**D4 -- make the widening re-consent mechanism explicit.** Section 2 ends with
"widening a declaration on upgrade is a new consent, handled like any other widened
permission", which names no mechanism. Specify it: hash the `contributions` block,
store the hash with the install record, and on upgrade diff the *declared surface* and
require consent only when it widened. A narrowing or a reorder must not prompt.

**D5 -- a per-app quota, not only per-unit.** Section 4 budgets 10,000 events per unit
per day. An app that contributes to 500 units has a 5,000,000-event budget nobody
wrote down, and section 5 caps no projection at all -- neither value size nor row
count. Add a per-app aggregate event budget and a projection row and byte cap.

**D6 -- a generation for a replaced contributor.** Section 6 covers disable and
uninstall. It does not cover replacement: during an in-place upgrade two generations of
the same app can hold subscriptions and publish to the same `(kind, id, key)`, and
`stateVersion` does not order them because both may claim the same value. Add a
generation token issued at registration, refuse a publish from a retired generation,
and state that teardown closes only the retired generation's subscriptions.

**D7 -- origin-graded contribution authority.** `installed.json` already records
`origin` (builtin, registry, local, external). The protocol grants identical authority
regardless, so a directory installed from disk may publish into a unit on the same
terms as a signed registry app. Grade it: make projection publishing for a
non-registry origin require an explicit grant, and say in section 2 that a declaration
is a request bounded by origin rather than a grant by itself.

Grading needs one fix first, verified while building the converter's proof: an app
installed from a local directory is recorded with origin `registry`, because
`install_app` leaves the field at its default instead of stamping `local`. The
dashboard renders that as "Origin: registry" for a directory that came off disk, so any
grading built on `origin` today would grade a local import as a registry install. That
is a pre-existing defect, not a protocol question, and it belongs in its own change.

It also collides with an existing rule, so it is the weakest of the seven as written:
[app-kit-platform.md](app-kit-platform.md) §0 states that behaviour hangs off
`resources` and `lifecycle`, **never** off `origin`, which is why `origin` drives
display and re-install lookups rather than branching. Either the diff carries a fourth
axis for contribution authority, or it grades on something §0 already sanctions. The
underlying need stands -- a directory off disk and a signed registry app should not
publish into a unit on identical terms -- but "grade on origin" is not the shape.

Convergent, no diff needed: section 5's `seq` plus `409 stale_seq` is the same rule as
grok-build's monotonic queue `version`, and section 5's "the gateway never executes
contributor code" is the same boundary as grok-build's "a contributor never owns loop
control".
