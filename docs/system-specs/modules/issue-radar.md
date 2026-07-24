# Issue Radar Module

Last Updated: 2026-07-24

## Overview

Issue Radar is an opt-in (`defaultEnabled: false`) built-in app for GitHub
issue triage. It connects one or more repos via the user's own `gh` CLI session
(no GitHub App, no PAT management) and provides a 3-column workbench:
browse/filter issues, view AI-summarized detail + timeline, apply triage actions
(label, close/reopen), and record per-issue investigation findings in a local
ledger. A background watcher optionally notifies on new issues.

## Routes

All routes live under `/api/apps/issue-radar/` and are registered by
`apps/builtins/issue_radar/backend/routes.py:register_routes`. Every handler is
wrapped in `_require_enabled` (returns 403 when the app is disabled).

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/connect` | Connect a repo (validates URL, verifies `gh` access) |
| GET | `/issues` | List open/closed issues (cached, paginated) |
| GET | `/issue` | Full issue detail + timeline |
| GET | `/labels` | Repo label set |
| GET | `/members` | Repo collaborators (authoritative API or fallback) |
| GET | `/repos` | Connected repos list (with permission self-heal) |
| DELETE | `/repos` | Disconnect a repo (drops config + cache) |
| GET | `/me` | Current `gh` login |
| GET/PUT | `/settings` | Per-repo triage settings |
| GET | `/issue-ai` | AI summary + suggested labels (kirocrew-lite) |
| POST | `/labels/apply` | Apply label changes (add/remove) |
| POST | `/issue/state` | Close/reopen an issue |
| GET/PUT | `/investigation` | Per-issue investigation record |
| GET/POST | `/recommendations` | AI label taxonomy recommendations |
| POST | `/labels/create` | Create a new repo label |

## Storage Schema

All data under `app_data_dir("issue-radar")` (typically `~/.kirocrew/apps/issue-radar/data/`):

```
config.json                         # Connected repos, per-repo settings
repos/<owner>/<repo>/
  issues-cache.json                 # Open issues (schema-versioned)
  issues-closed-cache.json          # Closed issues (capped at 100)
  labels-cache.json                 # Repo label definitions
  members-cache.json                # Collaborators roster + source
  issue-<N>.json                    # Per-issue detail cache
  issue-<N>-ai.json                 # AI summary cache
  recommendations-cache.json        # AI label taxonomy
  investigation-<N>.json            # Per-issue investigation record
  watch-state.json                  # Watcher high-water mark
```

`config.json` RMW operations are serialized via a cross-process file lock
(`platform_compat.file_lock` on `config.json.lock`).

## Permissions

Write routes (`/labels/apply`, `/issue/state`, `/labels/create`) are gated on
confirmed `triage` or `push` access (`_repo_can_write` returns `True` — unknown
permission is denied, not allowed). Read-only repos degrade to suggest-only.

## Security Controls

- **Spawn hardening**: All `gh` calls funnel through `_gh_run`, which resolves a
  canonical `gh` only from trusted system directories and validates it (and every
  parent) via `_validate_provider_executable` (root-owned, non-user-writable,
  canonical, non-symlinked). A minimal env is passed (no unrelated gateway
  secrets). Benign-allowlisted in the spawn audit (1 entry).
- **SEL audit**: Every `_gh_run` invocation emits an SEL tool-invocation event
  (success/failure/timeout). Write handlers additionally emit denied/ok/failure
  events around the permission check and mutation.
- **Input validation**: Owner/repo are charset-restricted + github.com host
  allowlisted (SSRF guard). Numbers are `int()`-coerced. Write bodies go via
  JSON stdin, never argv. Request bodies validated as `dict` before `.get()`.
- **Enabled-state guard**: All handlers wrapped in `_require_enabled`; returns 403
  when the app is disabled.

## Background Watcher

An in-process asyncio loop (`watch.py`) polls opted-in repos every 60s for new
issues (high-water mark in `watch-state.json`). Sends dashboard bell
notifications via `state.notify`. Zero-LLM. Guarded by `is_app_enabled` — silent
when disabled. Lifecycle hooks registered via `app.on_startup`/`on_cleanup`.

## Platform Requirements

- POSIX only (macOS/Linux). Windows raises `GhCliError` immediately.
- `gh` CLI authenticated on the host.
- Homebrew paths (`/opt/homebrew/bin`, `/usr/local/bin`) included in trusted dirs.
