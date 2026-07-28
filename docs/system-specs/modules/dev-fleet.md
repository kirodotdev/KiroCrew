# Dev Fleet Module

Last Updated: 2026-07-22

## Overview

Dev Fleet is a builtin App Store app (`kiro_crew/apps/builtins/dev_fleet/server.py`) for
managing KiroCrew feature worktrees (git worktrees of the main repo) and their isolated
pod test instances. It runs as a managed app backend SUBPROCESS: an aiohttp server on the
backend-assigned port, reached only through the gateway proxy. Every proxied request
carries an HMAC signature (`X-KiroCrew-Proxy: <ts>:<hmac>` over
`<ts>:<METHOD>:<path>[?q]:<sha256(body)>`, +/-60s window) verified fail-closed by the
backend's middleware; the shared secret lives at `apps_dir()/dev-fleet/.app_secret`.
Gateway session auth (token/cookie) gates the proxy entrance as with all builtin apps.

## Responsibilities

1. **Worktree discovery** — enumerates git worktrees via `git worktree list --porcelain`
2. **Pod integration** — spin up/down/restart isolated pod instances per worktree
3. **Pull+Build sync** — pull origin/main and rebuild (venv + frontend dist)
4. **Prune** — safely remove merged/empty worktrees with PR-shipped verification
5. **Rebase** — rebase feature branches onto main with conflict detection + abort
6. **GitHub PR status** — TTL-cached `gh pr list` queries for merge state
7. **Make Live** — repoint the live gateway at another worktree via a systemd
   `--user` drop-in (never edits the shipped unit file)

## Routes

Public routes are under `/apps/dev-fleet/api/*` (gateway proxy, session auth via token
query param or cookie); the backend subprocess serves them as `/api/*` after HMAC
verification. Route names below are relative to that prefix.

### Read (GET)

| Route | Description |
|-------|-------------|
| `/apps/dev-fleet/api/health` | Liveness + gateway **start identity**: `{status, start_id}`. `start_id` is the live unit's `ExecMainStartTimestampMonotonic` (or `null` when unavailable); the dashboard polls it to detect the NEW process after a restart (see Action narration). Served on the proxied `/api/` namespace because the gateway only forwards `/apps/dev-fleet/api/*` to the backend. (The bare `/health` carries the same body but is HMAC-exempt and reached only by the gateway's own internal liveness poll.) |
| `/apps/dev-fleet/api/fleet` | Lightweight worktree + pod list (polled every 12s). `?fresh=1` forces cache bypass. |
| `/apps/dev-fleet/api/worktree?name=` | Lazy per-branch detail: PR, commits, disk usage |
| `/apps/dev-fleet/api/pod/logs?name=&n=` | Pod journal tail (recent N lines, default 120) |
| `/apps/dev-fleet/api/run?id=` | Async run status + streamed output (last 60 lines) |
| `/apps/dev-fleet/api/prune-candidates` | List worktrees eligible for pruning |
| `/apps/dev-fleet/api/prune-status` | Live prune progress: per-item state machine (`items`) + backward-compatible top-level counters |
| `/apps/dev-fleet/api/disk` | Aggregate disk usage per worktree (async computation) |

### Write (POST)

| Route | Body | Description |
|-------|------|-------------|
| `/apps/dev-fleet/api/sync` | — | Pull main + rebuild (single-flight; a concurrent call is refused **409**) |
| `/apps/dev-fleet/api/worktree/remove` | `{name, force?}` | Remove a worktree (stops pod first) |
| `/apps/dev-fleet/api/prune-run` | `{names[]}` | Batch-remove eligible worktrees |
| `/apps/dev-fleet/api/pod/up` | `{name}` | Start isolated pod instance (re-verifies the unit is active) |
| `/apps/dev-fleet/api/pod/down` | `{name}` | Stop pod instance (re-verifies the unit is gone before reporting success) |
| `/apps/dev-fleet/api/pod/restart` | `{name}` | Stop then start pod |
| `/apps/dev-fleet/api/pod/token` | `{name}` | Mint a dashboard token for the pod |
| `/apps/dev-fleet/api/pod/provision` | `{name}` | Start async venv+dist build (returns `{run_id}`) |
| `/apps/dev-fleet/api/rebase` | `{name}` | Rebase worktree onto origin/main |
| `/apps/dev-fleet/api/restart-gateway` | — | Restart the live gateway in place (detached `systemd-run`); returns the pre-restart `start_id` for the restart handshake |
| `/apps/dev-fleet/api/make-live` | `{path, dry_run?}` | Repoint the live gateway at another worktree (see Make Live); a real cutover returns `start_id` for the restart handshake |

## Authorization

All endpoints inherit gateway session auth. No additional RBAC — all authenticated users
can manage worktrees. Destructive operations (remove, prune) require client-side confirmation
dialogs in the frontend.

## Input Validation

- `name` parameter is validated against the discovered worktree set before any operation
- Ambiguous worktree names (multiple checkouts with same basename) return HTTP 400
- `force` must be a boolean when provided
- Main worktree removal is always refused regardless of force flag

## Prune Rules

A worktree is eligible for automatic pruning if:

1. **PR merged** — GitHub PR state is `MERGED` AND `git cherry` shows 0 patch-unique
   commits ahead of main AND the worktree is not dirty
2. **Empty + stale** — zero own commits, not dirty, and older than 48 hours

Worktrees NOT pruned: dirty, active (own commits > 0), fresh (< 48h), or merged-with-
new-commits (unmerged follow-up work after the PR landed).

### Parallel execution & per-item progress (issue #435)

`prune-run` accepts a batch of names and processes them **concurrently** rather than one
at a time. The design separates the two cost classes:

- **Expensive per-item phases run in parallel.** The fresh `_prunable` re-verdict (which
  makes `gh`/`git` network calls) and pod shutdown run under an `asyncio.Semaphore(4)`, so
  a batch is bounded by the slowest ~4 items at a time instead of the sum of all of them.
- **Git mutations are serialized.** The `git worktree remove` + branch `update-ref -d` for
  every removal — including the single-worktree remove handler and the auto-prune reaper —
  run behind one shared `asyncio.Lock` (`_GIT_MUTATION_LOCK`), because they mutate the
  shared main-repo `.git` state (worktree admin dir + `packed-refs`). Concurrent git
  mutations would otherwise race on those lock files.

**Failure isolation:** each item is driven to a terminal state independently — one item
failing (a `gh` timeout, a stuck pod, or an unexpected exception) never aborts the rest of
the batch, and every item is finalized exactly once (terminal status + `done` bump).

**Per-item status API:** `prune-status` returns an `items` map keyed by worktree name,
each `{status, error}` where `status` is one of `pending | verifying | stopping_pod |
removing | done | failed`. The top-level `running`, `total`, `done`, `current`, and
`results` fields are retained for API-shape compatibility (the auto-prune reaper and older
consumers). Note that under parallel execution `current` is **best-effort**: it names one
of the currently in-flight items (never a completed one; `None` when idle), not "the"
single item being processed — new consumers should read `items` instead. Duplicate names
in a `prune-run` request are deduplicated (order-preserving) before workers launch, so a
name never has two workers racing to remove the same worktree. The frontend renders
`items` as a per-item checklist (status chip + inline
failure reason); the preview dialog maps the kept-list verdict codes to human-readable
reasons so users can see why a worktree is a candidate or is kept.

## Pod Integration

Relies on `kiro_crew.pod` subpackage (optional import — degrades gracefully if unavailable):

- `runtime.active_names(cfg)` — systemctl list (blocking, offloaded via `run_in_executor`)
- `runtime.derive_port(cfg, name)` — cksum-based port derivation (blocking, offloaded)
- `runtime.health(port, timeout)` — HTTP probe (blocking, offloaded)
- `runtime.mint_token(cfg, name, ttl)` — token minting (blocking, offloaded)
- `runtime.recent_journal(cfg, name, n)` — journalctl tail (blocking, offloaded)
- `provision.has_venv(path)` / `provision.has_dist(path)` — filesystem checks (offloaded)

All blocking pod operations are offloaded via `asyncio.get_running_loop().run_in_executor(
subprocess_executor(), ...)` to avoid blocking the gateway event loop.

Pod lifecycle verbs (`up`/`down`/`restart`/`provision`) shell the CLI via
`_find_cli()` = `[sys.executable, "-m", "kiro_crew"]` — the **package** entry
(`kiro_crew/__main__`, which also runs the required SSL-cert / UTF-8-console
setup), never `-m kiro_crew.cli`. `kiro_crew/cli.py` has no
`if __name__ == "__main__"` guard, so `python -m kiro_crew.cli <cmd>` imports the
module, runs no `main()`, and exits 0 with no output — which turned every pod op
into a **silent no-op the backend reported as success** (the "Stopped but still
running" bug, issue #220). As defence-in-depth, `_pod_up` and `_pod_down` both
re-check `runtime.active_names` after the CLI returns and fail closed
(`pod not active after start` / `pod still active after shutdown`) — a CLI exit 0
is never taken as proof of the state change, in either direction.

### Provisioning Dependency Install

`provision.ensure_venv` and `provision.build_dist` install the dependencies each
step needs before using them, so provisioning a **fresh** worktree (no
`.venv`, no gitignored `website/node_modules`) does not fail on missing tools:

- **venv (`ensure_venv`)** — after `python -m venv`, upgrades pip, then runs
  `pip install --editable <checkout> --group dev` so the PEP 735 `dev`
  dependency-group (pytest, flake8, isort, mypy, …) is present and the build
  gate can run inside the pod venv (issue #230). `pip --group` needs pip
  ≥ 25.1; if the command exits nonzero (older pip) it falls back to a
  runtime-only `pip install --editable <checkout>` and `_say`s a warning that
  dev tools were skipped — provisioning never hard-fails just because the dev
  extras could not be installed.
- **dist (`build_dist`)** — before `npm run build`, calls
  `ensure_node_modules(website)`: if `website/node_modules/.bin/tsc` is missing
  it runs `npm ci` (falling back to a NON-MUTATING `npm install
  --no-package-lock` on lockfile drift — the flag keeps the fallback from
  rewriting the tracked `website/package-lock.json`, so provisioning never
  dirties the worktree), otherwise
  it skips (fast idempotent path). Without this, a fresh worktree's `npm run
  build` dies with `tsc: command not found` (issue #229).

### Pod Unit ExecStart Self-Heal

On `pod up`, if the installed systemd unit template's `ExecStart` binary no longer exists
(typically because the worktree it resolved into was pruned), the pod CLI:

1. Detects the dangling binary via `unit.unit_exec_ok(cfg)` (reads the unit file, checks
   `os.access(exe, os.X_OK)` on the baked path)
2. Re-renders the unit with a currently-valid binary (`unit.install_unit(cfg)`)
3. Runs `daemon-reload`
4. Audits the self-heal event
5. Proceeds to start the pod normally

This prevents the permanent EXEC 203 failure loop that occurs when worktrees are pruned
after the unit was installed.

## Background Tasks

- **Status refresher** (`_status_refresher`) — runs every 60s, fetches origin + refreshes
  fleet cache. Started via `dev_fleet_startup` on app startup.
- **Auto-prune reaper** (`_auto_prune_reaper`) — opt-in background loop that removes
  merged worktrees on a timer, reusing the manual-prune verdict (`_prune_candidates`,
  filtered to `code == "merged"` only — the stale-empty class stays manual) and
  `_worktree_remove` guards (stops the pod first, squash-safe OID race guard, never
  force). Disabled by default; enable via `dev_fleet.auto_prune.enabled: true`
  (a **literal boolean** — a truthy string like `"false"` does NOT arm it) with
  optional `interval_secs` (floored at 300s, default 3600s), re-read each cycle
  so it toggles live
  without a restart. Cycles that remove or fail anything are SEL-audited under
  `dev_fleet_auto_prune`. Cancelled on `dev_fleet_cleanup`.
- **Fleet cache** — 10s TTL. Cold requests block on fresh data; warm requests serve stale
  and background-refresh.

## Async Runs

Long-running operations (sync, provision) are tracked via `_RUNS` dict with:
- Streamed stdout (last 500 lines kept **server-side**)
- Watchdog deadline (30 min default, configurable via `_RUN_DEADLINE_S`)
- Status: `running` → `done` | `timeout`

Clients poll `/apps/dev-fleet/api/run?id=<run_id>` for progress. The endpoint
returns only the **last 60 lines** of `run.output` (a sliding tail window), not
the full server-side 500-line buffer — see the accumulation note below.

### Provision progress UX (frontend)

A worktree being provisioned renders an inline **stepper strip** spanning the
row's right columns (mirroring the main-row Pull+Build stepper): spinner +
`Provisioning` label + a coarse phase tag (`venv`/`dist`, derived from
provision.py's `[provision] creating venv …` / `[provision] building dist …`
markers) + the last output line + elapsed time + a `log ▾`/`log ▴` toggle. The
toggle expands a `<pre>` panel under the row showing the accumulated log
(auto-scrolled while streaming).

**Log accumulation (what "full log" actually means).** The `/run` endpoint only
returns the last 60 output lines per poll, so a long provision scrolls early
lines out of that window. The client therefore **accumulates** windows rather
than replacing state each poll: `mergeLogWindow(buffer, window)` finds the
longest suffix of the running buffer that is also a prefix of the newly polled
window and appends only the non-overlapping remainder. This reconstructs the
full stream across the normal case where the window advances by fewer than 60
lines between two ~2s polls. **Honest limitation:** output that scrolls more
than a full 60-line window between two polls (extremely fast-scrolling bursts)
has no overlap to anchor on and those intermediate lines are lost. When that
happens (zero overlap against a non-empty buffer), the client inserts a visible
`[… lines missed …]` marker line into the panel so the transcript never
silently overstates its completeness — the panel is the best client-side
reconstruction plus an explicit gap signal, not a guaranteed-complete
transcript. The heuristic's retirement path (a `since=<index>` cursor or raised
tail on `/run` for a guaranteed-complete log) is tracked in issue #321.

**Reattach on button-click (single-flight).** The provision endpoint is
single-flighted per checkout: if a provision is already running it replies
`{ok:false, error:"provision already running", run_id:<in-flight rid>}`. The
frontend treats **any** response carrying a `run_id` as a run to attach to and
resumes polling it — it does **not** render a failure. Only a response with no
`run_id` is a genuine "failed to start". This makes a second Provision click
during an in-flight build reattach to the live run instead of showing a false
red state.

**Failure persistence:** on failure/timeout the run is **not** cleared — the
strip shows a red `✕ Provision failed (exit N)` label with the log
auto-expanded, and both persist until the user clicks the dismiss `×`
(dismiss also refreshes the fleet). On success it flashes a green
`✓ Provisioned` briefly, then clears (the fleet refetch flips the row to its
built state).

**Known limitation — no reattach after a page reload.** Provision run state
(including the persisted failed run and its log) lives only in component memory.
A browser reload during or after a provision loses it, because the `/fleet`
payload exposes no provision run ids to reattach to on mount (unlike sync, which
reattaches via `sync_run_id`). The single-flight reattach above only covers a
Provision **button-click** while a run is in flight, not a fresh page load.
Server-backed reattach (exposing active/failed provision run ids in `/fleet` so
the page can reattach on mount, mirroring `sync_run_id`) is tracked as follow-up
work ([issue #321](https://github.com/kirodotdev/KiroCrew/issues/321); see also
[issue #231](https://github.com/kirodotdev/KiroCrew/issues/231), PR #320).

## Action narration (restart + sync feedback)

Dev Fleet's two slowest actions — **Restart Gateway** and **Sync (Pull+Build)** —
narrate their progress so users don't read them as hung and fire them again. A
duplicate Restart Gateway causes a second real ~10s gateway outage
([issue #639](https://github.com/kirodotdev/KiroCrew/issues/639)).

### Restart identity handshake

`POST /apps/dev-fleet/api/restart-gateway` returns `{"ok": true, "start_id": …}`
the instant `systemd-run --collect systemctl --user restart <unit>` has
**scheduled** the restart — the bounce happens after the response and takes ~10s
because graceful shutdown times out. "The request succeeded" therefore says
nothing about whether the gateway is back.

To close that gap the backend captures the unit's **start identity** BEFORE
scheduling the restart and hands it to the frontend:

- **Identity = `ExecMainStartTimestampMonotonic`** — the CLOCK_MONOTONIC
  microsecond stamp of the unit's ExecStart *main* PID (`_gateway_start_id`).
  Chosen because it is (a) monotonic, so it can only increase and never repeats
  or goes backwards across a restart even if the wall clock is stepped by NTP,
  and (b) tied to the actual main-process spawn, so it changes the instant the
  NEW process starts (a unit can enter `active` before its replacement main PID
  exists, so `ActiveEnterTimestampMonotonic` is a weaker signal).
- The current identity is reported by extending the existing **`/health`**
  surface (`{status, start_id}`). Because the gateway proxies only
  `/apps/dev-fleet/api/*` to the backend, the same handler is registered at
  **`/api/health`** and the dashboard polls **`/apps/dev-fleet/api/health`**
  (the bare `/health` stays HMAC-exempt for the gateway's internal liveness
  poll). The gateway is treated as recovered ONLY when the reported `start_id`
  DIFFERS from the one captured before the restart. A 200 from the old process
  still winding down returns the SAME identity and is correctly NOT counted as
  recovered.
- **None-safe degrade.** On a platform that cannot report identity (non-Linux,
  no `systemctl`, or a `0`/absent stamp) `start_id` is `null`; the frontend then
  degrades to the legacy "reload on the first reachable response" instead of
  hanging forever in the restarting overlay.
- **A reachable 404 counts as recovery.** Cutting over to a worktree whose
  dev-fleet backend predates `/api/health` leaves that route answering 404
  permanently, so its `start_id` can never appear and waiting for one would burn
  the whole timeout. A 404 during the handshake still proves a gateway IS serving
  us, so it is treated as recovered and the page reloads into it. (A backend that
  is not up at all fails differently — the proxy answers 502, or the fetch
  rejects — so this rule does not fire while the new process is still starting.)
- **Make Live reuses the same handshake** — a cutover is a restart into
  different code with the identical early-200 hazard, so a real
  `POST …/make-live` cutover also returns the pre-restart `start_id` and the UI
  recovers on an identity change.

### Restarting UI state

While the handshake runs, the frontend holds an explicit **"Restarting —
reconnecting"** full-screen state and disables Restart / Pull+Build / Make Live
so the slow window cannot be re-fired. The poll is bounded (`RESTART_TIMEOUT_MS`,
60s); on timeout it surfaces an actionable error ("reload manually / check
`kirocrew logs`") instead of spinning forever.

**The lockout starts before the overlay does.** The restarting flag only goes
true once `POST …/make-live` has *returned*, but that request is itself what
writes the systemd drop-in and issues the daemon-reload — a Restart fired inside
that window can tear the gateway down between the write and the reload, leaving
persisted and loaded unit state inconsistent. Every global action predicate
therefore also honours an in-flight cutover on ANY worktree row (the busy flag is
per-worktree; the hazard is process-wide).

### Sync single-flight + step narration

`POST /apps/dev-fleet/api/sync` is single-flight: a second concurrent request is
refused with **HTTP 409** (`{"ok": false, "error": "sync already running",
"run_id": …}`) rather than launching a second ~90s fetch → merge → pip install →
npm ci → npm build. The run script emits a `::step::<idx>::<label>` marker per
step; the run worker records BOTH the authoritative step index and its **label**
onto the run entry (`step` / `step_label`), so `/run` can name the CURRENT step
even after the marker scrolls out of the 60-line output tail window. The
frontend shows that label beside the "Syncing" progress bar. This reuses the
existing `_RUNS` / `::step::` / `/run` run-tracking mechanism — the same channel
the provision log panel uses (#320) — rather than adding a second one.

## Make Live

`POST /apps/dev-fleet/api/make-live` repoints the live gateway at a different
worktree. `_restart_gateway` only bounces the live unit *in place* — the
shipped unit file hardcodes `WorkingDirectory`/`ExecStart`/`PATH`, so it cannot
point the gateway at another checkout. Make Live closes that gap with a systemd
`--user` **drop-in** that overrides those three fields; the shipped unit file is
never edited.

### Request / Response

Request body: `{path, dry_run?}` — `path` is a worktree path (validated against
the discovered set, never an arbitrary path); `dry_run` (bool, default false)
returns the plan without touching systemd.

- **dry_run success:** `{ok: true, dry_run: true, plan: {unit, dropin_path,
  dropin_content, target}}`
- **cutover success:** `{ok: true, cutover: true, target, plan}`
- **refusal:** `{ok: false, code, error}` — `code` is one of the values below.

The handler additionally returns HTTP 400 for a missing/non-string `path` or a
non-boolean `dry_run`.

### Error codes

| Code | Meaning |
|------|---------|
| `unknown_path` | `path` is not a discovered worktree |
| `missing_path` | the worktree path no longer exists on disk |
| `pod` | called from inside a pod — a throwaway test instance must never repoint the live gateway |
| `pod_indeterminate` | pod status could not be resolved (config home unresolvable) — **fail-closed**, never treated as "not a pod" |
| `no_systemd` | not Linux / `systemctl` absent — Make Live requires systemd `--user` |
| `no_user_unit` | `systemctl` present but the live gateway is **not** a loaded `--user` unit (e.g. a `kirocrew service install` SYSTEM unit) — the `--user` drop-in + restart would be a silent no-op |
| `already_live` | the target is already the live gateway |
| `missing_venv` | the worktree has no `.venv/bin/kirocrew` (Provision it first) |
| `venv_not_executable` | the worktree's `.venv/bin/kirocrew` exists but is **not executable** (`chmod +x` it or re-Provision) — a non-executable binary would stop the live gateway but could not start the replacement, leaving no gateway running |
| `missing_dist` | the worktree has no built `src/kiro_crew/static/dist/index.html` (Pull+Build first) — a cutover without a built dist serves a broken dashboard |
| `unsafe_path` | the worktree path contains a newline, NUL, or other control character and cannot be safely written into a systemd directive (paths with spaces / `%` / quotes are *escaped*, not rejected) |
| `write_failed` | writing the drop-in file failed |
| `reload_failed` | `systemctl --user daemon-reload` failed — the drop-in is rolled back to its prior state before returning (response carries `rolled_back`) |
| `restart_failed` | the detached `systemd-run` restart failed to launch — the drop-in is rolled back before returning (response carries `rolled_back`) |
| `busy` | another make-live cutover is already in progress — the mutation sequence is single-flighted, so a concurrent request is refused immediately (no queueing) rather than racing the in-flight cutover's drop-in write/rollback |
| `restart_pending` | a cutover has already been **successfully scheduled** in this gateway process — `systemd-run` only *schedules* the restart and returns immediately, so a process-local latch refuses every further request (cutover **and** `dry_run`) until the pending restart replaces the process. The fresh gateway starts with the latch clear |

On a `reload_failed` / `restart_failed` refusal the response includes
`rolled_back: true|false` — whether the pre-cutover drop-in state (prior
content, or absence) was successfully restored on disk.

### Concurrency

The cutover mutation (prior-state snapshot → atomic drop-in write →
`daemon-reload` → detached `systemd-run` restart → any rollback) runs under a
single module-level `asyncio.Lock`. Two concurrent cutovers would otherwise
race on the shared drop-in file — one request's failure rollback could
restore or delete the other's successful override, restarting the gateway into
the wrong worktree. A second request that arrives while the lock is held is
refused immediately with `busy` (fail-fast, **not** queued): serializing the
queue could apply a stale target after the winner already restarted the
gateway. The `dry_run` validation path mutates nothing and runs outside the
lock.

**Committed latch.** `systemd-run` only *schedules* the detached restart and
returns immediately, so the lock is released while the restart is still
pending. A process-local `_MAKE_LIVE_COMMITTED` flag is set to `True` — before
returning success, inside the lock — the moment a cutover is scheduled. It is
checked both at function entry and again after the lock is acquired (closing
the entry-check-vs-acquire race), so any further request — a second cutover for
a different target, or even a `dry_run` — is refused with `restart_pending`
instead of mutating the drop-in while the pending restart tears the backend
down. The latch is never persisted: the fresh gateway the restart spawns starts
clear. Failure paths **before** successful scheduling (write / `daemon-reload`
/ `systemd-run` launch) never set it, so a rolled-back cutover leaves the
process free to retry.

### Validation order

Every check runs for `dry_run` too, in this order (first failure wins):

`path` (exists as a known worktree) → **pod guard** (fail-closed on
indeterminate) → **user-unit check** (loaded systemd `--user` unit) →
`already_live` → `missing_venv` → `venv_not_executable` → `missing_dist`.

The pod guard and user-unit check precede the venv/dist checks so an operator on
an ineligible install gets an actionable refusal before any per-worktree state
matters.

### Drop-in mechanism

The drop-in is written to
`$XDG_CONFIG_HOME/systemd/user/kirocrew-gateway.service.d/make-live.conf`
(falls back to `~/.config`). Its body overrides exactly three fields:

```ini
[Service]
WorkingDirectory=<worktree>
ExecStart=
ExecStart=<worktree>/.venv/bin/kirocrew gateway --no-open
Environment=PATH=<worktree>/.venv/bin:~/.local/bin:/usr/local/bin:/usr/bin:/bin
```

The lone empty `ExecStart=` line **resets** the unit's `ExecStart` before the
replacement — systemd otherwise *appends*, and a `Type=simple` service with two
`ExecStart` values is a fatal unit error. `~` is not expanded inside
`Environment=`, so the operator bin dir is materialised to an absolute path.

**Value escaping.** All three directives undergo systemd specifier expansion,
so every interpolated value is serialised through `_sd_value`, which:

- **rejects** (→ `unsafe_path`) any value containing a newline, NUL, or other
  control character — such a value would split/truncate the drop-in, and the
  persisted-but-invalid override would then block every subsequent restart;
- doubles a literal `%` to `%%` (defeating specifier expansion);
- double-quotes the value — escaping `\` → `\\` and `"` → `\"` per systemd's
  command-line C-style quoting — **only** when it contains whitespace or a
  systemd metacharacter. A clean path is emitted verbatim (unquoted), so an
  ordinary worktree renders byte-for-byte as before. This makes a worktree path
  with spaces, `%`, or quotes cut over correctly instead of corrupting the
  unit.

### Detached restart

A real cutover writes the drop-in **atomically** (a temp file in the same
directory + `os.replace`, so a partial write never leaves a truncated unit),
runs `systemctl --user daemon-reload`, then issues the restart via `systemd-run
--user --collect systemctl --user restart kirocrew-gateway.service`. Because
the restart tears down this backend along with the gateway, the restart is
detached (same pattern as `restart-gateway`) so it survives our own death. The
`_LIVE_WORKTREE` cache is then invalidated so the next fleet poll re-resolves
the live checkout.

**Failure rollback.** Before writing, the prior drop-in state is snapshotted
(existing `make-live.conf` content, or absence). If `daemon-reload` or the
`systemd-run` launch fails, the drop-in is restored to that prior state
(rewrite the old content, or delete the file when there was none) and
`daemon-reload` is re-run best-effort so the loaded config matches disk. Without
this, a persisted override from a failed cutover would silently activate on the
NEXT unrelated restart. The refusal response carries `rolled_back: true|false`.

### Platform limitation

Make Live is **Linux + systemd `--user` only**. A `kirocrew service install`
SYSTEM unit (`/etc/systemd/system/kirocrew.service`) is not controllable via
`systemctl --user` and is refused up-front with `no_user_unit`; non-systemd
hosts are refused with `no_systemd`. Cutover from inside a pod is always
refused (`pod` / `pod_indeterminate`).

## Output Redaction

All user-visible output passes through `redact_credentials()` and
`redact_exfiltration_urls()` before HTTP response serialization.

## Platform Behavior

- **Linux only** — pod integration requires systemd (systemctl, journalctl)
- **macOS** — worktree management works; pod operations degrade (import fails gracefully)
- **Make Live** — Linux + systemd `--user` only; refuses on non-systemd hosts
  (`no_systemd`) and on SYSTEM-unit installs (`no_user_unit`)
- **git** and **gh** CLI required for full functionality; missing binaries produce
  graceful degradation via OSError catch in `_run_cmd`

## Bundled Skills

The app bundles two skills declared in `app.json`:

- `skills/pod-e2e` — end-to-end test harness for isolated pod instances
- `skills/feature-demo-recording` — headless Playwright video recording

`kirocrew-worktree-dev` is deliberately NOT bundled: the canonical copy is
owned by the `skills/kirocrew-dev/` development-skills folder (synced into
every install via the project-dir mechanism), and the app-bridged duplicate
was removed because two copies of the same skill drift and get loaded
nondeterministically against each other (PR #353 arbiter finding).

Skills are registered as symlinks into `~/.kiro/crew/skills/` via the app bridge at
two lifecycle points:

1. **On enable** — `register_app()` in `bridges.py` creates namespaced + flat symlinks
2. **On gateway startup** — `reconcile_app_skills()` in `bridges.py` (called from
   `start_enabled_app_backends()`) ensures manifest-declared skills are linked for
   already-enabled apps, creating missing symlinks and removing stale ones for skills
   dropped from the manifest since the last registration

This reconcile step addresses the upgrade gap: an in-place version upgrade that adds
new skills would otherwise never get symlinks without a disable/enable cycle.

## QA + Video Row Action

Each worktree row in the frontend exposes a "QA + video" action (Video icon) that:

1. Composes a seeded prompt (pod-e2e suite + feature-demo-recording)
2. Dispatches `setPendingInput(prompt)` to the chat store
3. Navigates to `/chat?autoSend=1&newSession=1`

This launches an agent session that runs the full QA cycle (pod up, API + Playwright
tests, demo video recording, summary) without any backend route — it is entirely a
frontend-only seeded session pattern.

## Live Worktree Removal Guard

The `POST /apps/dev-fleet/api/worktree/remove` endpoint (and its `force` variant)
performs a fresh uncached resolution of the live gateway's worktree path before any
removal. If the target worktree is the one currently running the live gateway process,
the request is refused with a descriptive error — regardless of the `force` flag.

The check uses `_live_worktree_path()` which performs a fresh filesystem resolution
(no caching) to avoid TOCTOU issues where a previously-cached path is stale.
