# Issue Radar Module

## Overview

Issue Radar is an opt-in (`defaultEnabled: false`) built-in app for GitHub
issue and pull-request triage. It connects one or more repos via the user's own
`gh` CLI session (no GitHub App, no PAT management) and provides a 3-column
workbench: browse/filter issues, view AI-summarized detail + timeline, apply
triage actions (label, close/reopen), and record per-issue investigation findings
in a local ledger. A parallel PULL REQUESTS section reuses the same shape —
filter by lifecycle (open / merged / closed-unmerged), person, draft and label;
read an AI summary of the description plus the whole review conversation; see
the automated checks ("auto review") on the head commit; and ACT on a PR without
leaving for the provider's web UI — approve / request changes, comment, close or
reopen, merge or arm the provider's own auto-merge, and cancel or re-run CI, per-PR
or in bulk across a selection (see Pull-Request Actions). A background watcher
optionally notifies on new issues.

## Routes

All routes live under `/api/apps/issue-radar/` and are registered by
`apps/builtins/issue_radar/backend/routes.py:register_routes`. Every handler is
wrapped in `_require_enabled` (returns 403 when the app is disabled).

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/connect` | Connect a repo (validates URL, verifies `gh` access) |
| GET | `/issues` | List open/closed issues (cached, paginated). `poll=1` takes the probe-gated path — see Client-Side List Polling. `first_page=1` (open only) takes the progressive first-paint fast path — see First Paint |
| GET | `/issue` | Full issue detail + timeline |
| GET | `/ref` | Compact summary of one referenced issue/PR (hover preview + issue-vs-PR resolution). One `gh` call, no timeline, short-TTL cache |
| GET | `/labels` | Repo label set (cached with a **10-min TTL**; see "The label cache expires") |
| GET | `/members` | Repo collaborators (authoritative API or fallback) |
| GET | `/repos` | Connected repos list. Rows missing a cached `permissions` object (connected before permissions were tracked) are self-healed with a live `verify_repo_access`, run CONCURRENTLY under a bounded semaphore (`_REPO_HEAL_CONCURRENCY`) rather than one-at-a-time, since this gates app open; a single unreadable repo is skipped, not fatal |
| GET | `/recent-repos` | Repos the `gh` user contributed to recently (connect-dialog picker) |
| DELETE | `/repos` | Disconnect a repo (drops config + cache) |
| GET | `/me` | Current `gh` login |
| GET/PUT | `/settings` | Per-repo triage settings. The PUT replaces the whole document, so it carries the `revision` it read and is refused with **409** if the stored revision has moved — otherwise a stale tab would erase a label appended meanwhile |
| POST | `/settings/role` | APPEND one label to a triage-label role, under the config lock. Exists because the PUT replaces the whole document, so a client read-modify-write only serializes itself — two dashboard tabs would each read the same settings and the later full replacement would drop the other's label |
| GET | `/issue-ai` | AI summary + suggested labels (kirocrew-lite) |
| GET | `/pulls` | List open/closed PRs (cached, `poll=1` probe-gated as for `/issues`; `first_page=1` (open only) takes the progressive first-paint fast path — see First Paint; rows enriched with diff size + check tally via one GraphQL call and merge readiness via a second, lean one run CONCURRENTLY, each topped up by number for rows outside its `first:100` window). Rows whose enrichment failed carry `null` (unknown, not zero) and are deliberately NOT written to the cache, so the next read retries |
| GET | `/pulls/search` | PRs matching a per-person filter, resolved server-side by GitHub search (escapes the list's page cap). Paginates only as far as its own cap and reports `truncated` so the UI says "newest N" rather than implying completeness |
| GET | `/pull` | Full PR detail + conversation (issue timeline merged with inline review comments) + automated checks on the head commit. Cache-first with a short server-side TTL (`PR_DETAIL_CACHE_TTL_SEC`), so a plain GET self-refreshes and no caller has to pass `refresh=1` to stay current |
| GET | `/pull-ai` | AI summary of a PR (description + whole conversation + check state), cached against a fingerprint that hashes the conversation's CONTENT — so an edited comment invalidates it, not just a new one |
| POST | `/labels/apply` | Apply label changes (add/remove) |
| POST | `/issue/state` | Close/reopen an issue |
| GET/PUT | `/investigation` | Per-issue investigation record. The PUT is the ONE app route also reachable with the gateway internal secret (`_MIXED_INTERNAL_API_PATHS`), because it is the write behind the `issue_radar_record_investigation` MCP tool — see [Recording findings](#recording-findings) |
| GET/POST | `/recommendations` | AI label taxonomy recommendations |
| POST | `/labels/create` | Create a new repo label |
| GET | `/tagging` | The untagged queue (also serves `bulk_max`, the bulk-apply cap, so the client chunks on the server's real limit; and `titles` bounded to the slice a recommendation's examples can cite) (open issues with ZERO labels) plus any cached per-issue label suggestions for it. Never runs the model, so opening the Tagging dashboard costs nothing; suggestions for issues that have since been labelled elsewhere are filtered out |
| POST | `/tagging` | Generate per-issue label suggestions with ONE batched model call (`_TAG_BATCH_MAX` = 50 issues). Without `numbers` it takes the next un-analysed slice, so repeated calls walk a long backlog without re-paying; with `numbers` it re-analyses specific issues. Proposals are intersected with the repo's real label set AND with the batch that was shown, so injected issue text can neither invent a label nor reach an issue outside the batch |
| POST | `/labels/apply-bulk` | Apply label ADDITIONS to many issues at once (add-only — removal stays a per-issue action). Unknown labels are rejected before any write, so a typo cannot half-apply the batch; per-issue failures are reported rather than swallowed, and only the issues that actually got labelled leave the queue |
| POST | `/pull/state` | Close or reopen a PR. Routed through the provider's PULL endpoint, not the issue endpoint — a merged PR's un-reopenability then comes from the provider instead of silently succeeding against the issue shadow |
| POST | `/pull/review` | Submit a review (`approve` / `request_changes` / `comment`). Requires `head_sha` — a review is a verdict on a REVISION, so it rides as GitHub's `commit_id` / GitLab's `sha` and a force-push between render and click is refused rather than recorded. A body is required for the latter two (the provider rejects them bodyless). GitLab has no "request changes" verb and the client REFUSES rather than degrading it to a comment |
| POST | `/pull/comment` | Post a conversation comment on a PR |
| POST | `/pull/merge` | Merge a PR now. Per-PR only — never bulk. Requires `head_sha`, sent as the provider's `sha` precondition so the merge is pinned to the reviewed commit. Cannot bypass a gate: the provider enforces branch protection on its own endpoint, and a 405 refusal is mapped to a readable message |
| POST | `/pull/auto-merge` | Arm or disarm the PROVIDER's own auto-merge, for a PR that is not mergeable yet. **GitHub only** — REFUSED on GitLab, where `merge_when_pipeline_succeeds` is a deferral modifier on the merge endpoint rather than an arm verb (see "GitLab auto-merge is REFUSED outright" below); the UI hides both controls there. Callers must send only rows that are not landable yet — the provider refuses an already-clean or already-merged PR, and the list row carries `mergeable_state` so the bulk bar can tell (see "Merge readiness is on the LIST row") |
| GET | `/pull/runs` | The CI runs on a PR's head commit, each with its id plus server-computed `cancellable`/`rerunnable`, so the UI never offers an action the provider will refuse |
| POST | `/pull/run` | Cancel or re-run one CI run (`failed_only` re-runs just the failed jobs) |
| POST | `/pulls/bulk` | Apply ONE action to many PRs (`_BULK_PR_ACTIONS`: close, reopen, approve, comment, auto_merge, cancel_auto_merge; max `_BULK_PR_MAX` = 50). `approve` additionally requires a `head_shas` map keyed by PR number, covering EVERY number in the request (see rule 2). Sequential, because the PRs share one provider rate limit. Partial failure is reported per PR rather than failing the batch |

## Recording findings

The Investigate / Review buttons open a KiroCrew chat session seeded with a
triage prompt. When the agent concludes it writes its verdict back into the
item's investigation record — that is what puts a verdict + summary on the
issue's card instead of leaving it in chat scrollback.

That write goes through the **`issue_radar_record_investigation` MCP tool**, not
a raw HTTP call. An agent session holds no dashboard credential:

- the access cookie is `httpOnly`, so the frontend cannot hand it to the agent;
- `KIROCREW_INTERNAL_SECRET` is stripped from agent env by
  `sandbox._AGENT_DENIED_ENV_KEYS`;
- `.local_secret` — needed for the `GET /api/token/local` bootstrap — is on the
  `security.py` sensitive-path denylist, for tool reads and for the shell forms.

So a direct `PUT /api/apps/issue-radar/investigation` from the agent is refused
with `403 {"error": "Token required"}`. It used to be exactly what the seed
prompt asked for, which meant no investigation ever recorded findings and the
card's verdict/summary render path was unreachable. The tool runs in the
`kirocrew-core` MCP server, which holds the internal secret legitimately, so the
route is listed in `_MIXED_INTERNAL_API_PATHS` — the full path only, never the
`/api/apps/issue-radar` prefix, which would also admit the forge-write routes
(`/labels/apply`, `/issue/state`) to any internal-secret holder.

The tool takes the findings as **flat** args (`verdict`, `root_cause`,
`suggested_labels`, `next_action`, `summary`) rather than a nested object:
`FieldSpec` validates scalars and string lists, so a `findings` dict would reach
the gateway unvalidated. Empty fields are dropped, because the store merges
findings **per key** (`store._merge_findings`) and reads an empty value as "leave
this alone" — so a patch carrying only a `verdict` keeps the `root_cause`,
`summary` and labels an earlier write stored. An explicit `null` clears the whole
findings object (the UI's clear path); there is deliberately no per-field clear.
`provider`/`host`/`kind` are always sent explicitly — the record is keyed on them,
and defaulting them records a GitLab item into a same-slug GitHub repo's ledger.

**In the MCP tool**, every finding string and label goes through the platform
redaction shim (`platform.redact_via_context` → exfil URLs + credentials) before the
PUT: findings are LLM prose about an untrusted issue body, they are stored verbatim,
and the card re-renders them on every visit, so a credential quoted into a
`root_cause` would otherwise be persisted and redisplayed. This is a tool-level
guarantee, not a route-level one — the route itself does not redact, because its
other caller is the cookie-authed frontend writing the session link, not model
output.

## Storage Schema

All data under `app_data_dir("issue-radar")` (typically `~/.kiro/crew/apps/issue-radar/data/`):

```
config.json                         # Connected repos, per-repo settings
repos/<owner>/<repo>/
  issues-cache.json                 # Open issues (schema-versioned, + poll probe)
  issues-closed-cache.json          # Closed issues (capped at 100)
  labels-cache.json                 # Repo label definitions (10-min TTL, + fetched_at)
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
`/labels/create`, and every MUTATING `/pull/*` + `/pulls/bulk` action) are gated on
confirmed `triage` or `push` access (`_repo_can_write` returns `True` — unknown
permission is denied, not allowed). Read-only repos degrade to suggest-only. Every PR
*mutation* goes through one `_pr_action_preamble` helper for the
JSON/owner/connected/permission checks, so the gate is not re-implemented per handler.
`GET /pull/runs` is a READ and is gated on the connected-repo check only, like the
other reads — it returns run metadata the PR's own `checks` already imply.

## Pull-Request Actions

The write half of the PR pane — approve / request changes, comment, close / reopen,
merge or arm auto-merge, cancel or re-run CI — available per-PR from the detail header
and,
for the actions that are safe to repeat, in bulk from the list. Six rules, each a
deliberate narrowing:

1. **Merging is offered in two forms, and the app refuses an unsatisfied PR itself
   rather than relying on the provider to.** `/pull/merge` lands a PR that is ready
   now; `/pull/auto-merge` hands one that is not yet ready to the provider to land once
   its checks pass. An earlier revision shipped only the second, reasoning that a direct
   merge could land unreviewed code — which left a repository with **no branch rule**
   (where auto-merge is unavailable) with no merge path at all.
   **Why the app has to do the checking.** It is tempting to say "the provider
   adjudicates": branch protection is enforced on its merge endpoint, and an unsatisfied
   PR comes back 405. That is true for an ordinary user and false for the account that
   matters most — a repository admin holding bypass-branch-protection, for whom the
   provider *honours* the merge. And `mergeable` alone does not mean "ready": it means
   only "no merge CONFLICTS", so a PR with unsatisfied required reviews is
   `mergeable: true` with `mergeable_state: "blocked"`. Gating on it therefore offered
   the most privileged account a one-click way to land a PR its own rules had rejected.
   So the route re-reads the PR and refuses anything outside `_MERGE_ALLOWED_STATES`
   (`clean` / `has_hooks` on GitHub, `mergeable` on GitLab) with a
   **409 `merge_not_ready`**, and the UI mirrors the same set so the button never appears
   where it would only be refused. Two exclusions are load-bearing:
   - `unstable` is often described as "only non-required checks are failing", but the
     state does not actually distinguish a failing *required* check from an optional one,
     so it cannot be read as "protections satisfied".
   - GitLab's **legacy `can_be_merged`** is the subtler one. `_norm_pull` falls back to
     the old `merge_status` field when `detailed_merge_status` is absent (a pre-16.x
     server, or a payload that omits it), and `merge_status` reports *only* whether the
     branches conflict — it is GitLab's exact analogue of GitHub's `mergeable` and knows
     nothing about unmet approvals, unresolved blocking discussions or a red required
     pipeline. Admitting it reproduced the very hole this set exists to close, on the
     servers least likely to be watched. Its modern replacement
     (`detailed_merge_status: "mergeable"`) *does* imply those rules are met, and is the
     one GitLab value in the set. Note the read side still reports `can_be_merged` as
     `mergeable: true` — "no conflicts" is a true, useful signal for the pane's warning;
     the merge *gate* keys off the raw status instead, which is why
     `gitlab_client._MERGEABLE_STATUSES` and `routes._MERGE_ALLOWED_STATES` deliberately
     differ.

   A gate that cannot tell must refuse — and such a PR is still one click from
   `auto_merge`, which lets the provider decide once the checks finish. A provider 405 is
   still mapped to a readable refusal, since *Method Not Allowed* on a merge button reads
   like an app bug.
   **The merge is PINNED to the reviewed head commit.** `head_sha` is required by the
   route (400 `head_sha_required`) and by both clients, and rides as the provider's own
   `sha` precondition — so a push landing between the read and the click answers 409
   instead of merging. The route also refuses when the live head has moved since its own
   state read: that state describes the commit it was read for, not a newer one. The UI
   does not offer the button until it knows the sha.
   **The merge METHOD is per-provider, and the tuples deliberately differ.**
   `_pr_merge_method_field` reads `PR_MERGE_METHODS` off the *key's own* client rather
   than `github_client`'s copy — which an earlier revision did, and which worked only
   because the two happened to match. They no longer do: GitHub's `/merge` accepts
   `MERGE` / `SQUASH` / `REBASE`, but GitLab's has **no rebase option at all** —
   merge-commit vs. semi-linear vs. fast-forward is the *project's* `merge_method`
   setting, and the only per-request lever is `squash`. Accepting `REBASE` there
   translated it to `squash: false`, so GitLab produced a **merge commit**: the caller
   named one history shape and silently got another, on the one operation that cannot
   be undone. `REBASE` is therefore absent from `gitlab_client.PR_MERGE_METHODS` and a
   request for it is a 400 `invalid_merge_method` — the same refuse-rather-than-
   approximate rule the client follows for "request changes" and a full CI re-run.
   (GitLab's separate `/rebase` endpoint does not merge, so it is not a substitute.)
   There is deliberately **no "override and merge"**: an override is a governance
   decision recorded ON the provider (this repo does it with a reviewed
   `/ai-review override` comment), and shedding a
   required check is the one thing no automatic gate should do quietly.
   `test_pr_actions.py::TestMergeBoundaries` and `TestMergePrimitive` pin all of it.
2. **A REVIEW is pinned to a commit too, for the same reason a merge is.** Approving is
   a verdict on a *revision*, not on a pull request. Left unpinned, the review attaches
   to whatever the head is when the request lands — so a force-push between the render
   and the click records an **approval of code the reviewer never saw**, and on GitHub
   that approval can then satisfy a required-review rule. So `head_sha` is required by
   `/pull/review` (400 `head_sha_required`, via the same `_pr_head_sha_field` the merge
   route uses) and by both clients, and rides to the provider as GitHub's `commit_id` on
   `POST .../reviews` and GitLab's `sha` on `/approve`.
   **The provider parameters are not equivalent, and only one of them refuses.**
   GitLab's `sha` is a real precondition. GitHub's `commit_id` is only *attribution*:
   GitHub accepts a review naming a commit that is no longer the head, records it
   against that commit, and whether the resulting stale approval still counts toward
   branch protection depends on the repository's "dismiss stale pull request approvals"
   setting — so wherever that is off, an unchecked approval satisfies protection on code
   nobody read. The pin therefore makes the verdict *honest* but cannot by itself make a
   stale one fail. The refusal is the ROUTE's job, and it is the same shape as the merge
   gate: `_refuse_if_head_moved` re-reads the PR's live head and answers **409
   `review_conflict`** before the provider call, for both verdict verbs and for every
   pinned row of `/pulls/bulk` (there, as that row's `failed` entry, so the batch still
   applies and the row stays ticked for a retry). That re-read passes
   `resolve_mergeable=False`: it needs only `head_sha` (returned eagerly), so it must NOT
   pay GitHub's lazy-mergeability retry (a 1.5s sleep + a second call, which fires on the
   common cold-`unknown` read) — that runs per row of a bulk approve, so on a 50-PR
   approve the default path was ~75s of serialized sleep. Skipping it cannot weaken the
   pin: the read is still a live read of the current head. A plain `comment` review skips the
   check — it records no verdict, so it stays valid prose whatever the head does. An
   *unknown* live head is deliberately not a refusal: fail-closed on a read gap would
   cost the feature on a provider that reports no head without buying any safety, since
   the sha still rides to the provider. The UI does not
   offer the two verdict buttons until the detail read has told it the head commit;
   commenting is not a verdict and needs no pin.
   **In bulk this is per PR.** A bulk approve is N verdicts, so `/pulls/bulk` takes a
   `head_shas` map keyed by **number** (not a parallel array — a client that reorders or
   filters its selection would otherwise pair a sha with the wrong PR) and requires an
   entry for *every* number in the request. A partial map is a 400 rather than being
   honoured for the subset that has one: approving fewer PRs than the button's own count
   claims is its own defect. `_PINNED_BULK_PR_ACTIONS` names the verbs this applies to —
   close, comment and the auto-merge pair act on the pull request itself and mean the
   same thing after a push, so they take no sha. To make this possible without an extra
   round trip per row, the **list** payload carries `head_sha` on both providers
   (`github_client._PR_JQ`, `gitlab_client._norm_pull`), and the client builds the map
   from the rendered rows — the sha the user saw is the sha the approval applies to.
   **The client must snapshot that sha, not re-read it at submit time.** Both the
   detail and the pulls queries POLL, so reading the live value when the button is
   pressed let a force-push landing in the window re-point the verdict at the new head
   — and the server-side pin cannot catch that, because the request would carry the
   *new* sha and there would be nothing to refuse. So `PrActionsBar` freezes the sha
   when the composer OPENS (one `openComposer` helper, so the snapshot cannot be
   forgotten at one of three call sites) and `PrBulkBar` records each row's sha when it
   is TICKED (first observation wins; a row leaving the selection forgets it, so a
   re-tick picks up what is showing then). The snapshot is seeded during render rather
   than in an effect — a bar mounting with rows already ticked would otherwise have an
   empty map on its first pass and offer no approve at all. The freeze is
   per-composer/per-tick, not permanent: reopening after a real refresh names the new
   head. Three frontend tests pin the retarget cases.
   **The person-filtered view needed a second source.** That list is served by
   `/pulls/search`, and GitHub's search API does not expose the head commit — so the
   "assigned to me" view could not be bulk-approved even though the plain list could.
   Rather than a call per row, the sha rides on the by-number card enrichment
   (`_PR_SUMMARY_SELECTION` gained `commit{oid}`), which already walks the head commit
   for its check rollup; `_apply_summaries` fills the field **only when the row does
   not already have one**, so the list row's own sha — the one the user saw — is never
   replaced by a newer one the enrichment happened to read, and a failed enrichment
   leaves it alone rather than blanking it. `_PR_SEARCH_JQ` carries the key as `null`
   for row-shape parity. GitLab needs none of this: its search rows go through
   `_norm_pull` like every other row.
   `test_pr_actions.py::TestReviewIsPinnedToACommit` and `TestReviewRoutePinning` pin it.
3. **Bulk is a fixed allowlist, not a generic fan-out.** `_BULK_PR_ACTIONS` names the
   six verbs the bulk endpoint accepts. `request_changes` is per-PR only (a mass
   change-request carries no per-PR reasoning) and so is `merge` — irreversible, and
   50 from one click is a blast radius no confirmation makes reasonable; arming
   auto-merge is the bulk-safe equivalent. The batch runs SEQUENTIALLY — the PRs share
   one provider rate limit, and a 50-wide parallel fan-out is how a bulk click becomes
   a secondary-rate-limit block that fails rows for no reason of their own.
   The cap (`_BULK_PR_MAX` = 50) is **published** on every `/pulls` and
   `/pulls/search` response as `bulk_max`, and the client CHUNKS on it. Neither is
   optional: the server rejects an over-cap batch outright, so an unchunked "select
   all" on a repo with more open PRs than the cap was a flat 400 with nothing applied
   — and a hardcoded client copy of the number breaks silently the day the cap moves
   (the same reasoning `/tagging`'s `bulk_max` already documents).
4. **Partial failure is reported, never swallowed** — the same contract as
   `/labels/apply-bulk`: per-PR `applied` / `failed` lists, so one locked or
   already-merged PR does not discard the rows that succeeded, and the caller is never
   told about a write that did not happen. In the UI the SUCCEEDED rows are unticked
   and the failures stay selected, so a retry hits exactly the rows that still need it
   — keeping the whole selection would re-apply to the ones that already worked, which
   for `comment` posts a visible second copy.
   Relatedly, a refusal is **not an exception on every provider**: GitLab answers 200
   with a non-merged state and a `merge_error` when its approval rules say no, so the
   merge path checks `merged` before touching any cache. Trusting the return value
   would evict a still-open PR from the open list and report success.
5. **Every action is permission-gated and SEL-audited**, and a per-PR authorization
   refusal inside a bulk run is audited as `denied`, not `failure` — collapsing the two
   (they share an exception base) would make a refused mutation indistinguishable from
   a network timeout, so a query for `outcome=denied` returned nothing for the whole
   bulk surface.
6. **Every action drops the caches it invalidated.** A close/reopen — **and a
   merge**, which also closes the PR — removes the row from the list it left
   (`apply_pr_state_change_to_caches`) and drops the PR's detail entry; everything
   else drops just the detail (`drop_pr_detail_cache`). The merge path applies that
   change only after confirming the provider actually merged (see rule 4). Without
   this, `PR_DETAIL_CACHE_TTL_SEC` is long enough for a user to click a button and
   watch nothing happen.

**Provider divergence is refused, not approximated.** GitLab has no "request changes"
verb (the closest thing, unapproving, is not a verdict on a revision) and its
`/retry` only retries failed and canceled jobs — so `submit_pr_review` raises for
`REQUEST_CHANGES` and `rerun_workflow_run` reports `failed_only: true` regardless of
what was asked. Reporting a verdict the platform never recorded, or a full re-run
that did not happen, would be worse than the error.

**The sharpest instance, because it is a security property rather than a cosmetic
one: GitLab auto-merge is REFUSED outright.** GitLab has no independent "arm" verb —
`merge_when_pipeline_succeeds` is a *modifier on the merge endpoint*, and with no
pipeline in flight GitLab merges the MR immediately. A revision of this change tried
to contain that by reading the head pipeline first and arming only when a run was
live, but that check is **not atomic**: a pipeline finishing between the read and the
call turns the same request into an immediate merge. Since arming is offered as a BULK
action with no typed confirmation (it is advertised as reversible), losing that race
would merge a whole selection irreversibly — so `enable_auto_merge` /
`disable_auto_merge` raise on GitLab and the UI hides both controls there rather than
narrowing the window and hoping. The capability is relocated, not lost:
`merge_pull_request` covers "merge now", and GitLab's own web UI owns the deferred
case. An MR armed on GitLab still *displays* as armed, since the read-side
`auto_merge` detail field is unaffected.
On GitHub, where `enablePullRequestAutoMerge` is a real, separate mutation, arming is
offered normally — and `auto_merge` is derived from the returned `autoMergeRequest`
rather than asserted, because a hardcoded `True` is a claim rather than an observation.

**Merge readiness is on the LIST row, because a BULK action is what needs it.**
`enablePullRequestAutoMerge` is only valid for a PR that is *not landable yet*: GitHub
refuses one that is already mergeable (`Pull request is in clean status`) and one that
has already merged (`Pull request is already merged`). The bulk bar had no way to know
either — `_PR_JQ` carries no mergeability at all, only `_PR_DETAIL_JQ` did — so it
offered "arm auto-merge" for every ticked row and collected one provider refusal per
row from a single click. Six of seven failures in the observed case were PRs that were
simply READY, i.e. the operator's actual intent was to merge them.

So `_PR_SUMMARY_SELECTION` also requests `mergeable`, `state` and `mergedAt` — free,
because that selection already walks the head commit for its check rollup, the same
trick that gave SEARCH rows a `head_sha`.

**`mergeStateStatus` is NOT free, and travels in its own query.** It is the one field
here GitHub has to COMPUTE (a merge commit per PR) rather than read. Folded into the card
selection — which already walks each head commit and paginates the whole check rollup —
the combined query reliably **502s at `first:100`**. Measured, same page size, same repo:
the selection without it succeeds; with `mergeable`/`state`/`mergedAt` added it succeeds;
with `mergeStateStatus` added it fails every time. Alone, with no rollup and no commit
walk, the same field is comfortable at `first:100`.

The failure would not have been graceful. Both enrichment paths carry that selection, so
a 502 leaves every row with a null diff size *and* null check tally,
`enrichment_complete` returns `False` so the route declines to cache, the list refetches
on every load — and `mergeable_state` ends up `None` for all of them, leaving the bulk
bar exactly as blind as before. That trades a seven-refusal annoyance for a total
enrichment outage on the large repos most likely to use bulk actions. So readiness is a
second, LEAN call (`fetch_pr_readiness` / `fetch_pr_readiness_by_number`,
`_PR_READINESS_SELECTION`): one extra request per list fetch, independently failable, and
a failure costs only the readiness field. Its by-number batch is **50**, not the
summaries' 100, because the by-number form asks for N computed fields in one query.
`test_readiness_is_a_SEPARATE_query_from_the_card_enrichment` pins the field out of the
card selection, and `test_readiness_survives_a_FAILED_card_enrichment_and_vice_versa`
pins the independence.

**Both calls are topped up by number, and the top-up tests MEMBERSHIP.** The
state-scoped readiness query is capped at `first:100` like the summaries', while the REST
list paginates every open PR, so on a repo with more than 100 the tail carried no
readiness at all, and since unknown readiness is offered NEITHER verb those rows were
silently unactionable in the bulk bar, on exactly the large repos bulk actions exist for.
The top-up asks for the numbers whose key is ABSENT from the result, never the ones whose
*value* is falsy. `UNKNOWN` is a real answer rather than a missing one, recorded as the
string `'unknown'` (the `None` mapping belongs to the separate `mergeable` field), and it
is roughly half a cold page, so a truthiness test would re-request every such row on every
list fetch and get `UNKNOWN` back. `test_readiness_is_topped_up_past_the_hundred_row_window` and
`test_the_top_up_tests_MEMBERSHIP_not_truthiness` pin the two halves.

Three further properties are load-bearing:

- **Normalized into REST's vocabulary at the parse boundary.** GraphQL SHOUTS its enums
  (`CLEAN`, `MERGEABLE`, `OPEN`) where REST is lowercase, so `_parse_summary_rows`
  lowers them; `routes._MERGE_ALLOWED_STATES` and the frontend's `MERGE_READY_STATES`
  both compare lowercase, and an un-lowered `CLEAN` would match neither and read as
  "not ready" — silently keeping the broken arm on offer.
- **`UNKNOWN` stays unknown, and it is the COMMON case.** GitHub computes mergeability
  asynchronously, and on a cold read roughly **half a page** can come back `UNKNOWN`
  (measured). `_graphql_mergeable` maps it to `None`, never `False`, and a failed
  enrichment writes `mergeable_state: None` rather than omitting the key — an absent
  value is falsy, so it would read as "not ready" and put the row straight back into the
  batch the provider refuses. A row of unknown readiness is offered NEITHER verb: a gate
  that cannot tell must refuse. (The detail path retries after 1.5s for exactly this
  reason; the list path does not, so a first visit legitimately shows fewer actionable
  rows than a second.)
- **Four distinct refusals, not one.** The provider declines to arm a PR that is already
  mergeable, already merged, a **draft**, or `dirty` (a conflict — no check resolves it,
  so there is no "once checks pass" to wait for). `canArmAutoMerge` excludes all four;
  `canMergeNow` shares the same lifecycle gate via one `isOpenCandidate` helper rather
  than open-coding it, because two copies of a security-relevant predicate diverge — the
  first review of this change caught exactly that, one copy checking `draft` and the
  other not.
- **`MERGED` collapses to `closed` + a timestamp.** GraphQL has a third lifecycle state;
  REST models the same fact as `state: "closed"` with `merged_at` set, and the row shape
  is REST's. `_rest_pr_state` therefore returns `closed`, and `_apply_summaries` writes
  `merged_at` in the SAME step — `PrList.prStateVisual` checks `merged_at` first, so a
  `closed` with no timestamp renders as the red closed-unmerged icon, and a literal
  `"merged"` would match no branch at all and paint a merged PR as open. `merged_at` is
  only ever gap-filled, never overwritten.

**The ready rows get a merge path, and it is NOT the bulk endpoint.** Rule 3 keeps
`merge` out of `_BULK_PR_ACTIONS` — irreversible, and 50 from one click is a blast
radius no confirmation makes reasonable. But refusing to arm a ready PR while offering
it no way to land was the gap that made the original click fail, so the bulk bar offers
a **sequential** merge (`useSequentialMerge`) over exactly the rows the provider already
reports mergeable. It drives the ordinary per-PR `/pull/merge` route once per row, so
every property of that route still holds per row: the `head_sha` pin (snapshotted at
TICK time, never re-read at submit — the list polls), the server-side
`_MERGE_ALLOWED_STATES` re-read, the permission gate and the SEL audit. It is gated on
its own typed token, distinct from the bulk-close token so typing one cannot arm the
other, and it is **capped at the server's published `bulk_max`** — the loop does not use
the bulk endpoint, so nothing else would bound it, and rule 3's "50 from one click"
reasoning applies to a loop exactly as much as to a batch.

Four properties make it safe to offer, each one a defect a review found and closed:

- **The target set is FROZEN when the confirmation opens.** The list polls, so a
  readiness change landing between "type the token" and "press Apply" would otherwise
  change what gets merged — and since a cold read reports `unknown` (neither bucket) and
  resolves to `clean` (ready) moments later, a warning that said "1 pull request" could
  execute six. `armedMerge` holds the frozen set; the warning text and the Apply count
  both read it, so the number shown is the number that runs.
- **It stops at the first refusal.** Each merge changes the base branch, so a later PR's
  mergeability is a function of the earlier one having landed; continuing past a failure
  would merge onto a base that no longer matches what was reviewed.
- **Cancel ABORTS a run in progress**, not just the composer. The in-flight merge cannot
  be recalled — that request is with the provider — but every row after it is spared,
  which on an irreversible mass action is the difference between Cancel meaning something
  and meaning nothing. A second entry point (the confirm input's Enter) is guarded on
  `busy` too, or two loops advance independently and defeat stop-on-first-failure.
- **The per-row report is `aria-live`** and names the row in flight, because the rows
  stream in one at a time and "which PR did it stop on" is the only question that
  matters afterwards. The "N not attempted" line counts against the size the run
  STARTED with, since merged rows untick themselves and so leave the live target list.
- **The run's OWN selection churn is exempt from the selection-reset effect.** A
  successful merge changes the selection twice. The per-row invalidation refetches the
  list, so the merged PR leaves it (and `numbers` is intersected with what is RENDERED),
  and the run unticks it afterwards. That effect could not tell either from a user click,
  so it fired mid-run: it wiped the per-row report at the moment it completed, and its
  `reset()` set `aborted`, silently skipping every remaining row of a 3+ row confirmation
  with no refusal to explain it. Two changes close it, each independently load-bearing
  (each has its own failing test under mutation):
  - `mergedByRun` records what the run merged, and the REPORT key adds those numbers back
    rather than filtering them out, because a merged row was in the key before it merged, so
    removing it changes the key just as much as its disappearance did (`7,8` -> `8`);
    re-adding reproduces the pre-run key exactly. A genuine reselection still resets,
    because it moves a number the run never merged.
  - **TWO keys, and the exemption belongs to only one of them.** `selectionKey` is the
    LITERAL ticked set and disarms an in-progress action; `reportKey` carries the exemption
    and decides only whether the run report is stale. Folding them into one key let the
    exemption mask an ADDITION as well as the run's own disappearance: with `mergedByRun =
    {7}`, ticking a live #7 left the key unchanged, so a typed CLOSE confirmation stayed
    armed and Apply closed a PR whose count the warning never included. `mergedByRun` is
    also reset when `scopeKey` changes, since PR numbers are per-repo and this component is
    not remounted by a repo switch.
  - **The disarm stands down while a merge run is BUSY.** Cancel lives in the confirmation
    composer, which renders only while `pending` is set, and the first merged row changes
    the ticked set, so disarming on it took the only control that stops the remaining rows
    off screen mid-run. An irreversible loop has to stay interruptible for as long as it is
    running; the run clears the composer itself when it ends. This is independent of the
    per-row veto above, which reads the live selection rather than the composer.
  - the effect calls `clearReport`, which drops the stale report WITHOUT aborting;
    `reset` (which aborts) stays for Cancel and the explicit clear. A caller that only
    wants to tidy the UI must not be able to stop a merge in flight.
  The test harness feeds both feedbacks back (`renderWithLiveSelection`); the inert
  `vi.fn()` every other case passes is why this went unseen, and the mid-run drop is the
  half that reproduces the abort.
- **Unticking a QUEUED row still stops that row, as a per-row veto.** Exempting the run's
  own churn removed the one thing the old unconditional reset got right: an operator
  deselecting a PR the run has not reached yet is withdrawing consent for that PR, and the
  loop reads the frozen `armedMerge` precisely so a poll cannot change what executes, so
  nothing else would notice. `mergeAll` therefore asks `stillWanted(number)` against the
  LIVE ticked set as it reaches each row and skips a deselected one. It is a per-row veto,
  NOT `abort`: the rows after it were not withdrawn, and `abort` is what Cancel means. The
  predicate is ref-held so it never enters the selection effect's dependencies, which must
  stay keyed on the selection alone or an unrelated render wipes a confirmation mid-entry.
- **The confirmation names the frozen SET and the merge METHOD, not just a count.** A
  count cannot be checked against what the user believes they ticked, and the method is
  hardcoded, so a merge-commit repo would otherwise discover the squash only afterwards.
  The list is bounded by `bulk_max` so it is always short, goes through `fmtList` for the
  locale's own enumeration, and renders each number as raw digits behind the PROVIDER's own
  sigil (`terms.sigil`, so GitLab reads `!7`, matching the run report directly below it and
  the operator's own tab; merge-now is reachable on GitLab, only the two AUTO-merge buttons
  are gated there). Raw digits because a PR number is an IDENTIFIER: a grouping separator
  would both misrender it (`#1,291`) and break copying it. `SEQUENTIAL_MERGE_METHOD` is the
  single symbol behind both the copy and the request, so the two cannot drift. The line is
  NOT muted, being the only one that names WHICH pull requests are about to merge, and it
  wraps rather than truncating: clipping the identifiers would defeat the inspection it
  exists for. The confirmation input points at both lines with `aria-describedby` and takes
  focus when it arms.

This deliberately accepts bulk-merge's blast radius; the mitigations are the readiness
precondition, the cap, the frozen typed confirmation, and stop-on-first-failure.

## Refresh preferences

How fresh the app feels and how much of the provider's hourly request budget it spends
are the same dial, so the intervals are user-settable (Settings → General → Refresh)
rather than hardcoded. They persist in the app's `localStorage` UI state alongside the
filters — they are per-browser view preferences, not repo configuration.

| Setting | Default | Effect |
|---|---|---|
| List refresh | 60s | `refetchInterval` on the issue + PR lists |
| Detail refresh | 30s | `refetchInterval` on an OPEN item; a closed one keeps `CLOSED_DETAIL_POLL_MS` regardless |
| Use cached data for | 30s | react-query `staleTime` — how long a fetch counts as current on mount/focus |
| Keep refreshing in background tabs | off | `refetchIntervalInBackground`; off means a hidden tab stops and is stale on return |
| Load pull requests on open | off | lifts the `prSurfaceActive` gate so the first visit to the PR pane is instant |

Two safeguards, both deliberate:

- **The values are an allowlist, not a range.** `coerceInterval` accepts only an offered
  choice and falls back to the default otherwise, on WRITE as well as on read, so a
  hand-edited `localStorage` value cannot install one. `Infinity` matters specifically:
  react-query reads it as "no interval", so it would silently DISABLE polling rather than
  speed it up. `0` is a real choice for the cache lifetime (it means "always refetch"),
  which is why the check is `includes`, not truthiness.
- **The 30s list floor is twice the backend's probe-coalescing window, and that is the
  whole reason it is 30s.** The binding constraint is NOT the 5,000/hr core budget — a
  poll is probe-gated, so its steady-state cost is one search call, not a paginated
  refetch — it is GitHub's **30/min search quota**, which the probe spends and the user's
  own `gh search` shares. `_PROBE_COALESCE_SEC` (15s) shares one reading per (repo, kind)
  across every open tab, so a 30s interval costs at most 2 probes/min/kind however many
  tabs are open. Halve the floor and that stops holding. Nothing enforces the
  relationship across the language boundary, so `issueRadarPolling.test.ts` asserts
  `min(choices) == 2 × 15s`: if the backend constant moves, that test is what says this
  floor must move with it.
- **`/pulls/search` opts out of BOTH new knobs.** Prefetch does not reach it, and
  `refetchIntervalInBackground` is deliberately not applied to it either — it is the one
  query in the app that ignores that setting. Every other poll is probe-gated, so
  background polling costs one shared probe; this route has no probe path at all, so each
  poll is a real provider search (up to 3 pages) against the same quota with nothing to
  absorb it. Honouring the toggle would let a person filter someone left on months ago
  spend that quota indefinitely — exactly what its surface gate exists to prevent.

### Surfaces stay CACHED across a tab switch

Every dashboard mounts its own queries and unmounts them on the way out: the views are
SWAPPED, not hidden (`views/registry.tsx` maps a tab to one component). Data for an
unmounted query survives only `gcTime` past that unmount, and the dashboard-wide default is
react-query's **5 minutes**, which is shorter than an ordinary triage session. Leave the
Tagging dashboard for six minutes, come back, and its queue has been evicted: a loading
line, then a full refetch. Once per tab click.

`IssueRadarPage` therefore sets ONE query default for the whole `['issue-radar', ...]` key
space: `gcTime = CACHE_RETENTION_MS` (**30 min**). Four properties:

- **Set once, for the key space** rather than repeated across the ~30 query sites, because
  a per-site option is one a newly added query silently forgets. Every key in the app
  already starts with the `issue-radar` segment, which is what makes one default reach all
  of them.
- **Scoped to this app.** Raising the global default would retain every other page's
  queries too, which is memory spent on data nothing asked to keep.
- **Retention is not freshness.** `staleTime` and the poll intervals still decide when a
  refetch happens, so a longer `gcTime` only changes whether there is something to paint
  WHILE that refetch runs. It can never serve something stale *instead* of fetching.
- **Bounded, not `Infinity`**, so a long-lived tab that has visited many repos does not
  retain every one of their lists for the life of the session.

That is sufficient on its own because the surfaces gate their loading copy on `isLoading`,
which is false whenever data is present: a remount inside the retention window paints the
retained rows immediately and any refetch runs behind them. `issueRadarPolling.test.ts`
pins the retention, its scoping, and the not-pending property.

The lists additionally keep their previous rows on screen while a new key loads, so
changing a filter repaints instantly instead of blanking to a spinner. That costs no
extra requests — it only changes what is shown during a fetch that was already happening.

**Scoped to the repo, not plain `keepPreviousData`.** That helper retains the previous
query's rows for ANY key change, which conflates two different transitions: a filter
change is a different view of the SAME repo (retain — that is the point), but a REPO
SWITCH is not. A PR number means something different in each repo, so repo A's rows
painted under repo B's identity make a row actionable against B's PR of the same number.
`keepWithinRepo` compares the previous query key's `scopeKey` (provider + host + slug, so
a same-slug repo on another host is correctly a different repo) and returns `undefined`
across a switch, which renders the honest loading state. The ticked selection is already
cleared on `scopeKey`, but that effect runs *after* the paint — it closes the window one
render late rather than never opening it, which is why the placeholder itself is scoped.
`issueRadarPolling.test.ts` pins both halves.

`add_pr_comment` is a separate function from `add_issue_comment` even though the two
coincide on GitHub (one number sequence per repo): GitLab numbers issues and merge
requests INDEPENDENTLY, so a single shared entry point would be a silent way to
comment on an unrelated item. The `ProviderClient` protocol and the
`TestClientParity` surface list both.

The UI reads the PR detail's `auto_merge` field to decide whether it offers "enable"
or "cancel", which is why `PR_DETAIL_CACHE_SCHEMA` is at **v5** — a v4 entry has no
such key, and defaulting it to absent would show "enable" on an already-armed PR.
`PULLS_CACHE_SCHEMA` moved to **v6** for the same reason: the list row now carries
`head_sha`, and a v5 row served as-is would silently disable bulk approve for every
already-cached repo until its TTL expired — a broken-looking button rather than a
visibly stale list. It moved again to **v7** when the row gained `mergeable_state` /
`mergeable` (see "Merge readiness is on the LIST row" below): a v6 row has neither
field, and an absent value is indistinguishable from "not ready", so serving one would
keep offering exactly the arm that fails.
CI runs are fetched separately from `/pull`'s `checks`, because a check is a per-job
RESULT (and may come from a service with no runs at all) while cancel/re-run acts on
the parent RUN and needs its id.

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

## First Paint (progressive open-list load)

The open-issue list is **fully paginated**: `list_open_issues` follows every `Link`
page in one `gh --paginate` process (a ~2.6k-issue repo is ~26 sequential requests
under `GH_PAGINATE_TIMEOUT_SEC`), then writes a multi-MB cache. That cost is paid
in-band before the list can render, so a COLD open (first-ever open of a repo, a
freshly connected one, or after an `ISSUES_CACHE_SCHEMA` bump invalidates the cache)
blocked on a skeleton for seconds. Every WARM re-open is already instant — the list
cache has no TTL and is served immediately — so this is a cold-cache-only problem.

The **open-PR list is the same shape and worse**: `list_open_pulls` paginates every
page AND the route then runs `enrich_pulls` (the GraphQL summaries + readiness
families) before a byte can render, so a cold PR pane is the app's slowest open. Both
lists therefore carry a `first_page=1` fast path.

`GET /issues?first_page=1` (open state only) is the fast path that ends the blank
wait, handled by `_handle_issues_first_page`:

- **Warm cache → served whole, `partial: false`, no fetch.** When the full snapshot
  exists there is nothing to gain from a partial, and the fast path must not add a
  `gh` call the warm path does not pay.
- **Cold cache → the newest single page in ONE request, `partial: true`.**
  `list_open_issues_first_page` is `_list_issues(..., paginate=False)`: the SAME issue
  shape and `sort=updated` order as the full fetch, capped at one `per_page=100` page,
  on the ordinary `GH_TIMEOUT_SEC` rather than the paginate budget. Because it is the
  same first page the full fetch returns, the complete set appends BEHIND it with no
  reordering when it lands.
- **It never WRITES the cache.** The durable cache is owned by the full fetch, which
  stores the complete rows plus the poll `probe` under one lock. Persisting a partial
  here would let a later `poll=1` serve an INCOMPLETE list as verified-fresh (and with
  no probe), so this path is strictly read-only — its result lives only in the client's
  transient first-paint query.

The client (`context.tsx`) runs `firstPageQuery` only in the exact cold window —
open state, and the authoritative `issuesQuery` has produced nothing for the key yet
(`data === undefined`, which also covers a cross-repo switch where `keepWithinRepo`
yields undefined). Its rows feed `issues` (and clear the skeleton) ONLY until the full
list resolves, after which it is disabled and its rows are ignored. It deliberately
does **not** feed `issuesQuery.isSuccess`: the one-shot auto-select and the members
gate both key off that, and a partial page must not satisfy "the repo's issues are
loaded". The list footer shows an `issuesPartial` "loading the rest" hint so the count
does not read as the whole repo. Net cost: exactly one extra single-page request per
cold repo-open. `list_open_issues_first_page` is on the `ProviderClient` protocol, so
GitLab implements the symmetric single-page variant and `test_provider_parity` holds.

`GET /pulls?first_page=1` (open state only) is the PR twin, handled by
`_handle_pulls_first_page` with one added rule: the first page is returned
**UN-ENRICHED**. Enrichment is the other slow leg the fast path exists to skip, so it
is deliberately not paid here; a row's missing diff/check data renders as absent (the
card's bottom row is omitted), never as a wrong "no diff, no checks", and the
authoritative fetch that runs next enriches and caches. Warm cache → whole,
`partial: false`, no fetch; cold cache → the newest single page
(`list_open_pulls_first_page`, `_list_pulls(..., paginate=False)`), `partial: true`, no
cache write (the full fetch owns the durable cache and refuses to persist incomplete,
un-enriched rows). The client runs `pullsFirstPageQuery` only in the exact cold window —
open state, **no person filter** (search owns that path and is already whole-repo), the
PR surface actually in use (same gate as `pullsQuery`, so no request is spent on an
unopened pane), and `pullsQuery.data === undefined`. Its rows feed `pulls` and clear the
skeleton until the full list lands; the footer shows a `pullsPartial` "loading the rest"
hint. `list_open_pulls_first_page` is on the `ProviderClient` protocol (GitLab's variant
is card-complete already, since it inlines `head_pipeline`), and `test_provider_parity`
holds.

`enrich_pulls` runs its two INDEPENDENT GraphQL families — card summaries and merge
readiness — **concurrently** on a two-worker `ThreadPoolExecutor` rather than
back-to-back. They must stay two calls (readiness cannot ride on the card selection
without 502ing it) and neither derives from the other, so overlapping their blocking
`gh` round trips makes the enrichment leg cost the slower family instead of their sum.
Each family (`_enrich_summaries` / `_enrich_readiness`) swallows its own `GhCliError`
internally, preserving the best-effort contract: one failing does not sink the other.

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

### The label cache expires, because an unbounded one reads as a TRUNCATED list

`labels-cache.json` had **no expiry**, and unlike the issue/PR lists nothing polls the
`/labels` query (it is a plain `useQuery` with no `refetchInterval`). So the first fetch
of a repo was served forever: a label created on GitHub afterwards, by a teammate or by
automation, was absent from the left-rail palette, the filter list and every picker until
the user happened to press Refresh. That presents as an **incomplete label set** rather
than a stale one, which is the reason it is worth a TTL: nothing on screen says the list is
partial, and the missing labels are silently unfilterable, so the user's conclusion is "the
app does not show all my labels".

`LABELS_CACHE_TTL_SEC` is **600s**, and `read_labels_cache` treats an older file as a MISS
so the route refetches. Three properties, each with its own test:

- **The TTL is the DEFAULT, not a per-caller argument.** Freshness is a property of the
  cache (the same rule `read_pr_detail_cache` follows), so a new route cannot forget it.
  `max_age_sec=None` is the explicit opt-out, and `add_label_to_cache` is the one caller
  that takes it, because it patches whatever is on disk, and reading an expired file as absent
  there would silently drop the append of a just-created label.
- **The age comes from a `fetched_at` stamp INSIDE the payload, not from mtime**, because
  `add_label_to_cache` rewrites the file without refetching. This is the same trap
  `_list_cache_age_sec` documents for the list caches, and it matters more here: the
  append carries the ORIGINAL stamp through, so creating labels in a repo cannot keep
  deferring the refetch that picks up everyone *else's* labels.
- **A pre-stamp cache falls back to mtime, on BOTH paths.** Such a file was only ever
  written by a real fetch, so its mtime *is* its fetch time: it must age out rather than be
  treated as ageless on read, and the write-through append must carry that mtime over
  rather than stamping the current time. Stamping there reset the TTL clock, so a cache nine
  minutes into its ten-minute life got a fresh ten and one label creation per interval could
  defer the refetch indefinitely.

A plain TTL rather than the lists' probe-gated poll: labels are ONE `per_page=100` request
against the 5,000/hr core budget (not the 30/min search quota the probes share), so the
worst case is ~6 requests an hour per open repo, and a probe here would cost about as much as
the refetch it guards. Both providers already paginate the label fetch (`--paginate` on
GitHub, explicit `page=N` walking on GitLab), so a repo with more than 100 labels was never
the truncation; the cache was.

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
