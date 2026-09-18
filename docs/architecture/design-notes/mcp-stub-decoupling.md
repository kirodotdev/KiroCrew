# Decoupling the MCP stub from the pooling allowlist

The stub roster is **opt-in per server**: `mcp_gateway.stub_servers` is empty by
default, so a default install runs no stub, no daemon and no gateway in the request
path. What this note records is why *whether a stub exists* is a separate question
from *whether the backend is shared*, and why a connection-private backend sits
outside the pooling budget.

## The config surface

- **`mcp_gateway.stub_servers`** — the stub ROSTER, empty by default. Routing is what
  interposes a stub, so this is the per-server decision and the only thing that can
  grant MCP Apps for that server. It is a layer an edition can own: a distribution
  that wants its known servers stubbed out of the box ships them here and keeps
  adding to it, because a toggle does not rewrite it.
- **`mcp_gateway.stub_overrides`** — the operator's deviations from that roster, as a
  sparse `name -> bool` map, and what MCP Management writes. A name the operator never
  touched keeps following the roster, so growing the roster reaches them while their
  own opt-outs survive it. An override that agrees with the roster is pruned rather
  than stored: identical in effect, and storing it would pin that server against the
  next roster change. A flat resulting list cannot do both — unstubbing one name out
  of a shipped roster means writing back the survivors, and that list then answers the
  question forever.
- **`mcp_gateway.enabled`** — share backends. Global over the stub set; there is no
  per-server sharing switch.
- **`mcp_gateway.poolable_servers`** — deprecated alias, read only when `stub_servers`
  is absent. A pooled server already ran behind a stub, so migrating it to the stub
  set preserves behaviour rather than granting anything.
- **`mcp_gateway.apps_enabled`** — deprecated and ignored. Capability follows the stub;
  a preference cannot grant it and cannot honestly withdraw it.
- **The broker starts iff something is stubbed.**

A stub for every stdio server is the shape this note originally argued for, and the
reason it is not the default is cost, not incoherence: it adds a daemon plus one proxy
process per (server, session) to an install that asked for neither — measured at 166
stub processes at ~15.3 MB PSS each on one developer machine. A topology change
belongs behind a choice rather than in a default.

## Why the two decisions are separate

The stub is the **addressing layer**: it gives an `(ACP connection, server)` pair a
name gatewayd can route a callback back to. Poolability is a **resource** decision:
may several connections share one backend process. Welding them together has three
consequences:

- **MCP Apps require the stub.** The render and callback paths live behind it, so a
  server with no stub cannot host an MCP App at all, however the MCP Apps switch is
  set.
- **A grant has to be per server.** A single global switch either stubs everything or
  nothing, and neither answers "MCP Apps for this one server".
- **Wanting isolation must not cost the feature.** An operator who deliberately keeps
  a stateful server unshared still gets its stub, and so still gets MCP Apps for it.

## Baseline topology

The baseline behaviour of MCP is **one backend per ACP connection per server** — what
happens with no gateway at all: the agent process spawns its own MCP server processes.
Pooling is the *deviation* that collapses many connections onto one process. The stub
is orthogonal to both.

- `poolable` does not decide whether a stub exists. It is a field in the stub's
  `register` payload — an input to *how the backend is acquired*. Absent means private,
  so an overlay predating the flag never silently starts sharing.
- A stubbed server that is not shared therefore gets exactly this: **a stub, and a
  backend 1:1 with its ACP connection.** Same process topology as no-gateway, plus a
  name. That is the common case, and the exclusive-backend machinery below is what
  carries it.
- `mcp_gateway.enabled` false means no stub is marked shareable — stubs stay, every
  connection gets its own backend.

## Why this is not just deleting the guard

Removing the guard alone would silently make every server shared. Backend reuse
is decided purely by the PoolKey digest: `get_or_create` hashes the key, finds a
live entry, and returns it.

`PoolKey` carries no session or connection dimension — deliberately, and it must
stay that way. Its fields are exactly the config/capability inputs that make two
backends interchangeable; adding a per-connection dimension would make every
backend connection-private and reduce the pool to a no-op. So two connections to
the same server compute the **same digest** and land on the same entry.

An unconditional stub therefore needs a way to acquire a backend **without**
digest reuse.

The same rule is why the per-session identity a stub carries is not a key field
either. Each ACP session's injected stub entries carry a token in their `env`
(`claim.mint_stub_session_token`), the stub returns it on `register`, and gatewayd
keys claim-push by `(pid, token)` so one session's claim cannot re-point another
session's stub on the same shared runtime, and refuses a token no claim has named
rather than falling back to the shared process tree. It travels as a **sibling** of
`poolable`: both describe the connection, neither says which backends are
interchangeable. A claim carrying no token keeps the PID-wide behaviour, so a stub
from a hand-written config or an overlay predating the token is unaffected.

## Where the stub comes from

Two paths emit stubs, and both are now unconditional for stdio servers:

- **Agent-declared servers** in `~/.kiro/agents/*.json`, wrapped in that agent's
  overlay.
- **Global `settings/mcp.json` servers**, injected into each agent's overlay so
  the stub carries the right agent identity. The injected stub takes precedence
  over the raw same-named global entry at ACP `session/new`
  (`session_servers.py`), which is what keeps a server from being wrapped twice
  under two identities; no settings overlay is written and the real settings
  file is never modified (#8111). The injection previously applied only to
  poolable servers, leaving everything else to merge raw with no stub and
  therefore no callback address.

## The acquisition path

Everything downstream of the digest is keyed on the digest *string*, not on the
`PoolKey` object: storage, lookup, reservation, refcount, idle sweep, LRU, and
the breaker. Most importantly so is callback resolution — `get_by_digest`, which
the MCP Apps `app-call` path uses against the digest the spool record persists.

So a private backend needs no new addressing mechanism, only a storage key that
cannot collide with another connection's. `Backend.storage_digest` supplies it:
the plain `PoolKey` digest for a shared backend, and that digest plus the
connection's `stub_uuid` for a private one. The spool record binds
`storage_digest` rather than recomputing the `PoolKey` hash, so the exact-match
guarantee `get_by_digest` exists to provide survives: without the discriminator,
two private backends for one server would share a digest and an app callback
could execute against another session's process.

Private backends live in their own map keyed by `stub_uuid`, separate from the
shared index. An entry there is never a reuse candidate — that is the point — and
the register payload's `poolable` field selects between the two paths at the
single acquisition site.

## Lifecycle: no new mechanism

A private backend has exactly one stub attached, so its refcount is 1 for its
whole life. Its stub disconnecting is therefore the end of its life, and the
connection-teardown path releases and shuts it down there. Because it is
deliberately outside the pooling maps, no sweeper is watching it, so the release
is unconditional on every disconnect and a no-op for a pooled stub.

No session-end hook and no bespoke TTL are required. That falls out of binding to
the connection rather than to a session identifier.

## Decision: private backends do not count against `max_backends`

`max_backends` defaults to 64. Connection-private backends are excluded from that
budget, and `stats()` reports their count separately so they stay visible.

The conceptual reason: a private backend is a process the host would have had
anyway with no gateway at all. Counting it against the *pooling* budget makes the
choice not to pool subject to a pooling limit.

The mechanical reason is stronger. Eviction only ever selects idle entries —
refcount 0 and unreserved. A private backend is refcount-1 for its entire life,
so it is **never** an eligible victim. If private entries shared the budget they
would accumulate as unevictable occupants until `add()` could find no victim at
all — at which point it raises `PoolAtCapacity` and a new **poolable** session is
refused. Sharing the budget converts resource pressure into a hard denial on the
shared path, caused entirely by connections that opted out of sharing.

Excluding them keeps the failure modes separate: pooling pressure stays a pooling
concern, and per-connection backends fail the way they would without a gateway —
by exhausting host resources, observably, rather than by silently rejecting an
unrelated session.

## Session identity does not depend on the stub

The stub used to be the only way one of Crew's own MCP servers could learn WHICH
session was calling it, and that coupling is now gone. It came about honestly: the
per-session token that names an ACP session was minted for gatewayd
(`mcp_gateway/claim.py`, `mint_stub_session_token`), rode only stub entries, and had
exactly one reader — the daemon, matching it against a claim frame. So a server with
no stub, on an install with no daemon, had no per-session identity channel at all. It
fell back to `KIROCREW_SESSION_KEY`, which is *wrong by construction* in the two
topologies that matter most:

- **warm-pool rekey.** A pooled process is re-keyed to a new session while the MCP
  children it already spawned keep the env they were spawned with, so their key names
  the PREVIOUS session.
- **`spawn_run` session sharing.** One kiro-cli process hosts N ACP sessions, so every
  process-keyed source — the env var, the `session_pid_<pid>` file, a `/proc` ancestor
  walk — answers with the PARENT's session for a subagent's server.

The token now has a second reader that needs no daemon: the gateway publishes a
`token -> session_key` mapping to a MAC-signed file (`session_token_sig.py`) at
`session/new` and again on every `rekey()`, and the strict resolver
(`mcp_core._resolve_session_key_strict`) reads it directly. Three consequences worth
stating because each one was previously false:

- **The token is minted unconditionally.** It used to be gated on a reachable gatewayd
  socket, on the correct reasoning that a token nothing reads is an inert value on
  every `session/new`. There is now a reader, so the socket gates the CLAIM alone.
- **It rides every control-plane element, not only stub entries** — through the one
  owner of that question (`providers/mirrors/identity.py`,
  `control_plane_identity_env`), plus the member-dispatch element and the kiro-cli
  child env. `KIROCREW_SESSION_KEY` stays beside it as the fallback for the one case
  the token cannot cover: no SEL trust root to sign the mapping with.
- **The resolver reads the token ABOVE the env var.** Where the two disagree the env
  var is the stale answer, so preferring it is the misattribution above. The
  gateway-injected per-call caller context still outranks both, because it is stamped
  per CALL and cannot go stale at all.

**No new config surface.** Nothing on the list at the top of this note changed, and
pooling stays opt-in: a default install — no stub, no daemon, gateway off — is exactly
the install this closes the identity gap for.

### The trust boundary, plainly

The mapping file lives in `config_dir()`, which is same-uid agent-writable, so the
file is not the trust root — the MAC is. It is keyed by a subkey derived from the SEL
trust root (`sel_hmac.key`), under a domain label distinct from the `session_pid`
sidecar's so the two can never be cross-replayed.

**How strong the key's protection actually is**, stated here so the follow-up inherits an
accurate baseline rather than a rounded-up one: `is_sensitive_path` refuses that leaf on
the agent FILE-TOOL path, but `security.md` classifies it `VISIBLE` — no OS fence, no
bash-layer fence — and a spawned shell reaches a file through an `open()` that never
routes through the tool gate. So the forgery defence below holds against the file tools
and not against a shell. That residual is not this protocol's: the SEL audit chain rests
on the same key and carries the same gap, and `security.md` names the remedy (move the
in-sandbox reader behind the gateway so the leaf can become `HIDDEN`, never another
matcher). A shell that can read the key can also mint a local API token and bypass
client-side resolution altogether, so this protocol is not the narrow point.

What the MAC buys, and what it does not:

- **Blocked:** forging a mapping (no valid MAC without the key); replaying
  another session's mapping under a token you hold (the token, not the filename, is
  bound into the MAC); tampering with the session key in place; and planting a symlink
  at the predictable path, on both sides — publication is `atomic_write`/`os.replace`
  and verification opens `O_NOFOLLOW` and refuses a non-regular file.
- **Out of scope, and UNCHANGED from the env var it sits beside:** a same-uid process
  that can read the mapping directory. It could already read the `session_pid` files
  and present another session's key through `KIROCREW_SESSION_KEY`, and a shell-capable
  same-uid agent can bypass client-side resolution entirely by minting a local API
  token. Same-uid processes are inside the boundary. This narrows nothing and widens
  nothing; authenticating the calling PROCESS (SO_PEERCRED over a gateway-owned
  socket) is the stronger, orthogonal follow-up tracked with the pid sidecar.

The filename is `sha256(token)` rather than the token, so the directory can be listed
without handing out a bearer name — and a listing is useless, because verification
requires the token that hashes to the name.

## Costs this accepts

- **Process count.** Servers off the allowlist become one backend per ACP
  connection instead of one shared. That is the no-gateway baseline, not a
  regression, but it is a real change from today's collapsed count. `stats()`
  exposes the count; it is not otherwise capped, which is the deliberate
  consequence of the decision above.
- **Head-of-line blocking gets more reachable.** More traffic crossing the stub
  seam means more traffic through a pooled backend's single-worker dispatch,
  where `ping` and `tools/list` bypass the queue and answer healthy while tool
  calls serialise. That defect is tracked separately and is not introduced here,
  but this change widens the set of paths that can hit it.

## Out of scope

- Changing `PoolKey`. It gains no dimension, in this change or any other.
- `UNPOOLABLE_SERVERS` — Kiro Crew's own MCP servers, passed through unwrapped.
  They are already per-session by construction; giving them stubs is a separate
  change — and no longer one their IDENTITY waits on, since the signed session-token
  mapping above reaches them with no stub at all.
- HTTP/SSE MCP entries. They need no stub and merge raw from the real settings
  file.
- Per-server MCP Apps control. Orthogonal to stub emission.
