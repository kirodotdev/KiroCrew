# Meetings (builtin app)

An AI meeting assistant. Transcribes a live meeting through KiroCrew's own
streaming speech-to-text, fans each line out to a small crew of background agents
(structured notes, an HTML/Mermaid diagram, an action-item list), and gates the
meeting's close behind a review of the extracted action items.

`defaultEnabled: false` — it appears in the App Store and is opt-in.

## Layout

| Path | What it is |
|---|---|
| `src/kiro_crew/apps/builtins/meetings/app.json` | manifest (`backend.routes`, `ui.pages`, agents, permissions) |
| `.../backend/constants.py` | every limit, state name, and provider id |
| `.../backend/store.py` | on-disk layout **and the single path-containment barrier** |
| `.../backend/domain/dictionary.py` | speech-correction dictionary (TOML) |
| `.../backend/domain/session.py` | batching dispatcher + meeting state machine |
| `.../backend/providers/tasks.py` | **task-provider seam** + the local ledger |
| `.../backend/providers/calendar.py` | **calendar-provider seam** + the `.ics` reader |
| `.../backend/routes/` | `_common` (gate + validation), `meeting_lifecycle`, `agents`, `tasks`, `calendar`, `settings` |
| `.../agents/*.json` | the three shipped agent specs |
| `src/kiro_crew/builtin_skills/meetings/SKILL.md` | the bundled skill (data layout, lifecycle, provider config) |
| `website/src/apps/meetings/` | `MeetingsPage` (list) → `MeetingView` → `TaskReviewView`, `SettingsView` |
| `website/public/app-assets/meetings/` | icon + hero art |

## Routes

Registered on the gateway's OWN aiohttp Application by
`backend/routes/__init__.py:register_routes` (the manifest names the same entry
point for the generic App Kit loader). Base path `/api/apps/meetings` — the
same-origin convention issue-radar and code-review-sage use, **not** the
`/apps/{name}/api` reverse proxy, because this app has no child process.

```
GET    /config                      config + the three provider catalogs
PUT    /config                      replace config (narrow allow-list)
GET    /dictionary                  speech-correction terms
POST   /dictionary                  add a term          {correct, aliases[]}
POST   /dictionary/remove           remove a term       {correct}
POST   /dictionary/reload           re-read from disk

GET    /calendar                    cached events + provider + configured flag
POST   /calendar/sync[?days=N]      fetch from the provider, replace the cache
GET    /calendar/providers          registered calendar providers

GET    /agents                      configured meeting agents
GET    /status                       live dispatcher status (or an all-idle shape)
GET    /task-providers              registered task providers + the active one

GET    /meetings                    every meeting with metadata on disk
GET    /meetings/{id}               one meeting's metadata + live status
DELETE /meetings/{id}               permanently remove an inactive meeting's local data
POST   /meetings/{id}/init          create folder/metadata/tasks/outputs (idempotent)
POST   /meetings/{id}/start         activate: seed outputs, spawn agent sessions
POST   /meetings/{id}/status        {status} — active | paused | reviewing | ended
POST   /meetings/{id}/stop          flush agents, send the finalize notice, mark ended
GET    /meetings/{id}/outputs       batch-read every agent output + tasks
POST   /meetings/{id}/attachments   {action: add|remove, attachments[]|index}
POST   /meetings/{id}/agents        {agent_id, enable} — toggle mid-meeting
POST   /meetings/{id}/mute          {agent_id, muted}
POST   /meetings/{id}/dispatch      {text, chat?} — one transcription/typed line
POST   /meetings/{id}/message       {agent_id, text} — one agent, flushed at once
POST   /meetings/{id}/reset         reset tripped circuit breakers
GET    /meetings/{id}/tasks         extracted action items
POST   /meetings/{id}/tasks         add one by hand   {description, …}
PATCH  /meetings/{id}/tasks         edit one          {id, fields}
DELETE /meetings/{id}/tasks         remove one        {id}
POST   /meetings/{id}/tasks/file    file through the task provider  {id}
POST   /meetings/{id}/tasks/review  {id, review_status} — pending | archived
```

Every handler is wrapped by `_common.route`, which applies the enable gate and
turns validation failures into 4xx.

## Data

All under `app_data_dir("meetings")` (`~/.kiro/crew/apps/meetings/data/`):

```
config.json                      app config (agents, providers, presets)
dictionary.toml                  speech-correction terms
calendar-cache.json              last calendar sync
task-ledger.json                 tasks filed through the local task provider
meetings/<safe_id>/session.json  per-meeting metadata
meetings/<safe_id>/tasks.json    extracted action items
meetings/<safe_id>/<agent>.md    a markdown agent's output
meetings/<safe_id>/<agent>.html  an HTML agent's output
```

Deleting a meeting removes its complete per-meeting directory (metadata, tasks,
notes, and diagrams). The route refuses a meeting with a live in-process session
with `409 meeting_active`; the dashboard keeps the row's delete affordance visible
but disabled for active, paused, and reviewing states. Calendar events are owned by
their provider, so deleting local meeting data does not delete the source event.

`ensure_data_dirs()` creates the subtree and seeds `dictionary.toml` +
`config.json` at app startup (an `on_startup` hook, run on the executor). It
never overwrites, so user edits survive every restart.

## Lifecycle

```
idle ──start──> active ⇄ paused ──> reviewing ──> ended
                  │                    ▲             │
                  └────────────────────┘         restart
```

`reviewing` is a **gate, not a state to pass through**: `ended` is reachable only
from it, so no extracted action item is silently dropped. The UI's transition
table (`useMeetingSession.ALLOWED_TRANSITIONS`) has a test asserting no other
state can reach `ended`.

`MAX_CONCURRENT_MEETINGS == 1`: a second `start` for a different meeting answers
409 while the first is live and unexpired. A session past
`MAX_SESSION_DURATION` (4h) answers 410 on dispatch.

## Agent dispatch

`domain/session.py`. One `AgentQueue` per enabled agent plus the always-on task
extractor. A queue batches lines and flushes every `BATCH_INTERVAL_SECS` (30s),
so an agent gets a paragraph of context rather than one interruption per
utterance. Three consecutive dispatch failures trip a circuit breaker (backoff
60s → 120s → stop); `POST …/reset` resumes.

A flush takes **whole lines up to `MAX_BATCH_CHARS` (60k)** and deletes exactly
the lines it dispatched, so a queue that grew past the cap — a long pause, or a
backed-off agent resuming — carries its tail into the next flush. Truncating the
joined batch while clearing the whole queue silently DESTROYED transcript, whose
only symptom was notes that skip the end of what was said. A single line over the
cap is still truncated and consumed, because requeueing it would wedge the queue.
Pinned by `test_meetings_session.py::TestAgentQueue`.

Ending or pausing a meeting drains rather than interrupts. `flush_now` treats a
pending flush task by state: still SLEEPING on its interval, it is cancelled (that
is the point of flushing now); already inside `flush()` awaiting the agent, it is
AWAITED. Cancelling an in-flight dispatch killed the live turn, and because `busy`
was still set the follow-up flush then no-opped — so stopping a meeting mid-dispatch
lost that batch and the finalization notice, at the one moment a meeting's notes
matter most. `busy` is the discriminator. Pinned by
`::test_flush_now_waits_for_an_in_flight_dispatch` and
`::test_flush_now_still_cancels_a_sleeping_timer`.

**A drain is a loop, not one flush.** `flush()` deliberately sends exactly ONE
batch, so an over-cap queue needs several — and `flush()` cannot reschedule itself,
because it runs as the body of `_flush_task` and `_schedule_flush` takes its
"already running" early return from in there. Attempting the reschedule inline
scheduled nothing at all, which re-opened the very tail loss `_take_batch` closed.
The loop therefore lives in the two places that own the lifecycle: `_delayed_flush`
chains sleep→flush while `flush()` reports work remaining, and `flush_now` drains
before returning because teardown discards anything still queued. Both are bounded
by `_MAX_DRAIN_BATCHES`, and a flush that consumes nothing (a failing dispatch)
exits the loop instead of spinning — the circuit breaker still trips normally.
Pinned by `::test_flush_now_drains_every_queued_batch`,
`::test_the_batching_timer_chains_until_the_queue_is_empty`, and
`::test_a_failing_dispatch_does_not_spin_the_drain`.

**Teardown drains; only `set()` may cancel.** `ACTIVE.clear()` calls `cancel_all()`,
which drops the pending flush timers — so a session torn down with a half-batch
queued lost that transcript, and the final notes silently omitted whatever had not
been dispatched. Every teardown path now calls `await ACTIVE.drain_and_clear()`,
which flushes first: the expiry path (a long meeting whose next line arrives after
the session lapsed), gateway shutdown, `status=ENDED`, and `handle_stop_meeting` —
where it is load-bearing, because the finalize notice is itself enqueued and the old
cancel would have discarded the very notice just broadcast. A flush failure still
tears the session down, so a wedged agent cannot block shutdown. **Replacing a session is a teardown too.** `set()` cancels the outgoing session's
queues, so starting a second meeting while an earlier (typically expired) one still
held a half-batch discarded that transcript — the same loss by a different route.
`handle_start_meeting` therefore drains before it replaces. `set()` itself now LOGS
the undispatched count rather than dropping it silently, because a leftover queue at
replace time always means transcript is about to be lost. `clear()` survives only as
the second half of `drain_and_clear`.

Guarded by AST checks over the route modules: no `ACTIVE.clear()` outside the
draining helper, and no handler calling `ACTIVE.set()` without a drain — so a new
teardown OR replace path cannot quietly reintroduce the loss.
`test_meetings_routes.py::TestTeardownDrainsBeforeClearing`.

**Dispatch is in-process.** Upstream POSTed each batch back to its own gateway
over authenticated loopback HTTP. Here the routes live ON the gateway, so a batch
goes straight to the shared `SessionManager` via
`llm_helpers.stream_and_collect` under `ToolApprovalPolicy.HOOK_BASED` — the
agents' file writes still traverse the PreToolUse gate (deny patterns,
sensitive paths, governance) exactly like any other turn.

## The two provider seams

Both follow `kiro_crew.embeddings`' `EmbeddingBackend` /
`register_embedding_backend` shape: an ABC, a name-keyed factory registry, and a
resolver that **degrades instead of raising** on an unknown id. Each ships
exactly one real implementation; the seam exists so an out-of-repo edition can
register an organization's own provider without patching the app. Nothing in the
app branches on a provider name, and the settings UI is populated from the
registries, so a registered provider appears with no frontend change.

### Task provider (`backend/providers/tasks.py`)

`TaskProvider` (`provider_id`, `display_name`, `create(TaskDraft) -> TaskRef`).
Shipped: `local` — an app-scoped JSON ledger (`task-ledger.json`). `create` is
called on the subprocess executor, because an edition provider may talk to a
tracker over the network.

That executor makes `create` genuinely concurrent, so its read-append-write is
held under a **module-level** lock (`_LEDGER_LOCK`). The write is atomic; the
read-modify-write around it was not, so two parallel filings each read the same
list and the second write landed a snapshot missing the first — with both
requests reporting success. The lock is module level rather than per instance
because `get_task_provider` builds a fresh provider per request. Pinned by
`test_meetings_providers.py::TestLocalTaskProvider::test_concurrent_filings_do_not_overwrite_each_other`.

`TaskDraft.sanitized()` runs before anything leaves the process: an action item
is LLM output and a filed task is an external surface, so credential +
exfiltration-URL redaction and length caps are applied there.

Why not `task_models.Project`: the task runner's dataclasses model an autonomous
execution plan (ordered, dependency-linked, attempt counts, a state machine the
runner drives). A meeting action item is a durable human-owned to-do nobody
executes automatically; reusing `Project` would mean inventing a fake spec per
meeting and leaving the executor fields permanently unused.

### Calendar provider (`backend/providers/calendar.py`)

`CalendarProvider` (`provider_id`, `display_name`, `requires_source`,
`async fetch(days) -> [CalendarEvent]`). Shipped: `none` (the default — the app
is fully usable with ad-hoc meetings) and `ics`, a stdlib iCalendar reader fed by
a local `.ics` path or a published `https://` URL.

`parse_ics` reads only the `VEVENT` fields the app displays. Recurrence
(`RRULE`) is deliberately **not** expanded: a correct expansion needs a full RFC
5545 engine, and silently showing wrong occurrence times is worse than showing
only the series' first instance.

Fetch safety:

* an `https://` source is fetched with **aiohttp** (never `requests`/`urllib`,
  which would block the gateway's single event loop); the response is size-capped
  (4 MiB) while streaming, and a redirect off https is refused;
* only `https://` is accepted (`webcal://` is rewritten to it) — every other
  scheme, including `file://` and `http://`, is refused, so a config value cannot
  turn the sync into a local-file read or a plaintext hop;
* the resolved address is refused when it is loopback/private/link-local/
  reserved/multicast/unspecified — the gateway performs this fetch, so an
  internal-only address would make the endpoint a request-forgery hop. An IPv4
  address embedded in IPv6 (`::ffff:10.0.0.1` v4-mapped, `2002:…` 6to4) is judged
  by the address it embeds, since that is where the packet lands. Resolution is a
  blocking syscall, so the whole validation step runs on the executor;
* **the vetted address is the connected address.** `_normalize_url` returns a
  `VettedTarget` (url + host + port + approved addresses), and the fetch hands
  those addresses to a `_PinnedResolver` installed on the `TCPConnector`
  (`use_dns_cache=False`). Returning only the URL is what made the old gate a
  TOCTOU: aiohttp resolved the same name a second time for the connect, so a host
  whose DNS answer changed in between (short TTL, or a resolver alternating a
  public and a private record) passed validation and was then fetched at the
  private address — the `169.254.169.254` metadata shape. `calendar.source` is
  reachable from a dashboard `PUT /api/apps/meetings/config`, so this is
  request-supplied, not operator-only. Substituting the *resolution* step rather
  than rewriting the URL to an IP is deliberate: the request URL keeps its
  hostname, so the `Host` header, TLS SNI, and certificate verification stay
  correct. Verification is never disabled and `ssl=False` is never passed — a
  test asserts the connector keeps aiohttp's verified context
  (`CERT_REQUIRED`, `check_hostname`). An unpinned host is refused by the
  resolver rather than resolved, so the mechanism is fail-closed. Each redirect
  hop is re-validated **and** pinned before its own request, so no hop is
  vetted-then-re-resolved;
* a multi-record answer is **all-or-nothing**: every address must pass, and the
  whole set is pinned. A host answering with a mix of public and private
  addresses is refused outright rather than filtered down to the public ones —
  that mix is the rebinding signature, and keeping the public record would let an
  attacker retry until the connector picked the private one. Same rule for IPv4
  and IPv6;
* a local path is read on the executor, size-capped, and refused when
  `is_sensitive_path` matches.

## Speech-to-text

KiroCrew's own `/api/ws/stt` (`dashboard/stt_stream.py`).
`hooks/useMeetingTranscription.ts` conforms to that endpoint's existing wire
protocol — connect, wait for `{"type":"ready"}`, send 16 kHz Int16 PCM from
`/pcm-worklet.js`, receive `partial`/`final`/`error`, send `{"type":"stop"}` and
let the server close so trailing finals arrive. Every FINAL segment is POSTed to
`…/dispatch`, which is what feeds the agents; partials only drive the caption.

Cloud transcription is an optional extra (`pip install kirocrew[voice]`). When it
is absent the endpoint answers a friendly WS error, the hook surfaces it as a
toast, and the user can still type into the broadcast bar to feed the agents.

## Security posture

* **Path containment.** `store.safe_meeting_id` is the only way a client-supplied
  id becomes a path segment (`[A-Za-z0-9._-]` after the one documented `:` → `_`
  substitution, leading dots refused). `store.contain` is the barrier every
  derived path passes through: `resolve()` collapses `..` AND follows symlinks,
  then containment under the data root is asserted; callers must use the returned
  path. A violation is SEL-audited and raises. Tests cover traversal, a symlink
  planted inside the data dir, and non-string ids.
* **Deny-by-default authorization.** `_common.require_enabled` refuses every
  route while the app is disabled (routes are registered once at startup, so a
  default-disabled app would otherwise stay callable). `is_app_enabled` runs off
  the loop.
* **Redaction.** Transcripts, agent outputs, extracted tasks, and calendar fields
  are LLM/user content on the way to the dashboard or a task provider, so
  `security.redact` (exfiltration URLs + credentials) is applied at each
  boundary: the dispatch entry point, the outputs response, task normalization,
  `TaskDraft.sanitized`, and `parse_ics`.
* **Strict field readers.** `_common.field_bool` refuses a non-boolean rather
  than coercing (`bool("false")` is `True`, which would invert a mute decision);
  `field_str` treats a non-string as missing rather than stringifying it.
* **Narrow config writer.** `PUT /config` is an allow-list, not a merge: an
  unknown provider id collapses to the default, an agent id that is not a safe
  slug is dropped, and an agent-spec reference with `..` or a leading `/` becomes
  `""`.
* **Model-generated HTML.** The sketch artist writes HTML *from* the transcript,
  which anyone who speaks in the meeting can influence, so the frame takes three
  independent controls — each one added because the previous one turned out to be
  insufficient. All three are built by
  `website/src/apps/meetings/lib/sketchSrcdoc.ts`; the markup is never mounted
  into the dashboard's own DOM.

  1. **Null-origin sandbox.** `srcDoc` iframe with `sandbox="allow-scripts"` and
     **no** `allow-same-origin`, so nothing in the frame can read this page, its
     cookies, or the gateway. A test pins the absence of `allow-same-origin`.
  2. **Egress-denying CSP.** The sandbox says nothing about OUTBOUND requests, so
     a `<meta>` CSP is emitted as the first child of `<head>` (ahead of every
     model byte — a meta policy only binds from where it is parsed):
     `default-src 'none'`, `connect-src 'none'`, `img-src data:`, `font-src
     data:`, `form-action 'none'`, `base-uri 'none'`, and `script-src` pinned to
     the single vendored same-origin Mermaid FILE. The frame needs no network, so
     it is granted none.
  3. **Model script is stripped.** The CSP must grant `script-src
     'unsafe-inline'` for the Mermaid bootstrap, which also let the *model's*
     inline script run — and script can loop `document.createElement('link')`
     with `rel="dns-prefetch"` to stream the transcript out through DNS lookups
     that no CSP directive governs. An earlier revision recorded this as an
     accepted "hostname-only" residual; **that assessment was wrong** (it assumed
     the channel was limited to static markup, and treated ~200 bytes per
     unlimited repeatable lookup as a trickle). The document is therefore scrubbed
     before serialization: `script` (HTML and SVG), `iframe`/`frame`,
     `object`/`embed`/`applet`, `link` (every `rel`, not an allowlist),
     `meta`, `base`, `template` and `noscript` are removed as elements; `on*`
     handler attributes are removed; and `javascript:`/`vbscript:`/non-image
     `data:` URLs are removed from URL attributes (matched after stripping
     whitespace and control characters, which the HTML URL parser ignores).
     Remote-URL *fetches* are left to the CSP rather than pattern-matched.

  Mermaid still renders, and this is what makes control 3 affordable: it is driven
  by KiroCrew's own bootstrap from the declarative `div.mermaid` / fenced
  ```mermaid markup the agent is instructed to emit, so the agent has no
  documented need to ship JS. Both directions are tested in
  `website/src/test/sketchSrcdoc.test.ts` — nothing executable survives, **and** a
  Mermaid diagram plus an inline-styled HTML table still render (the guards
  against over-stripping the panel into a blank).
* **No blocking call on the loop.** The calendar fetch is aiohttp; DNS
  validation, the local `.ics` read, the data-dir seed, the enable check, and the
  task-provider `create` all run on an executor.

## What the port changed

See `ATTRIBUTION.md` for the table. In short: the internal task system became
the task-provider seam, the internal calendar MCP became the calendar-provider
seam, the second (separately built, internally sourced) speech-to-text daemon was
deleted in favour of KiroCrew's own, the standalone server became in-gateway
routes, the shell-blob self-heal cron became Python at startup, and the
internal-git update-check cron was deleted (a builtin versions with the package).

## Tests

`test/test_meetings_store.py` (containment, layout, config),
`test_meetings_dictionary.py` (matching + hostile input),
`test_meetings_session.py` (dispatcher, breaker, lifecycle, prompts),
`test_meetings_providers.py` (both registries, the `.ics` parser,
scheme/address refusals), `test_meetings_routes.py` (the HTTP contract,
validation, redaction, the enable gate), with the shared fixtures and the fake
session manager in `test/meetings_helpers.py`. Every dispatch goes through that
fake session manager; no test spawns a process or opens a socket.

These live in the repo-level `test/` tree, not an in-package `tests/`:
`setup.cfg` sets `testpaths = test transfer`, so a test under
`src/kiro_crew/apps/builtins/...` is never collected by CI.

Frontend: `website/src/test/MeetingsApiClient.test.ts` (fetch-boundary
translation), `MeetingsSessionLogic.test.ts` (dedup, preset resolution, the
transition table), `MeetingsAgentPillBar.test.tsx`, `MeetingsBroadcastBar.test.tsx`,
`MeetingsAgentPanel.test.tsx` (including the iframe sandbox).
