# Agent-config mirrors

One agent spec (`~/.kiro/agents/<name>.json`) is the single source of truth for
every backend Kiro Crew drives. No two backends read it the same way. A **mirror**
projects that spec onto one backend's native configuration.

Design and rationale: `docs/request-for-change/rfc-agent-config-mirror.md`.

## Why this folder exists

The same defect shipped twice. A session came up holding
`tools: ["@kirocrew-core", ...]` with nothing defining `kirocrew-core` — refs
naming nothing, every Crew tool silently absent, the harness otherwise working and
no error anywhere. KAS hit it first and fixed it in `acp/kas_agents.py`;
claude-agent-acp hit the identical thing later and it was diagnosed again, from
scratch, by someone who did not know the first had happened. Then a fourth backend
(codex) arrived and got a copy-paste twin of claude's override hook.

Nothing in any of those files said "this is a projection of the agent spec, and
every backend needs one." That sentence is what this folder is.

## Runtime guard

A mirror is the fix for one backend. `agent_sdk/mcp_refs.py` is the detector, so a
fourth occurrence cannot be silent. At the one point where the
`session/new` / `session/load` `mcpServers` array is final — spec projection plus
the gateway's broker stubs — `acp/mcp_ref_guard.py` compares the spec's `@server`
refs against what the session is actually about to receive, and logs ONE structured
warning naming the backend, the agent, the unresolved refs and whether the shared
gateway is on. It also records them on the session's MCP report
(`unresolved_refs`), beside the buckets saying what a configured server reported —
a different claim, because a server nothing configured has no row there to be
missing from.

Its runtime reach is `AcpClient`'s composition — kiro-cli, claude, codex. **KAS
composes its array on `AcpRuntime` and never reaches that call site**, so a KAS
session's refs are checked only by `kirocrew doctor`; wiring the second transport is
a separate change, and claiming "every backend" here would be the same unexamined
claim this folder exists to stop.

The resolver sits in the SDK rather than in the ACP layer because the question is
not an ACP question: spec in, wire array in, backend id in, refs out. That is what
lets `kirocrew doctor` evaluate the same function per selectable backend, before a
session exists, without taking an ACP edge (`agent_spec_mcp_refs` in
`agent_sdk/drivers/acp.py` supplies it the spec and each backend's projection).

It never changes the array and never fails the session: a ref naming nothing is a
configuration fact, and the complaint about this defect class was that it was
invisible, not that it was tolerated. Two rules keep it from crying wolf — kiro-cli
reads the spec itself via `--agent`, so its refs resolve against the spec's own
`mcpServers` rather than the (deliberately empty) wire array; and `@builtin` and
bare tool names are not server refs. Until codex has a mirror, the warning fires
for every codex session that references a server, which is the guard being right.

## What a mirror must do

Implement `AgentConfigMirror` (`base.py`) in a file named after the backend, and
register it in `registry.py`.

- **`rulings()` is mandatory.** For every `Concern`, state one of four
  dispositions with a reason. It is abstract so a new backend cannot inherit
  silence.
- **`session_params()`** — the wire face, for params merged into `session/new` /
  `session/load`. Must be a pure in-memory read at the call site: that site is
  shared with kiro-cli, and adapter work must not add a scheduling or failure
  point to kiro-cli's construction path (harness-parity H13). Warm a cache on the
  spawn path.
- **`session_projection()`** — the structured face the CLIENT actually calls:
  the wire params plus any obligation the same spec parse hands the client
  (`SessionProjection.denied_tools`, the `(server, tool)` pairs the client must
  refuse when the backend asks permission for them). The default returns the
  wire params with nothing off-wire, so a mirror that has no such obligation
  implements only `session_params()`. A mirror that does (codex) overrides this
  and defines `session_params()` as its `.params`, so the two faces cannot drift.
  Also the seam the gateway's pooled stubs come through (`stub_elements`): the
  client's shared append is inert for every mirrored backend, so a mirror that
  does not place them ships a backend the gateway cannot pool onto.
- **`write_files()`** — the file face, for native config the harness loads itself.
  **Create-or-decline**: create the file, or leave the path entirely alone. Never
  read, merge into, rewrite or delete a file Crew did not author.

Implement either face, both, or neither. Claude Code uses both.

## The four dispositions

| Disposition | Means | Is it a gap? |
|---|---|---|
| `delivered` | reaches the backend in the spec's own shape | no |
| `translated` | reaches it under another name or vocabulary | no |
| `no-channel` | the backend HAS the capability, this transport cannot carry it | **yes** — a backlog item, and `channel` must name where it would go |
| `withheld` | deliberately not sent, with a reason | no — a decision |

`no-channel` and `withheld` are the two that matter. Conflating them is the
documented cause of the `hooks` regression: see `UNSUPPORTED_SPEC_KEYS` in
`acp/kas_agents.py`, whose comment states the rule this vocabulary generalises —
*no slot on the wire is not no such capability in the backend*.

## The four projection kinds

A disposition answers "what happens to this concern"; a **kind** answers the prior
question, "how does anything reach this backend at all?". Every backend id this
build can spell has one `McpProjection` in `registry.py`, and the kind is what a
test can read.

| Kind | Means | Required fields |
|---|---|---|
| `native` | the backend reads `~/.kiro/agents/<name>.json` itself, so there is nothing to project | `reason` |
| `mirror` | a mirror in this folder projects it; must have a class in `MIRRORS` | `reason` |
| `external` | Crew projects it, from a module outside this folder | `reason`, `projection`, `tracking` |
| `no-channel` | no transport this backend advertises can carry Crew's servers | `reason`, `channel`, `tracking` |

`no-channel` is the only kind under which a session legitimately holds none of
Crew's tools, and it is the one the prose form could not distinguish from a
backlog item. A paragraph can explain a gap without ever giving it an address, and
a gap with no address is indistinguishable from a decision — so the two kinds that
are not finished states are required to be ADDRESSABLE, by the constructor rather
than by a reviewer. `channel` names what would have to exist; `projection` names
the module a reader goes to; `tracking` is an issue URL or a repo-relative
`path#anchor` the parity test resolves.

A `native` or `mirror` declaration may carry neither, and its `reason` may not read
as a schedule: the parity test rejects "pending", "not yet moved", "unwritten" and
their relatives on those two kinds.

Every kind is additionally cross-checked against something outside its own text,
so no kind's honesty rests on how its reason is worded. `mirror` needs a
registered class and an `mcpServers` ruling of `delivered` or `translated`;
`external` needs an importable module; `no-channel` needs a channel, a resolvable
tracking pointer and an onboarding row; and `native` is checked against
`agent_sdk/mcp_refs.py`, which has to know the same fact to resolve a `@server`
ref at all — it satisfies a ref from the spec's OWN `mcpServers` for a
spec-reading backend and from the wire array for every other. A declaration and
the resolver acting on it may not diverge, in either direction. A selectable backend
whose projection was not written could previously sit under one name with an
explanation of when it would be, and every check stayed green — which is the
structural reason the same missing-tools defect shipped on four harnesses in a row.

## Adding a backend: checklist

1. **Decide the kind.** Read its `initialize` result before deciding: what the
   harness advertises is the answer, not what it resembles. A harness that
   advertises no transport the `session/new` array can use is `no-channel`, and
   that is a legitimate destination — named, not implied.
2. **Write the mirror, or write the declaration.** `mirror` means a
   `<backend>.py` here with a mirror class and its `rulings()`, registered in
   `MIRRORS`. Every other kind means an entry in `PROJECTIONS` with the fields its
   kind requires. A backend in neither table raises.
3. **Route it.** For a `mirror`, point the backend's session-params hook on
   `AcpClient` at the mirror so the declaration and the wire agree.
4. **The parity test then holds you to it** (`test/test_provider_mirrors.py`):
   one declaration per known and selectable id, `mirror` only with a class,
   `mcpServers` ruled `delivered` or `translated` on a mirror, `native` only for an
   id `agent_sdk/mcp_refs.py` resolves against the spec itself, a resolvable
   `tracking`, an importable `projection`, and every concern answered with a
   reason.
5. **The doctor row.** A selected `no-channel` backend prints one informational
   row naming its `channel` and `tracking`, so the operator who chose it learns
   that Crew's tools are absent by declaration rather than by diagnosis.
6. **The onboarding table row.** A selectable `no-channel` backend must also be
   named in `docs/system-specs/modules/harness-onboarding.md`, and the parity test
   checks it. The declaration is what code reads; the onboarding table is what a
   human reads BEFORE writing any of this, so a gap recorded in only one of the
   two is a gap the next author misses.

The folder makes a mirror easy to find and easy to copy. The test is what asks
the question. Both are needed — a folder alone is just a tidier place to forget.

## Where the translation logic lives

Beside the mirror, not inside it, when it is substantial:

- `acp/session_mcp.py` — the spec-entry to array-element translation, the `tools` allowlist and the registry filter. Shared: both session-array backends read it, and what is genuinely per-adapter stays in that adapter's mirror (codex's `codex_elements` narrows this output).
- `acp/kas_permissions.py` — KAS's `allowedTools` to `permissions` mapping.
- `agent_sdk/mcp_refs.py` — the provider-agnostic unresolved-ref resolver above,
  and the one reader of the `tools` ref vocabulary that `session_mcp` mounts
  through. `acp/mcp_ref_guard.py` is its one-line-of-log half, at the session
  call sites.

A mirror declares and routes; a helper translates.

## Current state

Declared in `PROJECTIONS` (`registry.py`); this table is a reading of it, not a
second source.

| Backend | Kind | Where it goes, and what is outstanding |
|---|---|---|
| `` (kiro-cli) | `native` | reads the spec itself via `--agent`. Its only native-config write is the small `cli.json` overlay, whose home is a separate decision |
| `claude` | `mirror` | `claude_code.py`, both faces; `hooks` is its one open `no-channel` disposition |
| `codex` | `mirror` | `codex.py`, wire face only — Crew writes no codex file, so the `session/new` array is its whole channel. `hooks` is its one open `no-channel` disposition; `disabledTools` is honoured by withholding a third-party server it narrows, and by refusing the call at the approval request for Crew's own control plane; the array, the withhold set and the deny pairs all come from one spec parse |
| `kas` | `external` | `acp/kas_agents.py` (+ `acp/kas_permissions.py`), travelling as `_meta.kiro.customAgents`. The most complete projection of any backend, down a real channel — what is outstanding is only WHERE the code sits, and the RFC schedules that as a pure relocation of its own so a live harness's projection is not moved and changed in one diff |
| `opencode` | `no-channel` | its `initialize` advertises `http` and `sse` MCP transports and no stdio, so the array cannot carry Crew's stdio servers, and its own config file lives in the operator's checkout. The shared MCP gateway does not reach it either: a broker stub is shaped as a stdio element too, so it lands in the same array. Its sessions hold none of Crew's own tools, gateway on or off |

## Verify against the adapter, not against the last mirror

Codex is the reason this section exists. Its hook sat at `[]` behind a docstring
that stated, as the one established constraint, that codex-acp answers `-32602`
for the whole `session/new` when it meets a transport it does not advertise. A
real adapter says otherwise: a malformed stdio element — and even an array member
that is not an object — leaves `session/new` succeeding with that element
dropped, while `sse` is the one fatal shape and fails with `-32600`. The fear was
the wrong code AND the wrong scope, and it had been load-bearing for a whole
harness's tool surface.

So a new mirror's transport and environment rules are MEASURED. `codex.py` cites
what was run and `test/test_codex_session_mcp.py` pins it against an installed
adapter, skipping cleanly when there is none. Copying the neighbouring mirror's
shape is the cheap half; only the adapter can tell you whether it is accepted.
