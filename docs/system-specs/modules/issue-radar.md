# Issue Radar Module

Last Updated: 2026-08-01

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
| POST | `/pull/auto-merge` | Arm or disarm the PROVIDER's own auto-merge, for a PR that is not mergeable yet. **GitHub only** — REFUSED on GitLab, where `merge_when_pipeline_succeeds` is a deferral modifier on the merge endpoint rather than an arm verb (see "GitLab auto-merge is REFUSED outright" below); the UI hides both controls there |
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
   `/ai-review override` comment plus the `defer-longterm` label), and shedding a
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
   applies and the row stays ticked for a retry). A plain `comment` review skips the
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
visibly stale list.
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
