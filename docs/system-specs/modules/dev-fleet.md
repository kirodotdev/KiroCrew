# Dev Fleet Module

## Overview

Dev Fleet is a builtin App Store app (`kiro_crew/apps/builtins/dev_fleet/`) for
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

## Backend component boundaries

`server.py` is the composition facade: it registers the unchanged HTTP routes,
coordinates startup and shutdown, and owns the process entry point. The implementation
behind that facade is split by state and lifecycle ownership:

- `runtime.py` owns command security, toolchain resolution, run descriptors, active
  subprocesses, and shutdown admission.
- `repository.py` owns `MAIN_REPO`, repository discovery and validation, git/worktree
  access, and dirty-state inspection.
- `live.py` owns live-target discovery, gateway restart backends, the Make Live lock and
  committed-cutover latch, and rollback.
- `fleet_state.py` owns PR/context/resource caches, fleet projections and tombstones, and
  provision reattachment state.
- `worktree_ops.py` owns pod actions, worktree remove/sync/rebase/prune orchestration, and
  the background task handles.
- `http_api.py` owns proxy HMAC verification, request/audit adapters, and response-shape
  translation.

Dependencies run in that direction from HTTP adapters toward the lower-level owners;
lower-level components do not call back through `server.py`. Cross-component calls use
the owning module, so mutable locks, registries, caches, and scalar state each have one
authoritative instance. The facade forwards legacy private attribute reads, writes, and
deletes to that owner, but tests patch the actual owner rather than treating the facade as
a dependency-injection namespace.

The split does not change lifecycle ordering. Shutdown first closes admission and
snapshots active runs under the runtime admission lock, then kills process trees before
cancelling their workers, and only then cancels the idle refresher/reaper/prune tasks.
Destructive worktree operations retain the lock order `_wt_lock(name)` ->
`_MAKE_LIVE_LOCK` -> `_GIT_MUTATION_LOCK`; changing either ordering can strand a build or
deadlock removal against a live cutover.

## Main Checkout Discovery

Every git operation is rooted at `MAIN_REPO`, the primary checkout whose worktrees the
fleet manages. It is resolved in this order, first hit wins:

| Tier | Source | Marker-tested? |
|------|--------|----------------|
| 1 | `KIROCREW_DEVFLEET_REPO` env var | no — taken verbatim |
| 2 | `dev_fleet.repo_path` in `config.json` / `config.local.json` | no — taken verbatim |
| 3 | `KIROCREW_PROJECT_DIR` | yes |
| 4 | the checkout this gateway is executing from (`src/kiro_crew` layout walk) | yes |
| 5 | conventional clone locations under `$HOME` (`kirocrew`, `KiroCrew`, `kiro-crew` directly and under `Repos`, `repos`, `src`, `Projects`, `projects`, `dev`, `git`, `code`, `workplace`) | yes |

Tier 5 matches directory names case-insensitively against each parent's own listing rather than joining the guessed spellings, so the resolved path is spelled the way the filesystem spells it. A blind join succeeds against a differently-cased directory on a case-insensitive filesystem (macOS) and yields a path that does not match the ones git reports for the same tree.

The marker test (`_is_kirocrew_checkout`) requires `.git`, `src/kiro_crew/` and
`pyproject.toml` together. `.git` alone is insufficient on purpose: an unrelated
repository adopted as the main checkout would have its worktrees listed and Pull+Build,
rebase and worktree-removal git commands run inside it. Tiers 1–2 skip the test *during
discovery* because the user named that path — a typo must surface as an error against it
rather than be silently replaced by a discovered checkout — but the path is still validated
once at startup, and `_repo()` — the single accessor every git argv and path build goes
through — then raises `RepoUnreadable` naming it. The gate lives in the accessor rather than
in worktree discovery because sync and the background refresher reach git without passing
through discovery, and `pull --ff-only` plus `pip install -e` inside an unrelated repository
is the worst available outcome. "Not replaced by a discovered checkout" and "not validated" are separable, and
only the first is wanted: a readable-but-wrong configured path would otherwise be operated
on rather than reported.

Module import evaluates tiers 1, 3 and 4 — two env reads and a handful of stats — because
the module is imported from the async route-registration path. Tier 2 (a config-file read)
and tier 5 (up to 30 candidate directories x 3 markers) run only on the subprocess executor
in `dev_fleet_startup()`. The startup result is then normalized through
`_resolve_primary_checkout`, so a hint naming a linked worktree still manages the whole
fleet.

The `/fleet` payload reports both the resolved `main_repo` and
`main_repo_inferred`. The latter is true for tiers 3–5 and false for the two
operator-configured tiers. The page surfaces an inferred path once above the fleet,
so the checkout targeted by Pull+Build, rebase, and prune is visible without adding
noise for operators who configured it explicitly.

When no tier resolves, `MAIN_REPO` is `""` — never a synthesized path. Discovery raises
`RepoNotConfigured` and `/fleet` answers `{"worktrees": [], "needs_setup": true}` with no
`error` field, which the page renders as a setup prompt. A synthesized default instead
produces a red "Discovery Error" naming a directory the user never chose, which reads as a
broken app rather than an unanswered question.

Because `""` would make `git -C ""` operate on the backend's own working directory (and
`Path("")` is `Path(".")`), no consumer reads the global directly: every site that runs git
against the checkout or builds paths from it resolves it through the `_repo()` accessor,
which returns the path or raises `RepoNotConfigured`. Sites that deliberately degrade
instead of failing catch it and say what the degraded answer is — upstream-remote
resolution falls back to `origin`, build-pending detection reports nothing pending,
fallback-remote loading leaves the list empty, sync refuses with its usual
`{"ok": false}` shape, and the background refresher idles. Bare `MAIN_REPO` loads outside
the accessor are limited to truthiness guards. An AST ratchet scans every Dev Fleet backend
component (`test/test_dev_fleet_repo_accessor.py`) and permits the authoritative load only
inside `repository._repo()`; helpers in every sibling module must route through that
accessor.

Every OTHER route that resolves a worktree (`/worktree`, `/disk`, `/prune-candidates`,
`/prune-run`, the pod routes, `/rebase`, `/make-live`) reaches `_discover_worktrees` too, so
both unresolved states are converted once in `hmac_proxy_middleware` into a `409`:
`RepoNotConfigured` → `{"ok": false, "code": "repo_not_configured"}`, and `RepoUnreadable`
(a checkout was named but git cannot enumerate it) → `{"ok": false, "code":
"repo_unreadable"}`. The boundary lives in the middleware rather than per handler so a newly
added route cannot forget the case and answer a click with an uncaught 500. The page
suppresses the fleet toolbar, the row-action how-to, and the stat-card counts in BOTH states
— the fleet is unknown either way, so a count would assert a number nobody measured — which
means those routes are not offered in the first place; the 409 is the backstop for a direct
API caller.

`/fleet` is the one route that distinguishes them: `needs_setup` for the unconfigured state,
an `error` string for the unreadable one, which the page renders as the Discovery Error
banner naming the path (the user chose it).

When a checkout WAS named and git cannot read it, the error names the mechanism that
supplied the path (`_repo_source_hint`) — the remedy is to edit that one, and listing both
leaves the user guessing which they set.

## Routes

Public routes are under `/apps/dev-fleet/api/*` (gateway proxy, session auth via token
query param or cookie); the backend subprocess serves them as `/api/*` after HMAC
verification. Route names below are relative to that prefix.

### Read (GET)

| Route | Description |
|-------|-------------|
| `/apps/dev-fleet/api/health` | Liveness + gateway **start identity**: `{status, start_id}`. `start_id` is the live unit's `ExecMainStartTimestampMonotonic` (launchd: job PID; foreground last resort: run-marker pid; `null` when unavailable); the dashboard polls it to detect the NEW process after a restart (see Action narration). Served on the proxied `/api/` namespace because the gateway only forwards `/apps/dev-fleet/api/*` to the backend. (The bare `/health` carries the same body but is HMAC-exempt and reached only by the gateway's own internal liveness poll.) |
| `/apps/dev-fleet/api/fleet` | Lightweight worktree + pod list (polled every 12s), including `main_repo` and `main_repo_inferred`. `?fresh=1` forces cache bypass. Answers `{worktrees: [], needs_setup: true}` when no main checkout was found (see Main Checkout Discovery) and `{worktrees: [], error}` when a named checkout is unreadable. |
| `/apps/dev-fleet/api/worktree?name=` | Lazy per-branch detail: PR, commits, disk usage |
| `/apps/dev-fleet/api/pod/logs?name=&n=` | Pod journal tail (recent N lines, default 120) |
| `/apps/dev-fleet/api/run?id=` | Async run status + streamed output (last 60 lines), plus `cause` on a sync failure the gateway can name |
| `/apps/dev-fleet/api/prune-candidates` | List worktrees eligible for pruning |
| `/apps/dev-fleet/api/prune-status` | Live prune progress: per-item state machine (`items`) + backward-compatible top-level counters |
| `/apps/dev-fleet/api/disk` | Aggregate disk usage per worktree (async computation) |

### Write (POST)

| Route | Body | Description |
|-------|------|-------------|
| `/apps/dev-fleet/api/sync` | — | Pull main + rebuild (single-flight; a concurrent call is refused **409**) |
| `/apps/dev-fleet/api/worktree/remove` | `{name, force?}` | Remove a worktree (stops its pod and reclaims that pod's isolated HOME first) |
| `/apps/dev-fleet/api/prune-run` | `{names[]}` | Batch-remove eligible worktrees |
| `/apps/dev-fleet/api/pod/up` | `{name}` | Start isolated pod instance (re-verifies the unit is active) |
| `/apps/dev-fleet/api/pod/down` | `{name}` | Stop pod instance (re-verifies the unit is gone before reporting success) |
| `/apps/dev-fleet/api/pod/restart` | `{name}` | Stop then start pod |
| `/apps/dev-fleet/api/pod/token` | `{name}` | Mint a dashboard token for the pod |
| `/apps/dev-fleet/api/pod/provision` | `{name}` | Start async venv+dist build (returns `{run_id}`) |
| `/apps/dev-fleet/api/pod/provision/dismiss` | `{name, run_id}` | Forget a terminal provision failure when the run id still matches |
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

- **Expensive per-item phases are concurrency-bounded.** The fresh `_prunable`
  re-verdict (which makes `gh`/`git` network calls) runs under an
  `asyncio.Semaphore(4)`, so a batch is bounded by the slowest ~4 items at a time
  instead of the sum of all of them. Pod shutdown remains inside the make-live
  exclusion window because removal must continuously protect the target from the
  final live/staged re-check through deletion.
- **Git mutations are serialized.** The `git worktree remove` + branch `update-ref -d` for
  every removal — including the single-worktree remove handler and the auto-prune reaper —
  run behind one shared `asyncio.Lock` (`_GIT_MUTATION_LOCK`), because they mutate the
  shared main-repo `.git` state (worktree admin dir + `packed-refs`). Concurrent git
  mutations would otherwise race on those lock files.
- **Lock order: `_wt_lock(name)` → `_MAKE_LIVE_LOCK` → `_GIT_MUTATION_LOCK`.**
  This order must never be reversed. Every removal first acquires the worktree lock,
  then acquires the make-live lock before the live/staged protection re-check and holds
  it through deletion. A concurrent rebase cannot claim the checkout after removal's
  initial fail-fast check, and a concurrent `/make-live` cannot stage the target between
  the protected re-check and `git worktree remove`. Forced prune delegates to the same
  internal removal path rather than pre-acquiring either lock.
- **Rebase gate.** `_worktree_remove` refuses immediately if `_wt_lock(name)` is already
  held, then acquires and holds that lock through deletion. The unlocked check and
  acquisition are adjacent with no intervening await, so acquiring a free `asyncio.Lock`
  does not yield an interleaving point. Rebase holds the same lock across fetch, rebase,
  and abort; deletion can therefore neither begin during a rebase nor race one that starts
  after the initial check.

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

**Scan feedback:** the preview that opens that dialog (`prune-candidates`) runs `git` —
and for merged-verdict candidates a `gh` lookup — per worktree, so on a large fleet the
click is followed by seconds of silence before the dialog can appear. The Prune merged
button therefore swaps its trash glyph for a spinner and sets `aria-busy` for the
duration: disabling alone is indistinguishable from a wedged page, and a user who reads
it as hung clicks again or reloads mid-scan.

## Pod Integration

Relies on `kiro_crew.pod` subpackage (optional import — degrades gracefully if unavailable):

- `runtime.active_names(cfg)` — systemctl list (blocking, offloaded via `run_in_executor`)
- `runtime.derive_port(cfg, name)` — cksum-based port derivation (blocking, offloaded)
- `runtime.health(cfg, name, port, timeout)` — identity-gated HTTP probe (blocking,
  offloaded). Takes the pod's NAME, not just its port, because a derived port is
  routinely held by another pod or by the live gateway: `port_owner` requires the
  process a `127.0.0.1` connect reaches to be this pod's own `MainPID`, and a
  responder that is provably somebody else's returns `HEALTH_FOREIGN` (`-2`)
  instead of its HTTP status. The fleet row treats that as unhealthy, since the
  frontend's `health >= 200` test already excludes a negative value. There is
  deliberately no bare-port variant to call — see `instances/run_marker`, which
  states the rule ("no caller can mistake reachability for identity")
- `runtime.mint_token(cfg, name, ttl)` — credential minting (blocking, offloaded).
  Requires POSITIVE ownership proof and refuses when ownership is merely
  unprovable, unlike `health`, which keeps its reading: this call sends the pod's
  own `.local_secret`, so failing open would hand a credential to whatever
  answered
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

### Pod HOME reclamation on worktree removal

Removing a worktree reclaims the isolated `KIROCREW_HOME` of that worktree's pod
whether or not the pod is still running, because a stopped pod still owns its
HOME and this is the last moment anything can attribute that directory to this
checkout — afterwards the per-pod env pin naming it is gone and only a bulk
`pod prune` could find it. Reclaiming only a LIVE unit would therefore reclaim
nothing on the ordinary path: the operator stops the pod when testing ends and
prunes days later once the PR merges, so the unit is inactive by then and every
removal stranded a full isolated HOME (a per-instance embedding-model copy
dominates its size).

Which directories qualify is decided by `runtime.orphan_homes`, the same
predicate `pod ls` and `pod prune` use, rather than a bare directory probe — so
symlinks are skipped and, on macOS, a name whose per-pod plist exists counts as
*installed* rather than orphaned and is never reclaimed from underneath a
concurrent `up`. That predicate keys on the pod root, liveness and plist and
never on the checkout pin, so attribution is not its job.

Attribution and teardown are ONE locked transaction. `_reclaim_pod_locked` runs
entirely inside `runtime.pod_name_mutex` — the cross-process flock every mutating
pod path cooperates on — and reads the checkout pin, decides ownership, calls
`runtime.stop_pod`, and clears the per-pod env file without ever releasing it.
Splitting those halves is what the lock exists to prevent: pod identities are
global basenames, so between an ownership check in one process and a teardown in
another, a concurrent `pod up` from a DIFFERENT checkout can claim the same name
and the teardown would stop that pod and delete its isolated HOME. Both call
sites in `_worktree_remove` — the live-unit path and the orphaned-HOME path — go
through this one helper, so neither carries that window.

That is also why the reclaim is in-process rather than a `pod down` shell-out:
the mutex is held per open-file-description and `stop_pod` re-acquires it, so a
caller holding it around a subprocess would block the child it waits on. The
mutex is reentrant *within a thread* and the helper is submitted to the executor
as a single callable, so `stop_pod`'s own acquisition nests instead of
deadlocking. The helper mirrors `_pod_checkout_guard`'s attribution rules with one deliberate
tightening: an ABSENT pin is a refusal here. The guard allows an unpinned name
when no unit is live, which is right for operating on a pod the caller located,
but this path DELETES the HOME and a same-basename leftover from another checkout
is indistinguishable from here, so deletion demands positive attribution. The
cost is that an unpinned orphan is not reclaimed automatically — `pod prune`
still takes it — which is the cheaper side of the trade. It also mirrors the
CLI's post-teardown env-file clear, and leaves that file alone when `stop_pod`
reports the name was handed to a new pod mid-teardown (it now pins the new pod's
checkout).

`handed_over` is a REFUSAL at both call sites, not a success: a new pod holds the
name, which checkout it belongs to is unknowable here, and it may be running out
of the very worktree about to be deleted. The post-stop liveness recheck is not a
substitute, since it can miss a unit that is still bootstrapping.

The two fail directions are scoped separately on the orphan path. The
ENUMERATION is best-effort cleanup — an orphan scan says nothing about liveness,
so its failure degrades to a named leftover rather than turning a lost directory
into a lost removal. The RECLAIM is teardown and fails CLOSED: a returned failure
refuses the removal, and a RAISED one is deliberately not caught there either,
because a teardown that died mid-flight (a stop that timed out against a
still-activating unit) is exactly the state in which removing the checkout is
unsafe.

The result reports the two outcomes separately: `stopped_pod` for a unit that was
running, `reclaimed_pod_home` for a HOME reclaimed with nothing running.

Two failure directions are deliberately different. A **liveness** check that
cannot run fails CLOSED and refuses the removal, because it guards against
deleting a checkout out from under a running pod. A **reclamation** step that
cannot run degrades: the orphan scan says nothing about liveness, so an
enumeration error logs the leftover (pointing at `pod prune`) and the removal
proceeds, rather than turning a lost directory into a lost removal. When the pod
backend is provably absent the HOME is left in place on purpose — liveness is
then unprovable and deleting a HOME that may belong to a live gateway is the one
outcome teardown must never risk — but the path is logged at WARNING with the
`pod down` verb that reclaims it, so the residue is visible instead of silent.

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

### Pod Unit Self-Heal

The unit template is written once by `pod install`, so a machine keeps whatever it
installed. On `pod up`, the pod CLI re-renders it when the installed unit is one this
build will not boot:

1. Detects a stale unit via `unit.unit_is_current(cfg)`, which fails on either of two
   triggers:
   - the baked `ExecStart` binary no longer exists — `unit.unit_exec_ok(cfg)` reads the
     unit file and checks `os.access(exe, os.X_OK)` on the baked path (typically the
     worktree it resolved into was pruned)
   - the unit carries a directive this build has removed (`unit._REMOVED_DIRECTIVES`,
     currently `ExecStopPost=` — see the pod module's teardown section)
2. Re-renders the unit with a currently-valid binary (`unit.install_unit(cfg)`)
3. Runs `daemon-reload`
4. Audits the self-heal event
5. Proceeds to start the pod normally

The first trigger prevents the permanent EXEC 203 failure loop that occurs when
worktrees are pruned after the unit was installed. The second is the upgrade path: a
unit installed by an older build would otherwise keep a teardown hook that races the
pod's own subprocesses and wipes the HOME on the stop half of a `Restart=`, and it
would keep doing so until someone reinstalled by hand.

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

**Reattach after a page reload (server-backed).** Each `/fleet` worktree entry
carries a `provision_run_id` while that checkout's provision run is still
executing or after it finished unsuccessfully (mirroring `sync_run_id`).
Successful and registry-evicted runs are omitted — there is nothing to
reattach to. On mount the page fetches `/run?id=<rid>` for each exposed id: a
running run resumes polling into the stepper (accumulating the log window as
usual), and a failed run restores the persisted red failure state with its log
auto-expanded. Reattached and locally-started runs are deduped by run id, so a
fleet refetch never starts a second poll loop for a run already being tracked.
The dismiss `×` posts the worktree name and run id to the server before clearing
the local strip. The server removes the persisted id only when it still matches
that terminal run; a stale dismiss cannot clear a newer provision, and a running
provision cannot be dismissed. A successful response therefore survives reload.

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
  when the replacement main process starts. On a host with NO drivable manager
  (the foreground last resort below) the identity is the pid the gateway records
  in its `run/gateway-<port>.pid` sidecar — written before readiness is
  published, rewritten by the replacement, and consumed by the same
  changed-identity comparison unchanged.
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
npm ci → npm build + stage. That refusal is a **state to act on, not a failure
to report**: the body names the run already in flight, and the client attaches
its progress stepper to that run. A second press is a user who cannot see the
sync, so reporting an error would leave them exactly where they started —
without progress, and with the button still inviting a third press. Because the
API client throws on any non-2xx, this path is reachable only through the
error branch, and the `run_id` reaches it via the parsed body carried on the
thrown error, never as a returned body.

The run script emits a
`::step::<idx>::<label>` marker per
step; the run worker records BOTH the authoritative step index and its **label**
onto the run entry (`step` / `step_label`), so `/run` can name the CURRENT step
even after the marker scrolls out of the 60-line output tail window. The
frontend shows that label beside the "Syncing" spinner. This reuses the
existing `_RUNS` / `::step::` / `/run` run-tracking mechanism — the same channel
the provision log panel uses (#320) — rather than adding a second one.

`sync_run_id` — the pointer a freshly-mounted page reattaches that stepper to —
and each row's `provision_run_id` are read at **request time** and overlaid onto
the fleet payload, not taken from the cached snapshot. `_FLEET_CACHE` is
stale-while-revalidate, so a pointer baked into the snapshot made a run started
after that build invisible for a full cache cycle plus a rebuild, which is the
same "no progress, press it again" trap from the other end. Both are in-memory
reads (a module global; a dict copy plus `_RUNS` lookups), so paying for them per
request is cheap. `_build_fleet` deliberately does **not** write them: one owner,
so no reader of `_FLEET_CACHE` can pick up a frozen id. The overlay is
authoritative rather than a fill-in — a provision that finished after the
snapshot has no reattachable run, so its pointer must read null instead of the id
a build-time write would have frozen. It copies the snapshot and its rows rather
than writing through them — the cached objects are shared with every other
in-flight request.

Note the two refusal conventions this leaves in place: sync refuses with 409 and
a thrown body, provision refuses with 200 and `ok: false`. Only sync's needs a
caller-side normalizer, because only a thrown error bypasses the returned-body
branch. Unifying them is a separate change; nothing here adds a second
normalizer.

Sync progress is reported as **indeterminate** — a spinner, the current step
label and elapsed time — and never as a percentage. The step index is a poor
basis for one: the five steps differ in duration by more than an order of
magnitude and shift with network and cache state, so a step-derived bar sits in
one band for most of the run and then jumps, which reads as a stall. The
spinner's `role="progressbar"` carries no `aria-valuenow`, which is the ARIA
form for "in progress, amount unknown".

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

### Dependency preflight and the `node_modules` transaction

`npm ci` deletes `node_modules` before it installs, so a registry refusal used to
leave the checkout with an emptied tree, a stale bundle against new backend code,
and no way back that did not need the registry that was unavailable. Two
independent triggers recur: a private-registry token that expires on a clock, and
a curated mirror that blocks a version the lockfile pins.

**The symptom is handled as a transaction.** The `npm ci` step carries `stash`
metadata; the generated runner moves the tree aside before the step, restores it
on any non-zero outcome, and drops the backup on success. This lives in the runner
because the runner is fail-fast — anything scheduled *after* a failed step never
runs, which is precisely the case that needs the restore. When a tree **and** a
leftover backup are both present the state is genuinely ambiguous (killed during
the install leaves a partial tree plus the good backup; killed during the success
cleanup leaves the good tree plus a half-deleted one, and nothing on disk tells
them apart), so the runner stops and touches neither, naming both paths.

**The cause is handled by a `Verify dependencies` step between `fetch` and
`merge`.** It runs a real script-free `npm ci` in a scratch directory against the
incoming lockfile, read from the fetched ref rather than the working tree. The
position is the mechanism: the lockfile is knowable as soon as fetch lands, fetch
moves only refs, so refusing there costs nothing and needs no rollback. It is not
an auth check — retrieval is integrity-addressed, so an auth probe fails while the
install it guards would have succeeded.

Fetch, probe and merge consume ONE commit. `<remote>/<base branch>` cannot serve
for that: it is mutable, and the status refresher re-fetches it every
`_NET_REFRESH_S` seconds in the same process, so with a real install between them
the probe could certify a revision the merge does not install. The fetch step also
writes the tip it brought to a per-process ref (`refs/kirocrew/sync-base-<pid>`),
which the refresher never touches; `_prune_dead_sync_base_refs` collects refs left
by gateway processes that are gone, and leaves alone any whose PID is still alive.

The probe executes a **snapshot** of `npm_preflight.py` copied into an unguessable
`mkdtemp`, run with `-I`, never imported from the checkout. The module is
stdlib-only, so the copy needs no package context. Both halves matter: `-I` drops
the cwd from `sys.path`, and the snapshot means an editable install cannot make
the tree being synced supply the code doing the verifying.

**The generated runner itself carries `-I` too, and for the same reason.** `python
-c` puts the inherited cwd at `sys.path[0]`, ahead of the standard library, and
the cwd a module-style app backend hands down is the gateway's own source root —
which on the editable install Dev Fleet exists to manage *is* `<checkout>/src`,
the tree being synced. So the runner's own startup imports (`os`, `shutil`,
`subprocess`, `json`) resolve against that directory first, and the runner is the
one process here that is **not** sandbox-wrapped: only the step argvs go through
`sandboxed_spawn_argv`. Without `-I`, a `shutil.py` dropped in that directory runs
arbitrary code outside the per-step sandbox. The shadowing module only has to be
on disk when the runner *starts* — a revision an earlier sync already landed, or
anything an agent wrote in the checkout between syncs, is enough — so this is not
a race with the run's own merge. It is set on the interpreter rather than scrubbed
inside the script so it holds for the process's whole life, including any import
the runner grows later after a step has merged untrusted content. It costs
nothing: the script is stdlib-only by design, and the `-E`/`-s` that `-I` implies
remove env and user-site import sources it never uses. A mask from the sandbox
could not substitute — an app backend's `extra_hidden_dirs` is silently dropped by
`wrap_argv`'s nested passthrough (pinned in `test_sandbox_argv.py`), so it would
read as a control at the call site and enforce nothing.

**The install is skipped when the answer is already on disk.** Most syncs are
backend-only and change nothing under `website/`, so paying a full scratch install
to re-derive "is this lockfile installable" on every Pull + Build is cost without
information. `_install_already_proven` skips it, and only when BOTH hold: `git
diff --name-only <ref> -- website` is empty, meaning the incoming ref changes no
path under the frontend half at all, AND `website/node_modules` is populated (not
merely present — an interrupted `npm ci` leaves an empty directory, which proves
nothing). Without a tree there is no evidence, so a fresh checkout's first sync
still probes. Anything the comparison cannot answer — a failing or missing `git`,
a timeout — probes as well: the unknown case costs an install rather than a
guarantee.

A populated tree is evidence, not a verified install, and the bound is worth
stating: a prior frontend sync whose post-merge `npm ci` died partway can leave a
partial tree beside the merged lockfile, and later backend-only syncs will skip on
it, since from there on the subtree is unchanged and nothing re-examines it. The
consequence is the same class as the dead-registry residual — the skip decides only
whether this sync pays for a rehearsal, so a refusal lands one step later rather
than never, and the transaction keeps the checkout consistent either way. Issue
[#7132](https://github.com/kirodotdev/KiroCrew/issues/7132) tracks the stronger
evidence check that would close it.

The condition is the whole subtree rather than just `package-lock.json` /
`package.json` / `.npmrc`, and the difference is load-bearing. With those three
identical but frontend SOURCE changed, a skipped probe lets the merge land, and a
failing `npm ci` afterwards leaves the checkout with new source and the
previously-built bundle — the stale-bundle half of the very defect this section
exists to prevent. Requiring the entire subtree to be unchanged makes that
unreachable: with no frontend change there is no new bundle owed, so a failed sync
leaves the frontend byte-for-byte as it was.

What makes the skip safe rather than merely cheap is where a failure lands. Under
this condition the transaction above restores the tree on any non-zero step, the
lockfile it matches did not change, and neither did the source the bundle was
built from. A skipped probe can only leave a state a later `npm ci` fixes, never
one no revision produced. A skip is reported on the run's `preflight:` detail line
rather than the generic pass line, so it is visible in the log instead of
inferable from a missing pause.

**Failure causes reach the dashboard as an exit code, not as text.** The probe
exits with a reserved code (41-45) and the runner owns two more (46 ambiguous
tree, 47 restore failed); `npm_preflight.explain_exit` maps each to one
registry-neutral sentence at run completion, surfaced as `cause` on `/run` and
preferred by the UI over the last output line. Two properties keep it honest: a
reserved code arriving from any step OTHER than the probe is demoted to a plain
failure, because every other step runs worktree-controlled code that can exit any
number it likes; and only the sync run kind is stamped at all, since `_start_run`
is shared with `provision`, whose script enforces no such reservation.

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

### Three outcomes: automatic, foreground last resort, staged-only

The cutover writes the pointer on every platform. What differs is whether Dev
Fleet can also bounce the gateway:

- **Automatic restart** (`can_restart = True`): the gateway runs as an active
  systemd `--user` unit or a current macOS LaunchAgent that Dev Fleet can drive.
  After writing the pointer, Dev Fleet asks the manager to restart it (`systemd-run`
  on Linux, bounded graceful `launchctl stop` on macOS), sets the
  `_MAKE_LIVE_COMMITTED` latch, and returns `start_id` for the restart handshake.
  The next gateway process reads the pointer and execs into the target checkout.
- **Foreground last resort**: when the manager probe reports one of
  `no_systemd` / `no_user_unit` / `no_launchd` / `no_agent` — nothing to drive at
  all, e.g. a terminal-launched gateway on a host whose per-user systemd cannot
  be used — `gateway_service.ForegroundBackend` finishes the cutover by
  establishing a **detached `kirocrew restart --port <port>`** (new session, so
  it survives the gateway it kills), reusing the CLI's whole kill-and-respawn
  path instead of reimplementing it. Selection is strictly
  systemd > launchd > foreground, POSIX-only, and requires: an UNCONFINED
  backend (no `KIROCREW_SANDBOX_ACTIVE` marker, not inside
  `kirocrew-agents.slice` — a replacement spawned from inside the sandbox or
  cgroup scope would inherit that confinement for the gateway's whole life);
  exactly ONE run-marker whose recorded pid is alive; and the marker's own
  recorded `kirocrew` launcher (keystone-fenced `run/` dir — there is
  deliberately NO `PATH` fallback, which an agent-planted `~/.local/bin/kirocrew`
  could poison). The mis-set-up manager codes (`user_unit_inactive`,
  `agent_not_indirected`, `agent_restart_contract_outdated`,
  `live_program_missing`) keep their named remedies and are never bounced
  behind the manager's back. On success the response looks exactly like an
  automatic cutover (`start_id` = pre-restart marker pid, latch set). **Fail
  safe:** the backend never signals any process itself — if any requirement
  above fails or the detached spawn cannot be established, nothing has been
  killed and the request degrades to the staged-only outcome below, pointer
  intact.
- **Staged only** (`can_restart = False`, no usable foreground gateway): no
  drivable service manager is available (system unit via `kirocrew service
  install`, macOS without a launchd agent or with a legacy restart contract, or
  another unsupported manager). The pointer is still
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

## Git environment hardening

Every git invocation from this module — foreground inspection, the unattended
background fetch, rebase, the sync pull, and any git a build step runs — carries
`runtime._GIT_ENV_NEUTRALIZERS`, injected as **environment** rather than as
per-call-site `-c` flags so one chokepoint covers call sites that have not been
written yet. The environment form has the same precedence as `git -c`, so it
overrides every config file, including an agent-writable repo-local one.

Two different jobs live in that one dict, and they are worth keeping apart:

- **Config-driven execution.** `GIT_ALLOW_PROTOCOL` / `GIT_PROTOCOL_FROM_USER`
  make git itself refuse `ext::` and custom remote helpers; the four
  `GIT_CONFIG_KEY_*` / `VALUE_*` pairs disable `core.fsmonitor` and
  `core.hooksPath`, reset `credential.helper` to empty, and pin `core.sshCommand`
  to plain `ssh`. Each of those is a config key a repo can set to name a program
  git will spawn. (The operator's own *global* credential helpers are re-pinned
  after the reset — see `_GIT_TRUSTED_HELPERS` — because a global config is
  operator-owned rather than part of the repo attack surface.)
- **Which object graph git answers from.** `GIT_NO_REPLACE_OBJECTS=1` is not a
  config key and is not about code execution. A `refs/replace/<oid>` ref
  substitutes one object for another in *every* read, so `log`,
  `rev-list --count`, `merge-base` and `merge --ff-only` all answer about the
  substitute graph — a history no checked-out commit names. Every git answer this
  module acts on is a statement about the checkout on disk, so the real graph is
  the only one that answers the question asked, and "behind by N commits" derived
  from a grafted walk is simply wrong. `git replace` is a legitimate local
  operation, so this is a correctness pin first and a tamper pin second. It is
  therefore an env var in its own right and **not** one of the counted config
  pairs: `GIT_CONFIG_COUNT` stays at 4. `platform/update_governance.py` and
  `auto_improvement`'s clone setup pin it for the same reason.

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
- `skills/feature-demo-recording` — records a demo of a web feature, in one of
  two modes (see below).

### Using `feature-demo-recording`

Two modes, and picking the wrong one wastes a recording:

- **Narrated film** — someone sits and watches it (a launch clip, a feature
  intro). The VOICEOVER drives the timeline: narration is synthesised and
  measured FIRST, and the recorder then paces the browser to those measured
  times. This is the order that keeps sound and picture together; pacing the
  recording first and fitting audio afterwards is what accumulates drift.
- **Silent evidence clip** — proof that a feature works, for a PR or a review.
  No narration, no measuring; `narrate.py --silent` writes a timeline from
  durations you state. The QA + Video row action uses this mode.

The five steps, each a script under the skill's `references/`:

| Step | Script | Produces |
| --- | --- | --- |
| 0 | `deps.py` | a report of what is missing, and installs what it may |
| 1 | `narrate.py script.json` | narration audio + `narr.json` (measured timeline) |
| 2 | `record_template.py` (copy and adapt) | the screen capture + `events.json` |
| 3 | `compose.py` | `index.html` — the composition, as a real web page |
| 4 | `verify_align.py` | pass/fail on drift, audio, picture and streams |

Two things about the shape of this that are easy to get wrong:

- **The composition is generated.** Slides, subtitles and camera moves live in
  `index.html`, which `compose.py` writes. Changing a word means re-composing,
  not re-recording — but it also means a palette or layout fix belongs in the
  generator. Editing the generated file alone gets silently overwritten by the
  next compose.
- **Delivery is decided by `verify_align.py`, not by eye.** It exits non-zero on
  drift beyond budget, on a silent audio track, on a picture that is black or
  blown out, on a render whose dimensions do not match the capture, and on
  missing streams. A film that has not passed it is not finished.

Speech providers are `piper` (local, nothing leaves the machine) and `polly`
(the operator's own AWS account, so it costs them money); `--provider auto`
prefers local and refuses rather than reaching for a third-party endpoint. Text
sent to the cloud provider is scrubbed of credential-shaped content first.

Rendering needs Node and pulls `hyperframes` plus GSAP from public registries,
so the render step is not offline. `deps.py` reports every one of these and says
which it can install without root.

Prefer `browser-recording` instead when a short silent clip of a UI interaction
is all that is wanted: it is a smaller tool and needs no narration script.

`kirocrew-worktree-dev` carries no app-bridged copy: the canonical copy is
owned by the `kirocrew-dev` development-skills suite under
`src/kiro_crew/builtin_skills/`, and the app-bridged duplicate was removed
because two copies of the same skill drift and get loaded nondeterministically
against each other (PR #353 arbiter finding). That single-copy rule is what
matters here; where the one copy lives is a packaging question, and it lives in
the packaged tree so `_ensure_builtin_skills` reaches every distribution. The
project-dir mechanism reaches only some: `_project_skills_dir()` reads
`KIROCREW_PROJECT_DIR`, which a repo checkout provides, but a `pip install` from
the wheel or sdist does not — and neither does the desktop bundle, whose builder
stages no top-level `skills/` tree.

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

## Forced Removal Refusal Matrix

The `_worktree_remove` decision surface evaluates `force × PR-merged × dirty`
and emits one audit action per outcome. `--force` is NEVER passed to
`git worktree remove`; every path either refuses or removes without `--force`
so that git's own dirty check is the atomic last line of defence.

| force | PR merged | dirty | Outcome | Audit action |
|-------|-----------|-------|---------|--------------|
| True | No | True | **Refuse** — uncommitted changes on unmerged branch | `refused_dirty_unmerged` |
| True | No | None | **Refuse** — cannot verify cleanliness | `refused_unverifiable` |
| True | No | False | **Remove** (no `--force`); git's own check guards the TOCTOU window | `unmerged_clean_no_git_force` |
| True | Yes | True | **Refuse** — fresh-MERGED confirms merge but tree is dirty | `refused_dirty_merged` |
| True | Yes | None | **Refuse** — fresh-MERGED confirms merge but tree is unverifiable | `refused_unverifiable_merged` |
| True | Yes | False | **Remove** (no `--force`); mirrors unmerged-clean TOCTOU pattern | `merged_clean_no_git_force` |
| False | Yes | * | Non-forced path (squash-safe OID race guard) | n/a (no force audit) |
| False | No | * | Non-forced path | n/a (no force audit) |

Additional pre-gates (evaluated before the matrix above):

| Condition | Outcome | Audit action |
|-----------|---------|--------------|
| Branch OID unpinnable | **Refuse** | `refused_unpinnable` |
| Cached MERGED but fresh verification fails | **Refuse** | `refused_stale_merged` |
| Fresh-MERGED but branch OID not contained in PR head | **Refuse** | `refused_uncontained_fresh_head` |
| Target is the live gateway worktree | **Refuse** | (live-worktree guard) |
| Worktree inside another worktree (containment) | **Refuse** | `refused_containment` |

**Invariant:** `git worktree remove --force` is unreachable from any code path.
Both clean-removal branches (unmerged and merged) set `force_use_git_force = False`
explicitly, and every non-clean state is a hard refusal. A late dirty edit in the
check-to-removal window is caught by git's own atomic dirty check (exit code != 0),
surfaced as `refused_dirty_at_removal`.
