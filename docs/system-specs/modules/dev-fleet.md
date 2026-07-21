# Dev Fleet Module

Last Updated: 2026-07-20

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
| `/apps/dev-fleet/api/pod/up` | `{name}` | Start isolated pod instance |
| `/apps/dev-fleet/api/pod/down` | `{name}` | Stop pod instance |
| `/apps/dev-fleet/api/pod/restart` | `{name}` | Stop then start pod |
| `/apps/dev-fleet/api/pod/token` | `{name}` | Mint a dashboard token for the pod |
| `/apps/dev-fleet/api/pod/provision` | `{name}` | Start async venv+dist build (returns `{run_id}`) |
| `/apps/dev-fleet/api/rebase` | `{name}` | Rebase worktree onto origin/main |

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

## Background Tasks

- **Status refresher** (`_status_refresher`) — runs every 60s, fetches origin + refreshes
  fleet cache. Started via `dev_fleet_startup` on app startup.
- **Fleet cache** — 10s TTL. Cold requests block on fresh data; warm requests serve stale
  and background-refresh.

## Async Runs

Long-running operations (sync, provision) are tracked via `_RUNS` dict with:
- Streamed stdout (last 500 lines kept)
- Watchdog deadline (30 min default, configurable via `_RUN_DEADLINE_S`)
- Status: `running` → `done` | `timeout`

Clients poll `/apps/dev-fleet/api/run?id=<run_id>` for progress.

## Output Redaction

All user-visible output passes through `redact_credentials()` and
`redact_exfiltration_urls()` before HTTP response serialization.

## Platform Behavior

- **Linux only** — pod integration requires systemd (systemctl, journalctl)
- **macOS** — worktree management works; pod operations degrade (import fails gracefully)
- **git** and **gh** CLI required for full functionality; missing binaries produce
  graceful degradation via OSError catch in `_run_cmd`
