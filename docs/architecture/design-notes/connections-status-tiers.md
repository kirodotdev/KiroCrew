# Connections status and cancel

How a Connections card learns whether a provider is actually authorized, what a
Cancel releases, and what the mint audit trail records about each route.

## The two axes a card needs

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

## The mint contract is preserved, not replaced

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

## connectedSince is source-backed

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

## Cancel releases what a mint holds

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

## Mint outcome telemetry

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

## A grant on disk is not a grant that works

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

## Runtime baseline

Written against **shipped** kiro-cli MCP OAuth behavior (kiro-team/kiro-cli
#3939–#3943, merged 2026-08-15: the initialization barrier and AS-metadata
persistence among them). No init-race padding, AS-metadata re-discovery, or
teardown-tolerance shim is carried here — those worked around pre-fix runtimes.
The grant-artifact layout and cache-key derivation remain pinned by the mint
module's drift guard, because they mirror an undocumented internal of an external
binary.

## Boot path

Both handlers import the mint engine and the status module **function-locally**.
The gateway imports the dashboard handlers package at boot, and the mint engine
drags in the ACP client, the credential predicate and the PID registry;
`test_the_handlers_package_does_not_import_the_mint_engine` enforces that in a
subprocess, so hoisting either import to module scope turns the suite red.
