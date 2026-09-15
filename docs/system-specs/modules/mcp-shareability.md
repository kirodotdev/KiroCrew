# MCP shareability (predicting which servers can share a backend)

Deciding whether an MCP server may share one backend across sessions is a correctness question the operator was previously asked to answer with no information. This module answers it from evidence gathered on the host, provokes the failure before a session can be hurt by it, and remembers the answer so the cost is paid once.

Nothing about which servers a machine runs ships with Kiro Crew and nothing leaves the host: shipped defaults are empty and every verdict is derived locally. That is why there is no curated list of server names in this repository — a list would both be wrong for most installs and be an inventory of the operator's tooling.

## The ordering invariant

Evidence is ranked, and the ranking is the load-bearing property. Strongest first:

| Strength | Source | Meaning |
|---|---|---|
| `refuted` | `hazards` ledger | The gateway WATCHED this server behave per-client while shared. |
| `disqualified` | pre-flight measurement, or a config/transport fact | Ruled out before any user is served. |
| `declared` | the server's own `capabilities.experimental` | It says it was written for a pooled backend. |
| `measured` | a pre-flight that ran and found no divergence | Provoked as two callers; the handshake replayed identically. |
| `no_objection` | absence of any of the above | Nothing disqualifying was found — the weakest useful verdict. |
| `unknown` | nothing observed | Not enough evidence to say anything. |

`measured` exists because `kirocrew.caller-identity` is an extension this project invented and the MCP base protocol has no equivalent, so no third-party server will ever reach `declared`. With `declared` as the only tier a pre-flight could reach, measuring was a one-sided cost: it could REFUTE a server but never record anything in its favour, and every real server rested at `no_objection` for ever.

**`measured` does NOT recommend sharing.** The pre-flight compares the HANDSHAKE — capability shapes, `protocolVersion`, `serverInfo`, the read-only listings — and never makes a tool call. A server whose state is process-global (one browser context, one database connection, one working directory) replays that handshake identically for two callers and still cannot serve two sessions: on a shared backend one caller reads state another caller wrote. A declaration is a claim about ISOLATION, a measurement is a fact about DETERMINISM, and only the first is grounds for co-tenancy. Nor can the ledger backstop the difference: its codes describe frames the gateway could not route, not state handed to the wrong session, so a wrong `recommend_share` here would never be refuted. What `measured` does carry is `recommend_stub` plus a verdict an operator can read, which is what separates "provoked and cleared" from "nobody looked".

**An OBSERVATION outranks everything; an inference outranks nothing.** That
ordering was once "a measurement always outranks a declaration", which had a server
advertising `kirocrew.caller-identity` come out `disqualified` because the
pre-flight caught it answering `initialize` differently per caller. The two halves
of that sentence contradicted each other: consuming the per-call caller block is
precisely what the capability declares, so a per-caller `initialize` result is the
declared feature working. More fundamentally, two spawns that both vary
`clientInfo` cannot isolate the variable, so the finding could never say whether
the cause was the caller or the server's own startup.

So a divergence is now a note that gates nothing, and the only thing that still
overrules a declaration is an entry in the hazard ledger — an event rather than an
inference. A server may pass the pre-flight and still be `refuted` later. The
engine (`mcp_gateway/shareability.py::assess`) is pure — no IO, no config, no clock
— so this ordering is verified by unit tests rather than by inspection.

`recommend_stub` and `recommend_share` are **separate outputs**. A stub keeps the backend 1:1 with the session (the same process topology as no gateway) and is what carries server-authored UI; sharing is the step that introduces co-tenancy. Weak evidence recommends the first and withholds the second.

The bulk stub action consults whichever of the two the global sharing switch makes true of a click, and it does so **server-side, inside the lock hold that performs the write** (`resolve_eligibility` on `POST /api/mcp-gateway/servers/stub`). The client sends candidate names and reads `stubbed` / `skipped` back. This is a placement decision, not a detail: a client can only answer for the moment it read its rows, while sharing is a separate switch another dashboard or the CLI can flip and the verdicts themselves move as measurements land. Deciding in the browser therefore leaves a window between the decision and the write, and each guard against that window — re-read the switch, bypass the query cache, compare-and-set the expected state — closes one instance while leaving the shape that produces them. Resolving inside the hold means one config state decides both, and the eligibility rule has exactly one implementation.

## Session-bound by construction, which is not the same as "ours"

The `disqualified` reason `first_party_session_scoped` names a server that resolves the calling session from its own PROCESS — an env var, a pid walk — so one backend can only ever serve one session correctly. It is decided by which servers actually consume the injected caller block, not by matching a name against Kiro Crew's managed set: keying it on authorship once disqualified `kirocrew-core` — which advertised the extension and consumed the block from the start — for a property only its unadopted siblings had. All four managed servers now advertise and consume the block (`kirocrew-core` from the start, `kirocrew-cron` since #4622's fix, `kirocrew-computer` and `kirocrew-dashboard` since #4659). Advertising is necessary but not sufficient for the shareable classification: `kirocrew-computer` stays session-bound deliberately, because a caller the gateway cannot name proceeds under `unresolved:<pid>` — #5322 gave each CONNECTION its own namespace there, but this classification is what `seed.py` turns into a config write and an ADOPTED pre-nonce daemon injects no nonce, so promotion waits on a negotiated guarantee rather than on the nonce alone. See `_MANAGED_SERVERS_ADVERTISING_BUT_WITHHELD` in `mcp_discovery.py`. The classification stays per-server because the next managed server starts unadopted, exactly as these did.

`mcp_discovery.managed_server_is_session_bound` answers from a module-level set, `_MANAGED_SERVERS_CALLER_AWARE`, and deliberately imports nothing: the assessment must answer WITHOUT a handshake (the probe cannot spawn on every host — Windows, macOS >= 26 — and has not run at all before the first probe cycle), and it is consulted on every render, so importing a server module to read its `ADVERTISE_CALLER_IDENTITY` would execute package code from an editable checkout on a path the sandbox never confines. `test/test_mcp_managed_caller_identity.py::test_the_classification_reads_no_module_at_import_time` pins that refusal.

The drift this trades for is covered where running the code is the point rather than a hazard: `test/test_mcp_managed_caller_identity.py` drives each server's real serve entry point in the TEST process and asserts the set agrees with the `advertise_caller_identity` argument actually handed to `run_mcp_stdio_loop`, since the two disagreeing is silent in both directions. `ADVERTISE_CALLER_IDENTITY` remains each server module's own declaration and the argument it passes to the shim; it is simply not read at classification time.

## What the pre-flight decides, and what it cannot

`mcp_gateway/preflight.py` spawns the server twice with two different `clientInfo` identities and compares every facet a pooled backend replays: the SHAPE of the advertised capabilities — which keys exist and which boolean flags are set — plus the negotiated `protocolVersion`, the shape of `serverInfo`, and the `tools/list` answer. The tool list matters because it is mandatory in every revision of the protocol while tool ANNOTATIONS only exist from MCP 2025-03-26, so it is the one facet decidable on a server too old to describe its own tools; the probe already issues that request, so comparing it costs no extra spawn. Tool names compare as a SET, because enumeration order is not a promise a replayed answer keeps. Annotations compare as an unordered MULTISET of shapes and are never paired with a tool name: the prober derives `tools` and `tool_annotations` from the same list under independent predicates (a truthy name, an `annotations` dict), so a tool with an empty name is dropped from one and kept in the other and the two lists can share a length while describing different tools. Alignment is therefore not recoverable on the consuming side, whether by index or by first comparing lengths, so the question is removed instead of answered — a differing claim is still detected, while a reorder, a partial list and a mismatched filter become unobservable rather than a wrong verdict. Per-tool attribution costs nothing to give up because one reason code covers every facet and no caller reads which tool diverged. Each shape is canonicalised to a string before ordering, since the projection yields dicts and ordering dicts raises. Free-form leaf values (a build id, a session token) collapse to a presence marker, because their value is not part of the contract a pooled backend must keep identical; comparing raw objects would report every such server as caller-sensitive and make the check useless.

Divergence is proof of caller sensitivity. This is the hazard `mcp_gateway/backend.py` documents when it caches the first stub's `initialize` result and replays it to later stubs, and which it describes as unverifiable at runtime — it is verifiable offline, which is the whole point of this module.

`PreflightResult.ran` separates "answered no" from "could not ask". A server that needs a credential, a tunnel or a display this host lacks did not fail the check, and collapsing the two would mark much of a fleet unshareable. Callers MUST branch on `ran` before reading `caller_sensitive`.

Not caught, deliberately: state that appears only after real work (a server that starts keying on the caller once someone authenticates) and behaviour that needs genuine concurrency. Those remain the ledger's job, which is why the ledger outranks the pre-flight rather than being its fallback.

The pre-flight NEVER runs on a request path — it spawns processes — and never mutates the `McpServerInfo` it is given, so it cannot overwrite the status or tool list the dashboard is showing. A probe run under a synthetic identity is also excluded from the shared per-name probe cache, for the same reason.

## Where the records live

Both files sit in the gateway's per-host records directory, resolved by one
helper (`rewriter.records_dir`) that BOTH the writer (gatewayd) and the reader
(the dashboard) call:

- a configured `mcp_gateway.socket_path` wins, so gatewayd's ledger lands beside
  its real socket and the page reads the same place;
- otherwise `$KIROCREW_HOME/mcp-gateway` — the directory the socket itself
  defaults into.

The fallback is load-bearing, not tidiness. `socket_path` is empty until a broker
has been configured, and these records must exist BEFORE that: their whole
purpose is to tell an operator who has **not** enabled stubbing whether it would
be safe to. Deriving the directory from the socket made the feature inert for
exactly its audience — no verdicts, no recommendation, ever.

A bare `"."` counts as unset alongside the empty string, because `Path("")`
constructs to `PosixPath(".")` and the two are indistinguishable afterwards;
branching on truthiness alone would put the ledger in whatever directory the
process happened to start in.

## Durable contract 1 — observed-hazard ledger (schema v1)

Path: `<records dir>/observed-hazards.json`, a sibling of `hot-keys.json`. Writer: gatewayd (`mcp_gateway/hazards.py`). Reader: the dashboard.

```json
{"schema": 1, "servers": {"<name>": {
  "identities": {"<hash>": {"codes": ["..."], "count": 3, "lastSeen": 1.0}},
  "codes": ["..."], "count": 3, "lastSeen": 1.0
}}}
```

`identities` is the authoritative shape; the flat `codes`/`count`/`lastSeen` beside it are its union, written for a reader that predates the map. The version is deliberately NOT bumped: Make Live can put an earlier worktree back in front of the same data home, and a version-gated shape change would read as "future, ignore" there and silently drop every observation. Emitting both under one version costs a derived duplicate — built from the same records in the same payload, so the two cannot disagree — and keeps a downgrade readable. A reader that finds no `identities` treats the flat fields as one record under the empty identity, which is exactly how an older writer's file is absorbed.

Codes are a closed vocabulary; an unknown code is refused on write and dropped on read, because a typo that silently became a permanent hazard would disqualify a server for ever with no way to read why:

- `unroutable_server_request` — a server-initiated request arrived with no `relatedRequestId` while more than one stub shared the backend. Unroutable without a cross-tenant leak, so the backend is recycled.
- `unattributable_notification` — a request-scoped notification could not be attributed to a pending call, so it was dropped rather than broadcast.

Both are recorded ONLY for a shared backend (`exclusive_token` empty). A 1:1 backend legitimately owns its single client, so the same frame there proves nothing, and recording it would disqualify servers for behaviour that is correct when unshared.

A missing, unreadable or future-schema file reads as "nothing observed" — never as "safe". Recording is in-memory and safe on the event loop; the flush is blocking IO and runs off-loop from the heartbeat sweep. Prior observations are reloaded when the sink is installed, so a daemon restart does not forget them.

**Invalidation is tied to change, never to age.** Observations are stored per launch `identity` — the command/args, effective env and binary fingerprints the pool key already holds, so the recording site stamps one without doing IO on the loop. Each identity accumulates independently and nothing is ever moved or discarded across identities, because two backends for one NAME can be live at once: a blue/green drain keeps the outgoing build serving its attached stubs while the incoming one starts, so both can still record. A store that collapsed them into one row per name would let a late frame from the draining backend overwrite what the new build had already observed, and that reads as "nothing observed" for the build actually being kept — the permissive direction.

Invalidation therefore happens on the READ: a launch whose identity has no record of its own reads as unobserved, so upgrading or reconfiguring a server clears its verdict without any write-side deletion. The identity deliberately excludes the pre-flight schema — a hazard is an observation of real traffic and stays true however the prober evolves, so folding that field in would let a schema bump erase evidence the gateway actually witnessed.

There is no expiry by age, and that is a decision rather than an omission: a server that personalises state per caller does not become safe because a month passed, so ageing an observation out would manufacture a "safe to share" that nothing observed. The one case identity cannot cover is a frame THIS gateway misattributed — our own defect, which changes nothing about the server — so `clear(server_name)` is the explicit operator path for dropping evidence that is otherwise still current, and the only way to drop it.

Row count is bounded by the number of distinct builds of a server that **actually misbehaved**: an identity appears only once a hazard is observed under it, so a well-behaved server never occupies space however often it is reconfigured. No cap is applied, deliberately — any age- or count-based eviction can evict the CURRENT build's record while a chatty outgoing one survives, which is the failure this shape exists to prevent.

A file written **without** `identities` — by an older build, or by one this build later hands back to an older one — still loads. Its flat fields become one record under the empty identity: surfaced by the name-only read so the dashboard keeps showing the withdrawal, and matching no launch for the identity-checked read, which is the honest treatment of evidence that cannot be attributed. The name-only read (`codes_for_name`) unions across identities; that is a bounded difference in the safe direction — a row may show a withdrawal the next probe clears, never a recommendation the evidence contradicts.

Because the flush runs off-loop while `record` keeps running ON it, an observation can land between building the payload and clearing the dirty flag. The generation counter is captured with the payload and dirty is cleared only if nothing moved in between: clearing unconditionally would drop that observation permanently, leaving the ledger looking clean while the file lacked the entry — a hazard the gateway actually saw that never withdraws a recommendation.

## Durable contract 2 — local pre-flight cache (entry schema v2)

Path: `<records dir>/shareability-verdicts.json`. Writer and reader: this host only; never shipped, never exported.

```json
{"entries": {"<key>": {"ran": true, "callerSensitive": false, "reasons": [], "evaluatedAt": 1.0, "reportedVersion": "1.2.3"}},
 "applied": ["<server name>"]}
```

**A stored row also records the version the server reported for itself**, as a second validity input kept OUT of the execution identity. It closes the one blind spot a launch fingerprint cannot see: a runtime-resolved launch (`npx some-server@latest`) keeps command, env and interpreter byte-identical while the code behind it is replaced upstream. It may only INVALIDATE, and only when both sides know a version and they differ — an absent version means "no information", so one unprobeable pass cannot discard a good measurement. It is not identity material because the identity is what `PoolKey` is built from and a self-reported string is not part of a pool key. A mismatch DROPS the row rather than only hiding it from that read, because the dashboard row builder is IO-free and reads rows by name without checking either validity input — a row left in place would still be rendered, as a measurement of code that has been replaced and in the permissive direction. Dropping is confined to this branch: an identity mismatch keeps the row, because `binary_version` is the string `unknown` for a binary mid-install, an `OSError` and a `which` miss alike, so there a mismatch can mean "could not tell" and dropping would discard a good measurement on a transient read. The version is whatever the server chose to call itself, so where it reaches a log line it is escaped rather than interpolated raw and bounded in length: a newline would otherwise forge a second gateway log entry and an ESC would recolor the surrounding output. Escaping is preferred over dropping those characters because it keeps the evidence that the server sent them.

**Only the expensive measurement is cached, never the composite verdict.** The cheap evidence — declared env names, advertised capabilities, observed hazards — is re-read on every request and recombined by `assess`, so a hazard observed a minute ago changes the answer immediately without invalidating anything here. Caching the composite would go stale the moment the ledger grew an entry.

An entry that cannot say whether the pre-flight `ran` is dropped on read: treating it as "ran" would fabricate evidence.

**Entry key** is the execution identity, NUL-joined: `server_name`, `command_args_hash`, `env_hash`, `binary_version`, `SCHEMA`. Reusing the hashes `PoolKey` is built from means "the MCP was upgraded" or "its env changed" invalidates the measurement for free, and — because `hash_effective_env` drops secret keys — a credential rotation does NOT look like a different server here either. A name-keyed cache would keep serving a conclusion about a binary that no longer exists. `SCHEMA` is part of the key rather than a file-level version so a smarter pre-flight re-derives instead of inheriting, and a mixed-version rollout degrades to re-evaluation instead of a wipe. A row whose stored identity carries a different `SCHEMA` is DROPPED when the file is read, not merely refused on lookup: the dashboard row builder reads rows by name without checking either validity input, so a surviving row would render as a verdict, its server would count as measured, and the action offering to measure it would be disabled -- on exactly the installs a schema bump exists to re-derive. Dropping is unconditional here because `SCHEMA` is this build's own constant and cannot differ for a transient reason, unlike a binary fingerprint.

`binary_version` is the pool's own fingerprint (`stub.binary_fingerprint`), not a placeholder, and that matters for the **in-place upgrade**: same path, same args, new bytes. Without it the key hits, the pre-flight never re-runs, and a binary that BECAME caller-sensitive hands its first caller's `initialize` result to every co-tenant. Deferring that case to the hazard ledger is not equivalent — the ledger only fires after a session has already lost its tools, which is the outcome this feature exists to prevent.

One row per configured server, keyed by NAME, with the execution identity stored as a field inside the row. Reading compares that field against the server's current identity and treats a mismatch as no measurement, so an upgraded binary or an edited command still forces a re-measurement. Keying by identity instead would make one server add a NEW row on every command edit and every binary upgrade, so the file would grow with config churn and need a size cap, an eviction policy, and a newest-wins rule for readers who only know a name; overwriting one row removes all three, and the row count follows the config rather than the history.

**`applied` is keyed by NAME, not by execution identity** — deliberately. It records that a server's recommendation has already been written into config, and an operator who switched a server off must stay switched off across an upgrade of that server; an identity-keyed marker would re-flip it the moment the binary version changed. Applied markers are NOT pruned when a server disappears from config, because a server removed and re-added should not be seeded twice: it may have been removed precisely to stop it being stubbed.

Absent or corrupt cache reads as empty, which costs a re-evaluation and never a wrong answer.

## When evaluation happens, and what it costs

`mcp_gateway/evaluate.py` owns the one policy question the layers below refuse to answer: which servers are worth paying for. Only servers whose stored measurement is missing or taken under a different identity are provoked, so the steady state is a file read and a newly installed MCP costs two spawns once. A single pass provokes at most **two** servers — four short-lived processes, fanned out under the prober's own concurrency ceiling — so a machine that just had twenty MCPs added covers them over ten probes instead of paying forty spawns inside one request. Until a server is measured it reads as `unknown`, which is the honest answer rather than a delay.

It runs from `POST /api/mcp/probe` — the explicit action that already spawns every configured server — and never while rendering a page. A failure there is swallowed: the probe's own contract is status and tools, and a shareability problem must not cost the operator the answer they asked for.

`MAX_EVALUATIONS_PER_PASS` bounds a pass reached from a REQUEST, so a machine that just had twenty MCPs added does not spend a spawn per server while somebody waits for a page; the remainder are evaluated on the next pass and read as `unknown` until then, which is the honest answer. An operator who asks for everything gets an UNCAPPED pass instead: `POST /api/mcp/measure` runs it as a background job and `GET /api/mcp/measure` reports `{running, done, measured, total}` so the wait is visible rather than hidden in a request. `done` counts servers ATTEMPTED and `measured` counts those that produced a verdict; they are separate fields because a pre-flight that could not run leaves no row on purpose, so that server is still unmeasured when the pass ends. A progress line has to advance on `done` or it would sit at zero for a whole pass in which nothing could be reached, while any claim about the OUTCOME has to be built from `measured` — reporting one number for both is what let a pass that measured nothing close by saying it had measured everything it tried, beside a table still showing every row as unmeasured and a button still offering the same count. When `measured` is zero the closing line is withheld entirely rather than rendered with a zero: silent, but never false, and the button's own unchanged count is what tells the reader nothing landed. A BUDGETED pass does not queue behind an uncapped one: the uncapped pass runs for minutes while the budgeted caller is a request whose status and tools are already computed, so it takes the lock only if free and otherwise serves stored rows. Nothing is lost by yielding -- its own cap meant it would measure a couple of servers, "not measured yet" is a state it already renders, and the pass holding the lock is measuring the same servers. One pass runs at a time — the store is rewritten whole, so two overlapping passes would each flush their own copy and the later one would drop the other's rows. A disabled server is never provoked — probing is the act consent gates. Measuring one server is isolated from the pass: every facet compared is JSON that server chose, walked by code that recurses, so the ways a hostile or broken payload can make the walk raise are not enumerable in advance. Any failure to measure a server therefore resolves to that server being unmeasurable rather than propagating, because the pass flushes only once at the end and an escaping error would discard every verdict already paid for at two spawns each. Cancellation is not a measurement outcome and passes through.

## The row builder does no IO

`GET /api/mcp-gateway/servers` loads both records ONCE, off the event loop, before it builds any row; `_assess_server` is pure and takes the loaded state as arguments. Reading per row put N synchronous parses of the cache on the loop for an N-server config — enough to stall the dashboard and every chat sharing that loop — and it also let two rows in one payload disagree about the same file. Both properties are pinned by structural tests, because reproducing the stall needs a large config and a loaded host while the property itself is exact.

## Seeding: designed, present, not yet wired

`mcp_gateway/seed.py` turns verdicts into configuration, and it has **no production
call site** — nothing on the startup path calls `plan_seed` or `apply_seed`, so a
start does not change `mcp_gateway.stub_servers` today. The module and its tests
ship ahead of that wiring because the wiring needs the config lock and deserves its
own review; the section below is the contract it will honour when a caller exists,
not behaviour a reader can observe now.

Three properties, each chosen so the operator is not surprised:

- **At start, never mid-run.** Nothing changes under a live session, so the failure this feature exists to prevent — a chat losing a server's tools because its backend was recycled — cannot happen as a side effect of seeding. No live re-apply path is needed either: `SessionManager.refresh_defaults` deliberately does not retrofit running sessions.
- **Written into the config, not acted on implicitly.** The MCP Management page renders the resolved stub set, so materialising the verdict there is what makes the row's toggle show the true state. The operator sees the decision in a file they own. A gateway that routed differently from what the page claimed would be the dishonest alternative. The decision lands in `mcp_gateway.stub_overrides` — the roster in `mcp_gateway.stub_servers` belongs to whoever ships it, and the page resolves the two together.
- **Once per server.** After the first seed the config is the operator's; a later start that finds the same recommendation must not undo their choice.

Seeding never turns the global `mcp_gateway.enabled` sharing switch on by itself. A recommendation to share is reported in `SeedPlan.wants_share` and left for a separate decision, because flipping that switch changes topology for every stubbed server, including ones stubbed for unrelated reasons.

`plan_seed` is pure and returns a plan; `apply_seed` mutates a loaded config section and records the markers. The caller owns reading, locking and writing the file, so the same logic serves the tests and the gateway with no second implementation.

## Reason codes

`Reason` separates a stable `code` from a `detail`. The code is the enum-like vocabulary the UI translates; the detail carries verbatim observation — an env variable name, a capability path — and is NOT translated.

That split is also the export contract. The telemetry layer requires low-cardinality enum-like attribute values and forbids user content, so any future aggregation may carry `code` and never `detail`: a server name or an env variable name must not leave the host. Remote aggregation is out of scope here; the OTLP path already exists and is opt-in and local-only by default.

| Code | Strength | Ground |
|---|---|---|
| `observed_hazard` | refuted | Ledger entry; detail is the hazard code. **One of only two durable grounds for refusing to share, because it happened rather than being inferred.** |
| `not_stdio` | disqualified | No stdio pipe — out of scope, not unsafe. **The other durable ground, and the only remaining `disqualified` code.** |
| `handshake_not_reproducible` | note | Pre-flight saw a divergent replayed surface (capabilities, protocol version, `serverInfo` shape, or the tool list). Reported and gates nothing: see below. |
| `session_bound_by_construction` | disqualified | A managed server that resolves its caller from its own process rather than the injected caller block -- or that consumes the block yet is deliberately withheld from the shareable classification. Kept gating because `mcp_cron._check_cron_job_ownership` treats a falsy session key as *allow*, so a pooled backend reading EMPTY skips the ownership check rather than merely losing a feature -- and no routing-shaped hazard code would ever record it. Current producer: `kirocrew-computer` (advertises; #5322 gave its unnamed co-tenants per-connection namespaces on a current gateway, but an adopted pre-nonce daemon injects none). |
| `rotating_secret_env` | note | Declares an env name excluded from the pool key. Detail is the name. Not emitted for a name in `mcp_gateway.pool_identity_env`, which IS in the pool key. |
| `degrades_when_shared` | note | `logging`; detail is `logging_level`. |
| `not_probed` | unknown | No handshake was observed. |
| `declares_caller_identity` | declared | Advertises the caller-identity extension. |
| `preflight_passed` / `preflight_not_run` | declared | Whether the declaration has actually been tested. `preflight_passed` is emitted only when the pass ran AND found no divergence, so it never appears beside `handshake_not_reproducible`. |
| `all_tools_read_only` | supporting | Every tool declares `readOnlyHint`. Positive evidence only. |
| `no_tool_annotations` | supporting | Annotations were unavailable; detail is the negotiated protocol version. |
| `no_objection_found` | no_objection | Nothing disqualifying was found. |
| `no_tools_listed` | supporting | `tools/list` produced nothing. |

### Why four of those are notes rather than disqualifiers

This layer exists to turn pooling ON for an operator who was never going to
hand-pick which servers may share a backend. That makes the cost asymmetric in a
way that runs opposite to the usual intuition: a wrong *yes* produces a hazard the
broker observes, retreats from and records, while a wrong *no* produces nothing at
all — the operator was not sharing anyway — and costs the layer its entire reason
to exist. Caution here is the failure mode, not the virtue.

Measured against that, four codes were disqualifying on an inference:

- `handshake_not_reproducible` — two spawns that both vary `clientInfo` cannot
  separate "computed from the caller" from "varies for the server's own reasons"
  (startup feature detection, a reachability probe). The second kind gives every
  co-tenant of one process the same answer, exactly as an unpooled process does
  within its lifetime. The row is also re-derived every pass rather than frozen, so
  a wrong one lives until the next measurement instead of for ever.
- `degrades_when_shared` — the capability does not leak, because
  `backend._notification_owner` already DROPS an unattributable request-scoped
  notification rather than broadcasting it. And it is not a property of the
  server: it is a gap in this broker. Log verbosity is fixable by emitting at the
  finest level any tenant asked for and filtering down per stub.
  `notifications/resources/updated` carries no entry here because it needs none:
  the broker keeps the `uri -> {stub_uuid}` subscription table
  (`backend._resource_subscriptions`) and delivers each update to exactly the
  stubs subscribed to its URI. The table's contract is delete-not-relax — an
  entry leaves the moment the broker learns to attribute the frames it covers.
- `rotating_secret_env` — a secret-prefixed key is never forwarded into a shared
  backend at all (`gatewayd._declared_non_secret_env`), so a pooled backend
  receives *nobody's* secret rather than the wrong session's. What pooling costs is
  a server that authenticates from declared env; one following the documented
  pattern (read the credential from disk) declares the key without needing it and
  pools fine, and the declaration cannot tell those apart. Naming the variable in
  `mcp_gateway.pool_identity_env` is how an operator resolves that ambiguity for a
  server that genuinely does authenticate from env: the key joins the pool key, so
  it is forwarded, `_withheld_env_count` stops counting it, the rewriter pools the
  entry — and this note goes with it. The withholding tracks the guard that
  actually runs in BOTH directions; leaving the note would make the page decline a
  share the broker performs.
`session_bound_by_construction` is NOT in that list. It looks like the same
shape -- a feature that stops working -- but the consumer decides what EMPTY means,
and `mcp_cron._check_cron_job_ownership` returns *allow* on a falsy session key, so
a pooled cron skips ownership entirely. That is a cross-session authorization
failure, and it is the one case the retreat cannot backstop: both hazard codes
describe frames that could not be routed, so serving the wrong session's data
produces no record. The producers have narrowed as managed servers adopted the
caller block (#4622, #4659); today the code has one deliberate producer --
`kirocrew-computer`, which advertises and consumes the block, and whose UNNAMED
co-tenants #5322 separated per CONNECTION on a current gateway but not on a
pre-nonce daemon `manager.py` adopted across an upgrade -- and it remains the
conservative default for the next managed server, which starts unadopted exactly
as the others did.

`*.listChanged` is deliberately absent from all of the above: those notifications
are global broadcasts (`backend._GLOBAL_BROADCAST_NOTIFICATIONS`) and are safe to
fan out to every attached stub.

Degradation detection is by PRESENCE of the capability key: in MCP an empty
object is the standard way to advertise a capability that takes no sub-options,
so `{"logging": {}}` means `logging/setLevel` IS supported, and truthiness would
read a server that advertises logging as one that does not. What logging costs on
a shared backend is that the last caller's level wins and a log line tied to one
caller's in-flight call is dropped — noisier logs and missing lines, which is why
it is a note. A flag-leaf entry — one whose leaf can be `false`, the server
explicitly declining the capability — needs a truthiness test instead, and adding
one to `_SHARED_BACKEND_DEGRADATIONS` means adding a per-entry detection mode
alongside it. `resources.subscribe` is deliberately NOT in the table: the broker
keeps a `uri -> {stub_uuid}` subscription table (`backend._resource_subscriptions`)
and delivers each `notifications/resources/updated` to exactly the stubs
subscribed to its URI, so subscriptions survive pooling and there is nothing to
warn the operator about.

Tool annotations (`readOnlyHint` and friends) exist only from MCP 2025-03-26 onward, while the handshake negotiates `2024-11-05`. They are therefore treated as opportunistic positive evidence: present means something, absent means nothing. The negotiated version is deliberately unchanged — raising it alters real handshake semantics with third-party servers, which is a separate decision that should be made with data (the recorded `protocolVersion` is what will supply it).
