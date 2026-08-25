# Connections warm-mint table

Cold mint (`kiro_crew.connections.mint`) spawns one kiro-cli process per provider for one
approval URL: ~7.5s per card. `kiro_crew.connections.warm` serves the whole gallery from one
process, and every rule below answers an observed failure.

**Placement.** All warm code is in `src/kiro_crew/connections/warm.py`; the dashboard handler
adds only endpoint wiring and a function-local `expire_dead_mints` import on the status path,
keeping the mint engine off the gateway's boot path.

**Scope boundary: slice N2a ships the TABLE half; the LIFECYCLE is DEFERRED to slice N2b.**
Shipped now: the shared row shape (`shared`/`generation`/`activation`), the liveness registry
those stamps are read against, and `expire_dead_mints()` on the status path. Deferred to N2b:
everything that spawns, activates, parks or kills a process -- spec planning, the spawn/respawn
rules, tool-alias key shape, the reaper, `warm_mint_all`, and the filesystem-drift guard that
covers their synchronous helpers. Every section below except *Measured facts* therefore
describes N2b's half, and N2b lands those tests together with the code. With no activation
live no row is ever `shared`, so the shipped `expire_dead_mints()` call is a no-op scan whose
predicate is nonetheless complete for the parked case, because a reader blind to a parked
process withdraws a URL that process can still redeem.

## Measured facts

- Activation costs a fixed ~5.18s whether the spec carries one remote server or six, and an
  initialized process mints in ~5.4s. ACP `initialize`, the expensive half, is paid once at
  spawn, so one activation warms every card.
- **A challenge is half per-process and half per-session.** The PKCE verifier is a value in
  process memory and coexists with its peers (six proven live); the loopback callback *server*
  is one of the session's MCP children, so `session/terminate` reaps it -- popping the URL and
  destroying the handle left a `redirect_uri` whose port accepted a bare connect, then reset
  every real exchange with zero bytes. So the session is *held*, and redeemability takes two
  questions: `generation_is_live` (the process holds the verifier) **and** `activation_is_live`
  (the session still answers the redirect). Process liveness alone passed the
  terminated-session case.

## Specs are read once at spawn

A spec written after spawn is invisible (`set_mode` answers "Mode not found") and a rewrite is
not honoured, so the whole set is written before spawn and any change needs a *new process*,
tracked by `_WarmSpecPlan.digest`. A respawn destroys every peer's in-flight consent listener,
so respawn frequency is the dominant design pressure:

- **The spec universe is registry-derived and blind to grant and cancel state.** Connect writes
  an MCP entry for the provider being connected, so a config-derived plan changed on every
  click, and a plan tracking "who needs a URL now" changed on every completed consent and every
  Cancel -- either retired a process holding other cards' listeners.
- **Digest equality is not the respawn test** -- it reads a set that *shrank* as one that
  changed. `_plan_is_servable` asks whether every entry the new plan needs is already resident
  with an identical authorization ask, re-activating on the same process when it is: a Connect
  costing 0.13s instead of 7.5s. An unservable change **parks** rather than kills, so the
  outgoing generation keeps serving the consents it holds until the reaper collects it, once
  its rows are gone or expired.

## Cut against the shipped engine

`mint.py` (PR #3154) is the reviewed engine and owns the row table, the row identity token,
grant detection, spec emission and the manifest sweep; warm imports all of it and adds only
what is genuinely per-process. Two adaptations:

- `_mint_holder_alive` is deliberately **not** reused -- it reads the row's own `client`, which
  a shared row does not own, so it answers False for every warm row. `_warm_row_alive` asks the
  generation/activation pair instead.
- Warm spec names are fixed (`kirocrew-mint-warm-*`), with no `-<pid>-<8hex>` suffix, keeping
  them out of the cold engine's manifest sweep. That shared prefix is a hazard in reverse -- a
  *cold* spec for a server named `warm-*` matches the warm glob -- so `_is_stale_warm_spec`
  refuses anything matching the cold name shape, and both patterns must share one **character
  class**: while warm accepted `[A-Za-z]` and cold only `[a-z]`, a mixed-case alias produced a
  live cold spec the warm sweep read as its own and unlinked.

## Tool-alias key shape

`resolve_tool_aliases` de-collides by registry **slug**, keying `@slug/tool`, while a warm spec
mounts under `mcp_server_alias(slug)`. Where the two differ a slug-keyed entry names a server
the spec never mounted, kiro-cli applies no rename, and the collision returns silently, so
`connections_tool_aliases` re-points keys at the mounted alias and leaves the resolver
authoritative over which tools collide. Every registry slug is slash-free today, so this is an
identity map holding the shape contract of the spec we write, not a live defect. Semantics are
#3260's -- **every** claimant is renamed, none keeps the bare name; an earlier draft asserted
the pre-#3260 rule and those assertions were not carried forward.

## Filesystem work never runs on the loop

Every flow reads the user's config, the shared agents directory, or kiro-cli's OAuth cache, any
of which can sit on a network mount where a stat is unbounded, so the synchronous helpers are
reached through `asyncio.to_thread` -- enforced by a fixed-point drift guard in
`test/test_connections_warm.py` that reuses the mint engine's own primitive sets so the two
cannot drift apart.

## Seams and residuals

**Revocation** is PR #5899's, through `_expire_shared_mints`; **proactive refresh** attaches in
`_warm_mint_reaper`; **a supervisor/watchdog** is absent, as are the accessors it would need.
Two residuals:

- **A cancel between the claim and the activation leaks the claim.** `warm_mint_all` takes its
  claim outside any `try`/`finally` and `_warm_activate` catches `Exception`, not
  `BaseException`, so a `CancelledError` in that window leaves rows `minting` with no watcher:
  nothing expires them, `_shared_mints_pending` stays true, and the process is never retired.
  Unreachable while `warm_mint_all` has no caller, and **must be closed before N2b wires it up.**
- **A hard gateway kill strands warm spec files.** They carry no manifest row, so the cold
  engine's aged-row sweep cannot see them. The next spawn's write-time sweep removes them, so
  the exposure is bounded, but it is not a clean teardown.
