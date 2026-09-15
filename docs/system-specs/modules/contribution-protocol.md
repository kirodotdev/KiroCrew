# Contribution protocol

Owners: `kiro_crew.eventlog` (log, projections, `contrib`, `grants`), `kiro_crew.dashboard.handlers.eventlog`, `kiro_crew.dashboard.eventlog_ws`, `website/src/state/memberProjectionStore.ts`

Status: implemented for kind `member`. The three workstreams build against this surface; anything a
workstream needs that is not here is a change to THIS file first.

## 1. Purpose

An out-of-process contributor — an app backend, or an adapter hosting a foreign plugin model — can read a
unit's append-only log, append events into it under its own namespace, and publish projected views that
the gateway treats exactly like its own. The gateway stays the only writer of every log, enforces
ownership and quota at the boundary, and pushes contributed views to dashboards with the same
whole-value frames it uses for built-in projections. Nothing in this protocol depends on the
contributor's language.

The unit kinds are those with a log. Today: `member`. The protocol is written for any kind: `kind` is a
path segment resolved against a registry (`eventlog.contrib.register_unit`), so a second kind is a
registration rather than a second set of routes.

## 2. Identity and authority

A contributor authenticates as an **app** with the app token it already holds (exchanged from its
`.app_secret` at `POST /api/apps/{name}/token`). Everything it may do is declared in its manifest:

```json
"contributions": {
  "events":      ["myapp/*"],
  "projections": ["myapp/*"],
  "units":       ["member"]
}
```

- An event `type` a contributor appends MUST match one of its declared `events` patterns, and every
  pattern MUST begin with the app's own name followed by `/`. The gateway refuses anything else. The
  prefix is re-checked at USE, not trusted from install: a manifest is a file an app trusted to run code
  can rewrite.
- A projection `key` a contributor publishes MUST match one of its declared `projections` patterns,
  same prefix rule. Built-in keys (`roster`, `activity`, `wake`, `driving`) cannot be published from
  outside.
- `units` lists the kinds the contributor may subscribe to and append to. An app that declares none
  can do nothing here. Declaring `events` or `projections` with no `units` is refused at install.
- Patterns glob with `*` and match case-sensitively.
- Reading is granted per kind, not per event type: a subscriber sees every event of a unit it may
  subscribe to. Sensitive data therefore does not belong in an event; it belongs in the durable store the
  event points at.
- A DISABLED app is refused everything, even though its token still validates, and disabling
  invalidates the cached grant immediately rather than at the end of its TTL.

Declaring `contributions` grants the HTTP paths and frames below; no separate `permissions.api` entry is
needed. Widening a declaration on upgrade is a new consent, handled like any other widened permission —
the declaration is covered by the manifest signature, so widening it on a signed app invalidates that
signature.

The log's own event vocabulary admits `<namespace>/<name>` for any namespace the built-ins do not own
(`member`, `activity`, `slot`, `patrol`). A type inside a reserved namespace that is not a known
built-in is a typo, not a contribution, and is still refused.

## 3. Reading: catch-up then stream

```
GET /api/eventlog/{kind}/{id}/events?after=<seq>&limit=<1..500>
  -> { "kind", "id", "<idField>", "events": [envelope...], "lastSeq": n }   oldest first, seq > after
```

`after` defaults to -1 (from the beginning); `limit` defaults to 200. An out-of-range `limit` is
refused rather than clamped: a consumer that asked for 5000 and silently received 200 would read a
short page as the end of the log. The response carries the id twice — once as `id` and once under the
kind's own field name (`slug` for a member) — so a consumer can key on either.

Over the app's WebSocket:

```
-> { "type": "eventlog_subscribe",   "data": { "kind", "id" } }
<- { "type": "eventlog_subscribed",  "data": { "kind", "id", "<idField>", "lastSeq": n } }
<- { "type": "eventlog_event",       "data": { "kind", "id", "<idField>", "event": envelope } }
-> { "type": "eventlog_unsubscribe", "data": { "kind", "id" } }
```

A REFUSED subscribe answers `eventlog_subscribed` with `code` and `error` and NO `lastSeq`, rather than
closing the socket: that socket multiplexes everything else the app uses, and a contributor that asked
for the wrong kind needs to be told, not dropped.

`eventlog_subscribed` is sent before any `eventlog_event` for that subscription. The event channel is a
**delta channel**: the consumer MUST check `event.seq === last + 1` and, on a gap, drop its fold and
re-read from `GET .../events?after=` — never fold across a gap. A subscriber that disconnects resumes
by catch-up from its last folded seq, then re-subscribes. The gateway closes a slow subscriber (256
frames queued) rather than growing its queue; a closed subscriber resumes the same way. One socket may
hold at most 64 subscriptions.

## 4. Appending

```
POST /api/eventlog/{kind}/{id}/events
  { "type": "myapp/thing-happened", "data": {...} }
  -> 201 envelope            | 403 code=event_type_not_owned | 404 code=unit_not_found
                             | 413 code=event_too_large | 429 code=quota_exceeded
```

The gateway assigns `seq` and `time`; a contributor never supplies them, and a supplied value is
ignored rather than refused — the returned envelope carries the authoritative pair. `data` is limited
to 64 KiB serialized. Each app has a per-unit event budget (default 10,000 events per unit per UTC
day); over budget is refused, never queued, and charged BEFORE the write so a refusal never spends it.

The budget lives in gateway memory: it bounds one process's exposure to a runaway contributor, and the
64 KiB cap already bounds the resource a persisted counter would protect.

## 5. Publishing a projection

```
POST /api/eventlog/{kind}/{id}/projections/{key}
  { "value": <json>, "seq": n, "stateVersion": v }
  -> 204                      | 403 code=projection_key_not_owned | 409 code=stale_seq
                              | 413 code=projection_too_large
```

`seq` is the log position the view is current as of. The gateway keeps one row per `(kind, id, key)`:
a publish with `seq` lower than or equal to the stored row is refused with `409 stale_seq` (a replay,
or a slower contributor); higher wins and is pushed to dashboards as the existing
`member_projection` frame `{slug, key, value, seq, schema?}` (for kind `member`; other kinds get a frame
of the same shape named for the kind). Contributed rows appear in the unit's `projections.values` block
next to built-in keys, so a dashboard needs no new code path to receive them.

`value` is limited to 640 KiB serialized — ten times the event cap, because a folded view legitimately
summarises many events, but not unbounded: this value is pushed whole to every dashboard socket.

`stateVersion` is the contributor's fold version. A publish with a higher `stateVersion` than the stored
row replaces it regardless of `seq`, so a contributor that changed its fold can re-publish from zero. A
LOWER `stateVersion` is refused as stale.

Rows are durable. A contributor publishes on its own cadence, so an in-memory table would blank every
contributed card on a gateway restart and leave the page empty until that contributor happened to
re-fold.

The `projections` block on a roster row therefore carries three maps:

```
"projections": {
  "asOfSeq": 12,
  "values":  { "roster": {...}, "myapp/count": {...} },
  "seqs":    { "myapp/count": 7 },
  "schemas": { "myapp/count": {"kind": "badge"} }
}
```

`seqs` and `schemas` are present only for contributed rows. `seqs` is REQUIRED rather than cosmetic: a
contributed row's seq is the contributor's own fold position, not the response's `asOfSeq`, and a client
that seeded it at `asOfSeq` would drop the contributor's next live push under the higher-seq-wins rule
and freeze the card at its baseline.

Folding happens in the contributor's process, against events read through §3. The gateway never
executes contributor code.

## 6. Teardown

Disabling or uninstalling an app: the gateway invalidates the app's grant, closes the app's
subscriptions (the socket itself, not just the subscription — the app's code is being stopped), deletes
every projection row the app published, and pushes a frame with `value: null` for each key. Events the
app appended stay in the log — they are history, and the log is never rewritten.

The order matters: the grant comes off FIRST, before the app's own shutdown hooks run, so an in-flight
append cannot land after the rows it folds into are deleted.

The deletion frame carries a `seq` above any real fold position, so the client's higher-seq-wins rule
accepts it instead of dropping it as stale. A client renders no card for a row whose value is null.

## 7. Rendering contributed views

A dashboard surface that shows a unit renders unknown `<app>/<key>` views generically: a card titled
by the key (or by a published `title`), badged with the app's name, and a body rendered from the value
by a small declarative schema the contributor may publish once per key:

```
POST /api/eventlog/{kind}/{id}/projections/{key}/schema
  { "title": "...", "kind": "badge|text|list|table|keyvalue", "path": ["selector", ...] }
  -> 204                      | 403 code=projection_key_not_owned
                              | 400 code=invalid_projection_value
```

`path` selectors are dotted paths into the value, read per kind: the first selector picks the array for
`list` and `table` (and the remaining ones name the table's columns), and the whole list names the
fields to show for `keyvalue`, `badge` and `text`. An absent or unusable schema falls back to a compact
key-value dump. Unknown schema fields are DROPPED rather than stored: the browser renders from this, so
an unrecognised field would be a rendering the host never agreed to.

A schema may be published before the first fold; such a row renders nothing until a value arrives. A
schema is sticky across later value publishes, which carry none.

No contributor code runs in the browser, and every string is truncated before it reaches the DOM.

## 8. Adapters

An adapter is an app whose backend process hosts a foreign plugin runtime and implements that runtime's
service surface as shims over §3–§5 and over the existing MCP and cron surfaces. The plugin code is
unmodified. Each foreign plugin model gets one adapter; the protocol does not change per adapter.

## 9. Error codes

Every error response carries a machine-readable `code`, and each code maps to exactly ONE HTTP status
(`eventlog.contrib.STATUS_FOR_CODE`) — a code whose status drifts per call site says less than it
appears to:

| code | status |
|---|---|
| `event_type_not_owned` | 403 |
| `projection_key_not_owned` | 403 |
| `unit_kind_not_granted` | 403 |
| `unit_not_found` | 404 |
| `stale_seq` | 409 |
| `event_too_large` | 413 |
| `projection_too_large` | 413 |
| `quota_exceeded` | 429 |
| `invalid_after` | 400 |
| `invalid_limit` | 400 |
| `invalid_projection_value` | 400 |

A dashboard-user token is refused on every route here with `unit_kind_not_granted`: the gateway's own
writes go through the service directly, so the only legitimate caller is a contributor.

## 10. Worked example

`test/contrib_protocol_demo.py` is a complete contributor in ~200 lines of standard-library Python:
it exchanges its app secret for a token, catches up with `GET .../events?after=`, subscribes over the
WebSocket, appends `demo/ping`, folds a `demo/count` view locally, publishes it with the schema, and
proves the gap rule by dropping its socket and catching up again.
