---
title: First-class loading of installed Agent Plugins / Kiro Powers
status: draft
author: ShreyPurohit
created: 2026-10-08
last-audited: 2026-10-11
audited-at: efb49507
doc-pr: null
implementation-prs: []
tracking-issues: [9212]
supersedes: []
superseded-by: []
---

# RFC: First-class loading of installed Agent Plugins / Kiro Powers

- Proposed capability: Kiro Crew discovers Agent Plugins / Kiro Powers already
  installed on the machine (`~/.kiro/powers/installed/<name>/`), shows each one's
  `mcp.json` servers and `skills/`, and activates them only after an explicit
  enable — plus a view that distinguishes discovered vs enabled and why anything
  failed.
- This is the **first-class install path** direction set by the close of
  [#14169](https://github.com/kirodotdev/KiroCrew/pull/14169) — a path of its
  own, explicitly **not** an extension of the `kirocrew app import` converter.
- Nothing here changes the enterprise governance ceiling or the PreToolUse gate;
  once enabled, plugin-supplied servers remain governed exactly as any other MCP
  server. Discovery alone must not activate them.

> Draft by @ShreyPurohit (author of the tracking issue
> [#9212](https://github.com/kirodotdev/KiroCrew/issues/9212)), submitted via the
> request-for-change process at the maintainer's request. Symbol citations were
> rechecked against `main` at `efb49507` after rebasing this RFC. Citations to the validator prototype in
> the now-closed [#14169](https://github.com/kirodotdev/KiroCrew/pull/14169)
> (`feat/agent-plugins-std-mcp` tip `e1b158033`) name symbols that are not on
> `main`. This document proposes a decision and asks a maintainer to settle the
> remaining open questions in the last section — it is not a description of what
> ships today.

## The decision this asks for

A Power installed on the machine should be discoverable by Kiro Crew and, after
an explicit enable, contribute its MCP servers and skills — with a surface that
shows per-Power discovered vs enabled status and the reason for any failure.
Discovery does not imply activation. The decision a maintainer owns is **that
this capability exists and takes the first-class-path shape** (not the converter
route), with disabled-by-default / explicit enablement as the proposed
activation model (see Settled product shape below), plus the remaining product
calls in the final section.

## Goals

- A Power installed under `~/.kiro/powers/installed/<name>/` is discoverable, and
  after an explicit enable its `mcp.json` servers and `skills/` become available
  in Kiro Crew.
- A dashboard surface shows, per installed Power, what it contributes, whether it
  is discovered vs enabled, and its load status, including the reason for any
  failed or refused entry.
- The path reuses Kiro Crew's existing MCP-merge and skill-root seams and is
  designed to reuse the MCP-entry URL validator prototyped in the now-closed
  #14169 once that dependency lands. Phase 1 implementation remains blocked
  until then, rather than adding a parallel subsystem.

## Non-goals

- The `kirocrew app import` converter route (withdrawn for Powers in #14169 —
  plugins get their own install path instead).
- Acquisition / importing Powers from a GitHub repo or local dir — that is
  [#2545](https://github.com/kirodotdev/KiroCrew/issues/2545).
- Browsing or installing from Claude Code plugin marketplaces — that is
  [#14490](https://github.com/kirodotdev/KiroCrew/issues/14490).
- The legacy `POWER.md` format, and a Powers catalog in the App Store (per the
  #14169 author's stated non-goals).

## What exists today, and why a Power is not loaded

An Agent Plugin / Kiro Power on disk carries `plugin.json`, `mcp.json`, and
`skills/`. Kiro IDE loads it; Kiro Crew does not, by either of its two load
paths:

- **MCP servers.** Kiro Crew pins `includeMcpJson` false in
  `_refresh_dynamic_fields` (`src/kiro_crew/agent.py`) so kiro-cli does not
  auto-ingest `~/.kiro/settings/mcp.json`. Kiro Crew instead reads that file
  itself at spec rebuild and merges it with inline agent `mcpServers`. A
  Power's `mcp.json` is on neither of those two inputs.
- **Skills.** Skills come from Kiro Crew's own on-disk skill roots. A Power's
  `skills/` directory is not one of them.

The result is duplicate configuration: the same plugin is set up once per client
and the two drift. There is also no view of which Powers are installed, what each
provides, or why one failed to load.

## What this replaces, and why that shape was wrong

A converter route already exists — `kirocrew app import`
(`src/kiro_crew/apps/plugin_import.py`) already parses the `plugin.json` /
agent-plugins.org schema and maps `mcpServers` / `skills` / `hooks`, specced in
`docs/system-specs/modules/plugin-import.md` and
`docs/system-specs/modules/harness-plugin-mapping.md`.

PR #14169 extended that converter route for Powers and was **[closed without merge by maintainer decision](https://github.com/kirodotdev/KiroCrew/pull/14169#issuecomment-5851246231)**, with the ruling that Agent Plugins get a _first-class install path of their own_ rather than being converted into apps through `kirocrew app import`. This RFC takes that ruling as settled: the converter is the thing being bypassed, and the two specs above are the mapping the first-class path must not contradict.

The now-closed #14169 (branch `feat/agent-plugins-std-mcp`, one commit, _"read
the standard mcp.json and register remote MCP urls"_) introduced a
converter-path prototype: it reads a package's root `mcp.json`, adds a
three-class MCP-entry **URL** validator in `src/kiro_crew/apps/manifest.py`
(`mcp_url_kind` → `backend` / `remote` / `invalid`, alongside
`mcp_url_violation`, `is_loopback_host`, `references_env`,
`mcp_entry_references_env`, and `mistyped_mcp_server_fields`), and registers
remote URLs instead of dropping them. **PR #14169 is closed, and these symbols
are not on `main` today.** No currently open PR carrying this validator
dependency has been identified. **Phase 1 depends on this work:** the validator
must land through a separate PR before Phase 1 is implemented, or be explicitly
included in the implementation PR with the dependency documented. Do not treat
the closed #14169 prototype as a landed dependency.

The prototype changes only the import/converter path; it adds no
`~/.kiro/powers/installed/` discovery, skills-loading change, or
installed-Powers view. The first-class path should reuse the URL validator and
its supporting helpers when that dependency lands, rather than reinvent it —
and must not treat URL classification as covering stdio `command` entries (a
separate launch/security surface; see Security). This RFC still supplies what
the prototype does not: discovery, skills, enablement, and the view.

## Proposed experience

1. **Discovery.** On startup / on demand, enumerate installed Powers from
   `~/.kiro/powers/installed/<name>/`, reading each `plugin.json` + `mcp.json` +
   `skills/`. Discovery records what is installed; it does **not** register
   servers into the active agent config or index skills for use.
2. **Explicit enablement.** An owner uses a minimal owner-authenticated dashboard
   control to enable or disable a Power. The control and its persisted consent
   state must enforce the write boundary described in Open Question 5; neither
   an agent-callable MCP tool nor an agent-invocable CLI command may grant this
   consent. Until that boundary is decided and verified, activation remains
   blocked.
3. **Inspection / diagnostics.** The expanded Installed-Powers view lists each
   discovered Power, the servers and skills it would contribute, discovered vs
   enabled state, and the reason for any validation/load failure — the
   operator-visible-surface need already tracked in
   [#14260](https://github.com/kirodotdev/KiroCrew/issues/14260) for refused MCP
   entries. This richer view is introduced in Phase 3; Phase 1 supplies only
   the minimal owner-only enable/disable control.

## The natural seam: this extends existing paths, it does not add a subsystem

The mechanical extension points for both halves already exist, and the app path
already set the precedent the #14169 ruling asked for:

- **MCP servers →** `merge_mcp_sources()` in
  `src/kiro_crew/agent_materialization/mcp_sources.py` composes an
  ordered, namespaced, `setdefault`-based chain of sources into
  `config["mcpServers"]`. The decisive precedent is `_collect_app_mcp_servers()`
  in that same module: enabled **apps already register their servers straight
  into the agent config instead of the shared `~/.kiro/settings/mcp.json`**, keyed
  `{app}:{server}` and exposed via `config["tools"]` — precisely _because_ the
  shared file is read by Kiro IDE and every other agent, so an app's private
  tools would otherwise leak. That is already a working "install path of its own,
  not the shared file." An **enabled** Powers loop reads each
  `~/.kiro/powers/installed/<slug>/` manifest and goes through the same keying /
  exposure / resolution path, under a per-Power namespace in the shape the app
  path already uses (e.g. `power-<slug>:<server>`) — the exact prefix is a
  maintainer's to set. Discovered-but-disabled Powers contribute nothing to the
  merge.
- **Skills →** the skill-root enumerators already form a pluggable, namespaced
  list: `_edition_skill_roots()` / `_canonical_skill_roots()` in
  `src/kiro_crew/dashboard/handlers/_shared_skill_roots.py` and
  `_trusted_skill_roots()` in `src/kiro_crew/skills_discovery.py`. App skills are
  already namespaced (`{app}/{skill}`) by `_register_skills` in
  `src/kiro_crew/apps/bridges_skills.py`. An **enabled** Power's skills attach as a
  `power-<slug>/` root and the catalog/index (`skill_runtime/catalog.py`,
  `skill_search_index.py`) pick them up.
- **Dashboard view →** `src/kiro_crew/dashboard/handlers/mcp_discover.py` already
  runs a `ProviderRegistry` of install providers; a `powers` provider enumerating
  `~/.kiro/powers/installed/` is the natural attach point. No dedicated React
  "Powers" view exists today — that part is net-new, most naturally mirroring the
  existing MCP-discover list.

## Scope boundaries (how the issue cluster fits together)

- **#2545 populates, #9212 consumes.** Importing/acquisition
  ([#2545](https://github.com/kirodotdev/KiroCrew/issues/2545)) brings a Power
  onto disk; this RFC loads what is on disk, installed by any means — including
  Kiro IDE or by hand. They would likely share one "installed-Powers registry."
- **#14490 is a sibling source.**
  [#14490](https://github.com/kirodotdev/KiroCrew/issues/14490) (Claude Code
  plugin marketplaces) feeds the same load/registry subsystem from a different
  origin.

## Settled product shape (stated, not asked)

These points are settled for this proposal — grounded in existing behaviour or in
the verified activation decision — rather than left open:

- **Where it lives:** an extension of the MCP-merge + skill-root seams above,
  reusing the app-server precedent — not a new subsystem. Whether Powers reuse the
  `{app}:{server}` machinery or get a dedicated per-Power scope is a sub-choice the
  seams support either way.
- **Activation:** discovered Powers are **disabled by default**; only an explicit
  enable contributes MCP servers / skills to the active agent configuration.
  Discovery must not itself activate a Power, because a Power's `mcp.json` may
  include stdio `command` entries and MCP process startup is outside PreToolUse
  (session/MCP mount by kiro-cli or the gateway — not `rebuild_agent_config`, and
  not a tool call). This matches the `mcp_discover.py` "land disabled until the
  operator enables" consent precedent. A maintainer may still reverse this by
  recording a different decision; until then this is the proposed shape.
- **Collision / precedence:** the implementation should reuse the existing
  `merge_mcp_sources()` machinery and **explicitly define and test** where an
  enabled Power's namespaced source sits in that order. Under a per-Power
  namespace, collisions with user-defined names are structurally avoided; exact
  assign-vs-fill-gap behaviour for the Power source is not treated as already
  settled by a single `setdefault` reading of today's merge.
- **IDE parity:** the codebase posture is native to Kiro Crew, not wire-parity with
  the IDE's `powers.mcpServers` merged-config shape (described in #9212). That
  shape has no reader in the tree. This is distinct from KAS's native
  `includePowers` agent-spec flag, which KAS honours when explicitly set and
  otherwise defaults to false. `kas_agents.py` deliberately forwards this flag
  only when the spec supplies a Boolean; it does not synthesize a host-specific
  default. Whether Crew-managed KAS specs must reject an explicit
  `includePowers: true` remains an open maintainer decision below. Until that
  policy is settled, the first-class Crew loader must not be described as the
  sole activation route without accounting for KAS's native route and its
  potential to bypass per-Power enablement, integrity, and consent checks or
  register duplicate MCP servers.
  `harness-parity.md` governs: a foreign plugin model adapts to Kiro Crew's
  existing extension points rather than importing another client's wire format.
  A Powers path follows suit.

## Migration plan

Each phase is independently shippable and independently abandonable; exit
criteria are stated as assertions a reviewer can test. Phase 1 depends on the
MCP-entry URL validator and remote-URL registration prototyped in the now-closed
#14169 — not on `main` today, and with no currently open PR identified —
landing through a separate PR or being explicitly included in the implementation
PR; it is marked blocked on that below.

- **Phase 1 — Discover + enable MCP servers.** A `powers` source enumerates
  `~/.kiro/powers/installed/<name>/`, reads each `mcp.json`, and classifies URL
  entries through the validator prototyped in the now-closed #14169
  (`manifest.mcp_url_kind`), once that dependency has landed (separate PR or
  included in the implementation PR). Discovered Powers stay disabled.
  Phase 1 includes a **minimal owner-authenticated dashboard enable/disable
  control** and persisted consent state. The control must enforce the owner
  authorization and write boundary described in Open Question 5. Neither an
  agent-callable MCP tool nor an agent-invocable CLI command may grant this
  consent, and direct file writes available to the agent must not be able to
  bypass the boundary. Only an **enabled** Power's surviving servers are
  registered through `merge_mcp_sources()` under a per-Power namespace.
  Stdio `command` handling is part of the security/validation design for this
  phase (URL classification alone is not sufficient).
  _Exit criteria:_ an installed Power can be discovered without appearing in the
  materialized agent config; Powers remain disabled until an owner explicitly
  enables them through the authorized dashboard control; consent state persists
  and cannot be modified through agent-controlled tools, commands, or direct file
  writes; only enabled Powers contribute MCP configuration under their namespace;
  an `invalid`-classified URL entry is dropped and does not reach the config; a
  host with no installed Powers adds no source; the phase's security design covers
  stdio `command` entries (not only URLs).
  _Blocked on:_ (1) the URL validator and remote-URL registration prototyped in
  the now-closed #14169 landing through a separate PR or being explicitly
  included in the implementation PR (not on `main` today; no currently open PR
  identified); (2) a recorded maintainer decision on Open Question 3 covering
  content integrity and renewed consent; and (3) a recorded maintainer decision
  on Open Question 5 establishing where enablement state is stored and how its
  writes are restricted, followed by verification of that boundary against the
  actual sandbox and write paths. Phase 1 must not activate Power MCP servers
  until all three blockers are resolved.
- **Phase 2 — Load skills for enabled Powers.** An enabled Power's `skills/`
  attaches as a namespaced skill root through the existing enumerators. The
  `_trusted_skill_roots()` allow-list must add only the resolved
  `<installed>/<slug>/skills` root for each individually enabled Power; it must
  never trust the parent `~/.kiro/powers/installed` tree. A discovered or disabled
  Power must not become trusted merely because it is installed. Phase 2 remains
  blocked on the Open Question 3 content-integrity/renewed-consent decision and
  the Open Question 5 enablement-state storage/write-boundary decision, as in
  Phase 1.
  _Exit criteria:_ a skill from an enabled Power is discoverable by the
  catalog/index under its `power-<slug>/` namespace; a discovered-but-disabled
  Power contributes no skill root; a skills-tree symlink targeting a disabled
  Power's `skills/` directory is rejected; the trusted root is scoped to each
  enabled Power's own `skills/` directory and never the parent installed-Powers
  tree; a same-named skill in two enabled Powers does not collide.
- **Phase 3 — Expanded Installed-Powers view.** Extend the minimal owner-only
  enable/disable control introduced in Phase 1 with a richer dashboard view
  listing each installed Power, its discovered vs enabled state, its contributed
  servers and skills, and per-Power load status with the reason for any failure.
  The Phase 3 view builds on the Phase 1 consent control; it does not defer the
  initial owner-authenticated enable/disable capability to Phase 3.
  _Exit criteria:_ a Power whose entry the validator refused shows as failed with
  the refusal reason; a discovered-but-disabled Power is visible and not active;
  an enabled Power shows its servers and skills and their load status.

## Backward compatibility

Compatible. Merely having an installed Power on disk does **not** change existing
behaviour until the operator explicitly enables it — discovery alone contributes
nothing to the agent config or skill roots. The design adds an opt-in source to
an existing merge and an opt-in root to an existing skill-root list; it
introduces no required field, removes no kind, and renames no key. A host with no
installed Powers is unaffected. Enabled Power entries are namespaced; the
implementation must define and test merge precedence for that new source so an
existing name is not overwritten unexpectedly.

## Security considerations

Today Kiro Crew has **no** installed-Powers loader, so this section describes a
**proposed** trust boundary — not a claim that `main` is already exploitable
through `~/.kiro/powers/installed/`.

KAS also has a native `includePowers` agent-spec flag that can expose Powers
tools independently of Crew's proposed installed-Powers loader. In
`kas_agents.py`, Crew forwards `includePowers` only when the incoming spec
supplies a Boolean; it intentionally does not synthesize a default, because
absent flags can have different meanings on different hosts. KAS currently
defaults an absent `includePowers` to false. Whether Crew-managed KAS sessions
must reject an explicit `includePowers: true`, and at which trusted boundary
that rejection belongs, is an open maintainer decision below, not a settled
requirement to rewrite the forwarding contract. Kiro CLI and the Claude Code
seam do not read this flag. Allowing KAS's native route alongside the Crew
loader could expose tools without Crew's per-Power enablement and
integrity/consent checks, or load the same server through both routes. The
implementation must test whichever policy maintainers select, including the
explicit-true case and the absent-flag case.

Reading a Power's `mcp.json` as an MCP source is a new configuration trust
boundary. That file may contain stdio `command` entries. Spec rebuild
(`rebuild_agent_config`) only materializes `mcpServers` into the agent config;
stdio MCP processes are launched later when the session/MCP connection mounts
them (kiro-cli, or the gateway when stubbed). PreToolUse gates tool calls, not
MCP process startup, so it cannot prevent that launch once a server is in the
active config.

Therefore discovery must not itself activate a Power or add its servers to the
active agent configuration. The proposed default is **disabled-by-default with
explicit enablement**, matching `mcp_discover.py`'s consent posture for
externally sourced installs. The initial enable/disable control belongs in
Phase 1 and must be owner-authenticated. Its persisted consent state must be
protected by a verified write boundary: neither an agent-callable MCP tool nor
an agent-invocable CLI command may grant consent, and direct file writes available
to the agent must not bypass the boundary. Until maintainers decide where the
state lives and verify the enforcement mechanism, activation remains blocked.
Once enabled, plugin-supplied servers pass through the same
`merge_mcp_sources()` path and remain subject to the enterprise governance
ceiling and the PreToolUse gate for subsequent tool calls — that is
not a claim that enablement "introduces no bypass" of process-start controls.

The URL validator prototyped in the now-closed #14169 (`mcp_url_kind` and
siblings; not on `main` today, and with no currently open PR identified) is
reused for remote/URL classification once that dependency lands (separate PR or
explicitly included in the implementation PR): a `${VAR}` reference in an entry
string, a cleartext (non-loopback `http`) URL, userinfo/fragment in a URL, and
mistyped fields classify `invalid` and are never written. That URL
classification does **not** cover stdio `command` entries; those are a separate
launch/security surface the implementation must address.

The implementation must define who/what may write `~/.kiro/powers/installed/`
and determine whether additional write protection or OS-level sealing is required
before treating that tree as an active MCP source (today it is not covered by
the same write-protect / agents-dir seals used for `~/.kiro/agents` and
`~/.kiro/settings/mcp.json`). It must also decide where enablement state is stored
and verify that the agent cannot alter that state to grant itself consent. The
open credential/consent question for a Power's _remote_ server remains below.

## Alternatives considered

- **Extend the `kirocrew app import` converter** (what #14169 did). Rejected by
  the maintainer: Powers are to get a first-class path, not be converted into
  apps. This RFC follows that ruling.
- **Wire-parity with the IDE's `powers.mcpServers` merged-config shape.** Rejected:
  that shape has no reader in the tree, and `harness-parity.md` directs a
  foreign model to adapt to Kiro Crew's existing seams.
- **KAS-native `includePowers` loading.** Not adopted as the proposed first-class
  path: KAS honours this backend-specific flag, but using it alongside the Crew
  loader could create a second route outside Crew's per-Power enablement and
  integrity/consent boundary or duplicate servers. The existing forwarding
  behavior in `kas_agents.py` deliberately preserves a spec-supplied Boolean
  rather than inventing a default. Whether Crew-managed specs should reject an
  explicit `true` is left to the maintainer decision below.
- **A new standalone Powers-loading subsystem.** Rejected as disproportionate: the
  MCP-merge and skill-root seams plus the URL validator prototyped in the
  now-closed #14169 (once that dependency lands) provide most of the machinery; a
  parallel subsystem would duplicate it.
- **Auto-load every discovered Power.** Rejected for this proposal: discovery must
  not activate MCP (stdio `command` startup is outside PreToolUse); explicit
  enablement is the proposed default (see Settled product shape).

## Open questions for a maintainer (the remaining product calls)

1. **Credential / consent model for plugin-supplied remote (URL) servers?** The primitives exist — installs can land disabled pending consent, the governance ceiling keeps remote calls at the PreToolUse gate, OAuth secrets bind from the vault at write time, and self-managed URLs are preserved (`harness-plugin-mapping.md` §5). With Power-level enablement already proposed as disabled-by-default, open: does a _Power's_ remote server need a further per-server consent step, and who owns its OAuth client?

2. **Discovery scope:** only `~/.kiro/powers/installed/`, or also the IDE clone location `~/.vscode/agent-plugins/github.com/<org>/<repo>/`? There is zero handling of either path today, so this is fully open. (Kiro Crew's onboarding importers do read sibling tools' homes for _import_, but not for live discovery.)

3. **Consent is keyed to a Power slug, not its content — bind it tighter or seal the tree?** Enablement consents to a Power slug, but the `~/.kiro/powers/installed/` tree is not sealed like `~/.kiro/agents/` and `~/.kiro/settings/mcp.json`, and stdio command startup is outside the `PreToolUse` gate. After one-time slug consent, anything that later edits the Power's content — including the agent itself through a shell — could add a stdio server or alter `plugin.json` / `skills/` content that reaches the model without fresh consent.

   This is a product and security decision for maintainers/operators to record:
   - **(a)** Bind enablement to a digest of the whole relevant installed-Power content tree, including `plugin.json`, `mcp.json`, `skills/`, and other behavior-affecting files, and require renewed consent whenever that content changes.
   - **(b)** Seal or otherwise protect the installed-Powers tree against unauthorized writes.

   Accepting subsequent changes without renewed consent is incompatible with the agent-untrusted governance boundary: an agent must not be able to grant itself new unsandboxed stdio launches by editing an already-enabled Power. Unless maintainers choose an effective tree-protection approach under (b), digest-bound consent with renewed approval under (a) is the default security requirement for any implementation PR.

   Per `AGENTS.md`, sandbox scope is the operator's to widen. This RFC raises the decision for maintainers; it does not select an option on their behalf.

4. **KAS native `includePowers` policy:** `kas_agents.py` forwards a
   spec-supplied Boolean and deliberately does not synthesize a default; KAS
   resolves an absent value to `false`. Should Crew-managed KAS sessions reject
   a spec-stated `includePowers: true` to ensure the first-class loader remains
   the only consent-checked activation path, or should the existing forwarding
   contract remain? If explicit `true` is rejected, maintainers must identify
   the trusted enforcement boundary, ensure checkout-controlled project specs
   cannot bypass it, and test both explicit `true` and an absent flag. This
   decision must be reconciled with `harness-parity.md` before implementation;
   the RFC does not silently reverse the existing forwarding decision.

5. **Where is enablement state stored, and who may write it?** The enablement
   record is itself security-sensitive: if an agent or a shell command available
   to it can change that record, it can grant itself permission to activate a
   Power and launch its stdio servers. The implementation must identify the
   record's storage location and enforce a write boundary that prevents
   agent-controlled modification, for example through sandbox-enforced read-only
   protection or writes exclusively through an owner-authenticated dashboard
   route that cannot be bypassed by direct file writes. The actual protection
   must be verified against the sandbox and write paths; merely choosing a
   location or relying on digest binding is insufficient if the agent can edit
   the consent record. Phase 1 remains blocked until maintainers record this
   decision and its enforcement mechanism.

## Consequences

- A Power installed once is discoverable in Kiro Crew; after explicit enable it
  contributes servers and skills without re-declaring them in
  `~/.kiro/settings/mcp.json` or a Kiro Crew skill root — so the duplicate-config
  drift between Kiro IDE and Kiro Crew can go away for that Power without
  auto-activating every install.
- Nothing changes for a user who has no installed Powers, or who has installed
  Powers but has not enabled any: discovery alone adds no source to the merge.
- Once enabled, plugin-supplied servers use the same merge path as other MCP
  servers; discovery itself is not treated as a process-start bypass.
- Most of the work is wiring existing seams to a new opt-in source plus the
  minimal Phase 1 consent control and the expanded Phase 3 Installed-Powers view;
  it does not introduce a parallel loading subsystem. The shared MCP-entry URL
  validator and remote-URL registration prototyped in the now-closed #14169 are
  not on `main` today. Phase 1 remains blocked until that dependency is merged
  separately or explicitly included in the implementation PR. Stdio `command`
  handling, content integrity, and the verified write-trust boundary for
  enablement state and `~/.kiro/powers/installed/` remain security concerns,
  alongside discovery, enablement, skills, and the view.
