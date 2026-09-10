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
  Also the seam a mirror that must place the gateway's pooled stubs itself takes
  them through (`stub_elements`).
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

## Adding a backend

1. Write `<backend>.py` here with a mirror class and its `rulings()`.
2. Add it to `MIRRORS` in `registry.py` — or, if it genuinely needs no
   projection, add it to `NO_MIRROR` **with the reason**. A backend in neither map
   raises.
3. Route the backend's session-params hook on `AcpClient` at the mirror.
4. The parity test then holds you to it: every backend x every concern must
   resolve to a disposition with a reason.

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

| Backend | Mirror | Notes |
|---|---|---|
| `claude` | `claude_code.py` | both faces; `hooks` is its one open `no-channel` |
| `codex` | `codex.py` | wire face only — Crew writes no codex file, so the `session/new` array is its whole channel. `hooks` is its one open `no-channel`; `disabledTools` is honoured by withholding a third-party server it narrows, and by refusing the call at the approval request for Crew's own control plane; the array, the withhold set and the deny pairs all come from one spec parse |
| `` (kiro-cli) | `NO_MIRROR` | reads the spec itself via `--agent`; only a small `cli.json` overlay, whose home is still an open decision |
| `kas` | `NO_MIRROR`, pending | has the most complete projection of any backend, not yet moved here |

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
