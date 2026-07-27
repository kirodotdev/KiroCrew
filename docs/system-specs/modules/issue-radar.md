# Issue Radar Module

Last Updated: 2026-07-25

## Overview

Issue Radar is an opt-in (`defaultEnabled: false`) built-in app for GitHub
issue and pull-request triage. It connects one or more repos via the user's own
`gh` CLI session (no GitHub App, no PAT management) and provides a 3-column
workbench: browse/filter issues, view AI-summarized detail + timeline, apply
triage actions (label, close/reopen), and record per-issue investigation findings
in a local ledger. A parallel PULL REQUESTS section reuses the same shape —
filter by lifecycle (open / merged / closed-unmerged), person, draft and label;
read an AI summary of the description plus the whole review conversation; and see
the automated checks ("auto review") on the head commit. A background watcher
optionally notifies on new issues.

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
| GET | `/recent-repos` | Repos the `gh` user contributed to recently (connect-dialog picker) |
| DELETE | `/repos` | Disconnect a repo (drops config + cache) |
| GET | `/me` | Current `gh` login |
| GET/PUT | `/settings` | Per-repo triage settings. The PUT replaces the whole document, so it carries the `revision` it read and is refused with **409** if the stored revision has moved — otherwise a stale tab would erase a label appended meanwhile |
| POST | `/settings/role` | APPEND one label to a triage-label role, under the config lock. Exists because the PUT replaces the whole document, so a client read-modify-write only serializes itself — two dashboard tabs would each read the same settings and the later full replacement would drop the other's label |
| GET | `/issue-ai` | AI summary + suggested labels (kirocrew-lite) |
| GET | `/pulls` | List open/closed PRs (cached; rows enriched with diff size + check tally via ONE GraphQL call, topped up by number for rows outside its window). Rows whose enrichment failed carry `null` (unknown, not zero) and are deliberately NOT written to the cache, so the next read retries |
| GET | `/pulls/search` | PRs matching a per-person filter, resolved server-side by GitHub search (escapes the list's page cap). Paginates only as far as its own cap and reports `truncated` so the UI says "newest N" rather than implying completeness |
| GET | `/pull` | Full PR detail + conversation (issue timeline merged with inline review comments) + automated checks on the head commit. Cache-first with a short server-side TTL (`PR_DETAIL_CACHE_TTL_SEC`), so a plain GET self-refreshes and no caller has to pass `refresh=1` to stay current |
| GET | `/pull-ai` | AI summary of a PR (description + whole conversation + check state), cached against a fingerprint that hashes the conversation's CONTENT — so an edited comment invalidates it, not just a new one |
| POST | `/labels/apply` | Apply label changes (add/remove) |
| POST | `/issue/state` | Close/reopen an issue |
| GET/PUT | `/investigation` | Per-issue investigation record |
| GET/POST | `/recommendations` | AI label taxonomy recommendations |
| POST | `/labels/create` | Create a new repo label |
| GET | `/tagging` | The untagged queue (also serves `bulk_max`, the bulk-apply cap, so the client chunks on the server's real limit; and `titles` bounded to the slice a recommendation's examples can cite) (open issues with ZERO labels) plus any cached per-issue label suggestions for it. Never runs the model, so opening the Tagging dashboard costs nothing; suggestions for issues that have since been labelled elsewhere are filtered out |
| POST | `/tagging` | Generate per-issue label suggestions with ONE batched model call (`_TAG_BATCH_MAX` = 50 issues). Without `numbers` it takes the next un-analysed slice, so repeated calls walk a long backlog without re-paying; with `numbers` it re-analyses specific issues. Proposals are intersected with the repo's real label set AND with the batch that was shown, so injected issue text can neither invent a label nor reach an issue outside the batch |
| POST | `/labels/apply-bulk` | Apply label ADDITIONS to many issues at once (add-only — removal stays a per-issue action). Unknown labels are rejected before any write, so a typo cannot half-apply the batch; per-issue failures are reported rather than swallowed, and only the issues that actually got labelled leave the queue |

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
  pulls-cache.json                  # Open PRs (schema-versioned)
  pulls-closed-cache.json           # Closed+merged PRs (capped at 100)
  pull-<N>.json                     # Per-PR detail + timeline + checks cache
  pull-<N>-ai.json                  # PR AI summary + the fingerprint it was built from
  recommendations-cache.json        # AI label taxonomy
  tagging-cache.json                # Per-issue label proposals for the untagged queue
  investigation-<N>.json            # Per-issue investigation record
  watch-state.json                  # Watcher high-water mark
```

`config.json` RMW operations are serialized via a cross-process file lock
(`platform_compat.file_lock` on `config.json.lock`). `tagging-cache.json` holds
the same lock discipline on its own `.lock` sidecar: every mutation is a merge
(generate) or a prune (apply) over the whole document, so overlapping cycles
would otherwise lose an update. An analysed issue the model declined to label is
stored as an EMPTY list, not omitted — otherwise "the next un-analysed slice"
would return the same unlabelable issues forever.

## Permissions

Write routes (`/labels/apply`, `/labels/apply-bulk`, `/issue/state`,
`/labels/create`) are gated on
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
- **Prompt-injection containment**: The AI routes feed UNTRUSTED repo text to the
  model — an issue body, and for `/pull-ai` the PR description plus every comment
  and review. That payload is fenced in explicit markers and declared as data, the
  call runs in a tool-less ephemeral session (`REJECT_ALL` approvals), and the
  output is redacted. Issue label suggestions are additionally intersected with the
  repo's real label set, so injected text cannot invent a label; a PR summary is
  prose that nothing downstream acts on.

## Background Watcher

An in-process asyncio loop (`watch.py`) polls opted-in repos every 60s for new
issues (high-water mark in `watch-state.json`). Sends dashboard bell
notifications via `state.notify`. Zero-LLM. Guarded by `is_app_enabled` — silent
when disabled. Lifecycle hooks registered via `app.on_startup`/`on_cleanup`.

## Platform Requirements

- POSIX only (macOS/Linux). Windows raises `GhCliError` immediately.
- `gh` CLI authenticated on the host.
- Homebrew paths (`/opt/homebrew/bin`, `/usr/local/bin`) included in trusted dirs.
