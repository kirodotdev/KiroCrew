# Connections

Third-party account connections: the provider registry and its tiers, the mint
endpoints a card drives, where credential custody sits, warm prewarming, owner-only
disconnect, and the two launch-gate rungs. The subsystem is
`src/kiro_crew/connections/` (`registry.py`, `mint.py`, `warm.py`, `status.py`,
`ownership.py`, `tool_aliases.py`, `alias_record.py`, `l0_probe.py`, `l0_drift.py`,
`l0_record.py`, `l1_smoke.py`, `tool_test.py`), plus
`dashboard/handlers/connections.py` and `website/src/pages/connections/`.

**Kiro Crew never holds a connection's credential.** kiro-cli owns the OAuth chain
end to end; Kiro Crew observes grant presence by `stat`, and every rule below follows
from that boundary. The credential-boundary detail lives in
[`../architecture/design-notes/mcp-oauth-ownership.md`](../../architecture/design-notes/mcp-oauth-ownership.md).

## Status and cancel

How a Connections card learns whether a provider is actually authorized, what a
Cancel releases, and what the mint audit trail records about each route.

### The two axes a card needs

A card asks two independent questions, and conflating them is what produced the
original defect:

| Axis | Question | Source |
|---|---|---|
| Reachability | does the endpoint answer? | `/api/mcp` (a real kiro-cli handshake) |
| Authorization | does kiro-cli hold a grant? | `/api/connections/status` (this note) |

The status probe carries no OAuth token — kiro-cli owns token custody and Kiro
Crew stores no credential — so a remote OAuth server answers it with 401 and the
gateway reports `needs_auth`. **Two different situations produce that identical
answer**: a provider nobody has authorized, and a provider authorized *outside*
the dashboard, which the runtime calls fine and which raised no `mcp_oauth`
banner here. Reachability alone cannot separate them, which is why a provider
with no token file could wear a Connected badge and an authorized one could sit
on "not verified".

`GET /api/connections/status` answers the authorization axis only. It reports
`grantPresent` — a local, network-free stat of kiro-cli's OAuth artifact
directory (the paired token + registration files; presence only, the bytes are
never opened) — plus `connectedSince`. It runs **no HTTP request**: adding a
second reachability probe would duplicate `/api/mcp` and give the card two
verdicts about the same fact that can disagree.

**Known limit — the badge claims authorization, not liveness.** `grantPresent`
is a statement about kiro-cli's local artifacts, and those artifacts outlive a
revocation performed AT THE PROVIDER: the tokenless reachability probe answers
`needs_auth` either way, so a remotely-revoked grant keeps its Connected badge
until the runtime actually fails a call (or the artifacts are removed locally).
The inverse direction — a grant present but the badge stale-downgraded —
self-heals within one 30-second poll.

The explicit **Test** action is the opt-in liveness check and does not change the
badge's polling contract. Owner-only `POST /api/connections/test` starts a
promptless kiro-cli ACP session, so bearer injection stays inside kiro-cli. Its
native `/mcp` result establishes whether the provider initialized and completed
`tools/list`; native `/tools` then identifies which of that provider's tools the
active agent actually exposes. It returns `usable`, `no_tools`, or `failed` with
a stable `code` and `toolCount`, always as HTTP 200. Invalid request and owner
denials retain their existing non-2xx machine-coded JSON contracts. The action
never calls a provider tool, never reads grant bytes, and does not alter mint,
warm-process, or OAuth-guard state.

Status vocabulary, all judged from local facts:

| `status` | Means |
|---|---|
| `connected` | a grant exists for this provider |
| `awaiting_consent` | no grant, but a mint is in flight (`minting`/`waiting`) |
| `not_connected` | no grant and nothing pending |

`accountLabel` is deliberately **absent**. Kiro Crew never sees a provider
credential, and neither the unauthenticated handshake nor the runtime's
notifications carry an account identity, so there is nothing truthful to report;
inventing a label locally would put an unverified identity on the card.

### The mint contract is preserved, not replaced

`POST /api/connections/mint` and `GET /api/connections/mint?slug=…` keep their
existing contract exactly: the POST reserves a row and returns
`{ok, slug, state, token}`, the GET is the card's authoritative feed for a
card-initiated mint (`idle|minting|waiting|granted|failed|expired`, with
`oauth_url` only while `waiting`), and the frontend keeps polling it at its own
cadence. Approval-URL ownership stays with the mint engine.

A cold mint whose URL is rejected by `oauth_url_contains_credential` disposes
that dedicated process, protected PID, and ephemeral spec, then creates one
fresh dedicated attempt with a new provider OAuth state. The retry keeps the
caller's row token, so the initiating tab continues to own the result. A second
rejection is terminal and surfaces the existing `failed` / `mint_url_rejected`
state; no other failure class retries. Warm URLs pass the same credential gate
before they become adoptable. A rejected warm claim is released, so a later
Connect follows the cold path and reaches this single retry owner rather than
carrying a second policy in the warm engine or dashboard handler.

The status endpoint is **additive** and never mints: it observes the mint table
to distinguish `awaiting_consent` from `not_connected`, and that is the whole of
its relationship to minting.

### connectedSince is source-backed

The timestamp is stamped when a provider is **first observed to hold a grant**,
persisted in `<data home>/connections/connected-since.json`, and forgotten the
moment the grant is gone. Nothing is fabricated at render time.

Two rejected alternatives, and why:

- **kiro-cli's artifact mtime.** A token refresh rewrites it, so the card would
  silently re-date an old connection to the last refresh.
- **Stamping on render.** That invents a clock reading with no lifecycle meaning
  and would restart on every gateway boot.

Pruning happens in the status read rather than in a disconnect path, which keeps
the record self-healing: an entry whose **grant** is gone stops being reported on
the next read, and re-authorizing starts a fresh clock. The trigger is the grant,
not a card action — a local-only Disconnect removes the MCP entry while kiro-cli
keeps the grant, so the timestamp survives and reconnecting continues the original
clock, which matches what the Disconnect copy tells the user it does not do for
them. A read-only home is tolerated — the timestamp is supplementary, so the card
omits the row rather than failing the status read.

**An unreadable grant lookup is not an absence.** `Path.is_file()` swallows
`OSError` and answers `False`, so an EACCES or a stalled mount would otherwise
look identical to a revoked grant and prune a timestamp nothing can reconstruct.
The status module therefore resolves three states — present, absent, and
indeterminate — from ONE stat per paired artifact (via the layout's single
source, `grant_artifact_paths`), never `grant_present()` followed by a
diagnostic re-stat: two passes race, and a transient failure clearing between
them reads as definitive absence. ENOENT-family answers are definitive absence;
any other `OSError` makes that artifact unknowable. The pair combines: either
artifact definitively absent decides the pair, a remaining failed stat makes it
indeterminate. An indeterminate read preserves whatever is stored, stamps
nothing new, reports `grantPresent: false` with `grantIndeterminate: true` and
reason `grant_unreadable`, so no card upgrades an unreadable state into a claim.
`grant_present` itself is unchanged — it is shared with the mint engine, where a
bool is the only sensible answer — and the discrimination lives in the status
module alone. Stats only; no artifact is opened, so token bytes never reach
this path.

**A stamp that cannot persist is not published.** On a read-only home the
sidecar write fails; a freshly stamped connected-since then exists only in this
process's memory, and publishing it would re-date the connection to each poll's
own clock. The reconcile therefore drops unpersisted fresh stamps from its
returned map (loaded entries are persisted truth and stay reportable), and the
card simply omits the row.

**The acted-on observation is SEL-audited.** Stamping a first-connect timestamp
is the credential-store observation this module acts on — it becomes a persisted
record and a Connected badge — so it emits one
`connections_status.oauth_grant_presence` audit event (registered in
`hooks._AUDIT_ONLY_READ_IDS`), mirroring the mint engine's `_grant_observed`
convention: audited on the acted-on transition only (never once per poll
sweep), best-effort rather than fail-closed because the artifacts are stat-ed,
never opened; an SEL outage leaves a warning, not a failed status read. A stamp
that failed to persist is not audited, because nothing was acted on.

### Cancel releases what a mint holds

`POST /api/connections/cancel` disposes the in-flight mint through the mint
engine's ownership API (`cancel_mint`), releasing the dedicated kiro-cli process,
its loopback listener, its protected PID and its ephemeral spec.

Before this, only a cancelled **new** connect did anything server-side, and only
indirectly: the card uninstalled the entry it had just created. A cancelled
**reconnect** or a stateless wait dropped the local wait and left the mint held
until its TTL expired — a real leak for a flow the user had abandoned.

The split of responsibility is deliberate:

- **The endpoint** disposes the mint and touches **no MCP config**.
- **The card** keeps owning the config decision, because it is the only side that
  knows which kind of attempt this was: a cancelled new connect uninstalls the
  entry it created, while a cancelled reconnect keeps the working connection.

`token` fences a stale tab: the mint table is keyed by slug, so a sibling tab
connecting the same provider *replaces* the row, and a cancel carrying its own
row token refuses to dispose a row that is no longer its. A cancel with no token
disposes whatever row is current — a caller that never held a token cannot
distinguish rows, so its intent is only "cancel this provider". The call is
idempotent (`dropped=false` when nothing was live).

The card **does not await** the dispose. Disposal waits on a child process
shutdown, bounded only by the gateway's ~10s shutdown timeout, so awaiting it
would leave Cancel un-actioned and re-clickable for that whole window. The
withdrawal the user asked for is local and happens immediately; the dispose is
fire-and-forget bookkeeping that follows, and its rejection is swallowed so a
gateway failure never surfaces as a Cancel that appeared not to work.

### Mint outcome telemetry

Every `connections_oauth_mint` audit event records the facts of its route
directly: `reason=validated_grant` when an on-disk grant was re-verified and
proven usable without spawning a fresh consent flow, and `url_minted=<bool>` on
a completed spawn — `True` when the dedicated kiro-cli spawn produced an
approval URL, `False` when it found no challenge (an open endpoint, or a grant
that landed concurrently). The route a Connect took is derivable from those
fields; no separate label is emitted. A latency-tier vocabulary belongs to the
warm-runtime seam (a URL already held; an activation on a shared warm process)
and ships with that seam, where its routes exist as code and its consumers
exist as dashboards.

Note that `Provider.tier` in the registry (1–3) is provider *categorization* and
is unrelated to mint latency.

### A grant on disk is not a grant that works

`grant_observed` (the artifact-pair stat both this module and `mint.py` share)
answers "does the pair exist", never "does it still work". The pair survives a
provider-side revoke and a dead refresh token exactly as it survives a live
one, because nothing in that stat asks kiro-cli to actually use it. Reporting a
mint `granted` on presence alone let a Connect click on an already-configured
provider flip the card to Connected instantly, only for the explicit Test
action's real authenticated check to reveal the pair was already dead and the
card to fall back to "not authorized" — a lie the card told for however long it
took the user to notice.

So a Connect or Reconnect mint that finds an existing artifact pair no longer
reports `granted` on that alone. It spawns the identical single-server
ephemeral session a fresh mint would spawn, and asks kiro-cli's own `/mcp` +
`/tools` — the same promptless, model-free command pair the Test button already
uses, classified through the same predicate (`tool_test._classify`) so both
surfaces agree on what "usable" means. A `usable` verdict reports `granted`
with `reason=validated_grant`; anything else — `no_tools`, `failed`, a spawn
that never completes — falls through to the ordinary fresh-mint spawn loop
below rather than returning early. That fallthrough is deliberate: the
validation spawn already proved the existing pair does not answer, so the next
thing Connect should do is exactly what a user clicking it expects — open a
real consent page — rather than surface a coarse error for a mint the button
was never asked to abandon.

**This validation is a mint-time decision, never a poll-time one.** The
30-second status poll (`collect_connection_statuses`) still answers from the
cheap artifact stat alone, because that badge only has to be *eventually*
honest and a per-poll authenticated spawn would turn an idle dashboard tab into
a standing cost with no click behind it. Validation runs exactly once, at the
moment a Connect or Reconnect click asks the mint to decide whether a URL is
needed — the one place the cost is bounded by a real user action and the one
place the answer changes what the click actually does.

### Boot path

Both handlers import the mint engine and the status module **function-locally**.
The gateway imports the dashboard handlers package at boot, and the mint engine
drags in the ACP client, the credential predicate and the PID registry;
`test_the_handlers_package_does_not_import_the_mint_engine` enforces that in a
subprocess, so hoisting either import to module scope turns the suite red.

## Minting and the warm table

Cold mint (`kiro_crew.connections.mint`) spawns one kiro-cli process per provider for one
approval URL: ~7.5s per card. `kiro_crew.connections.warm` serves the whole gallery from one
process, and every rule below answers an observed failure.

**Placement.** Warm engine code remains in `src/kiro_crew/connections/warm.py`; the dashboard
handler adds only endpoint wiring -- `expire_dead_mints` on the status path,
`mintable_providers` plus `warm_mint_all` on the premint path, and `adopt_shared_mint` on the
mint path. The dashboard server registers two lifecycle hooks in both full and headless modes:
a tracked startup task that scavenges dead private generations off-loop without delaying the
listener bind, and a cleanup hook that retires the live runtime. Those hooks import the warm
module lazily, so importing the server alone still does not construct or spawn the engine.

### The handoff: how a premint reaches the click it was minted for

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

**The premint request path.** `POST /api/connections/premint` is fired once on mount by the
Connections page (`premintFiredRef` in `ConnectionsPage.tsx`). The endpoint adds a handler, not
a rule: it scans the mintable candidates off the loop, hands that same list to `warm_mint_all`
so the slugs it reports and the rows the engine claims come from ONE registry read, and answers
without awaiting the activation — warming costs seconds, and a card's verdict is its own mint
state rather than this list. Proactive refresh attaches to the reaper (`_warm_mint_reaper`) when
its dependent slice lands.

**One automatic replacement, single-flight, disarmed by shutdown.** Resilience is owned by the
same reaper that detects process death. An explicit page warm arms ONE automatic replacement
(`arm_supervision`, `_WARM_REARM_ATTEMPTS = 1`). A dead generation reserves one single-flight
re-arm task that reuses the ordinary tri-state candidate scan plus `warm_mint_all` without
granting itself another attempt, so a second death cannot form a restart loop; a present grant
and an unreadable grant cache are both skipped by that scan. Concurrent observers share the one
task rather than each starting their own. Deliberate shutdown disarms recovery, settles the
in-flight task, then retires the process. No provider-level recovery state crosses the API.

**Parked generations are drained, not leaked.** `sweep_retiring` is the only thing that retires
a parked process, and every route to it runs off a new mint — so `_drain_parked_generations`
keeps sweeping until `parked_count` reaches zero, and both routes run it. Without that a parked
process, alive only for a card still mid-consent, would outlive the gateway that spawned it.

**A planned spec is verified twice, because a spec is activated BY NAME.** The writer refuses a
path whose contents this module did not write, which protects the FILE; `_unowned_plan_specs`
then re-verifies off the loop that every planned spec exists *and* is sentinel-owned BEFORE the
runtime is constructed, which protects the SPAWN. The runtime is handed `agent=<fixed name>` and
kiro-cli resolves that name from the project-local agents directory, so without the second check
a refusal would hand kiro-cli a stranger's spec at that name and initialize its `mcpServers`. A
refusal aborts warming entirely and is audited; the cold path still serves every Connect.

**Row identity is an opaque token, never a timestamp.** Claiming returns `{slug: token}` and
both absorb and release verify that per-row token (`_new_mint_token`). `started` is information,
never a fence: it is a `time.monotonic()` reading, and that clock has ~15.6 ms granularity on
Windows, so two Connects for one provider inside a single tick read as the same row and every
token guard keyed on it would fail open. The token is deliberately neither a clock reading nor a
counter — a counter restarts at the same value with the gateway, letting a tab holding a
pre-restart token match a post-restart row.

### Specs are read once at spawn

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

### Tool-alias key shape

`resolve_tool_aliases` de-collides by registry **slug**, keying `@slug/tool`, while a warm spec
mounts under `mcp_server_alias(slug)`. Where the two differ a slug-keyed entry names a server
the spec never mounted, kiro-cli applies no rename, and the collision returns silently, so
`connections_tool_aliases` re-points keys at the mounted alias and leaves the resolver
authoritative over which tools collide. Every registry slug is slash-free today, so this is an
identity map holding the shape contract of the spec we write, not a live defect. **Every**
claimant is renamed and none keeps the bare name, so a slug-collision test must assert that rule
rather than a keep-the-first-claimant one.

### Filesystem work never runs on the loop

Every flow reads the user's config, a private generation tree, the old global agents directory,
or kiro-cli's OAuth cache, any of which can sit on a network mount where a stat is unbounded, so
all of that work lives in SYNCHRONOUS helpers and a coroutine reaches them through
`asyncio.to_thread` -- enforced by a fixed-point drift guard in `test/test_connections_warm.py`
that reuses the mint engine's own primitive sets so the two cannot drift apart. What the guard
pins today is the exact set of helpers doing filesystem work, so the lifecycle slice can neither
call one from a coroutine nor quietly drop the filesystem work the guard's coverage rests on
without failing it.

### Session-handle ownership, made explicit

An implicit handle transfer — a handle moving between "the backend has it", "we have it" and
"the registry has it" with no stated rule about who is responsible when an await in between is
interrupted — is the defect this table exists to prevent. The rule is one sentence: **a handle is
registered before anything can interrupt, and forgotten only once its destroy has completed.**
Every touchpoint:

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
create carries no id we hold, so nothing can address it directly. Retirement of the runtime does
not bound it: any card holding a URL keeps `_shared_mints_pending` true, which resets the
reaper's idle clock on every cycle, while `_ensure_locked`'s digest-equality fast path keeps the
same generation reusable — so each repetition parks another orphan session and its callback
children on ONE live process, unbounded until listener or memory exhaustion.

So the generation is now **quarantined** instead: row 4 clears `_plan` and `_digest`, which
makes the next activation find the resident plan unservable and stand the generation down
through the ordinary path — parked for the drain when a card still needs it, killed outright
otherwise. Either way the process dies and takes the orphan with it. **The bound is therefore
at most one generation's sessions, released on the next activation or on idle retirement,
whichever comes first.** The cost is a respawn (~5s) on the next warm call after a transient
session timeout, which is the right trade: correctness over a warm-cache hit. Closing the
residual entirely would need the backend to expose a session id at request time rather than at
response time.

**Generation-owned spec cleanup closes the file-lifetime residual.** A hard gateway kill may
leave a private generation tree, and that tree publishes no agent mode globally. Its owner
marker carries gateway and runtime PID/start identities; the next gateway removes the tree only
when both are provably dead. Graceful shutdown removes each tree immediately after its process
kill succeeds, while a parked or unkillable process retains its own tree. Sentinel-owned files
from the former global location are cleaned at startup; unsentinelled files remain a documented
manual decision rather than being deleted on a name match.

### Measured latency

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
### Atomicity around an await

A mutation is either atomic by construction, or its cleanup runs in a `finally` /
`except BaseException` that re-raises. An `await` sitting between a state mutation and
that mutation's settlement, guarded only by `except Exception`, is the recurring defect
class here: a `CancelledError` inherits from `BaseException` and walks straight past
such a guard, leaving rows nothing withdraws, parked processes with no sweeper, forked
children, orphan specs, registered sessions with live loopback callback servers, or
generations with no reference left anywhere. Every mutation window in `warm.py` is
written to that rule.


## Disconnect

Removing the MCP entry and nothing else is not a disconnection — it hides one.
kiro-cli's stored grant artifacts would stay on disk, so the next Connect would
find a live refresh token and resume the old grant without asking, while the card
had already told the user this machine's connection was gone.

### Three local things, and one thing that is not ours

`POST /api/connections/disconnect` disposes any in-flight mint (a grant arriving
moments after the user asked for the connection to be gone is not a race worth
keeping), then — in ONE locked transaction — removes the MCP entry from the
scopes that configure this provider and unlinks the stored grant artifacts.

What it deliberately does **not** do is revoke at the provider. Nothing in this
process can, only the provider can, so the response never claims the upstream
grant is dead and the card keeps offering the provider's revoke page. The copy
was already honest about this before the grant was actually being deleted; the
change here is that the behaviour finally matches it. **Cancel** still never
revokes: a cancelled *new* connect suppresses its own feedback, so a shared grant
would vanish silently. Only a deliberate Disconnect touches the credential.

### Why the response carries several answers

The artifacts are a **pair** (token + registration) and either half can fail to
unlink alone, so "the token went" is not the same fact as "the grant is gone",
and neither implies the config entry came out.

| Field | Established |
|---|---|
| `grantRemoved` | at least one artifact was unlinked by this call |
| `grantSurviving` | labels still on disk after an ATTEMPTED unlink, re-stat'd rather than inferred from what the delete loop believed it removed |
| `entryRemoved` | at least one scope configured this provider's endpoint under our slug, and that entry is gone |
| `grantSharedWith` | other entries pointing at the same endpoint, which is why the grant was deliberately kept |
| `grantCensusIncomplete` | a source the decision needed could not be read, so the grant was kept with no sharer to name |

A survivor is the one outcome that must not be rounded up. The card renders it
through `role="alert"` rather than `role="status"` — announced by a screen reader,
and pointing at the provider's revoke page — because a local grant outliving the
click is precisely the state this endpoint exists to prevent.

Only pairs this Disconnect actually **tried** to unlink are re-stat'd, which is
what makes that alert safe to fire on sight. A pair kept on purpose — for a named
sharer, or because the census had a gap — is still on disk *by design*, so
re-stat'ing it reported a correct refusal as a surviving artifact and forced a
precedence ladder to decide which survivors were real. Restricting the re-stat to
attempts deletes the ambiguity instead of ranking it: `grantSurviving` now means
failed unlink and nothing else, the audit needs no `or grant_shared_with` escape,
and the card's clause order carries no hidden claim.

### A grant is keyed by `grant_key`, so it is not always ours to delete

`grant_key` is a sha256 over origin + path, query dropped and path kept verbatim.
**One artifact pair therefore serves every entry whose URL hashes to that key**,
whatever those entries are called. That makes the revoke a wider act than the
purge beside it, and it needs its own ownership question, asked with the
credential's OWN identity function:

- Entry identity is `normalized_endpoint` on name **and** url — the pair the card
  matches on. It keeps the query (a query can select a different server) and
  strips a trailing slash.
- Grant identity is `grant_key` equality, because the artifacts are files *named
  by* `grant_key`. The two disagree in both directions: a `?workspace=` variant is
  a different endpoint but the same pair; a trailing-slash variant is the same
  endpoint but a different pair. Testing the credential with the endpoint
  comparator would delete a shared grant in the first case, and in the second
  strand a live one — reported as a deliberate keep.

The sweep reads the **raw specs**, disabled entries included (a switched-off
server still owns its grant), with the probe view unioned in, so neither
disabling an entry nor holding it only in agent config makes its grant deletable.

### One lock, one census

The destructive acts share one judgement, so they share one read and one lock
hold. Splitting them produced three data-loss paths of the same shape — ownership
decided over an incomplete census, or acted on outside the lock that judged it:

- **The census was incomplete.** Discovery merges exactly one agent spec
  (`kirocrew.json`), so a spec the user wrote by hand, or one an app materialized,
  was invisible — while kiro-cli authorizes from it and shares the pair the
  endpoint names. `spec_census` globs the whole agents directory alongside the
  `mcp.json` scopes, precisely because the specs that matter are the ones Kiro
  Crew does *not* own. An agent spec contributes sharers but is never a purge
  target: Kiro Crew does not own a user's agent file.
- **The purge was wider than the judgement.** Ownership was read from the merged
  winner while `_purge_server_config` removed the name from *every* scope, so a
  same-named entry in a lower-priority scope pointing elsewhere was deleted
  unseen. The purge now takes the scopes ownership matched
  (`_purge_server_config(scopes=...)`). Passing `None` still means every scope,
  which is what an uninstall wants — there the *name* is going away, not one
  endpoint under it.
- **The revoke ran after the lock released**, so an entry created at the same
  endpoint in that window lost a grant it had not yet used. Holding the lock
  across two `unlink` calls under the user's home is the accepted cost, and it is
  the exposure every other writer under this lock already has.

Both the purge and the revoke go through `_offload_config_write`, not a bare
`to_thread`: a cancelled request task would otherwise release the lock while the
worker is still working, letting a concurrent purge interleave with a stale
snapshot or reopening the revoke window this transaction closes.

**Fail closed, asymmetrically**, because the two acts need opposite evidence. The
revoke needs the *absence* of a sharer, which an unreadable source can hide, so
one unreadable source keeps the grant and says so — and "unreadable" covers every
shape that means *entries unknown*: unreadable bytes, malformed JSON, a directory
where a file should be (the unenumerable-agents-dir sentinel), and structurally
invalid documents (`[]`, `{"mcpServers": []}`). Only genuine absence and a
document that declares no entries read as empty. The purge acts only on
*positive* evidence — a scope it read and matched — so an unreadable scope is
simply not purged; refusing the whole request would leave the user unable to
disconnect because of a file with nothing to do with this provider. This is also
why `spec_census` does not call `_load_mcp_json_by_source`: that function warns
and skips an unreadable source, which is right for a view and wrong for a
destructive decision.

A sibling URL outside the provable set (non-ASCII or percent-carrying host,
dot-segment or non-ASCII path) fails closed as census-incomplete — see the
identity section. *Within* the provable set, a host Python's IDNA still refuses
(empty or >63-char label) is skipped rather than kept: kiro-cli serializes such
a host verbatim, it differs from the registry host, so its key provably is not
ours — and failing closed on it would let one junk line block every disconnect.
Enumeration of the agents dir goes through `os.listdir`, not `Path.glob`,
because glob suppresses scan errors: an executable-but-unlistable directory
yields zero entries with no raise, reading a hidden sharer as absent. A missing
directory (fresh machine) stays genuine absence.

Disconnect is **owner-only**, checked before the request is even parsed: this
endpoint deletes machine-global config and OAuth artifacts, the same server-side
boundary every mutating agents route enforces, and presigned dashboard links admit
non-owner subjects.

**The raw census is authoritative; the probe view does not get a second vote.**
`list_servers` is a merged read whose sources include the rendered agent config,
so a mirrored entry reappears there as a plain row with its provenance lost.
Judged again, a mirrored same-name query variant (same artifact key, different
endpoint) reads as an independent sharer and blocks the revoke while the purge
removes both entries — leaving the credential behind. So a configured row whose
`(name, url)` the raw census already carries is skipped: it has been judged once,
with provenance. A row the census does *not* carry still votes, so a source the
census misses can never lose a real sharer.

Residuals, stated rather than papered over: the handler awaits `cancel_mint`first, so a wedged teardown can keep a Disconnect busy for its shutdown timeout
(firing it as a task would let a grant arrive after the user disowned the
connection); the census reads the per-project `.kiro/agents` dirs of every OPEN
chat slot as well as the user-level one, so a project with no open slot is still
invisible (slot state is what the dashboard actually knows about the checkouts
kiro-cli runs in); and the transaction lock serializes
the gateway's own writers (the mcp.json writers and, as of this change, the
agent-config PUT) but cannot serialize *external* writers — kiro-cli and hand
edits mutate the same census sources from outside the process, and a write of
theirs landing between the census read and the unlink is judged by a snapshot
that never saw it. No gateway-side lock can close that; it is inherent to
config files the gateway does not own.

Two sibling paths are knowingly left alone: removing the same server through the
plain MCP surface (`api_mcp_apply` → `_purge_server_config`), and removing it
from the Kiro global config through the REST handler
(`DELETE /api/mcp/servers/{name}`, which rewrites only that one file), both take
the entry out with no grant census and leave the grant pair on disk, so an
OAuth'd server uninstalled either way keeps a usable refresh token. The deferral
is deliberate rather than an oversight — nothing on either surface claims the
authorization was revoked, so neither produces the dishonesty this note exists
to remove, and revoking there would need the same ownership census this endpoint
runs, on paths whose callers include bulk edits that were never a withdrawal of
consent.

The rebuild runs **inside** this transaction, through the shielded offload. A
post-lock rebuild snapshots the config before a concurrent Disconnect's purge and
can write last, resurrecting an entry whose grant that transaction just deleted —
a configured provider with a dead credential, reachable with two dashboard tabs.
Nesting it is safe for a reason worth recording, because the opposite was assumed
once and was wrong: this endpoint holds the `~/.kiro/settings/mcp.lock` sidecar,
while `rebuild_agent_config`'s internal lock defaults to
`~/.kiro/agents/kirocrew.lock` (`apps/bridges._mcp_lock`). They are different
flocks, so the non-reentrancy that would otherwise deadlock never applies.

### Identity is the endpoint, never the entry name

The card matches a provider to an entry on **name and url** together
(`connectionProviderForServer`), and the purge honours the same pair. Removing by
name alone would delete a user's own server that merely happens to be called
`notion` because they clicked Disconnect on the Notion card. This is the rule
`l1_smoke` already keeps: a registry slug is a label a caller can collide with,
while an endpoint is the thing being talked to. When no scope configures our slug
at this endpoint, `entryRemoved` is false and nothing is purged — but the grant is
still ours to revoke, because it is keyed on the registry URL rather than on
whatever `mcp.json` currently holds.

The census judges ownership *first* and sharer-tests everything that is not ours
— including entries carrying our own name. A `notion` entry at a query variant
holds the same artifact pair (`grant_key` drops the query) while failing the
endpoint test, and a same-named agent-spec entry is never a purge target but
holds a grant like any other; keying the branches on the name let both fall
through the census entirely, which is how a same-named survivor lost its grant.

Two implementations compute the artifact key — this module and kiro-cli's WHATWG
url parser, which percent-decodes hostnames, IDNA-maps Unicode hosts and
normalizes dot-segments before hashing. So key equality is asserted only inside
the **provable set** (lowercase-ASCII LDH hosts, printable-ASCII paths free of
`%`, backslashes and dot-segments), where both serializations are byte-identical
by construction; any URL outside it is *unprovable* and fails closed as a
census-incomplete keep. `%6dcp.notion.com`, `/a/../mcp` and `/a\..\mcp` all name
the registry pair under WHATWG while hashing elsewhere here — the last one
because WHATWG folds `\` to `/` *before* removing the dot segment, while
`path.split("/")` sees one opaque segment and the dot-segment guard never fires,
so the backslash has to be excluded on its own. Guessing on either side of that
divergence is how a live consent dies. And because ownership is
endpoint-keyed (slash-insensitive) while the pair is artifact-keyed
(slash-sensitive), **every owned key's pair is judged and revoked separately**: an
owned entry at `<url>/` holds its own pair, and revoking only the registry key
would purge the entry while its real credential survives behind "Disconnected
locally". Sharers are therefore tracked **per key** — a sharer of the registry
pair says nothing about an owned trailing-slash pair nobody else uses, and one
flat flag skipped that pair's revoke while the purge still removed its entry.

**A mirror is not a sharer.** `_purge_server_config` strips the entry from the
rendered `<agents>/kirocrew.json` and from every `scope.agent_mcp_file` itself, so
those entries are reflections of the scopes this transaction is purging rather
than independent grant holders — counting them made an ordinary Disconnect see its
own reflection as a sharer and skip the revoke *every time*, which is the failure
mode where the feature silently never fires and no census-mocking test can see it.
A hand-written agent spec is the opposite: Kiro Crew never writes it, so its entry
keeps its own grant and must be able to block a revoke. The exclusion is scoped to
the justification — with nothing owned the purge does not run, so a mirrored entry
is a real holder again and blocks normally.
The census-incomplete user copy says a source "could not be read";
for an unprovable entry URL that is a slight imprecision (the file was readable,
the entry could not be safely compared) accepted to avoid a 13-bundle string
change — the essential claim, that another user of the grant could not be ruled
out, is exact.

### The credential boundary

The artifacts are stat'd and unlinked, never opened. kiro-cli owns the OAuth
chain and its store ([mcp-oauth-ownership.md](../../architecture/design-notes/mcp-oauth-ownership.md)); the
gateway may observe and delete, never read. A regression test pins it by making
`open`, `read_text` and `read_bytes` raise for the duration of a revoke. Every
one of those stats runs off the event loop, because they touch paths under the
user's home and stall as long as a network mount does. `surviving_grant_artifacts`
keeps `artifact_presence`'s three-valued answer rather than collapsing it: an
unreadable artifact counts as surviving, since reporting it gone would claim this
machine's connection is dead while a usable refresh token may still be there.

The single-file `{sha256}.json` form that shares the cache directory belongs to
AWS SSO and is deliberately never touched.

### Deferred: proving the grant still works

Upgrading **Test** from an MCP-level probe to an authenticated round-trip is not
here. It needs a provider HTTP probe and a real runtime activation (kiro-cli holds
the bearer), plus a verdict vocabulary reconciled against the status enum that
shipped with the tiers note. Test is not broken today; it performs a real probe,
just a shallower one than its name suggests. Splitting it keeps the
security-relevant fix from waiting on the expensive one.

## Launch gates: L0 and L1

A visible Connect card is a promise the flow works; each rung of the launch
ladder asserts something the rung below structurally cannot:

| Rung | Where | Needs an account? | Asserts |
|---|---|---|---|
| **L0** | `connections-l0.yml`, nightly, every branch | no | the provider's PUBLIC OAuth metadata still matches the committed `l0_expectations` |
| **L1** | `connections-l1.yml`, scheduled, opt-in box | yes, one human click per provider, once | a grant that exists still works against the live endpoint |
| **L2** | manual, at the flag-flip gate | yes | the UI walk-through a human has to actually see (see the Connections manual test SOP) |

L0 never authenticates, so it cannot prove a *connection*; L2 costs a human
every time. L1 sits between: one consent click, then automated.

### What a green L1 run actually proves

**Kiro Crew holds no token.** kiro-cli owns the OAuth chain and injects the
bearer inside its own process ([mcp-oauth-ownership.md](../../architecture/design-notes/mcp-oauth-ownership.md)),
so an exchange opened from the harness carries no credential for a managed
provider, and the live endpoint answers with an OAuth challenge -- exactly as it
answers the dashboard's probe. The verdict vocabulary is built around that fact:

| Verdict | Green? | Established |
|---|---|---|
| `PASS` | yes | the exchange ran through `initialize` and `tools/list` non-error, and the registry `smoke_fixture` tool is still advertised. Reachable for an entry that carries its own credential, or an unprotected server |
| `GRANT_HELD` | yes | the grant is still on disk **and** the endpoint is reachable and still answers a well-formed challenge. Does **not** prove a tool call would succeed |
| `NEEDS_RECONSENT` | no | a credential this process presented was refused, or an authorization error came back mid-exchange. A human must re-approve |
| `FAIL` | no | reached and wrong, or unreachable: non-2xx with no challenge, 5xx, timeout, transport error, broken `tools/list`, fixture tool no longer advertised |
| `SKIPPED` | yes | not a configured MCP server here, no grant for it, or the entry's endpoint does not match the registry `mcp_url`. **L1 never initiates consent** |

Each row also carries `depth` (`tools_list` / `challenge` / `none`), so how far
the exchange got is never inferred.

### The sweep never calls a tool

It stops at `tools/list`. A `tools/call` issued from here would reach the
provider outside the governed dispatch path, so a tool an enterprise policy
denies would be invoked anyway and no SEL event would record it — an unaudited
side channel is a worse outcome than a narrower verdict. The registry's
`smoke_fixture` is therefore used for its NAME only (`tools/list` already
answers whether it is still advertised); exercising it belongs to the ACP-side
slice below, where kiro-cli's own governed, audited dispatch owns the call.

### Runbook for a failing lane

| Symptom | What it means, what to do |
|---|---|
| `vacuous`, every provider `SKIPPED` | No grant is seeded, and seeding IS the one-time consent click: on the box, open Connections and click Connect per provider. Lower `--min-exercised` only if you mean "cover fewer providers" |
| `NEEDS_RECONSENT` | The grant is spent (expired refresh token, or revoked upstream). A human re-approves on the card |
| `FAIL` | Usually a provider-side change to its MCP surface: read their changelog before editing our registry |
| "exceeded its total timeout" | The provider outlasted its whole budget, not one request's. Raise `--timeout` only for a known-slow provider; otherwise treat as `FAIL` -- a session cannot get a tool out of it either |

### The decision that shaped this: a challenge is not a failure

A tokenless 401 is what a **healthy** authorized provider returns under runtime
custody, so grading it `NEEDS_RECONSENT` would hold the lane permanently red, and
a lane nobody believes is worse than no lane. The challenge is therefore its own
verdict, and `NEEDS_RECONSENT` means an attributable rejection. A tokenless `403` with no `WWW-Authenticate` grades
`FAIL`, not `NEEDS_RECONSENT` — an edge proxy or geo block reads identically,
and a consent verdict would send a human to re-approve a healthy grant.

### A run that exercised nothing is not green

With no seeded grants every provider is `SKIPPED` and the aggregate would be a
cheerful `ok` establishing nothing. `--min-exercised N` (the lane passes `1`)
makes that `vacuous`, nonzero. `GRANT_HELD` counts as exercised; `SKIPPED` not.

### Grant presence is observed, never read

`grant_present` aliases `mcp_grant.grant_presence` rather than copying it —
this slice's dedupe obligation, pinned by identity in the tests; the key
formula and cache-dir resolution are pinned transitively, since it derives
both internally (a drifted copy grades a live connection `SKIPPED` while the
card says connected). Presence is a `stat` of the paired
`{sha256}.token.json` + `{sha256}.registration.json` artifacts and opens
neither, so no token byte can enter the process, report, or a log line; the
single-file `{sha256}.json` SSO form is deliberately not consulted, and a test
fixture makes any *open* of either artifact an outright failure. L1 calls the
synchronous presence helper and emits no SEL read audit, unlike mint's use of
`grant_observed`: that covers a Connect flow acting for a remote caller, whereas
this is an operator's own CLI, `l0_probe`'s class.

### Authenticated enumeration belongs to the on-demand Test action

The scheduled L1 sweep remains deliberately tokenless: it has no interactive
agent session, so a runtime-custody provider still tops out at `GRANT_HELD` and
stops before `tools/list`. That verdict continues to mean only that a grant
artifact exists and the live endpoint still issues a valid challenge.

The Connections card's owner-only `POST /api/connections/test` closes the
listing gap through the runtime instead of widening this harness. It starts a
promptless kiro-cli ACP session under the real `kirocrew` agent and executes two
native commands, with no model turn:

1. `/mcp` reads kiro-cli's MCP-manager status and per-server tool count. A
   `running` row means kiro-cli authenticated the server and completed the
   provider's `tools/list`; loading, failed, disabled, missing, or malformed
   rows are failures rather than empty tool sets.
2. `/tools` reads the final agent-exposed inventory. Only rows whose source is
   `mcp:<provider alias>` count, so a server that advertises tools but is not
   mounted by the agent is honestly reported as exposing none.

The 200 response is always one of three verdicts, each with a stable `code` and
`toolCount`: `usable` (`tools_available`, count > 0), `no_tools`
(`no_tools_exposed`, count 0), or `failed` (a specific runtime/inventory code,
count 0). The path returns no tool descriptions or credential material and,
like L1, never issues `tools/call`; provider dispatch remains exclusively on the
governed, audited agent path.

### Running it by hand

`python3 -m kiro_crew.connections.l1_smoke --report /tmp/l1.json` (under a
pipx/venv install, use that environment's interpreter). `--min-exercised 1`
reproduces the lane's gate; `--concurrency`/`--timeout` are in `--help`.
