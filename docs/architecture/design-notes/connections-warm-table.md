# Connections warm-mint table

Cold mint (`kiro_crew.connections.mint`) spawns one kiro-cli process per provider for one
approval URL: ~7.5s per card. `kiro_crew.connections.warm` serves the whole gallery from one
process, and every rule below answers an observed failure.

**Placement.** All warm code is in `src/kiro_crew/connections/warm.py`; the dashboard handler
adds only endpoint wiring and function-local engine imports -- `expire_dead_mints` on the status
path, `mintable_providers` plus `warm_mint_all` on the premint path, and `adopt_shared_mint` on
the mint path -- keeping the mint engine off the gateway's boot path.

## The handoff: how a premint reaches the click it was minted for

The table and the per-caller Connect flow shared a row store and nothing else, so the slice
that gave premint a frontend caller made three defects reachable at once. All three came from
one conflation -- `shared` was being read as if it answered both *who owns this row* and *what
judges its liveness* -- and the fix separates the two axes:

| axis | mark | meaning |
|---|---|---|
| OWNERSHIP | `shared` | nobody has claimed this URL; any Connect may adopt it |
| PROVENANCE | `generation` / `activation` | the verifier is in the shared process, so `_warm_row_alive` judges redeemability |

`adopt_shared_mint` clears the first and keeps the second. Each defect follows from reading
one axis where the other was meant:

- **The click threw the answer away.** `api_connections_mint` called `reserve_mint_row`
  unconditionally, and that pops WHATEVER row is at the slug, so `start_oauth_mint` disposed
  the URL the premint had just minted and then paid a ~7.5s cold spawn to replace it. Adoption
  now runs FIRST; a refusal falls through to the cold path, which stays correct and stays the
  only path for a provider warming never covered. Adoption is atomic by construction -- every
  step between the read and the last write is synchronous -- so two clicks racing one row
  serialize on `_mints_lock` and the loser simply finds `shared` already cleared. The token is
  rotated, which is what fences the adopting tab against both the premint's own rollback and a
  sibling tab; **the watcher must be re-armed in the same synchronous run**, because every
  write in `_mint_watcher` is guarded on the token it was started with, so rotating alone would
  leave the row watched by a task that can no longer touch it -- nothing to flip it `granted`,
  nothing to expire it, and the shared process resident for good.
- **One premint flipped every card.** `_classify` read any `minting`/`waiting` row as
  `awaiting_consent`, and the page's `waitingSlugs` memo folds those slugs into its waiting
  set -- so warming presented every mintable card as mid-consent with no user action. An
  unclaimed row now falls THROUGH to the grant branches: the honest verdict for a URL nobody
  asked for is the one the card would get with no row at all, and a fourth status would be one
  the frontend has no rendering for. The seam is a `shared` flag on `pending_mint_for`'s view
  rather than that view hiding the row, because ONE view feeds two readers with opposite
  needs: the classifier must refuse it as consent, while the mint-state poll must still tell
  it from `idle`, which its own contract defines as "no mint exists for the provider" -- the
  lie a filtering view would tell on exactly the slug a card has just adopted.
- **The poll destroyed what it went looking for.** `_mint_holder_alive` reads the row's own
  `client`, which a warm row never owns, so it answered False -- and False there is a VERDICT,
  not a shrug, because `expire_dead_holder` acts on it. The first mint-state poll on a warm
  slug therefore withdrew a URL whose process and session were both alive. The cold judge now
  ABSTAINS on any row carrying a `generation`, which makes `expire_dead_mints` the only reader
  that can withdraw one -- it is also the only reader that can see the registry those stamps
  name.

Because adoption clears `shared`, every warm-side predicate had to move onto `_warm_table_row`
(`shared` OR `generation`) rather than `shared` alone, and both disjuncts are load-bearing:
`shared` is the only mark a row still `minting` carries -- precisely the row a cancelled
activation must not leave behind -- while `generation` is what an adopted row still has. Five
readers depend on it, and an ownership-only test breaks each differently: `_live_row_count` and
`_shared_mints_pending` stop keeping the adopted row's process parked, so **the reaper retires
the process holding the URL the user is part-way through redeeming**; `_activations_in_use`
lets the sweep destroy the session answering its redirect; and `_expire_shared_mints` plus
`expire_dead_mints` stop withdrawing it when its holder dies, leaving a card serving a URL
nothing can complete. `_claim_shared_mints` needed the mirror-image guard: `_mint_is_cold_held`
cannot see an adopted row either -- it owns no `client` -- so a later premint sweep displaced a
URL a user was mid-consent on. `_mint_is_adopted` is that guard.

**Scope boundary: the TABLE, the SPECS, the PROCESS LIFECYCLE and the ENDPOINT WIRING have
landed.** Shipped: the shared row shape (`shared`/`generation`/`activation`), the
liveness registry those stamps are read against, `expire_dead_mints()` on the status path, the
spec side -- the registry-derived universe, the plan and its servability test, the tool-alias key
shape, the spec files the plan writes, and the filesystem-drift guard covering their synchronous
helpers -- the lifecycle: spawn/respawn, activation, park-or-kill, the reaper, and
`warm_mint_all`, which is what gives the spec planner a caller; and the request path,
`POST /api/connections/premint`, which the Connections page will fire once on mount -- until
that slice lands the endpoint has no frontend caller and is reachable through the API only. The
endpoint
adds a handler, not a rule: it scans the mintable candidates off the loop, hands that same list
to `warm_mint_all` so the slugs it reports and the rows the engine claims come from ONE registry
read, and answers without awaiting the activation -- warming costs seconds, and the card's
verdict is its own mint state rather than this list. Still deferred: proactive refresh, which
attaches to the reaper in its own slice.

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
- **A frame becomes readable only when something DRAINS the session queue.**
  `pop_pending_oauth_requests()` reads a list that only `drain_init` appends to, and
  `create_session` runs exactly one drain before it hands the handle over. The settle loop
  originally slept between pops, which consumes nothing -- so a provider whose `oauth_request`
  landed after that create-time drain's idle exit was unreachable however many rounds elapsed.
  Each wait is now a bounded `drain_init(duration=0.5, idle_exit=0.5,
  no_report_ceiling=0.0)`. The ceiling argument is load-bearing: it arms the idle shortcut at
  entry so the call cannot hold waiting for a "first report" this session already produced
  during `create_session`'s own drain. Six consecutive quiet drains end collection, while each
  newly collected provider renews that quiet budget. The absolute cap is six drain windows per
  expected provider -- 3 seconds times the roster size -- because a handle accepts at most one
  OAuth request per server. Progress can therefore carry a multi-provider activation beyond the
  old fixed 3-second cutoff without making the wait unbounded.
- **An adoption miss gets one last read from the process already paid for.** Each live warm
  session retains its provider roster internally. When Connect finds no adoptable table row, it
  first rebuilds the current candidate plan. The provider must still be visible and enabled,
  have a definitive absent-grant verdict, remain compatible with the configured entry, and ask
  for the same URL, scopes, and client ID the retained session activated. Only then does Connect
  re-run the same bounded collector on that session, materialize every attributable late frame
  through the ordinary claim, credential-screen, watcher, and settlement paths, and retry
  adoption before reserving a dedicated cold row. Any failed revalidation uses the cold path
  without reading the old handle. The roster and yielded-provider diagnostics stay internal;
  customers still see only the existing card states.

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
what is genuinely per-process. Three adaptations:

- `_mint_holder_alive` is deliberately **not** reused -- it reads the row's own `client`, which
  a shared row does not own, so it answers False for every warm row. `_warm_row_alive` asks the
  generation/activation pair instead.
- Warm spec names are fixed (`kirocrew-mint-warm-*`), with no `-<pid>-<8hex>` suffix, keeping
  them out of the cold engine's manifest sweep. That shared prefix is a hazard in reverse -- a
  *cold* spec for a server named `warm-*` matches the warm glob -- so `_is_stale_warm_spec`
  refuses anything matching the cold name shape, and both patterns must share one **character
  class**: while warm accepted `[A-Za-z]` and cold only `[a-z]`, a mixed-case alias produced a
  live cold spec the warm sweep read as its own and unlinked.
- **A name is not ownership.** Those fixed names are predictable and they live in the user's own
  agents directory, next to the agents they hand-write, so a name says where a spec of ours
  would *go* and never that the file already there is one. Trusting the name shape alone was a
  defect in both directions: the write-time sweep unlinked a user's own agent spec sitting at
  such a path, and the write then clobbered one at a path the current plan wanted.
  `_warm_spec_is_foreign` proves ownership from the file's CONTENTS, and it takes two halves.
  The fields the spec body fixes (`model`, `includeMcpJson`, `prompt`, `allowedTools`) are read
  off `_mint_spec_body` so a change to the body cannot leave the module unable to recognise its
  own files -- but they are also **stock defaults** a hand-written or scaffolded agent plausibly
  carries, so on their own they still read a wholly user-authored spec as ours. The
  discriminating half is `_WARM_SPEC_SENTINEL`, stamped as the description prefix of every spec
  written here. `description` is the only field free enough to carry a marker while staying
  schema-legal: kiro-cli rejects an unknown spec key, and the agent-spec migration sweep strips
  bookkeeping keys. Requiring it orphans nothing, since no warm-spec writer has ever shipped --
  and a hypothetical unsentinelled file of ours would read as foreign, which means refused and
  left in place. It fails closed, because the mistakes are not symmetric: reading our own file
  as foreign leaves one stale spec as clutter, while reading a user's file as ours destroys it.
  A refusal is audited and skipped -- never raised -- so an occupied path costs one unwarmed
  provider, not a failed spawn.
- **The ownership checks guard one directory; kiro-cli reads two.** Everything above protects
  the user's agents directory, but kiro-cli also resolves PROJECT-LOCAL specs from
  `<cwd>/.kiro/agents`, and the process is activated BY NAME -- so a spec planted under the
  warm process's own working directory shadows the guarded one, and its `mcpServers` is what
  gets initialized and authorized against. No ownership check looks there. The working
  directory is therefore `<data home>/run/connections-warm`: `run/` is already on
  `security._SENSITIVE_HOME_DIRS` (it holds the sandbox launchers and run markers the gateway
  execs outside the sandbox), while `connections/` carries no entry and an agent file tool
  could write it. The property is asserted through `security.is_sensitive_write_path` rather
  than a path literal, so a later rename cannot silently leave the fence.

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
of which can sit on a network mount where a stat is unbounded, so all of that work lives in
SYNCHRONOUS helpers and a coroutine reaches them through `asyncio.to_thread` -- enforced by a
fixed-point drift guard in `test/test_connections_warm.py` that reuses the mint engine's own
primitive sets so the two cannot drift apart. What the guard pins today is the exact set of
helpers doing filesystem work, so the lifecycle slice can neither call one from a coroutine nor
quietly drop the filesystem work the guard's coverage rests on without failing it.

## Seams and residuals

**Shared-mint expiry** has landed with the lifecycle, keyed on the fact that a minting process is
gone rather than on a cause, which is what covers a process that went away by a route no expiry
path anticipated. PR #5899 is a different cause and a different table: it owns
**Disconnect-driven grant revocation**, and the two meet only where a revoke should re-warm.

**Mid-visit resilience** is owned by the reaper and the runtime it already supervises. Every
explicit page premint arms one automatic replacement. When that generation dies, the reaper
reserves a single-flight task and reuses the ordinary `mintable_providers` scan plus
`warm_mint_all`, so the replacement covers only providers whose grant absence is confirmed by
the existing tri-state eligibility check. A present grant and an unreadable grant cache are both
skipped. The replacement call does not re-arm the budget, so a second death cannot form a
restart loop; another explicit page premint is the only thing that grants a fresh attempt.
Concurrent observers receive the same task, while a generation mismatch, a live replacement, or
an explicit warm already holding the lifecycle lock suppresses duplicate work. Hard shutdown
disarms recovery, cancels and settles an in-flight task, then retires the process. This is all
internal lifecycle state -- the card and status APIs expose no provider-level warming status.

**Proactive refresh** remains deferred and attaches to `_warm_mint_reaper` independently of the
death-only replacement.

Three residuals the lifecycle slice was required to close, and did:

- **A cancel between the claim and the activation leaked the claim.** The entry point took its
  claim outside any `try`/`finally` and the activation catches `Exception`, not `BaseException`,
  so a `CancelledError` in that window left rows `minting` with no watcher: nothing expires them
  (`expire_dead_mints` judges `waiting` rows only), the pending check stays true, and the process
  is never retired. `warm_mint_all` now holds every claim inside a `try` whose `except
  BaseException` releases them and re-raises, so cancellation still propagates -- the activation
  deliberately does NOT swallow it, because reporting a clean stand-down to a caller being torn
  down is the worse failure. The window after the activation returns is covered by the same
  block. **The claim loop itself was a second copy of this window**, since the claim is taken
  *before* that `try`: it awaited `_dispose_mint` on each row it replaced, which suspends on a
  client teardown and again on the shielded spec removal in that function's `finally`. The claim
  loop is now await-free and atomic by construction -- it returns the displaced rows for the
  caller to dispose *inside* the protected region, which also moves the dispose outside the
  table lock, where the mint engine's own rule wants it. Pinned by an AST guard, not just by a
  behavioural test.
- **A shared row's fence was a batch clock reading, not a row identity** (issue #6110). Rows were
  separated by `entry["started"] == started`, one `time.monotonic()` value per `warm_mint_all`
  call. The clock has ~15.6ms granularity on Windows -- the reason `_new_mint_token` exists for
  the cold engine -- so two Connects for one provider inside a tick read as the same row and a
  late absorb wrote its URL over the newer claim. Claiming now returns `{slug: token}` and
  absorb and release both verify that opaque per-row token; `started` is kept as information and
  is never a fence.
- **An approval URL was stored without being screened for a credential.** `_absorb_warm_requests`
  wrote the popped `oauth_request` URL straight onto the row. It now passes every popped URL
  through `security.oauth_url_contains_credential` -- the same gate the cold mint and the chat
  consent banner apply, called, not re-implemented -- *before* the table lock, since the verdict
  is a pure function of the URL and the gate can read the operator's on-disk endpoint extension.
  A refused URL never reaches the store: the claim is released so the card asks for a fresh mint,
  and the refusal is audited without the value.

Two more the same slice closed, both about **withdrawal and retirement following the
generation** rather than anything else:

- **The retry's expiry pass was unscoped.** `_WarmMintDied` is raised only after the stand-down
  inside `mint_for` has already run `_expire_shared_mints(generation=<the dead one>)` through
  `_kill_generation`, so the rows whose verifier died are withdrawn before the retry ever sees
  the exception. A second pass in `_warm_activate`, narrowed only by the caller's own row
  tokens, therefore expired every *other* live shared row: a parked generation's URL is still
  redeemable (its process holds the verifier and its session still answers the redirect), and a
  concurrent batch's claims are another activation's to fill. The pass is gone, and
  `_expire_shared_mints` now takes `generation` as its only narrowing, because withdrawal
  follows the verifier.
- **A parked generation was stranded when the current process died.** `sweep_retiring` is the
  only thing that retires a parked process, and every route to it runs off a NEW mint -- which
  is exactly what the leak does not have. The reaper used to `return` as soon as the current
  process was gone, and the stand-down before a *failed* respawn cancelled the reaper without
  creating one, so in both cases the parked process, its sessions and their loopback servers
  stayed resident forever once its last card completed. `_drain_parked_generations` now keeps
  sweeping until nothing is parked, and both routes run it.

And one the second review round found, which is not a cancellation bug at all:

- **A refused spec was still activated by name.** The writer declines to overwrite a file at
  a planned spec path whose contents this module did not write — which protects the file and
  nothing else, because the spawn is handed `agent=<fixed name>` and kiro-cli resolves that
  name off the same agents directory. A hand-written agent parked at
  `kirocrew-mint-warm-base.json` would therefore have been executed, with its own
  `mcpServers` commands initialized, by the very code that had just refused to own it.
  `_unowned_plan_specs` now re-verifies, off the loop, that every planned spec exists *and*
  is sentinel-owned before the runtime is constructed; any refusal aborts warming entirely
  and is audited. Aborting is safe because the cold path still serves every Connect — it
  just spawns per provider.

One residual remains:

## Session-handle ownership, made explicit

Three review rounds kept producing findings in this area because the ownership transfers were
*implicit* — a handle moved between "the backend has it", "we have it", and "the registry has
it" with no stated rule about who is responsible if an await in between is interrupted. The
rule is now one sentence: **a handle is registered before anything can interrupt, and
forgotten only once its destroy has completed.** Every touchpoint:

| # | Point | Transfer | How it is cancellation-safe |
|---|---|---|---|
| 1 | `runtime.create_session` in `_activate_locked` | backend → us | Run as a SHIELDED task we keep a reference to, so the handle stays reachable when the wait is abandoned. `except BaseException` → `_abandon_session_creation_locked` → re-raise. |
| 2 | `_sessions[activation] = _WarmSession(...)` | us → registry | No await between the handle arriving and the registration (counter bump + dataclass are sync). Atomic by construction. |
| 3 | abandoned create, handle recovered | backend → registry | Registered settled-and-expired *before* the destroy, so it enters rule 6 rather than being a special case. |
| 4 | abandoned create, no handle recovered | — | `create.cancel()`, then the generation is **quarantined** (`_plan`/`_digest` cleared) so the next activation stands it down and the orphan dies with the process. See the bound below. |
| 5 | oauth-poll failure in `_activate_locked` | registry → destroyed | Record marked settled + `expires_at = 0` BEFORE the destroy, popped after. An interrupted destroy leaves a sweepable record. |
| 6 | `_sweep_sessions_locked` | registry → destroyed | `try/finally` popping only when `destroyed` is true. Held across the await on purpose: a lock-free reader then over-reports liveness briefly, which keeps a row waiting rather than withdrawing a URL. |
| 7 | `_drop_generation_sessions` | registry → dropped, no destroy | Sync, and correct: the process is dead, so its sessions died with it. |
| 8 | `_retire_locked`'s `_sessions.clear()` | registry → dropped, no destroy | Sync. Every runtime is being killed, so every session dies with its process; on this path withdrawing the rows is the intent. |
| 9 | `_destroy_session_quietly` | — | Swallows `Exception`, propagates `CancelledError` **by design** — that is what lets callers 5, 6 and 3 retain the record and retry. |

Two awaits in `_activate_locked`'s own `except BaseException` handler are the cleanup rather
than a window, and their safety is state ordering (rule 5) rather than a nested handler, so
they are pinned by behavioural tests instead of the AST guard.

That guard is now bound to **specific (function, await-target) pairs**, not to "this function
contains some protected try" — the coarse form is what let a bare sibling await in
`_activate_locked` pass by association, and it omitted `_sweep_sessions_locked` entirely.

**The residual, and its bound.** A `session/new` the backend accepts *after* we cancel the
create carries no id we hold, so nothing can address it directly. The first version of this
note claimed that was fine because "it is reaped when the runtime is retired" — and a review
round falsified exactly that argument: retirement is not guaranteed to arrive. Any card
holding a URL keeps `_shared_mints_pending` true, which resets the reaper's idle clock on
every cycle, while `_ensure_locked`'s digest-equality fast path keeps the same generation
reusable — so each repetition parked another orphan session and its callback children on ONE
live process, unbounded until listener or memory exhaustion.

So the generation is now **quarantined** instead: row 4 clears `_plan` and `_digest`, which
makes the next activation find the resident plan unservable and stand the generation down
through the ordinary path — parked for the drain when a card still needs it, killed outright
otherwise. Either way the process dies and takes the orphan with it. **The bound is therefore
at most one generation's sessions, released on the next activation or on idle retirement,
whichever comes first.** The cost is a respawn (~5s) on the next warm call after a transient
session timeout, which is the right trade: correctness over a warm-cache hit. Closing the
residual entirely would need the backend to expose a session id at request time rather than at
response time.

- **A hard gateway kill strands warm spec files.** They carry no manifest row, so the cold
  engine's aged-row sweep cannot see them. The next spawn's write-time sweep removes them, so
  the exposure is bounded, but it is not a clean teardown.

## The one bug class, and the uniform shape that closes it

Two independent review rounds on the lifecycle slice produced seven findings between them,
and **six were the same defect wearing different clothes**: an `await` sitting between a
state mutation and that mutation's settlement or cleanup, guarded only by an
`except Exception` — which a `CancelledError` walks straight past, because it inherits from
`BaseException`. Each instance leaks something different, which is why they read as
unrelated:

| Mutation | The await in the window | What leaked |
|---|---|---|
| rows installed as `minting` | `_dispose_mint` on a replaced row, inside the claim loop | rows nothing withdraws, process never retired |
| rows installed as `minting` | the activation and the absorb | same |
| a generation parked, its reaper cancelled | the spec write and `runtime.spawn()` | parked process with no sweeper, forked child, orphan specs |
| a session registered | the credential screen's thread hop | session + its loopback callback servers, for the process's life |
| generations moved out of `_retiring` | `_kill_generation` per generation | processes with no reference left anywhere |
| `_runtime` cleared, reaper cancelled | `_kill_generation` in the stand-down | the same |
| `_retiring` emptied, `_runtime` cleared | the hard teardown's kill loop | the same |

One further finding was NOT a cancellation window but the same *implicitness* — a state that
had always been reachable and became routine. `generation_is_live` denied liveness on the
equality branch: `generation == self._generation` answered `is_alive()`, which reads
`self._runtime`. But only a successful spawn bumps the counter, so a stood-down process sits
in `_retiring` under the number that is still `self._generation` with `self._runtime` already
cleared. A lock-free status scan in that window read a live parked process as dead, withdrew
its redeemable URLs, and the withdrawal then made `_generation_holds_live_rows` false so the
next sweep killed it. The ordinary incompatible-plan respawn always had this window; the
re-park fixes above *widened* it by adding a second route (an interrupted kill re-parks under
the current number). The equality test now confirms liveness and never denies it, falling
through to the parked list.

Fixing them one at a time is what produced two rounds, so the module now states the
invariant instead: **a mutation is either atomic by construction, or its cleanup runs in a
`finally` / `except BaseException` that re-raises.** In practice that is three shapes —
make the loop await-free so there is no window (the claim), settle in a `finally` that
takes its own fresh snapshot (the session), or re-park what a teardown did not finish and
arm the drain synchronously before any further await (every process path). An AST guard
asserts the functions owning these windows carry a bare-`finally` or `BaseException`
handler rather than only `except Exception`, so the class cannot silently return; the
remaining awaits are covered by a written audit rather than a test, because "this await has
no mutation behind it" is not mechanically checkable.

Three awaits are deliberately left unprotected, and each is a judgement rather than an
oversight: `_dispose_displaced_rows` can be interrupted partway, but the rows are already
evicted and their watchers are token-fenced, so a survivor writes nothing;
`_expire_shared_mints` and `expire_dead_mints` flip a row to `expired` before awaiting its
dispose, which the reaper's next pass re-scans and completes; and the reaper and drain
treat their own cancellation as the signal that a replacement is taking over.
