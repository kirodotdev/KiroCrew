# Issue Radar Module

Last Updated: 2026-07-28

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
| GET | `/issues` | List open/closed issues (cached, paginated). `poll=1` takes the probe-gated path — see Client-Side List Polling |
| GET | `/issue` | Full issue detail + timeline |
| GET | `/ref` | Compact summary of one referenced issue/PR (hover preview + issue-vs-PR resolution). One `gh` call, no timeline, short-TTL cache |
| GET | `/labels` | Repo label set |
| GET | `/members` | Repo collaborators (authoritative API or fallback) |
| GET | `/repos` | Connected repos list (with permission self-heal) |
| GET | `/recent-repos` | Repos the `gh` user contributed to recently (connect-dialog picker) |
| DELETE | `/repos` | Disconnect a repo (drops config + cache) |
| GET | `/me` | Current `gh` login |
| GET/PUT | `/settings` | Per-repo triage settings. The PUT replaces the whole document, so it carries the `revision` it read and is refused with **409** if the stored revision has moved — otherwise a stale tab would erase a label appended meanwhile |
| POST | `/settings/role` | APPEND one label to a triage-label role, under the config lock. Exists because the PUT replaces the whole document, so a client read-modify-write only serializes itself — two dashboard tabs would each read the same settings and the later full replacement would drop the other's label |
| GET | `/issue-ai` | AI summary + suggested labels (kirocrew-lite) |
| GET | `/pulls` | List open/closed PRs (cached, `poll=1` probe-gated as for `/issues`; rows enriched with diff size + check tally via ONE GraphQL call, topped up by number for rows outside its window). Rows whose enrichment failed carry `null` (unknown, not zero) and are deliberately NOT written to the cache, so the next read retries |
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
  issues-cache.json                 # Open issues (schema-versioned, + poll probe)
  issues-closed-cache.json          # Closed issues (capped at 100)
  labels-cache.json                 # Repo label definitions
  members-cache.json                # Collaborators roster + source
  issue-<N>.json                    # Per-issue detail cache
  issue-<N>-ai.json                 # AI summary cache
  pulls-cache.json                  # Open PRs (schema-versioned, + poll probe)
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
  canonical `gh` via the shared provider resolver
  (`source_providers.provider_executable_candidates` — well-known install dirs,
  then the ambient `PATH`) and validates it (and every parent) with
  `_validate_provider_executable`. The default policy accepts the user's OWN
  install (Homebrew/asdf/`~/.local/bin`) and refuses only provenance the user did
  not choose: a binary owned by another unprivileged account, a world-writable
  one (a world-writable *directory* is tolerated only when sticky, where the
  owner check still decides), or one inside the agent-writable project/workspace
  tree. A gateway running as root is refused outright in both modes.
  `KIROCREW_PROVIDER_BIN_STRICT=1` restores the historical root-owned,
  symlink-free requirement. A minimal env is passed (no unrelated gateway
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

## Client-Side List Polling

The issue and PR lists poll every 60s (`LIST_POLL_MS`, matching the watcher's
cadence so a bell notification and the row it refers to land in the same
window). Deliberately 6x the per-item detail interval (`DETAIL_POLL_MS`, 30s):
the open lists are FULLY paginated, so a whole-repo refetch is tens of REST
requests plus a multi-MB cache rewrite on a large repo, not one item's worth of
work.

A poll sends `poll=1`, NOT `refresh=1`. The client only declares intent ("I want
current data"); the **cost policy lives server-side** so it cannot be multiplied
by open tabs:

- `poll=1` — probe-gated. `_poll_can_serve_cache` runs ONE
  `github_client.probe_open_list` search call (`{total_count, top_updated_at}`
  for the open set) and serves the cache untouched unless that reading differs
  from the one recorded when the rows were last fetched. Two fields because
  either alone has a blind spot: `top_updated_at` catches a new/edited/commented
  item, `total_count` catches a CLOSE (which leaves the open set without bumping
  any remaining timestamp).
- `refresh=1` — the unconditional cache-bust, used by the manual Refresh button.
  Unchanged semantics.
- neither — cache-first at any age, so the app paints on open without waiting on
  `gh`. This is what the FIRST fetch for a query key sends.

The probe reading is stored under a `probe` key inside the list cache file, and
is only ever compared **probe against probe** — never against the cached rows —
so a systematic difference between what search counts and what the REST list
returns cancels out instead of reporting "changed" on every poll. Rows and probe
are read in ONE `read_*_snapshot` call: reading them separately let a concurrent
refresh pair old rows with a new probe, which the poll would then serve as
verified. The reading recorded with a refetch is the one taken BEFORE the fetch,
so a change landing mid-fetch leaves the record behind reality and the next poll
refetches rather than hiding it. For issues the probe is handed to
`store.refresh_issues_cache` so it is persisted by the SAME locked write that
stores the rows — a second write after the refresh would reopen the window that
lock closes (a label applied in between would be overwritten). The label and
check write-through patches read-modify-write the whole payload, so they carry
`probe` and `fetched_at` through untouched.

`LIST_POLL_MAX_STALENESS_SEC` (10 min, every 10th poll) bypasses the probe and
refetches unconditionally. This is the backstop for a probe that is **wrong
rather than unavailable** — a consistently wrong reading matches its own prior
recording forever, which no error handling can catch. Two live cases: GitHub is
retiring PR results from `search/issues` (the `advanced_search` transition),
after which the `is:pr` probe degenerates to a stable `{0, None}` that compares
equal to itself; and a PR check run turning red changes neither `updated_at` nor
the open count, so no metadata probe can observe CI moving. (The PR you have
*open* stays current either way — its detail poll writes fresh check state back
into the list cache via `apply_pr_checks_to_list_cache`.) The ceiling bounds the
worst case to ~6 full fetches an hour, still an order of magnitude under the
unprobed cost.

The age the ceiling measures comes from a `fetched_at` stamp **inside** the cache
payload, not from the file's mtime. The write-through patches
(`apply_pr_checks_to_list_cache`, `apply_label_change_to_caches`) rewrite the file
without refetching anything, so with mtime the age reset every 30s for as long as
a PR pane was open — leaving the ceiling unreachable in exactly the
degenerate-probe case it exists to bound. A cache written before the field
existed falls back to mtime for one refresh cycle.

A probe **error** keeps serving the cache rather than refetching: a sustained
probe outage (an exhausted search quota, say) would otherwise convert the poll
into exactly the fetch-per-minute drain this path exists to avoid. Staleness is
bounded by the ceiling above, which is the honest backstop.

`_coalesced_probe` shares one reading per `(owner, repo, kind)` for
`_PROBE_COALESCE_SEC` (15s), so the search quota (30/min, shared with the user's
own searches) does not scale with the number of open tabs. Concurrent polls for
the same key join one in-flight probe (a per-key future); the lock guards only
the memo/in-flight maps and is never held across the probe itself, so one repo's
`gh` timeout cannot stall another repo's or kind's poll. The reading is published
from the future's done-callback rather than by the awaiting request, so a client
that disconnects mid-probe still contributes the call it paid for.

Search is used rather than `repos/.../issues` because it reports `total_count` in
the same response and `is:issue`/`is:pr` keeps the two lists from triggering each
other.

Only the OPEN lists are probed; the closed lists are bounded to one
`per_page=100` page, so refetching one is already a single request.

The PR poll is additionally gated on the PR surface being open (that fetch runs
the GraphQL enrichment), the base list and the person-filter search are mutually
exclusive so only the rendered source polls, and react-query pauses every poll
while the window is unfocused. Because those two sources are gated on different
flags — the base list stands down as soon as a person filter is *requested*, the
search query only starts once `/me` resolves — `pullsLoading` covers the gap
between them, or restoring a persisted person filter would render "no pull
requests" until the login lands.

## In-App Cross-References

An issue/PR body or comment that links to ANOTHER issue or PR **in the connected
repo currently open** does not leave the app: the click opens that target in a
bottom sheet (`components/RefSheet.tsx`) over the workspace, rendering the same
detail pane (`IssueDetail` / `PrDetail`) the right column uses. Everything else —
the list, the filters, the selected item — is untouched.

- **Matching** (`lib/refLinks.ts:parseRepoRef`) is deliberately narrow. Only an
  absolute `http(s)` URL on `github.com` / `www.github.com` whose path is
  `/<owner>/<repo>/(issues|pull|pulls)/<positive int>` and whose owner/repo match
  the ACTIVE repo (case-insensitively) is claimed. Trailing segments (`/files`),
  query strings and `#issuecomment-…` fragments are ignored — same target. Any
  other link (a different repo, an Enterprise host, `/discussions/`, `/commit/`,
  a relative href, a non-`http(s)` scheme) keeps its existing behaviour and opens
  externally. A repo is identified by owner/repo only, so a same-path URL on an
  Enterprise host is a DIFFERENT repo and is never claimed.
- **Interception** happens at the ANCHOR, not on the DOM: `MarkdownRenderer`
  exposes a `LinkOverrideCtx` seam (a predicate-style render override consulted by
  its default anchor), and `components/RefMarkdown.tsx` provides one that returns
  `components/RefLink.tsx` for claimed hrefs. The markdown pipeline is otherwise
  untouched, nothing post-processes React-owned DOM, and links keep their
  href/target — so a modified click (Cmd/Ctrl/Shift/Alt), a middle click (which
  fires `auxclick`), and "copy link address" all still behave like GitHub links.
  Keyboard activation works because it dispatches the same click.
- **Shorthand.** `lib/refLinks.ts:linkifyIssueRefs` rewrites a bare `#123` into a
  real markdown link before rendering (the raw markdown the API returns carries
  only the literal text; GitHub's own web UI linkifies it at render time). Fenced
  code, inline code, autolinks, raw HTML and existing markdown links are masked
  out first. A shorthand is rejected when preceded by a word character, `/`
  (a URL fragment or a cross-repo `owner/repo#5`), `&` (`&#123;`), `[`, `(` or `#`,
  and when FOLLOWED by a word character (so `#1a2b3c` is not read as `#1`). An
  all-digit run is taken as a reference — GitHub does the same, and six-figure
  issue numbers are ordinary, so length cannot decide.
- **Affordance + preview.** A claimed reference renders with a DASHED accent
  underline (a solid one stays "ordinary external link"), and hovering or focusing
  it opens a preview card — number, title, author, when, lifecycle — after a short
  delay, fetched from `/ref` only on demand. The card is portalled to `<body>` with
  fixed coordinates so no `overflow: hidden` ancestor clips it, flips above the
  link near the viewport bottom, and is dismissed by scroll/resize (its position is
  captured at open time).
- **Kind resolution.** `#123` and `/issues/123` are both ambiguous, so the pane is
  chosen by `/ref`'s `is_pr`, not by the link's shape. An explicit `/pull/` link
  renders immediately; a failed lookup degrades to the issue pane rather than
  blocking. The lookup shares its query key with the hover card, so opening a
  reference you hovered costs nothing.
- **Stack.** `refStack` in the context holds the open trail, innermost last. A
  reference followed from inside the sheet pushes; Escape and the header's back
  control pop; the backdrop and the close button discard the whole trail. It is
  transient (never persisted) and is cleared on a repo switch, because a bare
  number means nothing across repos.
- **Presentation.** The sheet is bottom-ANCHORED with square bottom corners, so it
  reads as growing out of the page rather than as a card sitting low. It takes
  ~94%/93% of the app area (px-capped only on very large displays) — most of the
  space, because a detail pane is a two-column layout with a 236px sidebar, but
  never all of it: the workspace visible around the edges is what says "detour,
  not navigation".
- **Data path.** `GET /issue` and `GET /pull` already fetch any number on demand
  for a connected repo; only the cheap `/ref` summary is new. When the target is in
  the loaded list its row seeds the first paint (and the sheet offers "open in the
  workspace", which promotes it to the main selection); otherwise a placeholder row
  carries the number until the detail arrives. Both panes therefore read
  `detail?.x ?? row.x` for the title, the GitHub URL and the poll lifecycle.

## Platform Requirements

- POSIX only (macOS/Linux). Windows raises `GhCliError` immediately.
- `gh` CLI authenticated on the host.
- Any `gh` the user can run from their terminal is accepted: the well-known dirs
  (`/opt/homebrew/bin`, `/usr/local/bin`, `/usr/bin`, `/home/linuxbrew/…`, the
  managed `libexec/kirocrew` dirs) are searched first, then `PATH`. No `sudo`
  copy is required. Override with `KIROCREW_ISSUE_RADAR_GH`; harden with
  `KIROCREW_PROVIDER_BIN_STRICT=1`.
