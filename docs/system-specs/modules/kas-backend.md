# A second ACP backend

Kiro Crew drives one first-class agent harness, `kiro-cli`, over ACP. It also
supports a **second, adapted ACP backend** (`agent.acp_backend = "kas"`); the
default and first-class path stays `kiro-cli`. `agent.provider` remains `"acp"`
— the harness is never the provider selector (see
[harness-parity.md](harness-parity.md)).

**Scope.** This documents the Kiro Crew-side integration only. The second backend
is not open source; its wire shapes, storage, auth, and process internals are
not described here. Where its on-the-wire signals differ from `kiro-cli`'s, the
difference is absorbed in one Crew module (below), which is the only place that
needs editing if that backend changes.

## How Crew runs it

- It goes through the existing `AcpRuntime` (one process, multiplexed sessions)
  via the established backend seam — no new runtime subclass, just a spawn-argv
  branch plus adapters. `kiro-cli` keeps its own spawn path, per-harness
  handshake literals, and session machinery unchanged (harness-parity H9/H10).
- Backend-specific parsing (the display/telemetry frames whose shape differs
  from `kiro-cli`'s) is localized in **`acp/kas_wire.py`** — a single module, so
  an adjustment is a one-file edit. Backend-neutral logic (the context-meter
  math) lives on `AcpPromptStats` and is shared by both paths, so they cannot
  drift.
- Capabilities the session layer reads off a provider are declared on the
  `LLMProvider` ABC with safe defaults (harness-parity H14), so the adapted
  backend never forces a `getattr` probe onto the `kiro-cli` path.

## Identity is positive

Every "is this the `kiro-cli` harness" test is a positive comparison against a
named constant or membership in a named `ACP_BACKENDS_*` set — never
`not is_<other>`, which would hand a branch to a later harness by default. The
full invariant catalogue and its CI gate are in
[harness-parity.md](harness-parity.md).

## Switching backends

```
kirocrew config set agent.acp_backend kas   # then: kirocrew restart
kirocrew config set agent.acp_backend ""     # back to kiro-cli; restart
```

A config change affects only new sessions; `restart` reaps the prior runtime
process. `kirocrew doctor` reports the selected backend's readiness.

## Deferred

- **Hooks** are not wired for the second backend — a separate effort.
- **`/clear`** currently maps to a `kiro-cli`-only notification, so on the second
  backend it is a no-op today; a local reset is the intended parity fix.
- An **alternative transport mode** is out of scope, pending its own design and
  review.
