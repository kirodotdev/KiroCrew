---
title: Agent Config Mirror — one declared contract for projecting the agent spec onto every backend
status: draft
revision: v1
author: zejiangg, with Kiro
created: 2026-09-02
last-audited: 2026-09-02
audited-at: f51e65947
doc-pr:
implementation-prs: []
tracking-issues: []
supersedes: []
superseded-by: []
---
# RFC: Agent Config Mirror — one declared contract for projecting the agent spec onto every backend

- Status: draft — nothing proposed here has shipped. The three mirrors it
  describes all exist on main today; what does not exist is the contract that
  names them, the per-field disposition vocabulary, or any declared extension
  point. The migration is additive and consolidating: no mirror is rewritten in
  the first wave, each is re-expressed as an implementation of one interface.
- Author: zejiangg, with Kiro
- Created: 2026-09-02
- Audited against: `f51e65947`
- Related: `../system-specs/features/agent-host-contract.md` (the host contract
  this RFC adds a bucket to),
  `../system-specs/features/claude-code-provider.md`,
  `../system-specs/modules/acp-client.md`,
  `../system-specs/modules/harness-parity.md`,
  `../system-specs/modules/providers.md`,
  `rfc-crew-agent-sdk-boundary.md` (§4.2 and §11.5 scope host-contract
  seam-building out of that RFC; this document is the promotion of one of those
  deferred seams, not a competing abstraction)

## 1. Problem

One agent spec is the single source of truth for every backend Kiro Crew drives.
No backend reads it the same way, and **nothing declares that projecting it is a
thing a backend author has to do**. The result is three independent
implementations of one idea, none of them named as such, and the same defect
discovered twice.

The defect, in both discoveries, is identical: a session comes up holding
`tools: ["@kirocrew-core", ...]` and no definition of what `kirocrew-core` is, so
every Crew tool is silently absent. The harness works. The tools are gone. There
is no error.

- **KAS** hit it first. `acp/kas_agents.py` records the diagnosis in its own
  module docstring: omitting the `mcpServers` block left "refs naming nothing,
  and every Crew tool silently absent."
- **Claude Code** hit it again, months later, and it is being fixed a second time
  in a second module (`acp/session_mcp.py`) by a second investigation that did
  not know the first had happened.

A fourth backend hits it a fourth time. That is the cost this RFC removes.

## 2. What exists on main today

Three mirrors, three shapes, three locations, one of which is documented only in
`providers.md` and two of which are documented nowhere as mirrors.

| Mirror | Where | Channel | What it projects |
|---|---|---|---|
| kiro-cli | `providers/acp.py:61` `_write_cli_overlay`, `:136` `_write_tool_search_overlay` | a file the harness reads: `<work_dir>/.kiro/settings/cli.json` | model, effort, tool-search settings. The spec proper needs no mirror — kiro-cli is handed `--agent` and reads `~/.kiro/agents/<name>.json` itself |
| KAS | `acp/kas_agents.py` (561 lines) + `acp/kas_permissions.py` (231 lines) | per-session wire params: `_meta.kiro.customAgents` on `session/new` | `prompt` (inlined from `file://`, since KAS will not read a URI), `tools` (always explicit — absent means *no* tools in KAS, so an ambiguous spec fails closed rather than guessing `*`), `mcpServers` minus broker stubs, `permissions` derived from `allowedTools` through KAS's own capability vocabulary |
| Claude Code | `acp/session_mcp.py` + `acp/client.py` `_write_claude_local_settings` | both: per-session wire array (`mcpServers` on `session/new`) **and** a file (`<work_dir>/.claude/settings.local.json`) | MCP servers, `permissions.deny` from `disabledTools`, `availableModels`, `model` |

Two observations that decide the design:

1. **The channel is not a property of the backend's vendor, it is a property of
   the transport.** Two of the three use a file; two of the three use wire
   params; Claude Code uses both. A contract keyed on "is this Claude" cannot
   express that, which is why `ACP_BACKENDS_SESSION_MCP_ARRAY` is already a
   membership set rather than an identity test (harness-parity H6).
2. **KAS's mirror has already invented most of the vocabulary the other two
   lack.** Its `UNSUPPORTED_SPEC_KEYS` comment states the distinction this RFC
   builds on: *"No slot on the wire is NOT no such capability in KAS —
   conflating the two is what kept `hooks` written off as unsupported."* That
   sentence is the contract, discovered locally and not yet generalised.

## 3. Proposal

### 3.1 One declared interface, on `LLMProvider`

The mirror is declared on `LLMProvider` with a safe default, not on `AcpClient`
and not probed with `hasattr`.

`AcpClient` serves exactly two backends — its own `_is_kiro` docstring says so —
and KAS runs on `AcpRuntime` instead, so anything declared on `AcpClient` can
never be "every provider". Harness-parity H14 already prescribes the shape: a
capability read off the provider is declared on the provider with a default,
because a `hasattr` probe makes the absence of a method indistinguishable from a
provider that has not been updated.

The default is the honest one: **no mirror, and a declaration that says so.** A
backend that reads the spec itself (kiro-cli, via `--agent`) is not a gap.

### 3.2 Two faces, because the channel genuinely differs

- **Wire face** — params contributed to `session/new` / `session/load`. Already
  half-real: `agent-host-contract.md` §5.3 asks for exactly this ("a declared
  per-session `mcp_servers` extension point so injecting servers on the wire is a
  contract rather than a `getattr` override").
- **File face** — a native config file the harness loads by itself.

A backend may implement either, both, or neither. Claude Code is the existence
proof that "both" is normal, not exotic.

### 3.3 Per-field disposition, as a closed vocabulary

The interface's real product is not code, it is a **per-concern declaration**
that a reviewer can read and a test can assert. Every spec concern resolves to
exactly one of four dispositions:

| Disposition | Meaning | Example on main |
|---|---|---|
| `delivered` | reaches the backend in the spec's own shape | `mcpServers` to kiro-cli via `--agent` |
| `translated` | reaches it under another name or vocabulary | `allowedTools` → KAS `permissions` (`kas_permissions.py`) |
| `no-channel` | the backend HAS the capability; this transport cannot carry it | `hooks` to a wire-injected KAS agent |
| `withheld` | deliberately not sent, with a stated reason | `model` to KAS — set by its own protocol verb so it has exactly one owner; `autoApprove` to Claude Code — every MCP call must reach the host gate |

`no-channel` and `withheld` are the two the current code cannot tell apart, and
conflating them is precisely the documented cause of the `hooks` regression. A
`no-channel` row is a **backlog item with a known destination**. A `withheld` row
is a **decision**. Making the distinction declarable is most of the value here.

### 3.4 Invariants the interface inherits

1. **Create-or-decline for any file Crew does not own.** Crew creates the file or
   leaves the path entirely alone; it never reads, merges into, rewrites or
   deletes someone else's. This is not a new rule — it is the rule that ended a
   three-round review span on the Claude settings seam, and a generic mirror
   specified with merge semantics re-opens every finding it closed (symlink
   following on a path a checked-out repo controls, deleting a user-created file,
   blocking filesystem work on the teardown path).
2. **One owner per concern.** If a concern has a protocol verb, the verb owns it
   and the mirror withholds it. KAS already does this for `model`.
3. **Fail closed on an ambiguous spec.** KAS's `tools` handling is the precedent:
   absent means no access, so emit the list explicitly rather than guessing `*`.
4. **No slot on the wire is not no capability in the backend.** Every dropped
   field carries its disposition and, for `no-channel`, the channel that would
   carry it.
5. **Durable policy and disposable session config are separate.** Security
   policy that the backend applies globally belongs in a persistent write; a
   permission mode scoped to one session belongs in a per-session artifact that
   teardown removes. Collapsing the two into one per-session file is what forced
   the snapshot/restore machinery that generated the findings in §3.4.1.

### 3.5 One folder, one file per provider

Co-location is a first-class requirement, not tidiness. A contract whose
implementations are scattered across `providers/acp.py`, `acp/kas_agents.py` and
`acp/session_mcp.py` is the state that produced this RFC: nothing about any one
of those files tells a reader the other two are the same kind of thing.

```
src/kiro_crew/providers/mirrors/
  README.md          # what a mirror is, how to add one, the disposition vocabulary
  base.py            # AgentConfigMirror ABC + Disposition enum + the concern list
  registry.py        # backend id -> mirror, and the "declared no mirror" entries
  kiro_cli.py        # file face: <work_dir>/.kiro/settings/cli.json overlay
  kas.py             # wire face: _meta.kiro.customAgents (+ kas_permissions)
  claude_code.py     # both faces: session/new array + settings.local.json
```

`providers/mirrors/` rather than `acp/mirrors/`, because the interface is declared
on `LLMProvider` (`providers/base.py:61`) and an implementation belongs beside the
thing it implements. It is also the scope that survives a non-ACP backend: `acp/`
would be the wrong name the first time a provider arrives that does not speak ACP.

**This relocates into the agent SDK when that lands.** The eventual home is
`agent_sdk/mirrors/`, per `rfc-crew-agent-sdk-boundary.md`. That package does not
exist on main today (audited: no `agent_sdk` package, no import ratchet), so
blocking this RFC on it would mean shipping nothing. `providers/mirrors/` is
therefore the interim home, and the move is a package rename with no redesign —
one folder moving as a unit is exactly the cheap case, which is the second reason
to co-locate now rather than later.

### 3.6 A folder is discoverability; it is not the reminder

Co-location makes a mirror easy to find and easy to copy. It does not make anyone
write one. Three mechanisms do that, and all three are needed:

1. **A declared method with a default that must be answered.** The default is
   "no mirror", and a backend taking it has to say so in the registry rather than
   inherit it silently. Absence is then a statement, not an oversight.
2. **The parity test (§4.3 H4).** Every backend × every concern must resolve to
   one of the four dispositions with a reason. A new backend that leaves a
   concern unaddressed fails a test rather than shipping a session with missing
   tools — the exact failure mode discovered twice in §1.
3. **The new-provider checklist row** in `agent-host-contract.md`, which is what a
   human reads before writing any of this code.

The folder is where the answer goes; the test is what asks the question.

### 3.7 Where the supporting code lives

- Capability sets: `src/kiro_crew/acp_backends.py` only. Harness-parity R5/H8
  exempts no other path, and every member must already be in
  `ACP_BACKENDS_KNOWN`.
- Consumption: `backend in ACP_BACKENDS_<CAP>`. Never `not self._is_claude`,
  never `!= ACP_BACKEND_X`, never a name comparison (H5, H1).
- The declared method: `LLMProvider` (`providers/base.py`), with a default (H14).
- Anything needing disk stays off the Kiro construction path and behind a
  synchronous accessor over a warmed cache (H13) — the existing
  `_session_mcp_servers` cache is the pattern to copy.

## 4. Hooks delivery plan

Hooks are the worked example, because they are the one concern where all four
dispositions appear at once and where the current state is a real gap rather
than a documentation defect.

### 4.1 First, two different things called "hooks"

These are routinely conflated and the plan is wrong if they are:

- **Crew's own hooks** (`src/kiro_crew/hooks.py`, the `config.json` `hooks`
  section) fire on Crew's side of the wire, on ACP tool events, via
  `fire_tool_hooks` / `get_global_hook_store` in `acp/client.py:5881`. They are
  already backend-agnostic and already work everywhere. **This RFC does not
  touch them, and no gap here is about them.**
- **The spec's `hooks` block** (written by `agent.py:_kiro_hooks_only` +
  `_apply_user_kiro_hooks`) is executed by the *harness*. This is the one that
  does not arrive.

So the honest statement of the gap is narrow: *a user's per-agent hooks are
executed by kiro-cli and by nobody else.* Crew's own gate, audit and deny rules
are unaffected on every backend.

### 4.2 Current disposition per backend

| Backend | Today | Backend's own support |
|---|---|---|
| kiro-cli | `delivered` — spec `hooks` read from `~/.kiro/agents/<name>.json` via `--agent` | native |
| KAS | `no-channel` — `hooks` is in `kas_agents.UNSUPPORTED_SPEC_KEYS`; a wire-injected agent cannot carry it | **native**, and loads them from an agent profile on disk, and the module notes it "even accepts Crew's object form" |
| Claude Code | `no-channel` — never written | **native**, via `hooks` in its settings file |

Both gaps are `no-channel`, and in both cases the destination already exists.
Neither is a missing feature in the backend.

### 4.3 The plan

**Phase H1 — declare, do not deliver.** Land the disposition table with both
gaps marked `no-channel` and the destination named. This is the whole point of
the vocabulary: the gap becomes a visible row with an address instead of a
silent omission. No behaviour change.

**Phase H2 — Claude Code, via the file Crew already creates.** Claude Code reads
`hooks` from its settings file, and `_write_claude_local_settings` already
creates `<work_dir>/.claude/settings.local.json` under create-or-decline. Adding
a projected `hooks` block is a new key in a file Crew already owns, so it
inherits the ownership invariant for free and needs no new write path. Two
constraints: the projection must translate Crew's object form into the shape
Claude expects rather than passing it through, and a hook that fails to
translate is dropped with a warning naming the hook — never silently, and never
by shipping a shape the harness may reject at session start. `withheld` is the
correct disposition for a hook whose action Crew cannot express safely on that
harness.

**Phase H3 — KAS, via a Crew-owned agent profile on disk.** KAS's channel is a
disk profile, not the wire, so this needs a write target. It must be a
**Crew-owned** directory, not a user path — the same reasoning that makes an
isolated config root the right answer for the Claude `~/.claude` gap. Under
create-or-decline a Crew-owned directory is unambiguous: Crew authored it, so
Crew may rewrite and remove it, and none of the §3.4.1 findings apply. This
phase is where the file face of the interface earns its place; H2 alone could
have been done without an interface at all.

**Phase H4 — parity test.** One test asserts that every backend's declared
disposition for every concern is either implemented or explicitly `no-channel` /
`withheld` with a reason. That is the ratchet: a new backend cannot land with a
concern silently unaddressed, which is the failure mode that produced this RFC.

### 4.4 Sequencing note

H2 is small and independently useful. H3 is the one that needs the interface to
exist first. H1 is worth landing on its own even if H2 and H3 are deferred,
because a declared gap and an undeclared gap cost very different amounts to
rediscover.

## 5. Migration

| PR | Content | Risk |
|---|---|---|
| 1 | The folder and the declaration: `providers/mirrors/` with `base.py`, `registry.py` and `README.md`, the interface on `LLMProvider` with a safe default, the four-value disposition vocabulary, capability set(s) in `acp_backends.py`, and the disposition table for all three existing mirrors filled in from the code as it is. No mirror code moves yet | none — no behaviour change |
| 2 | **Move** Claude Code's mirror into `mirrors/claude_code.py` and re-express it as an implementation. Newest of the three and the only one with a live open PR, so it converts with the least archaeology | low |
| 3 | **Move** KAS's mirror into `mirrors/kas.py` (keeping `kas_permissions.py` as its translation helper). Largest of the three; expected to need no logic change, only relocation plus a declared table | low, but the biggest diff |
| 4 | **Move** the kiro-cli overlay into `mirrors/kiro_cli.py`, and correct `providers.md` | low |
| 5 | Hooks H2 (Claude Code settings `hooks`) | behavioural |
| 6 | Hooks H3 (KAS disk profile) + H4 parity test | behavioural |

PRs 2–4 are deliberately one-backend-each and pure relocation. A move that also
changes logic is the shape that makes a mechanical refactor unreviewable, and
each of these three files is load-bearing for a shipped backend.

PR 4 also fixes documentation that is wrong today independent of this RFC:
`providers.md` still states that Kiro Crew "drives a single LLM backend:
`kiro-cli` over ACP" and that the Claude seam is dormant and never selected,
which contradicts both `agent-host-contract.md` and `harness-parity.md` on
current main. That correction should not wait for this RFC to be accepted.

After PR 6 the folder is the complete inventory: every backend Crew drives has
exactly one file there, or a registry entry stating it needs no mirror and why.
That property is what makes the next backend cheap, and it is the deliverable —
not the interface itself.

## 6. Open questions, with dispositions

1. **Does the interface own the wire face at all, or only files?**
   Disposition: **both**, because Claude Code already needs both and splitting
   them would put two contracts where the concern is one. Reopens if a third
   face appears (an env-var channel is the plausible one).
2. **Should the model allowlist be `withheld` everywhere for consistency with
   KAS's `model`?**
   Disposition: **no** — the allowlist and the pin are different concerns. The
   allowlist changes what a versioned id resolves to, which the protocol verb
   cannot express. Reopens if a backend gains an allowlist verb.
3. **Does closing the Claude `~/.claude` inheritance gap belong here?**
   Disposition: **no, but the destination is this interface's file face.** An
   isolated config root has to carry credentials across or the harness cannot
   authenticate, which is a separate change with its own risk. This RFC re-scopes
   the existing "Known gap" statement to point at the mirror rather than
   deleting it.
4. **Is a provider selector being re-added?**
   Disposition: **no.** The standing rule in `claude-code-provider.md` holds
   unchanged. A mirror is a projection of one spec onto one backend; it does not
   make the backend user-selectable and does not multiply `agent.provider`.

5. **Should this wait for the agent SDK package so the folder lands in its final
   home?**
   Disposition: **no.** `agent_sdk/` does not exist on main and that RFC is still
   `draft`, so waiting means the three mirrors stay scattered indefinitely while
   a fourth backend arrives. `providers/mirrors/` moves as one unit later, which
   is the cheapest possible migration and is itself an argument for gathering the
   files now. Reopens only if the SDK package lands before PR 1 of this stack.

## 7. Documentation changes this requires

- `../system-specs/features/agent-host-contract.md` — a new "must declare" bucket
  for config mirroring, a row in the seam-status table, and a question in the
  new-provider checklist. Its §1 "definition target" declaration becomes a
  *write* target and must name format and timing.
- `../system-specs/modules/providers.md` — correct the single-backend and
  dormant-seam claims, and describe `_write_cli_overlay` /
  `_write_tool_search_overlay` as the kiro-cli mirror.
- `../system-specs/features/claude-code-provider.md` — re-scope the "Known gap"
  section to name the mirror's file face as the destination.
- `../system-specs/modules/platform-context.md` — the `providers` field
  description ("Kiro-CLI-ACP only") is already stale and becomes wrong once a
  mirror is registered per provider.
- `../system-specs/modules/harness-parity.md` — record the new capability set
  under the pre-launch `CONTRACT_VERSION` 1 audit list.
