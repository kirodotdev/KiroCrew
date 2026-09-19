# Connections

Third-party account connections: the provider registry and its tiers, the mint
endpoints a card drives, where credential custody sits, warm prewarming, owner-only
disconnect, and the two launch-gate rungs. The subsystem is
`src/kiro_crew/connections/` (`registry.py`, `mint.py`, `warm.py`, `status.py`,
`ownership.py`, `tool_aliases.py`, `alias_record.py`, `l0_probe.py`, `l0_drift.py`,
`l0_record.py`, `l1_smoke.py`, `tool_test.py`), plus
`dashboard/handlers/connections.py` and `website/src/pages/connections/`.

The `src/kiro_crew/connections/vendors/zoom/` subtree is a separate connector-campaign slice with its own owning spec — [connector-zoom.md](connector-zoom.md) (`W11-A` Zoom contract semantics) — not this subsystem's OAuth-grant plumbing.

**Kiro Crew never holds a connection's credential.** kiro-cli owns the OAuth chain
end to end; Kiro Crew observes grant presence by `stat`, and every rule below follows
from that boundary. The credential-boundary detail lives in
[`../architecture/design-notes/mcp-oauth-ownership.md`](../../architecture/design-notes/mcp-oauth-ownership.md).

One thing that looks like an exception is not one. A provider whose MCP server
refuses dynamic client registration (`auth.mode: "preregistered"` in the
registry — GitHub and Asana today) needs an OAuth **app** an operator registered
in the vendor console, and a confidential app comes with a `client_secret`. That
secret is the operator's *application* credential, not any user's grant: the
grant still lives with kiro-cli and nothing here reads it. Kiro Crew keeps the
secret in the encrypted vault (`CONNECTIONS_<SLUG>_CLIENT_SECRET`) as the source
of truth and writes it into the emitted agent spec as `oauth.clientSecret` only
because that file is the one thing kiro-cli reads — **the same footing the
existing `headers` secrets have: vault is truth, the spec is a projection.** See
"Pre-registered OAuth clients" below.

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

**Static-header entries: the reachability probe presents what the runtime
presents.** A remote entry may authorize with a static header whose value
carries a `${VAR}`/`${env:VAR}` reference — a documented config form kiro-cli
resolves at session runtime. The `/api/mcp` probe resolves those references
through the gateway rewriter's declared-env expander
(`mcp_gateway.rewriter._expand_env_placeholders`): same regex, same
credential-filtered source view (`is_secret_env_key`, `is_credential_env_key`
and `scrub_agent_denied_env` names are misses), and an unresolved reference is
left literal for kiro-cli parity. So a header that authenticates in a session
authenticates in the probe. A still-unresolved reference is **not a supplied
credential**: a 401 against it reports `needs_auth` rather than a
rejected-credential error whose remediation advice would be to delete a working
header. Probe-error redaction keys on the values the probe actually sent —
including each individually resolved placeholder value, since a partially
expanded header (`${TOKEN}${MISSING}`) sends a value a server can echo only a
fragment of — so an error echoing a *resolved* secret is scrubbed by the
exact-value layer, not left to the generic credential-shaped scanners alone.

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

## The registry roster

Every entry in `src/kiro_crew/connections/registry.json`, what stands between it
and the Connect grid, and the category it fills. `category` is a closed
vocabulary (`registry.PROVIDER_CATEGORIES`) so the coverage diff against the
ChatGPT and Claude connector directories is bucket-for-bucket; nothing mints,
mounts or gates on it.

| Provider | Category | Tier | Auth (DCR / PKCE) | Read-only shape | Grid | What the gate still needs |
|---|---|---|---|---|---|---|
| Notion | collaboration-docs | 1 | yes / yes | none (one combined grant) | shown | — |
| Linear | project-management | 1 | yes / yes | dedicated `/mcp/readonly` endpoint | shown | — |
| Atlassian (Jira, Confluence) | project-management | 1 | yes / yes | trim on the consent page | shown | — |
| Stripe | payments-finance | 1 | yes / yes | tool-level | shown | — |
| Vercel | developer-tools | 1 | yes / yes | none | shown | — |
| GitLab | developer-tools | 1 | yes / yes | none (single `mcp` scope) | shown | — |
| GitHub | developer-tools | 2 | **no** / yes — pre-registered client (`auth.mode`) | read-only server variant | shown as "needs configuration" until an operator enters a client; Connect after | manual launch-gate check with an operator-registered app ([runbook](../../guides/oauth-app-registration/github.md)) |
| Superhuman Mail | calendar-email | 1 | yes / yes | none | gated | logged-in revoke-surface check |
| Sentry | developer-tools | 2 | yes / yes | grant only the `inspect` + `docs` skills | gated | manual launch-gate check (L2 SOP) |
| Supabase | developer-tools | 2 | yes / yes | installs `?read_only=true` | gated | manual launch-gate check |
| Airtable | data-analytics | 2 | yes / yes | decline the `*:write` scopes at consent | gated | manual launch-gate check |
| PayPal | payments-finance | 2 | yes / yes | none — live endpoint moves money | gated | manual launch-gate check; sandbox first |
| Asana | project-management | 2 | **no** / yes — pre-registered client (`auth.mode`) | none (single `default` scope) | shown as "needs configuration" until an operator enters a client; Connect after | manual launch-gate check with an operator-registered app ([runbook](../../guides/oauth-app-registration/asana.md)) |
| Figma | design | 3 | yes / yes | Dev seat is read-only outside drafts | hidden | Figma admits clients from its MCP Catalog waitlist |
| Canva | design | 3 | yes / yes | none | hidden | Canva allow-lists the redirect URI per client |
| Dropbox | file-storage | 3 | yes / yes | `/dash` search server (Dash plan) | hidden | Dropbox honours DCR only for its trusted-client list |
| Miro | design | 2 | yes / yes | none — `boards:read` advertised but consent cannot drop `boards:write` | gated | manual launch-gate check |
| Webflow | design | 2 | yes / yes | none (per-tool grant, each tool bundles read and write) | gated | manual launch-gate check |
| Netlify | developer-tools | 2 | yes / yes | none — `read` scope is not enforced by the broker; call only `*-services-reader` | gated | manual launch-gate check |
| Amplitude | data-analytics | 2 | yes / yes | request `mcp:read` only | gated | manual launch-gate check |
| Mixpanel | data-analytics | 2 | yes / yes | none (analysis scopes only; role-limited) | gated | manual launch-gate check |
| Cloudflare (Workers Bindings) | developer-tools | 2 | yes / yes | none — fixed `workers:write` + `d1:write`; `observability` host is the read-only sibling | gated | manual launch-gate check |
| Hugging Face | developer-tools | 2 | yes / yes | request `read-mcp` only; installs `?login` so the server demands a grant | gated | manual launch-gate check; L1 must not read an anonymous 200 as a held grant |
| Zapier | developer-tools | 2 | yes / yes | manual server configuration at mcp.zapier.com | gated | manual launch-gate check |
| Square | payments-finance | 3 | yes / yes | tick `*_READ` permissions only at consent | hidden | Square admits MCP clients from an allowlist (developer-forum request) |
| Postman | developer-tools | 2 | yes / yes | installs `/minimal` (no delete tools); no scope picker | gated | manual launch-gate check |
| Neon | developer-tools | 2 | yes / yes | installs `?readonly=true` | gated | manual launch-gate check |
| Prisma | developer-tools | 2 | yes / yes | none (only `workspace:admin`) | gated | manual launch-gate check |

Tier is provider *categorization* (see the tiers note above), never mint
latency: tier 3 means the vendor gates clients by allowlist or waitlist, so
`vendor_approval_pending` is true and the loader refuses `launch_gate_passed`
for it; the entry exists so the nightly L0 probe watches the metadata and the
banner allowlist stays registry-derived while the admission is pursued.

### How an entry gets in, and what each rung buys

1. **Industry baseline.** The candidate is listed by at least one of the two
   connector directories, ChatGPT's or Claude's. Being listed by both, or
   filling a category the registry has none of, is what orders the backlog,
   not what admits an entry: a single-directory listing is a sufficient floor
   because rungs 2–4 hold the entry launch-gated, so registering it changes
   nothing a user sees until rung 5. The comparison lives in the research note
   `research/connectors-industry-baseline.md` in the operator workspace, not in
   this tree; this section records only the outcome. Of the roster above,
   Zapier, Postman, Neon, Prisma and Square are single-directory listings; the
   rest are listed by both.
2. **Public remote MCP with OAuth discovery.** The vendor hosts the server and
   publishes RFC 9728 protected-resource metadata naming an RFC 8414 issuer.
   Without that the L0 probe has nothing to assert and the entry cannot exist.
   A vendor that publishes the metadata but refuses dynamic client
   registration (GitHub, Asana, Google Workspace, Slack, HubSpot, Box) clears
   this rung as a **pre-registered** entry — see "Pre-registered OAuth clients"
   below; a vendor with no fixed public endpoint at all (Microsoft 365,
   Snowflake, Databricks) still fails it and would need a Kiro-built
   connector, which is a different product decision.
3. **L0 green in record mode, then strict mode.** `l0_probe --record` captures
   DCR, PKCE and the issuer; a strict run must then pass with those values
   committed. Issuers are compared as exact strings (RFC 8414 §2) with exactly
   one equivalence, `l0_probe.same_issuer`: an issuer whose path is empty
   equals the same issuer with path `/` (RFC 3986 §6.2.3), which is the
   root-slash disagreement Google and Box publish between their PRM and AS
   documents. A trailing slash after a **non-empty** path (`/tenant/` vs
   `/tenant`) remains a mismatch by design and is pinned by tests — that is the
   realm-substitution boundary the strict comparison exists for. Pre-registered
   entries record the vendor's DCR value but are not held to it.
4. **Banner allowlist.** The issuer's `authorization_endpoint` is added to
   `security.exfil._OAUTH_AUTHORIZATION_ENDPOINTS` and the consent-URL corpus,
   or the fail-closed banner blocks every connect.
5. **Manual launch gate (L2).** A human walks Connect → consent → Connected →
   Test → Disconnect on a pod and records the revoke surface; only then does
   `launch_gate_passed` flip and the card ship.

Rungs 1–4 are what this roster's gated entries have; rung 5 is what they wait
on.

## What a card shows inline, and what it hides behind the triangle

A Connections card is one cell of a grid, so every line it renders is paid by
every card that reaches the same state. The card therefore has exactly three
kinds of content, and a new state or a new piece of copy lands in one of them:

| Content | Where it renders | Examples |
|---|---|---|
| The state, in a word | the header badge (icon + label) | Not connected, Needs configuration, Connected |
| A **pre-action caveat** — what the user should know before pressing the row's action, or what pressing it entails | the amber warning triangle (`PrerequisiteTip`) placed immediately before that action, in the same row: hover or focus previews the copy, click pins it, Escape or an outside press dismisses it | GitLab's Duo/group requirement and Atlassian's site requirement beside **Connect**; the one-time OAuth-app setup explanation beside **Configure OAuth app** |
| A **verdict the user must act on**, or a form the state needs | an inline band, the only thing allowed to add a row | the not-verified / not-authorized verdict, the needs-attention diagnosis, the return-address relay while waiting for approval |

The line between the last two is what the copy is *about*. A verdict reports a
fact about the card's current state; a caveat annotates a button. Copy phrased
as "X needs …", "Y opens …", "requires …", "before you …" is a caveat and goes
in the triangle however important it feels — importance is why it sits next to
the action the user is about to press, not a reason to grow the card. One
triangle per action row; two caveats for one action merge into one bubble.

This was decided by trying the alternative: the prerequisite warnings were
built as always-visible bands while under review and were reverted to the
triangle on the maintainer's ruling before they merged, so the bands never
reached `main`. The needs-configuration state later shipped a band for its
setup explanation and was moved behind the triangle the same way. A
review-lane suggestion to "make the warning visible" is answered by this
section and by `website/AUTOSDE.yaml`'s blocking `connection-card-caveat-tip`
rule, which names the same three kinds; the frontend test that renders the
needs-configuration card pins the explanation as absent from the document
until the triangle is hovered.

The triangle's copy comes from two places. A provider-side prerequisite is the
registry's `prerequisite_copy` (English fallback) rendered through the
slug-keyed `prerequisite_<slug>` catalog entries, kept in lockstep by a test;
a state-level caveat, like the setup explanation, is an ordinary catalog key
(`needs_configuration_help`). Both ride under the one `before_you_connect`
heading with the `prerequisites_for_provider` accessible name — a new state
reuses them rather than adding a second tooltip component or heading.

## Pre-registered OAuth clients

Most providers let kiro-cli register a public OAuth client at runtime (RFC 7591),
so nothing about the client exists before the first Connect. A provider that
refuses that carries an `auth` block in the registry:

```json
"auth": {"mode": "preregistered", "confidential": true, "redirect_host": "127.0.0.1",
         "redirect_port": 48101, "registration_guide": "oauth-app-registration/github.md"}
```

`registry.py` validates the block (`_validate_auth`): the mode vocabulary is
closed, a `dcr` block carries nothing else, a `preregistered` block names
`confidential`, a unique `redirect_port` in 1024–49151, an optional
`redirect_host` of `127.0.0.1` (default, RFC 8252 §7.3) or `localhost` (for a
vendor whose HTTPS exemption names only that host — HubSpot refuses IP
literals), and a runbook under `docs/guides/oauth-app-registration/`. The
callback path is the constant `CALLBACK_PATH = "/callback"`, so
`registry.redirect_uri(provider)` is the one string both the runbook prints and
the runtime pins: `http://<host>:<port>/callback`. A shipped `redirect_host` /
`redirect_port` is immutable: every operator who followed the runbook has
registered that exact URI in a vendor console Kiro Crew cannot reach, so changing
it silently breaks each of their apps until they re-register. Retire a port by
adding a provider, never by renumbering one;
`test_connections_registry_auth.py` pins the shipped values so a change fails
loudly.

**Where the client lives.** `connections/oauth_clients.py` resolves it with a
fixed precedence — environment
(`KIROCREW_CONNECTIONS_<SLUG>_CLIENT_ID` / `_CLIENT_SECRET`, the container and
CI shape) over `config.json` (`connections.oauth_clients.<slug>.client_id`, the
public half) and the vault (`CONNECTIONS_<SLUG>_CLIENT_SECRET`, the secret
half), with the registry's own `client_id` as a last-resort default for the id
only. `resolve_oauth_client` answers `None` when what is stored cannot attempt
an authorization (no id, or a confidential client without a secret), and
`oauth_client_view` is the dashboard shape, which by construction never carries
a secret value.

**How it reaches kiro-cli.** kiro-cli's remote-MCP `oauth` block accepts
`clientId`, `clientSecret` (when both are set DCR is skipped and the secret is
presented at the token endpoint) and `redirectUri` (pins the loopback host, port
and path). `agent._apply_operator_oauth_client` writes the three at agent-spec
emission for a server whose name is the provider slug **and** whose URL is the
registry `mcp_url` (`provider_for_server` — name alone would land an operator's
client on a hand-authored stranger); `warm._registry_server_entry` does the same
for the premint path and answers `None` for an unconfigured provider so nothing
warms against a vendor that will only say "unknown client". The mint spec copies
the emitted entry verbatim, so the cold path inherits it.

**What the user sees.** `get_visible_providers` shows a pre-registered entry
regardless of `launch_gate_passed` (vendor approval still hides), because until
a client exists the card is an instruction, not an offer: `/api/connections/status`
marks the row `needsClientConfig` (only while no grant is held), and the card
renders the sixth state `needs-configuration` — a lock badge, the Documentation
link, and a **Configure OAuth app** route to **Settings → OAuth Apps** in place
of Connect. What the setup involves (a guided settings page, nothing changes
until saved, removable later, registers an app and enters its client ID and
secret) is the row's pre-action caveat, so it sits behind the amber triangle
beside that route, never as a band — see [What a card shows
inline](#what-a-card-shows-inline-and-what-it-hides-behind-the-triangle). On
the Settings tab one card per pre-registered provider carries the redirect URI
to copy, the Client ID field, the Client secret field (write-only, vault-backed)
and the runbook link. The routes behind it are
`GET /api/connections/oauth-clients` (any dashboard user; no secret value) and
owner-only `PUT` / `DELETE /api/connections/oauth-clients/{slug}`. The launch
gate keeps governing the entry's quality claims: a configured GitHub still runs
the normal first-connect (`not-verified`) flow, and `launch_gate_passed` flips
only after the manual L2 walk with an operator-registered app.

**Runbooks.** One per provider under `docs/guides/oauth-app-registration/`
(index in its `README.md`): console, app type, scopes, the exact redirect URI,
review or publishing requirements, and where the result goes. Registering the
app is the operator's action; the tree only ships the instructions. Microsoft
365 has a runbook and no entry — there is no fixed public endpoint to probe.

## The shared control-plane seam (W01)

`connections/control_plane/` is the single, shared, typed seam every provider
stream (`W02`..`W14`) dispatches a connector operation through. It exists so the
vocabulary a downstream manifest entry declares (`operation_kind`, `effect`,
`service_id`, its auth mode) and the vocabulary a runtime dispatch switches on
are ONE set of constants, defined once, instead of each stream re-deriving its
own and drifting. It is pure types with zero IO: descriptors and envelopes, no
client, no token, no network.

Four typed pieces, in the `TypedDict` + module-level schema-version shape the
rest of this subsystem uses (`l0_probe.ProbeResult`, `l1_smoke.SmokeResult`,
`status.ConnectionStatus`):

| Module | Carries |
|---|---|
| `operation.py` | `OperationDescriptor` — what an operation *is*: `operation_id`, `service_id`, `operation_kind`, `effect`, and `credential_modes`. THREE enums — `operation_kind` / `effect` / `service_id` — are copied VERBATIM from the closed sets in [connector-capability-manifest.md](connector-capability-manifest.md); that spec owns them, and this seam neither invents a value nor drops one. `credential_modes` is not a new axis: it IS the manifest's per-operation `auth_modes` array, named as the shared `CredentialMode` set. It is PLURAL — the *set of modes the operation permits* — because the manifest defines `auth_modes` as an array and a real operation (W02's GitHub `get_rate_limit`) supports OAuth + PAT + service-to-service. |
| `context.py` | `OperationContext` — the per-call references: `binding_ref`, `tenant_ref`, `subject_ref`, `deadline`, plus `credential_mode` (singular). No field is a credential VALUE; `binding_ref`/`tenant_ref`/`subject_ref` are references, `deadline` is an absolute POSIX-seconds UTC cutoff, and `credential_mode` is the single SELECTED mode identifier for this call (a mode *type*, never a token), chosen from — and never outside — the descriptor's declared `credential_modes` set. |
| `result.py` | `OperationResult` — the outcome envelope: `status` (`ok` / `partial`) and `next_cursor` (an opaque continuation token, or `None` when complete). `next_cursor` is deliberately opaque: the manifest declares each operation's own `pagination` contract, and the envelope only says "resume here, or you are done". |
| `errors.py` | `OperationError` — the RUN-01 typed error taxonomy: a twelve-value closed set (`auth`, `scope`, `consent`, `not_found`, `forbidden`, `quota`, `throttle`, `conflict`, `input`, `temporary`, `partial`, `ambiguous`) copied verbatim from the manifest, plus `detail`. |
| `auth_modes.py` | Per-operation *permitted* credential modes + a single deny-by-default authorization path bounded by the descriptor (W01 · L05): the descriptor's `credential_modes` is the outer bound, a policy `PermittedModes` may only narrow within it, and `permit_operation` / `permit_registered_operation` (both REQUIRE a descriptor) deny a mode the descriptor did not declare **even if a registry permits it**, and deny an unknown mode **even if descriptor and policy both carry it** (real `policy ∩ descriptor ∩ CREDENTIAL_MODES` intersection in `effective_permitted_modes`, not a docstring). No public policy-only bypass. Mode identifiers and `operation_id` references only, never a credential value. See "Per-operation permitted credential modes, denied by default" below. |

**`detail` reuses the subsystem's redaction discipline, opening no new
channel.** A typed error's `detail` can reflect provider-returned text, so it
goes through the SAME redact-then-truncate discipline as
`l1_smoke._redacted_detail`, capped at the same 200 characters. `redacted_detail`
(and the `operation_error` constructor that calls it) delegate to
`kiro_crew.security.redact_and_truncate`, which runs the site-wide credential /
exfiltration-URL scanners over the whole string BEFORE the slice — truncating
first would bisect a credential straddling the boundary and leak the prefix past
the regex. There is no un-redacted path onto `detail`.

### The binding record (L02 · AUTH-01)

`control_plane/binding.py` is what a context's `binding_ref` points AT: the
`Binding` record for one authorized account. Still pure types with zero IO — it
MINTS and INCREMENTS a record; it does not persist one, resolve one to a
credential, or reach kiro-cli. Four properties, each a distinct defence:

| Property | How it is enforced |
|---|---|
| **Random `binding_id`** | Minted from `secrets.token_hex(16)` (128 bits) — unguessable and NOT derived from the provider slug, tenant, or subject. A derived id would let anyone who knows the (often public) slug/tenant reconstruct or enumerate binding ids. The id carries no meaning; it is a name resolved through a later leaf's table, never parsed. |
| **Verified subject + tenant** | `create_binding(...)` takes a required `SubjectTenantVerifier` and stores ONLY the `VerifiedIdentity` it returns — never the caller's raw `claimed_*` inputs. A failed verification raises `BindingVerificationError` and no record is built (verification runs BEFORE anything is minted, so there is no partial or unverified binding). There is no path from a claimed identity to a stored one that skips the verifier. |
| **Monotonic `generation`** | A fresh binding starts at `INITIAL_GENERATION`; `next_generation(binding)` returns a new record with `generation` incremented by one and every other field carried through unchanged (the input is not mutated). Its PURPOSE is L04's revoke fencing — a revoke raises the generation so anything stamped with an older one is fenced off — but **this slice lands the field and the increment semantics only; it implements neither refresh nor revoke**, and nothing here decides what a bumped generation invalidates. |
| **`secret_ref` metadata, never a value** | A binding records a `SecretRef` — the vault entry `name` plus metadata (`backend`, `bound_at`) — and it is a hard invariant of the type that no plaintext lives on it. The name follows the same `CONNECTIONS_<SLUG>_...` family `oauth_clients.client_secret_name` uses (via `binding_secret_ref`, with a distinct `_BINDING_SECRET` suffix so a per-binding grant secret never collides with the operator's `_CLIENT_SECRET` application credential). Resolving the name to a `SecretVault` / `SecretValue` is a later leaf's job. |

**The old-custody invariant, at its sharpest.** Under the pre-native model the
OAuth token chain lives entirely inside kiro-cli and the backend only `stat()`s
to probe whether a grant exists. `binding.py` **never reads, copies, or
references an old kiro-cli OAuth token.** A `SecretRef` names a NEW authorization
grant's secret under a NEW trusted owner — it carries only a vault-entry name,
not a filesystem path into kiro-cli's token store, so it is not a place to
smuggle a moved-over legacy token.

### The derived handle (L08)

`control_plane/handle.py` derives a `DerivedHandle` from a trusted `Binding`.
Where a binding is a LONG-LIVED, FULL-AUTHORITY record — it names the connection
and references its secret and never expires — a handle is what a call site
should actually hold: a capability strictly NARROWER than the binding, that
EXPIRES, and that cannot be turned back into the binding. It MINTS a handle
(`derive_handle`) and DECIDES whether one may be used (`ensure_usable`); it does
not persist across processes, resolve to a credential, revoke, or reach
kiro-cli.

**The handle is a CLAIM; the issuance record is the authority.** A
`DerivedHandle` is an ordinary mutable `TypedDict` a caller holds — so it is not
trusted. A caller can edit its `scopes`, push its `not_after`, or change its
`generation` in the dict. Therefore NO enforcement decision is read off the
handle's own fields. Every mint records the authoritative facts (scope set,
expiry, generation, binding fingerprint) in a module-private ISSUANCE REGISTRY
keyed by the random `handle_id`, and `ensure_usable(handle, now=…)` judges ONLY
against that record. A consumer **MUST NOT** read `scopes` / `not_after` /
`generation` off the handle to make its own allow/deny decision — it MUST call
`ensure_usable`; reading the fields directly re-opens exactly the hole this
design closes.

Three invariants, each enforced by real code with a real refusal path (never a
docstring), each with a counterexample test:

| Invariant | How it is enforced |
|---|---|
| **Scope only narrows** | *At mint:* `derive_handle(binding, granted_scopes=…, requested_scopes=…, …)` refuses any requested scope the binding does not grant with a typed RUN-01 `scope` error (`HandleScopeError`) and mints NO handle — never silently dropped to what is granted. *At use:* `ensure_usable` refuses a handle whose presented `scopes` are not a subset of the RECORD's scopes (`HandleTamperedError`, typed `auth`) — a caller who widens the dict after minting is refused, not obeyed. The binding record itself carries no scope set; the authorized-scope set is supplied by the layer that owns it (e.g. L06 governance) and passed in, and this module treats scopes as opaque strings — it never consults or mutates `platform/governance.SCOPE_CATALOG`. |
| **Expiry is enforced** | `ensure_usable` first refuses a non-finite `now` (a `NaN` clock would make `now >= not_after` silently False and let an expired handle through), then compares the caller's `now` against the RECORD's `not_after` (inclusive) and refuses an expired handle with a typed RUN-01 `input` error (`HandleExpiredError`) — pushing the handle's own `not_after` changes nothing, and a claimed expiry later than the record's is itself a tamper refusal. At mint, `ttl_seconds`/`now` must be FINITE and `ttl_seconds > 0`, and `now + ttl_seconds` must not overflow to `inf`; otherwise `ValueError`. |
| **Routing/auth axes are trusted, not caller-chosen** | `ensure_usable` also checks the handle's `service_id` and `credential_mode` against the record and refuses a mismatch (`HandleTamperedError`, typed `auth`) — a caller cannot flip `service_id` to redirect a Graph handle at another provider, or swap the credential mode. And it does not return `None`: on success it returns a **`TrustedHandleView`** built from the RECORD. |
| **A malformed handle is refused, not crashed** | Because the handle's fields are UNTRUSTED, the very FIRST step of `ensure_usable` validates the COMPLETE shape — that the handle is a mapping, every required field is present, and each value is of a type the enforcement can operate on (a `handle_id` that keys the registry, a `scopes` that `set()` accepts, a `not_after` that `>=` orders). A missing or wrong-typed field is the SAME class of untrusted input as a widened scope, so it is a typed RUN-01 `auth` refusal (`HandleTamperedError`) whose `detail` names only the offending field (never its value, and redacted regardless) — never a bare `KeyError` / `TypeError` that would carry no `error_class`, skip the audit, and jump the decision chain onto the caller. The boolean predicate `is_expired` shares the registry lookup's crash surface but is not allowed to raise (its contract routes the typed refusal to `ensure_usable`), so a malformed handle — weaker than "unknown" — fails CLOSED there too: `is_expired` answers `True` (treated as expired) rather than crashing. |
| **Cannot reconstruct the binding** | The handle carries NOTHING that rebuilds its binding: no `binding_id`, no `subject_ref` / `tenant_ref`, no `secret_ref` (the handle deliberately carries no secret reference at all). Its own `handle_id` is random (`secrets.token_hex(16)`), independent of the binding. The sole link back is `binding_fingerprint` — a ONE-WAY keyed digest (HMAC-SHA256 under a per-process random key) of `binding_id`, which L04 revoke fencing can MATCH against a candidate binding but which no handle holder can INVERT to recover the id. |

**A consumer routes on the returned view, never on the handle fields.**
`ensure_usable(handle, now=…)` returns a frozen `TrustedHandleView` whose
`service_id` / `credential_mode` / `scopes` / `generation` / `not_after` are
copied from the trusted issuance record. **W05 / L09 MUST decide where to send
the call and which credential to use from this returned view, and MUST NOT read
`service_id` / `credential_mode` / `scopes` off the `DerivedHandle` dict.**
Reading them off the handle re-opens the "validate, then use the unvalidated
value" hole the whole design closes — a caller who edited `service_id` on the
dict would route the call to the wrong provider even though `ensure_usable`
verified a different one. The handle is a claim; the returned view is the answer.
L09's real entry point may re-assert this check at its boundary; it must not fall
back to trusting the handle.

**Cross-process and restart: a fail-closed contract, not undefined behavior.**
The issuance registry is process-local and in-memory; it starts empty at import
and is never persisted. So two things are guaranteed: (1) **after a restart,
every handle minted before the restart is refused** — the new process's registry
is empty, so no prior `handle_id` resolves and `ensure_usable` fails closed with
a typed `auth` refusal (`HandleNotIssuedError`), never silently accepted; (2) **a
handle minted in another process is refused here** — each process has its own
registry. The capability boundary is therefore explicit: a derived handle is
usable ONLY within the process that issued it, while that process lives and the
record has not expired. A handle is not a bearer token that survives
serialization to another process — that is deliberate and enforced by the
lookup. (A cross-process handoff, if a later slice needs it, is a signed-record
or shared-store design that is out of this slice.)

**Why `generation` rides along but `binding_id` does not.** L04's revoke fencing
(NOT implemented here) needs both the handle's `generation` (to compare against
the binding's current one) and WHICH binding it came from. Carrying the raw
`binding_id` would answer the second but break the no-reconstruction invariant,
so the record and handle carry the non-invertible `binding_fingerprint` instead:
L04 recomputes the fingerprint of a candidate binding under the same process key,
matches, and fences off any handle whose `generation` is older than that
binding's current one. This slice lands `generation` + the fingerprint and proves
they are preserved; it makes no revoke decision.

**Secret custody stays where it is.** This module handles references and
derivation only. Secret storage remains the existing mechanism — the
`secrets/vault.py` `SecretVault` / `SecretValue` machinery and the
`oauth_clients.client_secret_name(slug)` naming convention — and a handle
carries no secret reference, so resolving a binding's secret is still a later
leaf's job, gated by the handle's scope and expiry rather than reachable from
the handle itself.
### The binding lifecycle (L04): trusted store, single-writer rotation, revoke fencing

L02's `resolve_binding_for_principal` matches over an `Iterable` the CALLER
hands in — so the candidate set is caller-controlled, and a caller can slip a
binding it should not own into that set and have the resolve "legitimately" hit
it. That is the FIFTH recurrence of the trusted-source discipline (recorded
provenance → returned views → the executor's inputs → secret resolution → now
the candidate set), so L04 closes it as a PATTERN rather than one more point
fix. `control_plane/lifecycle.py` adds the persistent, trusted lifecycle — it is
IO-doing by design (L01/L02 were pure types), but it persists a binding RECORD,
never a secret value, and **never reads, copies, or references an old kiro-cli
OAuth token.**

| Concern | How L04 closes it |
|---|---|
| **Candidate set comes from a TRUSTED PERSISTENT store, never the caller** | `BindingStore.resolve(...)` has NO `bindings` parameter — it matches only over records the store admitted via `insert`, read from a file under `config_dir()/connections/control_plane_bindings.json`. A caller cannot present a candidate the store never admitted, and the store survives a process restart (a fresh `BindingStore` on the same file resolves the same bindings). **No new vault**: a secret is still only a `SecretRef` under the existing `SecretVault` / `client_secret_name` convention; what is persisted is the binding record. |
| **`(deployment, account)` uniqueness domain, ENFORCED on the domain** | The domain is `(deployment_id, service_id, subject_ref, tenant_ref)` — the **provider-side deployment** that hosts the account (a specific GitHub Enterprise host, a Salesforce org, a Microsoft Graph tenant deployment; **NOT** a Kiro Crew instance — using our own instance id here would merge two provider deployments into one binding and split one deployment seen from two Kiro instances into two, exactly backwards), plus the provider **account** it names (service + verified subject + verified tenant, what the downstream ACL's `resolve(principal, provider, account)` keys on). `insert` enforces uniqueness on the DOMAIN, independent of `binding_id`: it REFUSES (`BindingUniquenessError`) a second binding colliding on that 4-tuple even with a fresh random id, AND refuses a re-insert under an existing `binding_id` whose domain fields changed (an illegal mutation of an immutable identity — the round-23 bypass). A same-id + same-domain re-insert is idempotent ONLY when it is byte-identical over an explicitly-scoped field set (the embedded binding's `binding_id` / `service_id` / `subject_ref` / `tenant_ref` / `credential_mode` / `generation` and `secret_ref`'s `name` + `backend`, plus the record's `deployment_id` and `kiro_principal`; volatile timestamps `created_at` / `bound_at` / `updated_at` are excluded); a re-insert differing in `kiro_principal`, `credential_mode`, or the secret reference is a CONFLICT (`BindingUniquenessError`), never a silent `return prior` of the old record (a "write looked successful but stored the old value" trap). It never silently overwrites. |
| **The Kiro principal → authorization link is REAL** | Each stored binding records the `kiro_principal` authorized to use it, and `resolve(...)` requires the caller's authenticated principal to EQUAL it — a different principal does not resolve even with a correct provider identity and deployment. This is the `resolve(principal, provider, account)` seam enforced in code, not asserted in prose. |
| **The per-binding secret SELECTOR resolves from the live store, by binding** | `BindingStore.select_secret(binding, *, reader)` is the payload path's secret selector. It resolves the secret reference **by binding, from the trusted live store** — NOT by provider slug (the legacy `binding_secret_ref` collapses every binding under a provider onto one name) and NOT from any caller-supplied set. It first calls `assert_live` (a fresh LIVE-store read that fences a revoked / stale / forged / swapped handle), takes the `secret_ref` from the STORE's returned record (never the caller's dict), reads the value through the injected `SecretReader` (the existing `SecretVault` satisfies it — no new vault), and returns a `ResolvedCredential` whose every field is the store's. So a rotated/revoked/tampered handle cannot pull a secret, and a slug-level collision cannot surface another binding's credential. Sixth recurrence of the trusted-source discipline, closed as the pattern. |
| **Refresh rotation is a CROSS-PROCESS SINGLE WRITER around the REAL refresh** | `BindingStore.rotate(binding_id, *, observed_generation, refresh)` serializes through `platform_compat.acquire_lock` / `release_lock` (**never a raw `fcntl`** — a hard cross-platform constraint), and the caller's REAL token-refresh callable is invoked **inside that lock**, so the provider's token endpoint is hit EXACTLY ONCE under contention, not once per process. The winner calls `refresh`, advances `live_generation`, and returns `(record, True)`; the LOSER re-reads under the lock, finds the generation already advanced past its `observed_generation`, and returns `(winner_record, False)` WITHOUT calling `refresh` or rotating. Proven by a REAL two-process test that counts hits on a controlled token endpoint — not an internal counter, not "the lock was acquired". Fails closed: a stuck holder past the ceiling raises. |
| **Revoke FENCES on EXACT generation AND identity** | `BindingStore.revoke(binding_id)` marks the binding revoked and raises `live_generation` (L02 left the counter and its `next_generation` increment here). `assert_live(binding)` and `resolve(...)` then require the presented handle's `generation` to EXACTLY equal the store's current live generation for the SAME binding identity (matching `service_id` / `subject_ref` / `tenant_ref`) — not "less than", not "not greater". So a STALE pre-revoke handle is refused, a FORGED future generation (e.g. `999` the store never issued) is refused, and a numerically-valid generation borrowed from ANOTHER binding is refused on the identity mismatch. The fence ALSO compares the presented `credential_mode` and the whole `secret_ref` against the store's record — a handle that keeps the right id/identity/generation but swaps the mode or points the secret reference at another vault entry is refused. `assert_live` RETURNS the store's trusted live `Binding` (not `None`), and the real sender MUST consume THAT return value for the ref/mode/secret it uses — never re-reading the caller-supplied dict after the fence, closing the "validate then use an unvalidated value" gap. |

Every mutation (`insert` / `rotate` / `revoke`) is a reload-merge-publish under
the cross-process lock, mirroring `acp/seed_provenance.py`: the store file is
shared by every Crew process on the host and `atomic_write` makes the last
writer win, so a writer that did not first reload would drop a sibling's
records. Reads are lock-free snapshots. A store file that is present but
UNREADABLE or malformed is **not** treated as empty — that would let the next
write republish a fresh store and erase every binding, and let a colliding
insert "become legal" again — so both reads and writes FAIL CLOSED with
`BindingStoreCorruptError` and honestly report "unreadable", while an ABSENT file
is a genuinely empty store; an individual malformed record inside an otherwise
valid file is dropped fail-closed. The L04 symbols (`BindingStore`,
`BindingUniquenessError`, `BindingRevokedError`, `BindingStoreCorruptError`,
`StoredBinding`, `LIFECYCLE_SCHEMA_VERSION`, `store_path`) are canonical to
`control_plane` and are NOT re-exported as top-level `connections` aliases,
guarded by test.

### The ACL binding-resolver adapter (L04): the host side of the knowledge seam

The knowledge subsystem's ACL (`kiro_crew.knowledge.acl`) gates every managed
(cloud/structured) item behind a per-candidate identity check, and it does NOT
know how a principal maps to a provider account — that is a host/W01 concern. It
declares a `@runtime_checkable` `Protocol`,
`BindingResolver.resolve(principal, provider, account) -> AccessContext | None`,
and its own docstring says *"Implemented by the host/W01 layer, not here."*
`control_plane/acl_binding_resolver.py` is that host implementation, backed by
the L04 trusted store.

**It matches the CALL SHAPE, not by import — and matching the signature is not
the whole contract.** A `Protocol` is structural, so the adapter matches
`resolve(self, principal, provider, account) -> …|None` by shape and does NOT
import — MUST NOT import — `kiro_crew.knowledge.acl` (that module lives on an
un-merged branch; importing it would invert the dependency and is not reachable
from a control-plane wheel). But **the ACL gate reads a field W01 does not
produce**: `_subject_tenant_ok` evaluates `grant.subjects & ctx.subject_ids`,
where `ctx.subject_ids` is a `@property` on ACL's `AccessContext`
(`frozenset({subject, *groups})`). `subject_ids` exists **nowhere in W01**, so the
`AccessGrant` this adapter returns cannot be handed to the ACL gate as-is — it
would raise `AttributeError` at `ctx.subject_ids`. This section therefore makes
**no** claim of structural equivalence or drop-in injection (that would be the
"documented property that does not exist" trap this stack has hit before).

**The actual bridge chain** has two halves. (1) W01 hands the ACL an `AccessGrant`
carrying the provider-mapped, VERIFIED identity for one candidate (`subject` /
`tenant` from the binding's `subject_ref` / `tenant_ref`; `groups` empty). (2) The
ACL side consumes commit `dd715f12a` via ordinary git and, **on the ACL side**,
bridges `AccessGrant` into its own `AccessContext`
(`AccessContext(subject=grant.subject, tenant=grant.tenant, groups=grant.groups)`),
which is where the `subject_ids` property comes into existence. `subject_ids` is
an **ACL-owned derivation**, explicitly NOT a field W01 emits. So `AccessGrant` is
a plainly-named W01 transport record, deliberately not a copy of ACL's
`AccessContext` (no `subject_ids`, no `__post_init__`); every symbol it uses
(`BindingStore`, `ServiceId`, `map_provider_to_service_id`,
`ControlPlaneBindingResolver`, `AccessGrant`, `AccountToDeployment`) is real,
importable, and tested — nothing is a "the ACL will call …" placeholder.

| Concern | How the adapter closes it |
|---|---|
| **CALL-shape adapter, fail-closed** | `ControlPlaneBindingResolver.resolve(self, principal, provider, account)` (positional, matching the Protocol) maps the ACL inputs onto the store's keyword-only lookup and returns an `AccessGrant` or `None`. It returns `None` — deny this candidate, no fallback — for an unmappable provider, an unverified/empty/`local_library` principal, an `account` the host cannot map to a deployment, and a principal holding no (or a revoked) binding on the resolved `(deployment, service)`. It raises only when the store itself is unreadable (corruption is surfaced, never read as "no binding"). |
| **`provider` lands on a CLOSED set** | `map_provider_to_service_id(provider)` maps the ACL connector-id to W01's closed `ServiceId` set: a verbatim member maps 1:1, the one alias `excel → excel_shared_engine` is translated, and ANY other string (unknown, misspelled, free-form) returns `None` → REFUSE. No lenient acceptance of arbitrary strings. |
| **`resolve_ref(principal, ref)` is the single judgment path (endpoint-discriminated)** | The ACL adopted the four-field contract: `resolve_ref(principal, ref) -> AccessGrant \| None`, consuming the `ProviderResourceRef` STRUCTURALLY (`.provider`/`.account`/`.resource_id`/`.locator`, no ACL import). It carries the endpoint the bare `(provider, account)` pair lacks. Five FAIL-CLOSED checks in order: (1) principal established (see the trust row); (2) provider → `ServiceId` closed set; (3) the service's REQUIRED locator keys present (from `_LOCATOR_SPECS`), no best-effort completion; (4) the endpoint (read from the ref's locator) must be REGISTERED in the trusted store — `BindingStore.resolve_deployment_for_account_endpoint` returns a deployment only for a stored `(service, account, endpoint)`, so a caller-supplied endpoint the store never registered does not match; (5) ambiguity denies. The returned view comes ENTIRELY from the store's verified record; **the ref is a lookup key only, never an identity source**. The old two-arg `resolve(provider, account)` is now a THIN SHELL that builds an endpoint-less ref and defers to `resolve_ref` — NOT a second judgment path, and it fail-closes for any endpoint-requiring provider. Only three providers have a real connector today: **salesforce** carries the real endpoint `instanceUrl` (the two-deployment discriminator); **github**'s connector emits `{owner,repo,…}` with NO endpoint field yet (W02 owns adding it — NOT fabricated here, so github stays fail-closed on the endpoint axis); **google_drive** `{fileId,[driveId]}` has no host endpoint. The Microsoft Graph family + gmail/zoom/slack/asana are PROPOSED shapes (no connector emits them) and are marked proposed, not invented as wired. |
| **`account` is NOT a `deployment_id` — a store-backed mapping, not a silent equation** | The ACL keys on `(provider, account)`, where `account` is the VENDOR-side account/org/tenant/**driveId** an object lives in (per acl.py: "a Graph tenant id, a Salesforce org id, a Drive driveId, a GitHub org/login, a Slack workspace id"). L04's `deployment_id` is instead the **provider-side deployment that hosts** the account (a GHE host, a Salesforce org, a Graph tenant deployment) — DIFFERENT axes (a GitHub org/login is not a GHE host; a Graph driveId is a resource, not the tenant deployment). The adapter does NOT pass `deployment_id=account`. The persisted store records each binding's vendor `account` AND its `endpoint` alongside its `deployment_id` (optional `StoredBinding.account`/`endpoint`, threaded through `insert`, carried through `rotate`/`revoke`, in `_same_stored_binding` and `_well_formed`; L02's `Binding` is untouched). `resolve_ref` keys on `(service, account, endpoint)`; the endpoint-less two-key `StoreBackedAccountToDeployment` remains as a store method but is no longer the judgment path (it fail-closes the same-name-cross-host case the endpoint discriminator resolves). |
| **Principal trust: a bare session-key is not proof** | An ACL `QueryPrincipal` carries `principal_id`/`local_library` but NO verification provenance, and the ACL side has minted a principal from a bare `X-Session-Key` fallback (`a31`). A bare session key is not an authentication proof, so the adapter does NOT trust `principal_id` on its face: it accepts a principal as an authorization subject ONLY when a wired `principal_verified` predicate vouches its verification is established, and FAILS CLOSED otherwise (no predicate wired ⇒ no principal accepted). The predicate is the trust-contract seam the ACL/request layer must supply — the concrete discriminator (a verified marker / the signed request context the id was derived from) is NAMED, same discipline as `tenant_ref`/`subject_ref`: only verifier-produced identity counts, never a caller self-report. |
| **GitHub trusted tenant** | The returned `subject`/`tenant` are the resolved binding's `subject_ref`/`tenant_ref`, which the `SubjectTenantVerifier` produced at INSERT time and the store persisted. `resolve_for_acl` takes NO caller-claimed subject/tenant and runs no verifier (the ACL contract carries none), so the tenant a caller ultimately sees is a VERIFIED provider tenant read from the trusted store, never a value the caller asserted. |
| **`groups` empty, `bypass_acl` never true** | `AccessGrant.groups` is ALWAYS `frozenset()`: W01 does not produce or invent a group/role dimension — group membership is a separate authority (the provider's directory) the ACL folds into `subject_ids` on its side. `AccessGrant.bypass_acl` is ALWAYS `False`: `bypass_acl=True` is the ACL-side local-single-user library context, and a provider binding is a managed, cross-identity path. |
| **Shared handler stays on the ACL side** | W01 does not build a second ACL or shared handler and does not extend any handle's self-reported fields to satisfy `AccessContext`. Authorization/routing consume only what a trusted read returns; the adapter's job ends at handing the ACL a provider-mapped `AccessGrant`. |

**Not covered by W01, left to the ACL bridge (named, not hidden):** `subject_ids`
(the `frozenset({subject, *groups})` derivation the ACL gate reads). Because the
Protocol type is not reachable here, `isinstance(adapter, BindingResolver)` cannot
be asserted; the tests pin the CALL shape directly AND pin the bridge-chain
contract — that `AccessGrant` carries exactly `subject`/`tenant`/`groups`/`bypass_acl`
and that `subject_ids` is NOT among them (`test_access_grant_does_not_provide_subject_ids…`).
The adapter symbols (`AccessGrant`, `AccountToDeployment`,
`ControlPlaneBindingResolver`, `map_provider_to_service_id`) are canonical to
`control_plane`, not re-exported as top-level `connections` aliases, guarded by
test.

### Two orthogonal axes, the same word in this repo, kept apart on purpose

Two independent questions wear the word "mode" in this subsystem today, and this
seam pins the distinction so a later leaf cannot quietly collapse them:

- **Axis A — registration mode** answers *where the OAuth client came from*:
  `dcr` (dynamic client registration) or `preregistered` (an app an operator
  registered in the vendor console). It lives in `registry.AuthConfig` and is
  read through `auth_mode()` / `is_preregistered()`. **W01 changes none of it.**
- **Axis B — credential mode** answers *what credential this operation
  authenticates its call with*: `oauth_user`, `fine_grained_pat`, or
  `service_to_service`. This IS the manifest's per-operation `auth_modes` axis,
  named as the shared `CredentialMode` set. The operation descriptor declares a
  SET of them (`credential_modes`, plural, on `control_plane/operation.py`); the
  single mode a given invocation uses lives on the per-call `OperationContext`
  (`credential_mode`, singular).

**The two combine freely and neither is derived from the other.** The manifest
already states the general form of this rule — *"an account type never stands in
for an auth mode"* — and registration mode standing in for credential mode is
the same category error. A `preregistered` client (Axis A) can still
authenticate a given operation as `oauth_user` OR `service_to_service` (Axis B);
a `dcr` client says nothing about which credential a particular operation uses.
Any code that inferred one axis from the other would reintroduce exactly the
collapse the manifest's two-axis design exists to prevent, so a dispatch reads
`credential_modes` off the operation descriptor directly and never computes it
from a provider's registration mode.

### Descriptor declares, policy narrows — one source of truth for the mode set

`credential_modes` on the descriptor is the OUTER bound of which credential
modes an operation permits, and it is the single source of truth for that set.
A governance policy — for example W05/L05's `permit_operation` / `PermittedModes`
— may **narrow** which of the declared modes are permitted in a given context,
but MUST NOT permit a mode the descriptor did not declare: a mode absent from
`credential_modes` is refused even if a registry or account would otherwise
allow it. The per-call `OperationContext.credential_mode` is likewise chosen
from within the declared set. This keeps "what modes exist for this operation"
in exactly one place (the descriptor); everything downstream only subtracts.

### RUN-01 replaces string-sniffing, but not in this slice

RUN-01 (the twelve-value taxonomy above) is the typed replacement for
classifying a failure by sniffing substrings out of a free-text message — the
`l1_smoke._RECONSENT_TOKENS` / `_reconsent_error` matching of `"unauthorized"` /
`"forbidden"` is the in-repo example of the anti-pattern it supersedes.
**Reconnecting that classifier to RUN-01 is a separate later slice; W01 only
fixes the taxonomy it will target and does not touch `l1_smoke`.**

### Per-operation permitted credential modes, denied by default

`control_plane/auth_modes.py` (W01 · L05) adds the call-time counterpart to the
descriptor's `credential_modes`. Where `operation.py` fixes the Axis-B closed
set and carries the SET a descriptor declares, this module lets a governance
policy declare which of those modes it *permits*, and decides allow/deny for a
caller-offered mode against BOTH the descriptor's declared set and the policy.

- **Declaration.** `declare_permitted_modes(modes)` builds an immutable
  `PermittedModes` (`frozenset[CredentialMode]`) from a subset of the L01 closed
  set. A value outside `CREDENTIAL_MODES` is rejected with `ValueError`: this
  slice never mints a fourth credential mode, exactly as `operation.py` never
  does — a genuinely new mode is a scoped revision of the manifest's `auth_modes`
  vocabulary, not a value smuggled in through a declaration.
- **Decision (the ONE authorization path).** `permit_operation(descriptor,
  offered_mode, permitted)` is the module's single authorization decision. It
  REQUIRES the descriptor and returns `None` on allow, or a RUN-01 `auth`
  `OperationError` on deny — built with `operation_error(...)`, so `detail` is
  unconditionally redacted-then-truncated and this module opens no un-redacted
  error channel. `permit_registered_operation(descriptor, offered_mode,
  registry)` does the same against a whole `PermittedModeRegistry`
  (`Mapping[str, PermittedModes]`). There is deliberately NO public policy-only
  sibling that skips the descriptor: a function named like an authz decision
  that did not consult the descriptor would be a bypass waiting to be used, so
  the descriptor is required. (The bare policy-membership predicate is kept
  PRIVATE as `_is_mode_permitted`, out of `__all__`.)
- **Enforcing the descriptor bound + closed set (real code, not a docstring).**
  `context.py` *says* the selected mode must come from within the descriptor's
  declared `credential_modes`, but a `TypedDict` validates nothing at runtime,
  so that sentence needs code behind it. `effective_permitted_modes(descriptor,
  permitted)` is that code: it returns `permitted ∩ descriptor["credential_modes"]
  ∩ CREDENTIAL_MODES`. The descriptor factor drops a mode the descriptor did not
  declare **even if the policy named it** (the counter-example the slice is
  judged on, deny reason `not DECLARED`). The closed-set factor is NOT redundant:
  a descriptor's `credential_modes` and a policy set are both un-validated at
  runtime, so an out-of-closed-set string (a typo, a fabricated fourth mode) can
  sit in BOTH and a bare `policy ∩ declared` would keep it — intersecting with
  `CREDENTIAL_MODES` makes an unknown mode fall out no matter how many places
  carry it, so "an unknown mode is always denied" holds structurally.
- **Deny-by-default.** An operation that declares no permitted mode — an empty
  `PermittedModes`, an empty descriptor `credential_modes`, or an `operation_id`
  absent from a `PermittedModeRegistry` — is DENIED, never allowed. The absence
  of a declaration is not an open door: a registry is the complete statement of
  what is permitted, and anything unstated is nothing, so an operation nobody
  configured fails closed. This is pinned by tests.
- **Zero credential values.** The whole API accepts and returns only mode
  IDENTIFIERS (`CredentialMode`) and reference strings (`operation_id`). It never
  accepts, stores, or returns a token, a client secret, or any credential
  plaintext — the same "references, never a value" invariant `context.py`
  carries. A permitted mode says which KIND of credential is allowed; resolving a
  kind to a live credential is a later leaf in kiro-cli custody, not here.

This gate operates on **Axis B only**. It never reads a provider's registration
mode (Axis A, `dcr` / `preregistered`) and never infers a permitted credential
mode from one — the two value sets are disjoint (`{dcr, preregistered}` vs
`{oauth_user, fine_grained_pat, service_to_service}`), and a companion test
asserts they do not intersect, so the collapse the manifest's two-axis design
forbids cannot creep in through this slice.

### The `vendors/` container anchor

`connections/vendors/__init__.py` is a committed anchor for an otherwise-empty
container this slice creates ONCE, for two reasons. First, **packaging**:
`setup.cfg` builds with `packages = find:` (`find_packages`), which discovers
only directories containing an `__init__.py` and drops every subpackage beneath
a directory that lacks one — even a `vendors/<slug>/` that has its own
`__init__.py`. So without this committed file, `vendors/` and all of it would be
absent from the wheel/sdist and a non-editable install would ship no provider
code (a build-artifact effect, not a source one — under an editable/source tree
PEP 420 namespace packages let `vendors.<slug>` import regardless). Second,
**race avoidance**: each provider stream (`W02`..`W14`) owns its own
`vendors/<slug>/` subpackage, and a single owner minting the anchor here stops
several streams landing in parallel from each trying to create `vendors/` and
colliding. **W01 creates the anchor and nothing under it** — no `vendors/<slug>/`
subdirectory is this slice's to make. The name is `vendors`, not `providers`,
deliberately: `src/kiro_crew/providers/` already exists and means LLM providers,
so a `connections/providers/` here would be one word for two different things in
one package tree.

### Five-layer governance intersection + approval binding (W01 · L06)

`control_plane/policy.py` answers one question for a connector operation: *given
every governance layer with an opinion, is this scoped item permitted, and does
an approval on record still apply to the exact parameters it was granted for?*
It composes **five** layers:

    platform ∩ workspace/profile ∩ session/App/job ∩ connection ∩ vendor

**It sits ON TOP of `platform.governance.resolve()` and changes
`platform/governance.py` by not one character.** `Decision.layer` there is a
two-layer vocabulary today (`policy | profile | both | default`) and `resolve`
composes exactly those two (an enterprise ceiling ∩ a per-surface profile) under
Rule 2. Widening that closed set to name three more layers would edit a module
outside this stream's ownership, so L06 does not: it *consumes the platform
primitives read-only* — the one `SCOPE_CATALOG`, `resolve`, `GovernanceCeiling`,
`Profile`, `Decision` — and layers the extra narrowing on top of them.

- **Intersection only narrows — `resolve_layers()`.** The platform ∩ workspace
  pair is delegated verbatim to `resolve(ceiling, profile, scope, item)`; the
  remaining three layers are each an ordinary `GovernanceCeiling`, queried
  through the SAME primitive with no profile (`resolve(layer_ceiling, None, …)`,
  which returns just that one layer's answer). The effective decision is the AND
  of all five layer permits, so the result is ⊆ every individual layer's
  permitted set and, over the range `resolve()` itself covers, ⊆ its answer. AND
  is monotone non-increasing: adding a layer can only shrink the permitted set,
  never grow it, and no layer can turn another layer's deny into a permit. The
  first layer that denies is returned, so `Decision.layer` / `reason` name the
  deciding layer.
- **Ceilings are inputs, never hardcoded — `LayerCeilings`.** Each layer's
  ceiling/profile is a parameter (`platform`, `workspace`, `session`,
  `connection`, `vendor`); a layer with nothing to say passes `None` (ungoverned
  = unrestricted = contributes a permit, exactly `resolve`'s own `None`
  semantics). The module hardcodes NO per-layer legal-scope-subset table — and
  deliberately so: neither the connector-capability manifest nor this document
  defines which scopes a "connection" or "vendor" layer may govern, so L06 does
  not invent one. It intersects whatever the caller supplies.
- **Deny-by-default — `decide()`.** An unknown `scope` (not a live
  `SCOPE_CATALOG` member) is refused before any layer is consulted, and an
  unknown layer name is outside the `LAYERS` closed set. A misspelled scope or an
  unlisted layer gets a typed denial, not a silent open door.
- **Approval-parameter binding — `Approval` / `approval_applies()`.** An approval
  is granted for a specific parameter set, bound at grant time to a canonical
  (order-independent, value-sensitive) fingerprint of those parameters. Reusing
  it for a call whose parameters differ — an added key, a removed key, a changed
  value — is refused: the approval on record does not permit those parameters and
  is NOT reused. This closes the "approve once, then swap the arguments" replay.
- **Typed refusals.** Every refusal is an L01 `OperationError` built with
  `operation_error(...)`, which redacts its `detail` unconditionally; L06
  hand-assembles no error string that would bypass that redaction.

**No new `connection.*` / `provider.*` scope is registered.** Whether the
connector campaign gets its own governed scope family is still an open decision
and is not L06's to make: this module reuses the existing `SCOPE_CATALOG` rows
and adds none.

Exports for these symbols (`LAYERS`, `POLICY_SCHEMA_VERSION`, `Approval`,
`LayerCeilings`, `LayerName`, `approval_applies`, `decide`, `resolve_layers`)
are added ONLY to `control_plane/__init__.py`; they are NOT re-exported as
top-level `kiro_crew.connections` aliases — a zero-consumer second spelling is a
rename hazard, not a convenience.

### An uncertain non-idempotent write is not blindly replayed (L07)

A non-idempotent write — a POST that creates a resource, an
`external_send` that posts a message — that is issued and then leaves its
caller with an **uncertain** outcome (a timeout, a dropped connection, a 5xx
with no usable body) is the case a naive retry gets wrong: reissuing it can
apply the effect **twice** — two issues created, two messages sent. The
core invariant `control_plane/writes.py` fixes is:

> When a non-idempotent write's outcome is `unknown`, a replay MUST be refused
> unless there is evidence the prior attempt did not take effect (or its
> recorded result can be reused).

The module is pure decision logic, zero IO — it neither issues the write nor
holds a client nor resolves a credential. It records what is KNOWN about a prior
attempt and decides whether replaying it now is allowed:

- An `AttemptRecord` gives one attempt an identity — the triple
  `(operation_id, args_fingerprint, idempotency_key)`, so a retry is recognized
  as a replay of a known prior attempt — and one of three **outcome** states
  (`AttemptOutcome`, a closed set distinct from the success envelope's
  `ResultStatus` and the RUN-01 `ErrorClass`, which classify what a call
  RETURNED; this records whether the effect LANDED): `succeeded`,
  `failed_not_applied`, or `unknown`.
- `replay_decision(descriptor, record)` is the gate. `failed_not_applied` →
  **allow** (the write provably did not land, so a reissue cannot duplicate it);
  `succeeded` → **reuse** the recorded result (do not reissue and duplicate);
  `unknown` for a non-idempotent operation → **refuse**, a typed
  `operation_error("conflict", …)` whose detail says the outcome is uncertain.
  A refusal is always a typed rejection built through `operation_error(…)`,
  never a bare boolean.

**The idempotent-write exception is decided only by an explicit assertion.**
Whether `unknown` is safe to replay is decided ONLY by an explicit, trusted
`idempotent` assertion the caller places on the attempt record (a fixed-key
upsert, a provider-honored idempotency token). It is **never** inferred from the
descriptor's `effect` — in particular `effect=delete` is NOT treated as
idempotent: a second `delete` can land on a resource RECREATED in the interim
(deleting someone else's new resource), and a given API's delete may itself not
be idempotent. The safe default is refuse; the exception is something the caller
must state.

**Attribution comes first.** Before any outcome is honored, the gate checks the
record BELONGS to this request: `operation_id` must equal the descriptor's, and
the `args_fingerprint` / `idempotency_key` must equal the request's own. A
record for another operation, or another argument set, is refused — never
allowed, never reused. The stored fingerprint and key are compared, not merely
retained. A `succeeded` record with no recorded result is likewise refused
rather than reused (reuse would return an empty result).

Like every other symbol on this seam, the L07 exports live on the canonical
`kiro_crew.connections.control_plane` subpackage only; they are **not**
re-exported as top-level `kiro_crew.connections` aliases (a second spelling with
zero consumers is a rename hazard, not a convenience).

### The executor wires the four judgments together (L09)

L01..L08 delivered four decision primitives; L09 (`control_plane/executor.py`)
is the first real CALLER that runs them in the one order a dispatch must, BEFORE
any transport call is emitted:

    ensure_usable (trusted handle view)
      → permit_operation (credential-mode permit)
        → decide (five-layer governance intersection)
          → replay_decision (write-replay gate)
            → transport

- **A denied gate emits ZERO calls.** Any judgment that rejects returns a typed
  `OperationError` in an `ExecutionOutcome` and the transport is **never**
  called — "emit first, decide after" would make all four upstream slices dead
  code. This is pinned by a test using a call-counting in-memory fake transport
  asserting `calls == 0` on every deny path (undeclared / unpermitted mode,
  unknown governance scope, tampered / expired handle, non-finite clock, and an
  `unknown`-outcome non-idempotent write).
- **Routing trusts the VIEW, never the handle.** `service_id` and
  `credential_mode` that reach the transport are read only off the
  `TrustedHandleView` `ensure_usable` returns from its issuance record — never
  off the mutable `DerivedHandle`. A test mutates the handle's `service_id` and
  asserts the executor never routes to the forged service (the tamper is refused
  outright, so the call is not even emitted).
- **Server clock, not the caller's word.** A non-finite `now` (`NaN` / `±inf`)
  is refused up front, so a bad clock cannot slip an expired handle or a stale
  precondition through — the same fail-closed direction L08 enforces.
- **HTTP 412 is preserved, not flattened.** A precondition-failed response is
  returned as a structured `PreconditionFailure` carrying the failed
  precondition names and the server's current ETag, so the CALLER can decide to
  re-read and re-derive rather than blind-retry. It records the write's outcome
  as L07's `failed_not_applied` (the write provably did not land — the branch
  `replay_decision` *allows*), but "allowed to replay" is **not** "safe to
  replay as-is": a 412 tells you only that your asserted precondition is false,
  never the server's current state. The correct shape is 412 → preserve the
  structured signal → readback + re-derive the precondition → only then retry.
  The executor deliberately does NOT auto-reissue. **MS owns its own 412 /
  readback / baseline / fresh / locator revalidation under `vendors/microsoft/`;
  L09 changes nothing there and only preserves the shared signal MS consumes.**
- **Three surfaces for two downstreams.** The outer interface covers `execute`
  (dispatch), `advance_page` over a `PageWalk` (real cursor pagination — it
  re-runs the full gate chain per page, advances on the response's
  `next_cursor`, terminates on `None`, and refuses to loop on a repeated cursor,
  so it neither drops nor duplicates a page), and `classify_error` (HTTP-status
  → RUN-01 `ErrorClass`). W02 (GitHub) needs fetch / pagination / error; W05/MS
  needs the 412 structured signal.
- **`EXECUTOR_SCHEMA_VERSION`.** A pinnable constant (currently `1`) the two
  downstreams encode against; a change to the request/response envelope, the
  transport-callable contract, or the paging / error surface bumps it.
- **Not a query-ACL substitute.** L06's five-layer intersection and the handle's
  scope narrowing do **not** replace document- / provider-level query
  permissions; that is a separately-owned gap and L09 makes no claim to cover it.

#### The send path resolves its credential from L04's LIVE store, per binding

`control_plane/production.py` composes the real transport, and the credential it
sends is resolved by **`BindingStore.select_secret(binding, reader=vault)`** — the
L04 selector described above — on **every call**. `build_production_transport`
takes a live `store` plus a `gate` (`BindingCustodyGate`), and the gate answers
only *which binding* a call may resolve for; it selects no secret and derives no
vault name.

The defect this closes: the transport used to derive the vault entry name with
`binding_secret_ref(slug)`. A slug names a PROVIDER, so every binding of one
provider collapsed onto ONE vault entry — two accounts in two tenants resolved
the same credential, and per-binding custody was apparent rather than real. That
derivation is **removed from the send path with no fallback branch**: `slug` is
not a parameter of the composition, `BindingCustodyGate` carries no `slug` /
`secret_backend` field, and `production.py` no longer imports
`binding_secret_ref` at all (pinned by an AST check, so a docstring mention
cannot pass for a call site).

Order per call, and where each refusal lands:

| Step | What it does | On refusal |
|---|---|---|
| 1. `gate.trusted_binding_for(view)` | Matches the call's trusted binding identity (fingerprint + the `service_id` / `credential_mode` read OFF its binding, so a gate cannot check one identity and present another) and returns the binding to fence, stamped with the generation THIS CALL presents — not one frozen at composition. | `401`, ZERO calls, store and vault never asked |
| 2. `locator(...)` | Vendor request shaping; no credential in it. | — |
| 3. `store.select_secret(...)` | ONE call that FENCES via `assert_live` against a fresh LIVE-store read and resolves the value by the STORE's own `secret_ref`. A revoked binding, a stale or forged generation, a swapped identity / mode / `secret_ref` are all refused. | `401` (fenced or secret absent) / `503` (corrupt store, fail-closed and honest), ZERO calls |
| 4. reveal → `Authorization: Bearer` | The one place plaintext exists, on a local that dies with the call. | — |

Because the fence is a fresh live-store read per call with nothing cached, a
revoke or rotation takes effect on the **next** call — including the next page of
a `PageWalk`, which re-runs the whole chain through the same closure rather than
reusing page 1's answer, so a binding revoked between page 1 and page 2 refuses
page 2 with zero calls. All of this is proven over the REAL TLS loopback with the
REAL on-disk `SecretVault` and the REAL on-disk `BindingStore`, counting sockets.

**What is NOT wired here, stated so it is not read as working:**

- **L03 (auth-code / PKCE) is required and NOT IMPLEMENTED anywhere in this
  stack.** Whatever the vault holds had to be put there by an authorization flow
  that does not exist yet. This path reads custody; it never establishes it.
- **`principal -> binding` is not wired into the send path.** L04's
  `BindingStore.resolve` IS a trusted principal→binding resolver, but the
  transport does not call it: a gate is composed for ONE binding and refuses every
  other, so one transport still cannot serve many tenants. That is a refusal, not
  multi-identity support.
- **Refresh rotation is not driven from here.** `BindingStore.rotate` exists and
  is single-writer, but the transport never calls it — it does not react to a 401
  by refreshing. It honours the OUTCOME: once something else rotates or revokes,
  the next call is fenced.
- `PRODUCTION_SCHEMA_VERSION` is `4` for this change (composition signature +
  fenced behaviour). `RESULT_SCHEMA_VERSION` stays `3` and
  `EXECUTOR_SCHEMA_VERSION` stays `4`: the transport contract and the result
  envelope kept their shapes — only how this module obtains the credential behind
  that contract changed.

Like every other symbol on this seam, the L09 exports live on the canonical
`kiro_crew.connections.control_plane` subpackage only; they are **not**
re-exported as top-level `kiro_crew.connections` aliases (a second spelling with
zero consumers is a rename hazard, not a convenience).

