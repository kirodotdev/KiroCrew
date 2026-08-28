# App SDK: durable jobs and view state

> **Status: proposed, not implemented.** Nothing described here exists in the
> code yet — `JobSDK`, `useAppJob` and `useAppViewState` are the design this spec
> is asking for, and §7 is the order in which they would land. The rest of this
> directory documents shipped behaviour; treat this file as the plan until a
> phase merges and this banner is cut. §1's account of the *existing* code is
> current and citation-checked, and is the only part safe to rely on today.

Two capabilities the App SDK does not offer today, so every app either re-invents
them or ships without them: a **durable record of a task in progress**, and a
**URL-expressible record of where the user is inside an app**. Both are the same
mistake in two places — a fact that only ever exists in a React component, and so
dies when that component unmounts.

This spec fixes them as SDK surfaces rather than per-app patches, because the
evidence in §1 shows per-app effort does not converge.

## 1. Problem

### 1.1 A task in progress has no server-side existence

AWS Control runs a backup inline in the HTTP request
(`src/kiro_crew/apps/builtins/aws_control/backend/routes.py:1014`):

```python
record = await asyncio.to_thread(runner, _account, profile, region, bucket)
```

There is no job id and no in-flight registry. The only server-side state is the
*terminal* run record, written by `_record_run` (`backend/backup.py:128`) as the
last statement of each runner — and written **inside the worker thread**
(`backend/backup.py:268`, `:448`), so it lands even with nobody listening.

The consequences are precise:

- When the client goes away, **nothing stops the work.** The gateway never sets
  aiohttp's `handler_cancellation` (`src/kiro_crew/dashboard/slowloris.py:157`
  passes the base config's value through and no caller enables it), so aiohttp's
  default applies and the handler is *not* cancelled on disconnect: it runs to
  completion, as does the OS thread `asyncio.to_thread` started. Even had the
  coroutine been cancelled, the thread could not be — the only stop primitive,
  `_STOP` (`backend/backup.py:163`), is set at app teardown only. **The work
  finishes; the response has nowhere to go.**
- `GET /backup/{account}` reports `runs` from `last_runs`
  (`backend/backup.py:530`) — the last *completed* run per kind. There is no
  in-flight field.
- The UI's running indicator is
  `runMut.isPending && runMut.variables === kind`
  (`website/src/apps/aws-control/DrivePage.tsx:795`) — TanStack Query mutation
  state, destroyed on unmount. No `AbortController` is wired anywhere in the
  app's request layer (`website/src/apps/aws-control/api.ts:50`).

So after navigating away and back, the UI shows the *previous* completed run and
reads as "stopped" while the backup is still running. A user who believes it
acts on that belief and starts a second one: a second archive build, a second S3
upload, a second month of storage. **The bug is not lost work, it is a UI that
lies in the direction of duplicate spend.**

### 1.2 Dev Fleet solved it, halfway, for two of its actions

Dev Fleet does keep run state server-side: `_SYNC_RID`
(`src/kiro_crew/apps/builtins/dev_fleet/server.py:3574`, set at `:3898`, never
cleared) is overlaid onto every fleet payload as a live read (`:4340`), and the
frontend reattaches on mount (`website/src/pages/DevFleetPage.tsx:844`). SPA
navigation alone does **not** break it.

What breaks it is durability. `_RUNS` is a module-memory dict
(`src/kiro_crew/apps/builtins/dev_fleet/server.py:501`), so a
gateway restart empties it and `GET /api/run?id=` answers `404`. Pull + Build on
the main row auto-restarts the gateway on success, which is exactly how a
*completed* run becomes un-reattachable and surfaces as the explicit
"gateway restarted mid sync, run lost" message. And because `dev_fleet_cleanup`
(`src/kiro_crew/apps/builtins/dev_fleet/server.py:4694`) snapshots `_ACTIVE_RUNS`
(`:4706`) and kills each run's process tree (`_kill_tree`, `:4711`) — its own
comment gives the reason, "otherwise a gateway restart leaves pip/npm mutating
shared checkouts" — a restart *during* a run genuinely kills the run.

The load-bearing observation is narrower and harder: **Dev Fleet applied its own
mechanism to two of its long actions and not the rest.** Pull + Build and
provision reattach; pod up / down / restart do not, and prune polls
`/prune-status` from component state with no `prune_run_id` advertised, so it
cannot reattach either. The app that invented the correct pattern could not
afford to use it consistently *inside itself*. That is the argument for a
default, not a recipe.

### 1.3 Nothing global rescues either half

The shared QueryClient (`website/src/api/queryClient.ts:41`) configures `queries`
defaults only — no `mutationKey` defaults, no `setMutationDefaults`, no
persister. Every `useMutation` running-state handle in the dashboard is therefore
component-local: all of AWS Control's actions, Artifact Deploy, app install, MCP
install, and the settings panels.

### 1.4 View position cannot be expressed at all

`website/src/App.tsx:3563` gives a builtin app exactly one URL segment:

```tsx
<Route path="/:builtinApp" element={<BuiltinAppRoute />} />
```

`website/src/apps/BuiltinAppRoute.tsx` resolves that one segment to one
component and renders it with no `<Outlet/>` and no child `<Routes>`. So an app
cannot own a **sub-path** — but it *can* already read the query string, because
React Router matches on pathname only:
`website/src/apps/code-review-sage/CodeReviewSagePage.tsx:13` calls
`useSearchParams()` today under this very route. Nothing forces an app to keep
its position in memory; the SDK simply never offered a way to put it in the URL,
so no app does. AWS Control's three levels — accounts list, one account's
console, that account's drive — are consequently plain `useState`, which the code
states outright (`AwsControlPage.tsx:252-255`, `:278-282`;
`apps/aws-control/ConsoleView.tsx:5-6`). Refresh, back, and navigate-away all
reset to the accounts list, and no link can point at an account's drive.

### 1.5 Neither gap was a known deferral

`.kiro/specs/app-sdk-gateway-hooks/design.md` — the workstream that produced
`Route_Registry`, `CronSDK`, `App_Context` and `AppStorage` — closes with seven
"Known Limitations and Future Directions" (storage quota, event scoping,
dependency ordering, hot reload, route middleware, metrics, manifest schema
evolution). Durable jobs and view state are not among them. This spec is the
continuation of that line, not a revision of it.

## 2. Solution overview

Two SDK surfaces, both shaped like the SDK's existing `CronSDK`
(`src/kiro_crew/apps/cron_sdk.py`): app-scoped, ownership-enforced, persisted by
the gateway, with sync/async twins where a caller may be on the loop. `CronSDK`
is the template; neither surface invents a new shape.

| Fact | Lives today | Moves to |
|---|---|---|
| "a task of mine is running" | one component's `useMutation` | gateway-side run record, on disk, re-advertised to any fresh mount |
| "the run's progress so far" | component state, or nowhere | bounded progress tail on the run record |
| "where the user is in my app" | `useState` in the page component | namespaced URL search params |

`CronSDK` schedules work for later; the Job SDK tracks work a human started and
is watching now. They are siblings, not alternatives.

## 3. Job SDK — backend

Exposed on `App_Context` beside `cron`, `storage` and `events`.

```python
class JobSDK:
    # A runner is REGISTERED once, at app init (the manifest's on_startup hook),
    # binding a kind to the callable that services it. This is what lets a
    # caller that cannot hold a Python callable -- the browser, and the
    # restart-reconciliation pass -- name a run by `kind` alone.
    def register(self, kind: str, fn: JobFn, *, cancellable: bool = False) -> None: ...

    # Mutators come as sync/async twins, exactly like CronSDK's
    # add_job / add_job_async: a caller already on the event loop must not
    # block on the persisting write.
    def start(self, kind: str, *, params: dict | None = None,
              dedupe_key: str | None = None) -> str: ...      # -> run_id
    async def start_async(self, kind: str, *, params: dict | None = None,
                          dedupe_key: str | None = None) -> str: ...
    def cancel(self, run_id: str) -> bool: ...
    async def cancel_async(self, run_id: str) -> bool: ...

    # Reads
    def get(self, run_id: str) -> Run | None: ...
    def list_active(self, kind: str | None = None) -> list[Run]: ...
    def list_recent(self, kind: str | None = None, limit: int = 20) -> list[Run]: ...
```

**The wire contract, because the seam is the load-bearing joint.** `useAppJob`
is not allowed to invent app routes, so the SDK mounts these itself under the
app's own namespace, and they are the only path between the two halves:

| Route | Maps to |
|---|---|
| `POST {app}/_jobs/{kind}/start` | `start(kind, params=…, dedupe_key=…)` |
| `GET {app}/_jobs/active?kind=` | `list_active(kind)` |
| `GET {app}/_jobs/recent?kind=&limit=` | `list_recent(kind, limit)` |
| `GET {app}/_jobs/{run_id}` | `get(run_id)` |
| `POST {app}/_jobs/{run_id}/cancel` | `cancel(run_id)` |

They are registered by the Route Registry like any app route, so the existing
`permissions.api` deny-by-default gate and the token-authenticated boundary apply
unchanged — an app reaches only its own runs, and a `kind` with no registered
runner is a 404 rather than a queued run nothing will ever service. Fixing the
seam is a P1 obligation, not a P2 discovery: getting it wrong is exactly what
would force a breaking reshape of the backend API once the first consumer lands.

**Ownership.** `get`, `list_active` and `cancel` see only this app's runs, and
`cancel` refuses a run it does not own — the same discipline as
`CronSDK.list_jobs` / `remove_job`.

**Persistence.** A run record is written to the app's own data directory as a
single document via `kiro_crew.atomic_write` (already imported by
`backend/backup.py:49`). This is the one thing Dev Fleet gets wrong: in-memory
run state cannot survive the restart that its own action triggers.

**States.** `queued → running → (done | failed | cancelled | interrupted)`.

**Cooperative cancellation is mandatory, not decorative.** `fn` receives a
handle:

```python
def run(handle: JobHandle, **params) -> dict: ...
#   handle.cancelled   -> threading.Event, checked at the runner's own checkpoints
#   handle.progress(pct=None, step=None, line=None)
```

A worker thread cannot be killed, which is why AWS Control's `_STOP` is
teardown-only and why a `cancel()` that cannot reach the runner would be a lie.
The SDK cannot inspect arbitrary `fn` code to find out whether it ever checks the
event, so **cancellability is declared, not inferred**: `register(..., cancellable=True)`
is the app's assertion that this kind's runner has checkpoints, and the default is
`False`. A run recorded `cancellable: false` has the control hidden in the UI
rather than offering a button that does nothing, and `cancel()` on it returns
`False` instead of pretending.

**Restart reconciliation.** On startup, after every app has registered its
runners, a persisted run still marked `running` whose owning process is gone is
resolved to `interrupted`, keeping its last progress. The registry is what makes
this decidable: a run whose `kind` no longer has a registered runner — the app was
disabled, or the kind was removed — is reconciled the same way rather than left
addressable by a hook that no longer exists. A run must never be left `running`
forever, and must never silently vanish — Dev Fleet's two failure directions
respectively.

**Deduplication.** `dedupe_key` makes a second `start` with a live key adopt the
existing run instead of beginning a second one. This is the direct remedy for
§1.1's duplicate-spend hazard: a double click, or two tabs, join one run.

**Observability, for free.** One registry means one place to emit run-level
records. Today `gateway.log` is WARNING-only with no per-run lines, so a single
run id cannot be traced at all; this investigation could only infer a restart
from a PID rollover.

## 4. Job SDK — frontend

```tsx
import { useAppJob } from '@kirocrew/app-sdk'

const job = useAppJob('backup')
//  job.active   : Run | null   — adopted on mount via list_active, no app code required
//  job.start(params) / job.cancel()
//  job.history  : Run[]        — from list_recent, so a fresh mount can populate it
```

On mount the hook calls `list_active(kind)` and adopts any in-flight run. That
single behaviour is what makes "navigate away and come back" correct by default.
Polling or streaming the progress tail is the SDK's business, not the app's.

Scope: this replaces `useMutation.isPending` as the *running* indicator for long
actions only. A short mutation — saving a setting — keeps `useMutation`, which is
the right tool for a request whose whole lifetime fits inside one view.

**Explicitly forbidden alternative:** do not configure a global mutation
persister to "also fix" this. Persisting a mutation cache asserts something
different from "the server has a run", and it invites replay of a mutation whose
effect already landed. The running fact must be server-owned; the client only
reattaches.

## 5. View-state SDK

**No host change is required.** React Router matches on pathname, so the query
string is already available under the flat `/:builtinApp` route — a builtin app
does it today (`website/src/apps/code-review-sage/CodeReviewSagePage.tsx:13`).
What is missing is not the capability but the contract:

```tsx
const [view, setView] = useAppViewState({ account: '', view: 'overview' })
setView({ account: '740412361337', view: 'drive' })   // -> ?ac.account=…&ac.view=drive
```

The app declares its keys and their defaults; the SDK owns serialization,
replace-vs-push semantics, and a per-app namespace so an app key can never
collide with a host param. Refresh, back, forward, and navigate-away-and-return
all follow from the URL being the source of truth.

This also unlocks a capability that is impossible today rather than merely
broken: a **shareable deep link into an app's internal position** — one account's
drive, one incident, one note.

Blast radius: none. `useAppViewState` is a pure addition to the SDK, so it ships
with its first consumer instead of needing a host PR of its own.

**Deferred, and genuinely optional:** *path*-backed view state
(`/aws-control/740412361337/drive`) would need `<Route path="/:builtinApp/*">`.
That change is safe — `App.tsx` already ships the same pattern for `/settings/*`,
a splat matches zero trailing segments, `BuiltinAppRoute` reads only the
`:builtinApp` param, and no builtin owns a sub-path today — but nothing in this
spec needs it, so it is not in the plan. Take it only if prettier URLs later
justify a host-wide route change on their own merits.

## 6. Reference consumers

**Job SDK — AWS Control backup** (`backup_run`, both kinds). The smallest change
with the clearest user-visible win, and it retires the duplicate-spend hazard via
`dedupe_key`. The runner is already idempotent in the way that matters: its S3
key is stamp-named, so an interrupted run cannot corrupt a previous archive.

**Job SDK — Dev Fleet prune**, second. Deliberately the action Dev Fleet's own
hand-rolled mechanism skipped, so the SDK is proven on the case that a per-app
effort did not reach.

**View-state SDK — AWS Control's three levels.** The code comments already name
the missing URL position as the reason all three are `useState`, so this consumer
converts a documented limitation into a deleted one.

## 7. Migration path

Each phase ships independently; no app is forced to move.

| Phase | Scope | User-visible |
|---|---|---|
| P1 | `JobSDK` incl. the runner registry and the `_jobs/*` wire routes, persistence, restart reconciliation; no consumers | none (additive) |
| P2 | `useAppJob`; AWS Control backup migrated | backup survives navigation; no duplicate runs |
| P3 | Dev Fleet onto the SDK — pull+build/provision drop `_RUNS`/`_SYNC_RID`; pod up/down and prune gain reattachment | pull survives the gateway restart it triggers |
| P4 | `useAppViewState` + AWS Control view state on the URL | lands where you left; drive links shareable |
| P5 | `docs/app-kit/api-reference.md` gains a `### Jobs` section beside `### Cron Jobs`, and both hooks join the `## App SDK Hooks` list; `getting-started.md` follows | app authors get the contract |

P5 is last on purpose. The app-kit docs are a contract with app authors, so they
describe an API that exists, not one that is planned.

## 8. Non-goals

- **Not a work queue or scheduler.** No fan-out, no priorities, no retry
  policies. `CronSDK` owns work-for-later; this owns work-a-human-is-watching.
- **Not distributed.** One gateway process on one host. A run record is
  meaningful only to the gateway that owns the app.
- **Not a replacement for the session work ledger.** That records an *agent's*
  state across context compaction; this records a *task's* state across unmount
  and restart. Same underlying move — promote a fact out of a volatile medium
  into a recoverable record — but different owners, different lifetimes, separate
  stores.
- **Not automatic retry.** An `interrupted` or `failed` run is reported, never
  silently re-run; the side effect may be half-applied and only the app knows if
  that is safe.
- **Not general UI-state persistence.** View state is URL-expressible view
  coordinates. Scroll offsets, draft text and transient selection stay local.

## 9. Failure modes

- **A runner declared `cancellable=True` that never checks `handle.cancelled`.**
  `cancel()` cannot reach it and the UI offers a control that does nothing. The
  declaration is the app's assertion and the SDK cannot verify it, so the
  migration checklist for a consumer includes naming the runner's checkpoints.
- **Gateway killed mid-run.** The run reconciles to `interrupted` on next
  startup with its last progress. The side effect may be partially applied, so a
  runner is expected to be idempotent or to name its own recovery.
- **Two clients watching one run.** Both adopt it through `list_active`; there is
  one owner record, and `dedupe_key` prevents a second start.
- **Unbounded run record.** The progress tail is bounded, on the same discipline
  as the session work ledger's event tail; a run keeps a window, not a
  transcript.
- **Two apps, or an app and the host, claim the same query key.** The per-app
  namespace prefix is what prevents it, so the prefix is part of the contract
  rather than a convention — an app writing bare keys can collide with a host
  param on a shared URL.
- **A migrated app double-reports.** During P2/P3 an action must not keep both
  `useMutation.isPending` and `job.active` as running indicators; the migration
  removes the former in the same commit that adds the latter.
