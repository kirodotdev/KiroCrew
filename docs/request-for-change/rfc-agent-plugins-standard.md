---
title: Agent Plugins standard support on every harness
status: in-progress
author: zezhexu
created: 2026-11-02
last-audited: 2026-11-07
audited-at: 752c0b9c89
doc-pr: 27719
implementation-prs: [28461, 28686]
tracking-issues: [27710, 9212]
supersedes: [rfc-agent-plugins-load-installed.md]
superseded-by: []
---

# RFC: Agent Plugins standard support on every harness

- Proposed capability: Kiro Crew becomes a conformant client of the
  [Agent Plugins](https://agent-plugins.org/) 1.0.0 standard. It finds plugins
  where Kiro installs them, validates each one against the spec, and, once the
  owner turns a plugin on, gives its skills and MCP servers to every harness
  through the paths Crew already uses.
- Supersedes [rfc-agent-plugins-load-installed.md](rfc-agent-plugins-load-installed.md),
  which was written against Kiro Powers internals before Powers adopted the
  standard. Its activation and consent analysis carries over. Its open
  questions 1, 2, 3 and 5 are answered below, though the answer to 3 (consent
  bound to a digest of the plugin's content, §2) still waits for the edit-risk
  ruling. Its open question 4, the shipped `includePowers` forwarding, is not
  answered here: the 2026-11-07 narrowing below holds it for a ruling.
- Tracks [#27710](https://github.com/kirodotdev/KiroCrew/issues/27710); the
  original request is [#9212](https://github.com/kirodotdev/KiroCrew/issues/9212).
- Accepted by @CrysisDeu (maintainer) on 2026-11-02, with option B (one
  Crew-owned loader for every harness) over each harness's native plugin
  support. Phases 1 to 4 may proceed; Phase 5 stays blocked on Open question 1.
- Narrowed on 2026-11-07 by the maintainer Bolin Chen (@bolichen97), with the
  project owner Ray Xu (@buluoray) asked to co-sign:
  [the decision entry](../decisions/2026-11-07-installed-kiro-powers-are-listed-not-loaded-until-the-plugin-rulings.md). Phase 1, Phase 1b and a read-only list (Phase 1c)
  land now. Phase 2 onward waits for the rulings that entry names: Open
  question 4 of the superseded RFC (the shipped `includePowers` forwarding), the
  edit risk of §2, the lowest kiro-cli release for any spec key Crew writes, and
  Crew as a second writer of `installed.json`. Kiro Crew adds no `POWER.md`
  loader or converter; the `kirodotdev/powers` catalog is asked to republish
  its `POWER.md` Powers as Agent Plugins.

## Summary

One Crew-owned loader reads `~/.kiro/powers/installed/`, applies the spec's
validation and failure rules, and keeps every plugin off until the owner turns it
on from the dashboard. An enabled plugin's skills become a skill root and its MCP
servers become one more source in `merge_mcp_sources()`, so Claude, Codex,
opencode, goose, strands and KAS all receive the same plugin the same way. KAS's
own plugin loader stays off, so there is one loader and one switch. Stdio servers
start through a Crew launcher that applies the spec's `cwd`, `PLUGIN_ROOT` and
`PLUGIN_DATA` rules on every harness. The official conformance suite is the
acceptance test.

## Motivation

### The standard

Agent Plugins 1.0.0 (published) defines a directory with a closed `plugin.json`
manifest, Agent Skills under `skills/`, and MCP servers in a closed `mcp.json`
(`stdio`, `streamable-http`, `sse`). It fixes the component locations, requires
`PLUGIN_ROOT` and `PLUGIN_DATA` for stdio servers, defines per-component failure
boundaries, and leaves installation, permissions and UI to each client. Client
data lives under reverse-domain `extensions` keys and top-level directories.
Version 1.1.0 is a working draft with the same normative text.

### What other clients do (measured 2026-11-02)

| Client | Where plugins live | Who loads them |
|---|---|---|
| Kiro IDE | `~/.kiro/powers/installed/<name>/`, listed in `~/.kiro/powers/installed.json`; data in `~/.kiro/powers/data/<name>/` | The KAS agent. The IDE reads load state back over the ACP extension method `_kiro/powers/list`. Accepts `plugin.json` and legacy `POWER.md` |
| kiro-cli 2.37.1 | Same directory. `kiro-cli powers install <path>` copies the folder and appends to `installed.json` without validating it (a `plugin.json` with no `$schema`, and a `POWER.md`-only folder, both install) | Nothing |
| KAS 0.66.33 (bundled) | Same directory and data path | A spec-conformant loader that checks `$schema`, closed fields, path containment, URL rules and the `PLUGIN_*` placeholders. Runs only when the agent spec sets `includePowers: true`. Reads Kiro's own extension directory `dev.kiro/steering` |
| Codex | Its own cache, `~/.codex/plugins/cache/<marketplace>/<name>/<version>`, data under `plugins/data` | Native loader; its CI runs the conformance suite |
| Emdash (30+ harnesses) | No standard-plugin loader | Skills go to one shared directory; MCP servers are written into each harness's own config; its own integration is installed per harness as `file-drop`, `cli` or `none` |

The `kirodotdev/powers` catalog currently holds 3 `plugin.json` powers and 28
`POWER.md` powers.

Almost nothing published today is in the standard layout. Counted in the three
largest official marketplaces on 2026-11-02:

| Marketplace | Plugins | Standard root `plugin.json` | Manifest they ship |
|---|---|---|---|
| `anthropics/claude-plugins-official` | 40 | 0 | `.claude-plugin/plugin.json` |
| `openai/plugins` | 64 | 0 | `.codex-plugin/plugin.json` |
| `cursor/plugins` | 129 | 3 | `.cursor-plugin/plugin.json` |

Their MCP servers are mostly remote: of 141 server entries, 129 are `"type":
"http"` (the standard spells it `streamable-http`), 11 are `stdio` written
without a `type`, and 25 reference a `${...}` variable, most often
`${CLAUDE_PLUGIN_ROOT}`. A loader that reads only the standard layout would
report nearly all of them as `not-a-plugin`.

### What Crew does today (verified at `752c0b9c89` with Phases 1, 1b and 1c applied)

- The only reader of `~/.kiro/powers/` is the Phase 1c list: the owner's
  `GET /api/agent-plugins` runs the Phase 1 discovery and shows every installed
  plugin and Power as not loaded. It writes nothing, so a plugin installed for
  Kiro is listed but still reaches no Crew session on any harness.
- `_refresh_dynamic_fields` in `src/kiro_crew/agent.py` and
  `_write_heartbeat_spec` in `src/kiro_crew/agent_parts/spec_rebuild.py` pin
  `includeMcpJson` false. No spec Crew writes carries `includePowers`;
  `to_client_custom_agent` in `src/kiro_crew/acp/kas_agents.py` forwards it only
  when a spec states a bool, and KAS resolves an absent flag to false. So KAS's
  loader runs only when a spec states `includePowers: true`, which a user can
  set by hand and Crew never does.
- KAS's `customAgents` wire schema discards `cwd` from every projected MCP
  entry (`_KAS_DISCARDED_ENTRY_KEYS` in `src/kiro_crew/acp/kas_tool_limits.py`
  names it), so a spec-conformant stdio server cannot get its required default
  working directory on KAS through the spec alone.
- `kirocrew app import` (`src/kiro_crew/apps/plugin_import.py`, specced in
  `docs/system-specs/modules/plugin-import.md`) converts a plugin into an app. It
  refuses package-relative commands such as `./bin/server`, which the spec
  requires a client to run; sets neither `PLUGIN_ROOT` nor `PLUGIN_DATA`; does
  not check either `$schema`; and reads inline and `.*-plugin/` manifests that a
  1.0.0 client must not treat as the manifest.
- `~/.kiro/powers/` is not sealed by the sandbox (`src/kiro_crew/sandbox_leaves.py`
  names no `powers` leaf), so a sandboxed agent can write into an installed
  plugin.

## Goals

1. A plugin in `~/.kiro/powers/installed/` is discovered and validated per the
   1.0.0 spec, with every validation and component failure reported by reason.
2. Nothing a plugin carries reaches any session until the owner turns that
   plugin on.
3. An enabled plugin works the same on every selectable harness: same skills,
   same MCP servers, same launch environment.
4. Kiro Crew passes the official conformance suite.
5. A plugin published in a client-specific layout (Claude Code, Codex, Cursor) is
   converted into a standard Agent Plugin, following the spec's own migration
   guide, so the loader only ever reads the standard.

## Non-goals

- Changing `kirocrew app import`. It stays as it is; whether to retire it is a
  separate decision.
- A marketplace or catalog browser (#14490). Converting one plugin from a
  marketplace's layout is in scope (§6); browsing a marketplace is not.
- Reading client-specific layouts at load time. They are converted once, at
  install, into the standard; the loader stays a strict 1.0.0 client.
- Legacy `POWER.md` powers. They are listed as "not an Agent Plugin" and never
  loaded.
- Client extensions other than the one named in Phase 5. Hooks, agents and
  commands have no portable definition in 1.0.0.
- Any change to the governance ceiling or the PreToolUse gate. A plugin's tools
  are governed exactly like any other MCP server's.

## Design

### 1. Discovery and validation

A new module, `src/kiro_crew/agent_plugins/`, owns the loader. It reads
`installed.json` and the directories under `~/.kiro/powers/installed/`
(honouring `KIRO_POWERS_HOME`, as Kiro IDE does) and produces one record per
directory:

| State | Meaning |
|---|---|
| `invalid` | `plugin.json` failed a fatal rule (§5.2, §5.3, §5.5) or resolves outside the root. Nothing from it is used |
| `not-a-plugin` | No `plugin.json` (for example a `POWER.md` power) |
| `off` | Valid, not enabled |
| `on` | Valid, enabled, content unchanged since it was enabled |
| `changed` | Was enabled, content changed since; treated as off |

Each record also carries the per-component results the spec asks a client to
report: skipped skills, a disabled `mcp.json`, skipped server entries,
unsupported transports. Validation is written from the spec text, which is
authoritative over the schemas; the two published schemas are vendored as test
fixtures, never fetched. Only `https://agent-plugins.org/schemas/1.0.0/…` is
recognized; 1.1.0 is added when it is published.

Discovery has no side effects. It never writes `installed.json`, never starts a
process and never adds a skill root.

### 2. The on/off switch

The owner turns a plugin on or off from the dashboard. The record is a new
crew-home leaf, `agent-plugins.json`, holding for each enabled plugin its name
and a SHA-256 digest of every file in its root. The leaf is added to
`_CREW_READONLY_LEAVES` in `src/kiro_crew/sandbox_leaves.py`, so a sandboxed
agent can read it and cannot write it, and it is written only by an
owner-authenticated dashboard route. There is no MCP tool and no CLI command
that turns a plugin on.

The digest closes the gap left by the unsealed plugin tree: an agent can still
edit an installed plugin, but an edited plugin's digest no longer matches, so it
drops to `changed` and contributes nothing until the owner turns it on again.
The check runs at spec rebuild and again in the launcher (§4) immediately before
a server starts.

### 3. Skills and MCP servers reach every harness

- **Skills.** Each `on` plugin's resolved `skills/` directory is added to the
  skill roots, namespaced `plugin-<name>/<skill>`, and to the allow-list in
  `_trusted_skill_roots()` in `src/kiro_crew/skills_discovery.py` for that
  directory only, never for `~/.kiro/powers/installed/` as a whole. Only the
  immediate children holding `SKILL.md` count (§7.1).
- **MCP servers.** Each `on` plugin's valid server entries become one more
  source in `merge_mcp_sources()`
  in `src/kiro_crew/agent_materialization/mcp_sources.py`, keyed
  `plugin-<name>:<server>`, beside `_collect_app_mcp_servers()`. Like
  the app source, it is re-derived on every rebuild and assigned rather than
  `setdefault`ed, so a plugin turned off or `changed` disappears on the next
  rebuild. Its position in the chain is pinned by a test.
- **KAS.** Crew keeps `includePowers` off for every spec it writes, so KAS's own
  loader never runs beside Crew's and no server is registered twice.

Because both halves go through seams every harness mirror already reads, this
adds no harness-specific branch, as
[harness-parity.md](../system-specs/modules/harness-parity.md) requires.

### 4. The stdio launcher

Every plugin stdio server is projected as a call to a small Crew launcher rather
than the plugin's own command. The launcher:

1. re-checks the plugin's digest against `agent-plugins.json` and exits with a
   clear message if it no longer matches;
2. creates `~/.kiro/powers/data/<name>/` (the path Kiro IDE and KAS use, so a
   plugin keeps its data across clients);
3. expands `${PLUGIN_ROOT}` and `${PLUGIN_DATA}` in `args`, `env` and `cwd`
   exactly once, non-recursively (§9.2);
4. sets `PLUGIN_ROOT` and `PLUGIN_DATA` in the environment last (§9.1);
5. resolves `command` (bare name by `PATH`, `./` against the root) as one token,
   changes to the resolved `cwd` (the root when omitted), and replaces itself
   with the server process.

This gives every harness the spec's launch behaviour, including KAS, which drops
`cwd`.

Remote servers (`streamable-http`, `sse`) are projected as URL entries after the
spec's URL rules: absolute HTTP(S), no user info, no fragment, HTTPS unless the
host is loopback, no placeholder expansion, no duplicate header names. Their
authorization uses Crew's existing MCP sign-in; an auth failure is a connection
failure for that server only.

### 5. Dashboard

A Plugins list shows each discovered plugin with its state, what it would
contribute, and every reported problem in plain words, plus the on/off switch.
Placement and copy are settled with real screenshots in Phase 3.

### 6. Converting client-specific plugins

Decided by @CrysisDeu on 2026-11-02: a plugin that is not in the standard layout
is converted into one, following the official
[migration guide](https://github.com/agentplugins/agent-plugins-example/blob/main/skills/migrate-agent-plugin/references/migration-guide.md).
Conversion is a pure file transform in `kiro_crew.agent_plugins.convert`: it reads
the source, writes a NEW standard plugin folder, leaves the source untouched, and
runs no code from the package.

**Input.** A folder with no conformant root `plugin.json` that carries one of
`.claude-plugin/plugin.json`, `.codex-plugin/plugin.json`,
`.cursor-plugin/plugin.json`, `.plugin/plugin.json`,
`.github/plugin/plugin.json`, or a root `plugin.json` without the Agent Plugins
`$schema`. When several are present the first in that order is used, and the
choice is reported.

**Mapping**, one row per artifact the guide names:

| Source | Standard output | Rule |
|---|---|---|
| `name` | `name` | Lowercased, other characters turned into `-`; refused if still not a §5.5 name |
| `version`, `description`, `homepage`, `repository`, `license`, `keywords` | Same field | Copied when the type matches; dropped and reported otherwise |
| `author` as a string | `author.name` | Kept fields: `name`, `email`, `url` |
| Skills (manifest `skills` path or list, else `skills/`, `.claude/skills/`, `.agents/skills/`) | `skills/<name>/` | Each folder holding `SKILL.md` is copied whole as an immediate child of `skills/`, named after its frontmatter `name`; a nested or unnamed one is reported |
| MCP (manifest `mcpServers` inline or path, else `.mcp.json`, else `mcp.json`) | `mcp.json` with the 1.0.0 `$schema` | Per server, below |
| Hooks, commands, agents, rules, apps, LSP, UI, `userConfig`, `variables`, `interface`, `displayName`, logos | Not converted | No portable v1 destination; each is listed in the report |

Per MCP server:

- `type`: absent with `command` becomes `stdio`; absent with `url` becomes
  `streamable-http`; `http` becomes `streamable-http`; `stdio` and `sse` stay.
- `${CLAUDE_PLUGIN_ROOT}` becomes `${PLUGIN_ROOT}` in `args`, `env` and `cwd`. In
  `command`, `${CLAUDE_PLUGIN_ROOT}/x` and a package-relative `x/y` that names a
  file in the package become `./x` and `./x/y`. A relative `cwd` gains `./`.
- Fields outside the standard's closed set (`oauth`, `auth`, `scopes`,
  timeouts, `note`, `placement`, ...) are dropped and reported. Remote sign-in
  then goes through Crew's own MCP sign-in.
- A server that still references any other `${VAR}` is not converted: the spec
  forbids expanding it, so the literal text would reach the server as a fake
  value. It is reported as needing a value only the owner can supply.

**Record.** The output's `plugin.json` carries what was done under Crew's
extension namespace, which other clients ignore (§8.1):
`"extensions": {"dev.kiro.crew": {"convertedFrom": ".claude-plugin/plugin.json",
"notConverted": [...]}}`.

**Check.** The output is validated with the Phase 1 loader before it is written
anywhere; a conversion whose output would not validate writes nothing. Phase 4's
install writes the converted folder into `~/.kiro/powers/installed/`, so Kiro IDE
and KAS read the same standard plugin Crew does.

## Migration plan

Each phase is one PR, independently shippable, verified on a pod before push.

- **Phase 1: Loader and validation.** The `agent_plugins` module, discovery and
  the five states, with spec-derived tests and the conformance fixtures checked
  in. Nothing is loaded.
  _Exit criteria:_ every conformance fixture classifies as the suite expects; a
  `POWER.md` folder is `not-a-plugin`; a manifest with an unknown top-level field
  is reported and still valid; a manifest with no `$schema` is `invalid`;
  discovery writes nothing and starts nothing.
- **Phase 1b: Converter.** `kiro_crew.agent_plugins.convert` (§6), with no
  caller yet beyond its tests and a measurement over the three marketplaces.
  _Exit criteria:_ every plugin in the three marketplaces either converts to an
  output the Phase 1 loader accepts, or is refused with a reason; every
  not-converted artifact is listed; the source folder is byte-identical after
  conversion; no process is started.
- **Phase 1c: Read-only list.** The `GET` half of `/api/agent-plugins`
  (owner-only) and a Customize > Kiro Powers list that shows every installed
  plugin and Power as not loaded, with its reason. No switch, no write route.
  _Exit criteria:_ a `POWER.md` Power is listed as not loaded with its reason; a
  valid plugin shows what it would contribute; reading the list writes nothing;
  anyone but the owner is refused.
- **Phase 2: Switch, MCP and launcher.** Held until the rulings named in the
  [2026-11-07 decision entry](../decisions/2026-11-07-installed-kiro-powers-are-listed-not-loaded-until-the-plugin-rulings.md). `agent-plugins.json` as a read-only
  leaf, the owner-only route, the MCP source, the launcher, and `includePowers`
  pinned off.
  _Exit criteria:_ an `off` plugin adds nothing to any materialized spec; a write
  to `agent-plugins.json` from inside the sandbox fails; editing a file in an
  `on` plugin makes the next rebuild drop its servers and makes the launcher
  refuse to start; the conformance suite's stdio checks pass on KAS and on one
  other harness.
- **Phase 3: Skills and the dashboard switch.** Skill roots for `on` plugins and
  the on/off switch on the Phase 1c list, shown to the owner as real screenshots
  before the PR opens.
  _Exit criteria:_ an `on` plugin's skill is found by `skill_search` under its
  namespace; an `off` plugin's is not; a symlink into an `off` plugin's skills is
  rejected; the full conformance report passes, including the optional HTTP
  checks.
- **Phase 4: Install.** "Add from folder" and "Add from git URL" that copy into
  `~/.kiro/powers/installed/` and append to `installed.json` the way Kiro IDE
  does, so the plugin also appears in Kiro. A new plugin lands `off`.
- **Phase 5: `dev.kiro` extension.** Read `dev.kiro/steering/` the way KAS does,
  so a Kiro power behaves the same in Crew and in Kiro. Blocked on Open question 1.

## Backward compatibility

Compatible. A host with no plugins, or with plugins that are all off, gets
byte-identical specs. `kirocrew app import` is untouched. Pinning
`includePowers` off is not free: Crew forwards a spec-stated `includePowers` to
KAS as a shipped, documented feature
([agent-spec-fields](../../src/kiro_crew/docs/agent-spec-fields.md)), so a pin
written by Crew's spec writers would change every host's specs on the first
rebuild and take KAS's own loader from anyone who set it by hand. That pin is
held for the ruling the 2026-11-07 decision entry names.

## Security considerations

- **Starting a process is the risk.** A stdio entry runs a program outside the
  PreToolUse gate. That is why nothing starts until the owner turns a plugin on,
  why the switch is a read-only leaf the agent cannot write, and why the digest
  is checked both at rebuild and at launch.
- **Skills are instructions.** Only an `on`, unchanged plugin's own `skills/`
  directory is trusted, never the parent tree.
- **Headers and env are public data.** The spec forbids secrets there; Crew does
  not treat them as secrets and never expands environment variables into them.
- **The sandbox scope is not widened.** One new read-only leaf is added, which
  this RFC names. `~/.kiro/powers/` stays writable, because Kiro IDE and the
  owner write it; the digest is what makes that safe.

## Alternatives considered

- **Each harness's native plugin support** (KAS `includePowers`, Codex's own
  loader, nothing elsewhere). Least code and identical to Kiro IDE on KAS, but
  every harness behaves differently, the switch exists several times over, and
  KAS's loader has no Crew consent check in front of it.
- **A Crew-owned plugin directory** (as Codex does). Rejected: a plugin
  installed for Kiro would have to be installed twice, and the two copies drift.
- **Extending `kirocrew app import`.** Ruled out when #14169 closed: plugins get
  a first-class path, not a conversion into apps.

## Open questions

1. Should Crew read Kiro's own `dev.kiro` extension (Phase 5), or only the
   portable core? Reading it makes a Kiro power behave the same in both products.
2. Should `kirocrew app import` be retired once Phase 3 lands, or kept for
   vendor-directory packages that are not Agent Plugins?
