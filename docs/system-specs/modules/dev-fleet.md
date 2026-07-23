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
| `/apps/dev-fleet/api/fleet` | Lightweight worktree + pod list (polled every 12s). `?fresh=1` forces cache bypass. |
| `/apps/dev-fleet/api/worktree?name=` | Lazy per-branch detail: PR, commits, disk usage |
| `/apps/dev-fleet/api/pod/logs?name=&n=` | Pod journal tail (recent N lines, default 120) |
| `/apps/dev-fleet/api/run?id=` | Async run status + streamed output (last 60 lines) |
| `/apps/dev-fleet/api/prune-candidates` | List worktrees eligible for pruning |
| `/apps/dev-fleet/api/prune-status` | Current prune operation progress |
| `/apps/dev-fleet/api/disk` | Aggregate disk usage per worktree (async computation) |

### Write (POST)

| Route | Body | Description |
|-------|------|-------------|
| `/apps/dev-fleet/api/sync` | — | Pull main + rebuild (single-flight) |
| `/apps/dev-fleet/api/worktree/remove` | `{name, force?}` | Remove a worktree (stops pod first) |
| `/apps/dev-fleet/api/prune-run` | `{names[]}` | Batch-remove eligible worktrees |
| `/apps/dev-fleet/api/pod/up` | `{name}` | Start isolated pod instance (re-verifies the unit is active) |
| `/apps/dev-fleet/api/pod/down` | `{name}` | Stop pod instance (re-verifies the unit is gone before reporting success) |
| `/apps/dev-fleet/api/pod/restart` | `{name}` | Stop then start pod |
| `/apps/dev-fleet/api/pod/token` | `{name}` | Mint a dashboard token for the pod |
| `/apps/dev-fleet/api/pod/provision` | `{name}` | Start async venv+dist build (returns `{run_id}`) |
| `/apps/dev-fleet/api/rebase` | `{name}` | Rebase worktree onto origin/main |
| `/apps/dev-fleet/api/restart-gateway` | — | Restart the live gateway in place (detached `systemd-run`) |
| `/apps/dev-fleet/api/make-live` | `{path, dry_run?}` | Repoint the live gateway at another worktree (see Make Live) |

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
- Streamed stdout (last 500 lines kept)
- Watchdog deadline (30 min default, configurable via `_RUN_DEADLINE_S`)
- Status: `running` → `done` | `timeout`

Clients poll `/apps/dev-fleet/api/run?id=<run_id>` for progress.

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
