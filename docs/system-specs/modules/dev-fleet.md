# Dev Fleet Module

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

1. **Worktree discovery** — enumerates git worktrees via `git worktree list --porcelain`,
   dropping records git flags `prunable` (checkout directory deleted without a
   `git worktree prune`); the primary checkout is never dropped, since it anchors `is_main`
2. **Pod integration** — spin up/down/restart isolated pod instances per worktree
3. **Pull+Build sync** — pull origin/main and rebuild (venv + frontend dist)
4. **Prune** — safely remove merged/empty worktrees with PR-shipped verification
5. **Rebase** — rebase feature branches onto main with conflict detection + abort
6. **GitHub PR status** — TTL-cached `gh pr list` queries for merge state
7. **Make Live** — repoint the live gateway at another worktree via a
   live-target pointer file (no service definition is ever mutated)

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
| `/apps/dev-fleet/api/restart-gateway` | — | Restart the live gateway through its service-manager backend; returns the pre-restart `start_id` for the restart handshake |
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
  and background-refresh. Concurrent rebuilds (the background revalidate plus any number
  of `?fresh=1` requests) coalesce onto a single in-flight build, so a rebuild never costs
  more than one `gh pr` round-trip per branch. A successful `_worktree_remove` evicts that
  worktree from the cached snapshot and zeroes the timestamp, so the next response stops
  listing a removed worktree without waiting for a rebuild. An eviction also tombstones the
  name against an eviction counter: a rebuild that started before the removal still read the
  worktree from git, so it re-applies any eviction recorded after it began rather than
  storing a snapshot that would resurrect the row. Tombstones are reaped by the first build
  that started after them, so a worktree later re-created under the same name is not hidden.
  The dashboard refreshes with
  `?fresh=1` after every mutating action (and on the explicit Refresh button) so it never
  renders the pre-mutation snapshot.

## Async Runs

Long-running operations (sync, provision) are tracked via `_RUNS` dict with:
- Streamed stdout (last 500 lines kept **server-side**)
- Watchdog deadline (30 min default, configurable via `_RUN_DEADLINE_S`)
- Status: `running` → `done` | `timeout`

On deadline expiry the run's whole process tree is reaped, in two steps. The
spawned CLI gets its own process group, so a single `killpg` covers it and its
ordinary children (pip, git, npm). That is not sufficient on its own: build
tooling spawns grandchildren into *new sessions*, which sit in a different
process group and survive a group kill. So descendants are enumerated **before**
any signal is sent — killing reparents survivors to init and erases the PPID
links that identify them — and each survivor is then killed via its own tree
kill, so a nested group (npm → vite) goes down with it.

This matters beyond tidiness: an escaped `npm run build` keeps rewriting
`website/dist` after the run is reported dead, and its staging lock died with
the process that held it. A later sync would then stage a bundle a live writer
is still mutating, and the completeness check cannot detect it — that check only
resolves `/assets/` references reachable from `index.html`, while the
lazy-loaded chunks such a writer is mid-write on are unreachable from it.

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
after the platform manager accepts the restart. Linux schedules detached
`systemd-run`; macOS submits `launchctl stop` under the loaded contract described
below. The bounce happens after the response, so success does not mean the new
gateway is serving yet.

To close that gap the backend captures the unit's **start identity** BEFORE
scheduling the restart and hands it to the frontend:

- **Identity is manager-specific.** systemd uses
  `ExecMainStartTimestampMonotonic`; launchd uses the loaded job PID. Both change
  when the replacement main process starts.
- The current identity is reported by extending the existing **`/health`**
  surface (`{status, start_id}`). Because the gateway proxies only
  `/apps/dev-fleet/api/*` to the backend, the same handler is registered at
  **`/api/health`** and the dashboard polls **`/apps/dev-fleet/api/health`**
  (the bare `/health` stays HMAC-exempt for the gateway's internal liveness
  poll). The gateway is treated as recovered ONLY when the reported `start_id`
  DIFFERS from the one captured before the restart. A 200 from the old process
  still winding down returns the SAME identity and is correctly NOT counted as
  recovered.
- **None-safe degrade.** An absent/zero systemd stamp or absent launchd PID
  yields `start_id: null`; the frontend then reloads on the first reachable
  response instead of waiting forever.
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
writes the live-target pointer and issues the restart — a Restart fired inside
that window can tear the gateway down between the pointer write and the restart,
leaving a stale process running against the new pointer. Every global action
predicate therefore also honours an in-flight cutover on ANY worktree row (the
busy flag is per-worktree; the hazard is process-wide).

### Sync single-flight + step narration

`POST /apps/dev-fleet/api/sync` is single-flight: a second concurrent request is
refused with **HTTP 409** (`{"ok": false, "error": "sync already running",
"run_id": …}`) rather than launching a second ~90s fetch → merge → pip install →
npm ci → npm build + stage. The run script emits a
`::step::<idx>::<label>` marker per
step; the run worker records BOTH the authoritative step index and its **label**
onto the run entry (`step` / `step_label`), so `/run` can name the CURRENT step
even after the marker scrolls out of the 60-line output tail window. The
frontend shows that label beside the "Syncing" progress bar. This reuses the
existing `_RUNS` / `::step::` / `/run` run-tracking mechanism — the same channel
the provision log panel uses (#320) — rather than adding a second one.

The whole FRONTEND half of the sync — `npm ci` and `npm build + stage` — is
**skipped on an edition checkout** (`frontend.edition_configured()`). The build
runs under `_build_env()`, whose allowlist drops `KIROCREW_EDITION_DIR` and
`KIROCREW_ALLOW_EDITION`, so on an edition composition root it can only compile
the STOCK SPA; staging that would silently replace the edition dashboard with
upstream's. Skipping is what makes it safe, and it costs an edition nothing —
the only artifact this path could produce for it is a bundle it must never
serve.

The final **npm build + stage** step builds the frontend and copies `website/dist` into
`src/kiro_crew/static/dist` under the Dev Fleet backend's OWN interpreter, with
the target repo passed as an argument. Resolving the helper from the target
instead would make the step's very existence contingent on the pulled revision
already carrying it, so an older target would turn the whole Pull+Build into an
ImportError. It is not cosmetic. On a source install `static/dist` is a *symlink*
to `website/dist` (`ensure_dev_dist_symlink`), and aiohttp resolves a static
route's directory once at registration — so a gateway started in that state is
pinned to the Vite output directory for its whole life, and every `npm build`
rewrites the tree it is serving. Staging leaves a real snapshot there, so from
the gateway's **next start** onward a build cannot touch what it serves. It
publishes the same bundle the build just wrote into `website/dist`, which keeps
the pinned `/assets` route and the staged `index.html` on the same hashed
chunks. The run script stops at the first non-zero step, so a build or staging
failure fails the sync rather than silently leaving the symlink in place.

The build and the copy are ONE step because they share ONE holder of the staging
lock (`.dist.staging.lock`, next to `static/dist`). `npm run build` empties
`website/dist` before repopulating it, so a peer flow — another sync, or the
dashboard's own update — that held the lock only for the copy could still read a
partially written tree. Inspecting the copy afterwards cannot substitute: a
bundle's lazy route chunks are referenced from inside the entry chunk, not from
`index.html`, so most of the tree is invisible to any index-based check. `npm ci`
stays a separate step since it does not touch `website/dist`.

Not covered: a gateway process that started while `static/dist` was still a
symlink to `website/dist` — the first staging sync, and equally any process
booted after something re-created the symlink (a `git clean` re-running
`ensure_dev_dist_symlink`). Such a process is pinned to `website/dist`, so its
dashboard still 404s while Vite rewrites it; pairing Pull+Build with Restart
Gateway is what closes it. A process that booted against a staged real
directory is unaffected.

## Make Live

`POST /apps/dev-fleet/api/make-live` repoints the live gateway at a different
worktree by writing a **live-target pointer file** (`live_target.json`). The
gateway resolves this pointer at startup and `execve`s into the named checkout's
own `kirocrew` binary — moving the working directory and `PATH` with it. No
service definition is ever mutated.

The mechanism is the version-selector shape used by `rustup` (reads
`rust-toolchain.toml`), the Go toolchain (`go` execs from the `toolchain` line
in `go.mod`), and `pyenv`/`rbenv` shims.

### Pointer file

Location: `config_dir() / "live_target.json"` (inside the active data home,
typically `~/.kiro/crew/live_target.json`). Contents:

```json
{"checkout": "/absolute/path/to/worktree"}
```

Written atomically (temp file + `os.replace`) with mode `0o600`. The file is
**keystone-fenced** (in `_CREW_SECRET_LEAVES`) so agent tools can neither read
nor write it — only the human-driven dashboard cutover action writes it, and
the gateway's startup reader (`live_target.maybe_reexec`) opens it directly
rather than through the gate.

### Live-worktree resolution

`_live_worktree_path()` checks `live_target.read_target()` FIRST (after the
TTL cache), before any launchd/systemd service-definition probe. A cutover
writes the pointer and never touches the service definition, so the unit's
`WorkingDirectory` still names the checkout the gateway was installed from.
Reading the definition first would report that stale checkout as live.

### Request / Response

Request body: `{path, dry_run?}` — `path` is a worktree path (validated against
the discovered set, never an arbitrary path); `dry_run` (bool, default false)
returns the plan without writing the pointer.

- **dry_run success:** `{ok: true, dry_run: true, plan: {mechanism, pointer_path,
  exec, restart, target, [manual_restart]}}`
- **cutover success (automatic restart):** `{ok: true, cutover: true, target,
  plan, start_id}`
- **cutover success (staged only):** `{ok: true, cutover: true, staged_only: true,
  target, plan, manual_restart, notice}` — the pointer is written and correct;
  the operator finishes the cutover by restarting the gateway themselves.
- **refusal:** `{ok: false, code, error}` — `code` is one of the values below.

The handler additionally returns HTTP 400 for a missing/non-string `path` or a
non-boolean `dry_run`.

The `plan` object describes the cutover mechanism:

| Key | Value |
|-----|-------|
| `mechanism` | `"live-target pointer"` |
| `pointer_path` | absolute path to the pointer file |
| `exec` | the target worktree's `kirocrew` binary that the gateway execs into |
| `restart` | `"automatic"` when a drivable service manager is present; `"manual"` otherwise |
| `manual_restart` | (only when `restart` is `"manual"`) the shell command the operator runs |

### Error codes

| Code | Meaning |
|------|---------|
| `unknown_path` | `path` is not a discovered worktree |
| `missing_path` | the worktree path no longer exists on disk |
| `pod` | called from inside a pod — a throwaway test instance must never repoint the live gateway |
| `pod_indeterminate` | pod status could not be resolved (config home unresolvable) — **fail-closed**, never treated as "not a pod" |
| `already_live` | the target is already the live gateway |
| `missing_venv` | the worktree has no `.venv/bin/kirocrew` (Provision it first) |
| `venv_not_executable` | the worktree's `.venv/bin/kirocrew` exists but is **not executable** (`chmod +x` it or re-Provision) — a non-executable binary would stop the live gateway but could not start the replacement, leaving no gateway running |
| `missing_dist` | the worktree has no built `src/kiro_crew/static/dist/index.html` (Pull+Build first) — a cutover without a built dist serves a broken dashboard |
| `unsafe_path` | the worktree path cannot be used as a live target (control characters, unresolvable, missing binary, no `src/kiro_crew` dir) |
| `write_failed` | writing the pointer file failed — rolled back to prior state |
| `restart_failed` | the detached restart failed to launch — the pointer is rolled back before returning (response carries `rolled_back`) |
| `busy` | another make-live cutover is already in progress — the mutation sequence is single-flighted, so a concurrent request is refused immediately (no queueing) rather than racing the in-flight pointer write/rollback |
| `restart_pending` | a cutover has already been **successfully scheduled** in this gateway process — the restart is still pending, so a process-local latch refuses every further request (cutover **and** `dry_run`) until the pending restart replaces the process. The fresh gateway starts with the latch clear |

On a `write_failed` / `restart_failed` refusal the response includes
`rolled_back: true|false` — whether the pre-cutover pointer state (prior
content, or absence) was successfully restored on disk.

### Two outcomes: automatic vs staged-only

The cutover writes the pointer on every platform. What differs is whether Dev
Fleet can also bounce the gateway:

- **Automatic restart** (`can_restart = True`): the gateway runs as an active
  systemd `--user` unit or a current macOS LaunchAgent that Dev Fleet can drive.
  After writing the pointer, Dev Fleet asks the manager to restart it (`systemd-run`
  on Linux, bounded graceful `launchctl stop` on macOS), sets the
  `_MAKE_LIVE_COMMITTED` latch, and returns `start_id` for the restart handshake.
  The next gateway process reads the pointer and execs into the target checkout.
- **Staged only** (`can_restart = False`): no drivable service manager is
  available (system unit via `kirocrew service install`, macOS without a launchd
  agent or with a legacy restart contract, terminal-launched gateway, or another
  unsupported manager). The pointer is still
  written and the cutover is reported as a success carrying `staged_only: true`,
  plus `manual_restart` (the shell command that finishes it) and a human-readable
  `notice`. The latch is deliberately NOT set — no restart is pending, so a
  subsequent cutover to yet another worktree stays allowed.

### Concurrency

The cutover mutation (prior-state snapshot → atomic pointer write → optional
restart → any rollback) runs under a single module-level `asyncio.Lock`. Two
concurrent cutovers would otherwise race on the shared pointer — one request's
failure rollback could restore or delete the other's successful write. A second
request that arrives while the lock is held is refused immediately with `busy`
(fail-fast, **not** queued). The `dry_run` validation path mutates nothing and
runs outside the lock.

**Committed latch.** The detached restart returns immediately while the restart
is still pending. A process-local `_MAKE_LIVE_COMMITTED` flag is set to `True`
— before returning success, inside the lock — the moment a restart is scheduled.
It is checked both at function entry and again after the lock is acquired
(closing the entry-check-vs-acquire race), so any further request is refused
with `restart_pending`. The latch is never persisted: the fresh gateway starts
clear. Failure paths before successful scheduling never set it, so a rolled-back
cutover leaves the process free to retry. In the `staged_only` path the latch is
never set because there is no pending restart to race against.

### Validation order

Every check runs for `dry_run` too, in this order (first failure wins):

`path` (exists as a known worktree) → **pod guard** (fail-closed on
indeterminate) → `already_live` → `missing_venv` → `venv_not_executable` →
`missing_dist` → `_make_live_plan` (runs `live_target.validate`, catching
`InvalidTarget` as `unsafe_path`).

The pod guard precedes the venv/dist checks so an operator inside a pod gets an
actionable refusal before any per-worktree state matters. The plan step validates
the target path the same way the real write does, so a dry run reports an
unusable worktree instead of promising a cutover that would then be refused.

### Pointer validation (`live_target.validate`)

Rejects with a distinct message for each: empty/blank value; control characters
(ord < 0x20 or 0x7F); unresolvable path; path is not a directory; missing
`target_bin` (`.venv/bin/kirocrew`, or `.venv/Scripts/kirocrew.exe` on Windows);
`target_bin` not executable; no `src/kiro_crew` directory in the checkout.
Returns the resolved checkout path on success.

### Rollback semantics

Before writing the pointer, the prior state is snapshotted via
`live_target.snapshot()` — the raw file content, or `None` when the file is
absent. An UNREADABLE (as opposed to absent) pointer aborts here: `restore(None)`
interprets `None` as "there was nothing" and deletes the file, so continuing
would let a failed restart destroy a live target the code merely could not read.

If the pointer write raises `InvalidTarget` the cutover is refused without
rollback (no state was changed). If it raises `OSError`, or if the detached
restart fails to launch, the pointer is restored to its prior state via
`live_target.restore(prior)` — rewriting the old content, or deleting the file
when there was none. The refusal response carries `rolled_back: true|false`.

### Platform scope

Staging (writing the pointer) works on every platform — Linux, macOS, and
Windows. Automatic restart requires a drivable manager: an active systemd
`--user` unit or an active macOS per-user LaunchAgent with the current restart
contract. Without one, the cutover succeeds as `staged_only` and the operator
restarts manually. Cutover from inside a pod is always refused (`pod` /
`pod_indeterminate`).

On macOS, Restart and automatic Make Live submit `launchctl stop <label>`.
Disk and loaded launchd definitions must both report `KeepAlive=true` and
`ExitTimeOut=TOTAL_SHUTDOWN_BUDGET_SECS` (20s). The Gateway's cooperative cap is
`GRACEFUL_SHUTDOWN_SECS` (10s), leaving the remaining budget for cleanup and
exit before launchd escalates to SIGKILL. An agent with a legacy contract falls
back to staged-only Make Live and names `kirocrew service install` as the repair.

## Output Redaction

All user-visible output passes through `redact_credentials()` and
`redact_exfiltration_urls()` before HTTP response serialization.

## Platform Behavior

The app declares `platform.os: ["macos", "linux", "windows"]` in `app.json`,
because that is where it genuinely runs: the fleet view, PR status, commit and
disk figures, Provision, Sync, Rebase and Prune are git and filesystem work with
no systemd in them. Only the pod plane needs Linux; Make Live stages its pointer
on every platform (only the automatic restart needs a drivable service manager).
The app says so in the UI rather than in the manifest — a `highlights` line
states the pod requirement, and `GET /api/fleet` carries the reason that renders
as a banner.

Declaring one platform per capability is not expressible here: `os` is a single
list describing the whole app, so any value is a summary. `["linux"]` was the
wrong summary — it read as "does not run on macOS" for an app whose non-pod half
runs there fine, which is the same misinformation in the opposite direction from
the pre-#1254 silence (an absent `platform` block defaults to
`["macos", "linux"]`, quietly advertising macOS parity).

The declaration is **not** an install gate for this app: `installMode` is the
default `"server"` and the App Store's platform check at `registry.py` only
refuses `installMode: "client"` apps, so dev-fleet installs and enables
everywhere regardless. What the list drives is the App Store detail page, which
renders it verbatim (`AppDetailPage.tsx` → "Platform: macos, linux, windows").

Two separate capability flags drive the degradation, because they gate different
things:

| Flag | Meaning | True when |
|---|---|---|
| `_POD_IMPORTED` | the `kiro_crew.pod` modules imported, so its platform-neutral helpers are callable | the import succeeded (any platform) |
| `_POD_AVAILABLE` | pods can actually **run** here | Linux **and** `systemctl` on PATH |

Conflating the two used to report every worktree as "not built" off Linux, since
the `prov.has_venv` / `prov.has_dist` calls — plain filesystem checks — sat
behind the pod-runnable gate. Build state is now computed on every platform.

`GET /api/fleet` reports host support so the UI can explain itself rather than
offering controls that fail:

| Field | Meaning |
|---|---|
| `pods_available` | `_POD_AVAILABLE` — whether pods can run on this host |
| `pods_unavailable_reason` | the human-readable reason, or `null` when pods are available |

Before this existed, the reason string was computed into `_POD_ERROR` and then
**never read by anything** — a non-Linux user saw pod controls that silently
failed with no explanation.

Per-platform behavior:

- **Linux + systemd `--user`** — everything works.
- **macOS / Windows / Linux without `systemctl`** — the Fleet view, per-branch PR
  status, commit counts, disk usage, Provision, Sync (pull main + rebuild),
  Rebase and Prune all work. The UI shows a notice carrying
  `pods_unavailable_reason` and hides the actions that cannot work: Spin up /
  Restart / Stop pod, Open, QA + video. Make Live and Provision are **not**
  hidden — `kirocrew pod provision` does not touch systemd, so building a
  worktree's venv + dist works anywhere; Make Live stages the pointer on any
  platform and reports `staged_only` when it cannot bounce the gateway itself.
- **Make Live** — staging (pointer write) works on every platform. Automatic
  restart requires an active systemd `--user` unit or a current macOS
  LaunchAgent; without one the cutover succeeds with `staged_only: true` and
  the operator restarts manually.
- **git** and **gh** CLI required for full functionality; missing binaries produce
  graceful degradation via OSError catch in `_run_cmd`.

## Bundled Skills

The app bundles two skills declared in `app.json`:

- `skills/pod-e2e` — end-to-end test harness for isolated pod instances.
  Every phase is time-bounded: the Playwright phase runs under `timeout`
  (`POD_E2E_PW_TIMEOUT`, default 600s) and each browser-teardown step under
  `POD_E2E_TEARDOWN_TIMEOUT` (default 30s), because video finalization
  (`context.close()`) can block indefinitely. On expiry the runner keeps the
  artifacts, kills the browser descendants, and reports a timeout as a distinct
  outcome. Per-phase results are appended to `verdict.jsonl` as they are decided
  so a killed run still yields a verdict.
- `skills/feature-demo-recording` — headless Playwright video recording

`kirocrew-worktree-dev` carries no app-bridged copy: the canonical copy is
owned by the `kirocrew-dev` development-skills suite under
`src/kiro_crew/builtin_skills/`, and the app-bridged duplicate was removed
because two copies of the same skill drift and get loaded nondeterministically
against each other (PR #353 arbiter finding). That single-copy rule is what
matters here; where the one copy lives is a packaging question, and it lives in
the packaged tree so `_ensure_builtin_skills` reaches every distribution. The
project-dir mechanism reaches only some: `_project_skills_dir()` reads
`KIROCREW_PROJECT_DIR`, which a repo checkout and the desktop bundle both
provide (`packaging/kirocrew-backend.spec` ships the top-level `skills/` tree
and `website/electron/main.js` sets the variable), but a `pip install` from the
wheel or sdist provides neither.

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
