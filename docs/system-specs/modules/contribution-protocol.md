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
- A grant is answered from memory. These checks are called from synchronous code on the gateway's
  serving loop — the per-frame event scoper and the auth path classifier both answer inside a
  non-async predicate — so an expired entry is served as it stands and re-resolved on a worker
  thread, rather than turning an authorization check into a manifest read on that loop every TTL.
  The one read that cannot move off the caller is an app's first: there is no previous answer to
  serve, and answering empty would deny a request the app is entitled to make. The revoke tombstone
  is consulted ahead of the cache either way, so a teardown is never answered from a stale entry.
- Every subscription decision is recorded, granted as well as denied, on the same
  `contribution_protocol` audit stream the HTTP surface writes to. A granted subscription opens a
  durable read of a unit's whole event stream to third-party code, so "who was allowed to read
  what" is the half an auditor needs and a deny-only record cannot answer.

Declaring `contributions` grants the HTTP paths and frames below; no separate `permissions.api` entry is
needed. Widening a declaration on upgrade is a new consent, handled like any other widened permission —
the declaration is covered by the manifest signature, so widening it on a signed app invalidates that
signature.

The log's own event vocabulary admits `<namespace>/<name>` for any namespace the built-ins do not own
(`member`, `activity`, `slot`, `patrol`). A type inside a reserved namespace that is not a known
built-in is a typo, not a contribution, and is still refused.

**Every write re-asks its grant under the lock that writes.** A route checks the grant and then
hands the write off across an `await` (`asyncio.to_thread`), so the answer it holds can go stale
before the row lands: an app revoked inside that window has already passed the check. All three
write paths — appending an event, publishing a projection value, and attaching a render schema —
therefore take a `still_granted` callable and re-ask it while holding the write lock, which is
what makes the decision and the write one step. A schema is not a lesser write, because it is
what the dashboard renders the key with, so a revoked app must not be able to leave one behind.
Callers with no grant to re-ask — the gateway's own observers, the legacy migration — pass
nothing and are unaffected.

## 3. Reading: catch-up then stream

```
GET /api/eventlog/{kind}/{id}/events?after=<seq>&limit=<1..500>
  -> { "kind", "id", "<idField>", "events": [envelope...], "lastSeq": n }   oldest first, seq > after
```

`after` defaults to 0 (from the beginning, since the first event's `seq` is 1; -1 is accepted as the
same for a client that computed its cursor from an empty fold); `limit` defaults to 200. An out-of-range `limit` is
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
                             | 400 code=invalid_event_type
```

The gateway assigns `seq` and `time`; a contributor never supplies them, and a supplied value is
ignored rather than refused — the returned envelope carries the authoritative pair. `data` is limited
to 64 KiB serialized. `type` is bounded in content as well as in shape and length: a credential- or
presigned-URL-shaped name is refused with `invalid_event_type`, because the type is stored verbatim
and the redactors that run on the way out cover only `data`. Each app has a per-unit event budget (default 10,000 events per unit per UTC
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
`member_projection` frame `{slug, key, value, seq, stateVersion, schema?}` (for kind `member`; other kinds get a frame
of the same shape named for the kind). Contributed rows appear in the unit's `projections.values` block
next to built-in keys, so a dashboard needs no new code path to receive them.

`value` is limited to 640 KiB serialized — ten times the event cap, because a folded view legitimately
summarises many events, but not unbounded: this value is pushed whole to every dashboard socket.

That limit, and the per-app key count, are enforced when a row is LOADED as well as when it is published.
The contribution store is a file under the data home, not a fenced path, so a row can reach it without
passing the publish path; a row that never passed a bound would otherwise be retained in memory and
shipped in every roster response for the unit, and a valid-JSON file reloads that way on every restart
rather than self-healing. A row over either bound is dropped on load, with a warning naming the file —
dropped rather than failing the read, because a contributor republishes on its own cadence, so a drop
recovers while a refusal would keep the Members page down until someone edited the file by hand.

`stateVersion` is the contributor's fold version. A publish with a higher `stateVersion` than the stored
row replaces it regardless of `seq`, so a contributor that changed its fold can re-publish from zero. A
LOWER `stateVersion` is refused as stale.

It therefore travels on the frame and in the baseline, and a client MUST read it BEFORE the seq rule:
the two rules are not the same rule, and a client applying seq-wins alone drops the one frame this
paragraph exists to allow — a refold the server has already accepted — and goes on rendering the
obsolete card.

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

`seqs`, `stateVersions` and `schemas` are present only for contributed rows. `seqs` is REQUIRED rather
than cosmetic: a contributed row's seq is the contributor's own fold position, not the response's
`asOfSeq`, and a client that seeded it at `asOfSeq` would drop the contributor's next live push under the
higher-seq-wins rule and freeze the card at its baseline. `stateVersions` carries the same field the
frame does, for the same reason.

This block is also the AUTHORITATIVE set of contributed rows, and a client MUST delete the contributed
rows it holds that the block does not carry. Teardown otherwise rests entirely on the single null-value
frame §6 sends, and a socket that drops at the wrong moment never delivers it, so a withdrawn app's card
outlives the app. A client that reconciles must not delete a row a live frame wrote while the read was in
flight: such a row is newer than the answer, so absence from it says nothing.

Folding happens in the contributor's process, against events read through §3. The gateway never
executes contributor code.

### 5.1 The published row is display-only, and forgeable

Published rows live at `<data home>/eventlog/contrib/<kind>/<id>.json`, which is NOT under the fenced
`crew-log/` root. An agent's file tools reach it, so a sandboxed agent can change what a contributed
card shows.

That is the recorded decision, not an oversight, and it rests on what the file is FOR: the authority for
a contributed row is the contributor's own next publish, and the file exists only so a gateway restart
does not blank every card until each contributor happens to re-publish. Nothing in the gateway reads a
published row to decide anything: the rows reach the member drawer's cards and nowhere else, and the
events a contributor appends — the part that IS history, and the part a member's trust reads — go to the
member's crew log, which is fenced.

The consequence, stated rather than implied: a forged row renders as that app's view until the
contributor publishes again, and for a contributor that never publishes again, indefinitely. So a reader
may not treat a contributed card as evidence of anything; a card is what an app last said, at whatever
fold position the row carries.

Fencing it is the resolution if that ever stops being enough — specifically if a published row gains a
consumer that decides something, which would make it a record rather than a display. The fence is the
same mechanism the member log uses (a leaf named in `_CREW_SECRET_LEAVES` and `_CREW_HIDDEN_LEAVES`), so
the change is a move of the root, not a second fence.

## 6. Teardown

Disabling or uninstalling an app: the gateway invalidates the app's grant, closes the app's
subscriptions (the socket itself, not just the subscription — the app's code is being stopped), deletes
every projection row the app published, and pushes a frame with `value: null` for each key. Events the
app appended stay in the log — they are history, and the log is never rewritten.

The order matters: the grant comes off FIRST, before ANY of the app's own code runs on the disable path
— its `onDisable` lifecycle script as well as the disable hooks after it — so an in-flight append cannot
land after the rows it folds into are deleted. `onDisable` is the app's documented place to wind itself
down, which makes it the code most likely to make one last contribution call, and it runs with the app's
own token against this same surface.

The deletion frame carries a `seq` above any real fold position, so the client's higher-seq-wins rule
accepts it instead of dropping it as stale. A client renders no card for a row whose value is null.

**A delete that cannot be PERSISTED is rolled back and not reported as deleted.** The in-memory delete is
not the authoritative effect here, because this flow has already revoked the app's grant: no contributor
remains to republish, so the usual self-correction on the next load does not apply, and a cold load
restores the row from disk for good. Reporting the key anyway would push `value: null` for a row that
survives, leaving a card the dashboard believes is gone with nothing able to correct it. The unit's rows
are restored and left out of the answer instead, which is the same shape a failed publish uses, and it
keeps the stale card visible — the truthful state, and the one an operator can act on. There is no
best-effort persistence mode on this store for that reason.

**A grant is asked TWICE on the subscribe path, with nothing awaitable between the second answer and the
registration.** The first check sits upstream of a real await — the unit resolve runs off-loop — so a
teardown landing in that window revokes the grant and closes the app's sockets while the continuation is
suspended, and the close cannot see a subscription that does not exist yet. Because teardown revokes
before it closes, a revocation that beats the second check is caught by it, and one that loses to it is
caught by the close, the subscription being registered by then. Both halves are synchronous, so nothing
can interleave between them and leave a third case.

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

Every body kind renders at most 20 rows, and a body that sliced says so with a "+N more" line. That line
is what keeps a truncated body from reading as a complete one, so it is not optional for any kind: a
key-value dump of a large object slices exactly like a list does, and a dump without the marker claims to
be the whole object. The cap is deliberate and there is no expand — a contributor that wants a specific
view published should fold it into one, which is what the schema is for.

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
| `invalid_event_type` | 400 |
| `invalid_projection_value` | 400 |
| `invalid_projection_key` | 400 |

A dashboard-user token is refused on every route here with `unit_kind_not_granted`: the gateway's own
writes go through the service directly, so the only legitimate caller is a contributor.

## 10. Worked example

`test/contrib_protocol_demo.py` is a complete contributor in ~200 lines of standard-library Python:
it exchanges its app secret for a token, catches up with `GET .../events?after=`, subscribes over the
WebSocket, appends `demo/ping`, folds a `demo/count` view locally, publishes it with the schema, and
proves the gap rule by dropping its socket and catching up again.
