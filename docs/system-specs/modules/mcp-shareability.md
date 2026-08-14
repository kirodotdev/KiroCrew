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
| `no_objection` | absence of any of the above | Nothing disqualifying was found — the weakest useful verdict. |
| `unknown` | nothing observed | Not enough evidence to say anything. |

A measurement always outranks a declaration, and an observation always outranks a measurement. Concretely: a server may advertise `kirocrew.caller-identity` and still be `disqualified` because the pre-flight caught it answering `initialize` differently per caller, and it may pass the pre-flight and still be `refuted` later. The engine (`mcp_gateway/shareability.py::assess`) is pure — no IO, no config, no clock — so this ordering is verified by unit tests rather than by inspection.

`recommend_stub` and `recommend_share` are **separate outputs**. A stub keeps the backend 1:1 with the session (the same process topology as no gateway) and is what carries server-authored UI; sharing is the step that introduces co-tenancy. Weak evidence recommends the first and withholds the second.

## What the pre-flight decides, and what it cannot

`mcp_gateway/preflight.py` spawns the server twice with two different `clientInfo` identities and compares the SHAPE of the advertised capabilities — which keys exist and which boolean flags are set. Free-form leaf values (a build id, a session token) collapse to a presence marker, because their value is not part of the contract a pooled backend must keep identical; comparing raw objects would report every such server as caller-sensitive and make the check useless.

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

## Durable contract 2 — local pre-flight cache (entry schema v1)

Path: `<records dir>/shareability-verdicts.json`. Writer and reader: this host only; never shipped, never exported.

```json
{"entries": {"<key>": {"ran": true, "callerSensitive": false, "reasons": [], "evaluatedAt": 1.0}},
 "applied": ["<server name>"]}
```

**Only the expensive measurement is cached, never the composite verdict.** The cheap evidence — declared env names, advertised capabilities, observed hazards — is re-read on every request and recombined by `assess`, so a hazard observed a minute ago changes the answer immediately without invalidating anything here. Caching the composite would go stale the moment the ledger grew an entry.

An entry that cannot say whether the pre-flight `ran` is dropped on read: treating it as "ran" would fabricate evidence.

**Entry key** is the execution identity, NUL-joined: `server_name`, `command_args_hash`, `env_hash`, `binary_version`, `SCHEMA`. Reusing the hashes `PoolKey` is built from means "the MCP was upgraded" or "its env changed" invalidates the measurement for free, and — because `hash_effective_env` drops secret keys — a credential rotation does NOT look like a different server here either. A name-keyed cache would keep serving a conclusion about a binary that no longer exists. `SCHEMA` is part of the key rather than a file-level version so a smarter pre-flight re-derives instead of inheriting, and a mixed-version rollout degrades to re-evaluation instead of a wipe.

`binary_version` is the pool's own fingerprint (`stub.binary_fingerprint`), not a placeholder, and that matters for the **in-place upgrade**: same path, same args, new bytes. Without it the key hits, the pre-flight never re-runs, and a binary that BECAME caller-sensitive hands its first caller's `initialize` result to every co-tenant. Deferring that case to the hazard ledger is not equivalent — the ledger only fires after a session has already lost its tools, which is the outcome this feature exists to prevent.

One row per configured server, keyed by NAME, with the execution identity stored as a field inside the row. Reading compares that field against the server's current identity and treats a mismatch as no measurement, so an upgraded binary or an edited command still forces a re-measurement. Keying by identity instead would make one server add a NEW row on every command edit and every binary upgrade, so the file would grow with config churn and need a size cap, an eviction policy, and a newest-wins rule for readers who only know a name; overwriting one row removes all three, and the row count follows the config rather than the history.

**`applied` is keyed by NAME, not by execution identity** — deliberately. It records that a server's recommendation has already been written into config, and an operator who switched a server off must stay switched off across an upgrade of that server; an identity-keyed marker would re-flip it the moment the binary version changed. Applied markers are NOT pruned when a server disappears from config, because a server removed and re-added should not be seeded twice: it may have been removed precisely to stop it being stubbed.

Absent or corrupt cache reads as empty, which costs a re-evaluation and never a wrong answer.

## When evaluation happens, and what it costs

`mcp_gateway/evaluate.py` owns the one policy question the layers below refuse to answer: which servers are worth paying for. Only servers whose stored measurement is missing or taken under a different identity are provoked, so the steady state is a file read and a newly installed MCP costs two spawns once. A single pass provokes at most **two** servers — four short-lived processes, fanned out under the prober's own concurrency ceiling — so a machine that just had twenty MCPs added covers them over ten probes instead of paying forty spawns inside one request. Until a server is measured it reads as `unknown`, which is the honest answer rather than a delay.

It runs from `POST /api/mcp/probe` — the explicit action that already spawns every configured server — and never while rendering a page. A failure there is swallowed: the probe's own contract is status and tools, and a shareability problem must not cost the operator the answer they asked for.

`MAX_EVALUATIONS_PER_PASS` (8) bounds one pass, so a machine that just had twenty MCPs added does not spend forty spawns inside one request; the remainder are evaluated on the next pass and read as `unknown` until then, which is the honest answer. A disabled server is never provoked — probing is the act consent gates.

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
- **Written into `mcp_gateway.stub_servers`, not acted on implicitly.** The MCP Management page renders that key, so materialising the verdict there is what makes the row's toggle show the true state. The operator sees the decision in a file they own. A gateway that routed differently from what the page claimed would be the dishonest alternative.
- **Once per server.** After the first seed the config is the operator's; a later start that finds the same recommendation must not undo their choice.

Seeding never turns the global `mcp_gateway.enabled` sharing switch on by itself. A recommendation to share is reported in `SeedPlan.wants_share` and left for a separate decision, because flipping that switch changes topology for every stubbed server, including ones stubbed for unrelated reasons.

`plan_seed` is pure and returns a plan; `apply_seed` mutates a loaded config section and records the markers. The caller owns reading, locking and writing the file, so the same logic serves the tests and the gateway with no second implementation.

## Reason codes

`Reason` separates a stable `code` from a `detail`. The code is the enum-like vocabulary the UI translates; the detail carries verbatim observation — an env variable name, a capability path — and is NOT translated.

That split is also the export contract. The telemetry layer requires low-cardinality enum-like attribute values and forbids user content, so any future aggregation may carry `code` and never `detail`: a server name or an env variable name must not leave the host. Remote aggregation is out of scope here; the OTLP path already exists and is opt-in and local-only by default.

| Code | Strength | Ground |
|---|---|---|
| `observed_hazard` | refuted | Ledger entry; detail is the hazard code. |
| `caller_sensitive_initialize` | disqualified | Pre-flight measured divergent capabilities. |
| `not_stdio` | disqualified | No stdio pipe — out of scope, not unsafe. |
| `first_party_session_scoped` | disqualified | A managed server bound to the session key. |
| `rotating_secret_env` | disqualified | Declares an env name excluded from the pool key, so co-tenants can disagree on its value. Detail is the name. |
| `per_client_capability` | disqualified | `resources.subscribe` or `logging`; detail names which. |
| `not_probed` | unknown | No handshake was observed. |
| `declares_caller_identity` | declared | Advertises the caller-identity extension. |
| `preflight_passed` / `preflight_not_run` | declared | Whether the declaration has actually been tested. |
| `all_tools_read_only` | supporting | Every tool declares `readOnlyHint`. Positive evidence only. |
| `no_tool_annotations` | supporting | Annotations were unavailable; detail is the negotiated protocol version. |
| `no_objection_found` | no_objection | Nothing disqualifying was found. |
| `no_tools_listed` | supporting | `tools/list` produced nothing. |

`*.listChanged` is deliberately absent from the disqualifiers: those notifications are global broadcasts (`backend._GLOBAL_BROADCAST_NOTIFICATIONS`) and are safe to fan out to every attached stub.

Two DIFFERENT detection modes, because a capability and a flag inside one are different claims:

- `resources.subscribe` is a **flag** — `{"subscribe": false}` is the server explicitly saying it does not subscribe, so truthiness is the right test and an explicit `false` must not count against it.
- `logging` is a **capability** — in MCP an empty object is the standard way to advertise one that takes no sub-options, so `{"logging": {}}` means `logging/setLevel` IS supported, and that level is process-wide: one co-tenant's call changes what every other session receives. Presence is the right test.

Tool annotations (`readOnlyHint` and friends) exist only from MCP 2025-03-26 onward, while the handshake negotiates `2024-11-05`. They are therefore treated as opportunistic positive evidence: present means something, absent means nothing. The negotiated version is deliberately unchanged — raising it alters real handshake semantics with third-party servers, which is a separate decision that should be made with data (the recorded `protocolVersion` is what will supply it).
