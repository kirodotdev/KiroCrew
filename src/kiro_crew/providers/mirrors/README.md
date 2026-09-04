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

- `acp/session_mcp.py` — Claude Code's spec-entry to array-element translation,
  the `tools` allowlist and the registry filter.
- `acp/kas_permissions.py` — KAS's `allowedTools` to `permissions` mapping.

A mirror declares and routes; a helper translates.

## Current state

| Backend | Mirror | Notes |
|---|---|---|
| `claude` | `claude_code.py` | both faces; `hooks` is its one open `no-channel` |
| `` (kiro-cli) | `NO_MIRROR` | reads the spec itself via `--agent`; only a small `cli.json` overlay, whose home is still an open decision |
| `kas` | `NO_MIRROR`, pending | has the most complete projection of any backend, not yet moved here |
| `codex` | `NO_MIRROR` | known but not selectable, so no session to configure yet |
