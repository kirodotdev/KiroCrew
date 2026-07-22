# Code Review Sage — Scale & CPU Optimization Plan

Status: proposed · Owner: KiroCrew contributors · Scope: `apps/builtins/code_review_sage` + `acp/` + `website/src/apps/code-review-sage`

## Problem

1. **CPU "roar"** when reviewing. Root cause (verified, not a spin loop):
   - Up to 5 full kiro-cli **subprocesses** at once (`review_pool.py` `MAX_CONCURRENT=5`).
   - Convergence loop stacks ~5 agent turns/PR (`review_driver.py` `max_review_rounds` default 3).
   - Full **process respawn on every worker reuse** (`AcpReviewWorker.reset()` = `shutdown()`+`start()`), because `AcpClient` has no in-process reset.
2. No way to point at a **repo** and review all open PRs; only pasted PR links.
3. Pool = one subprocess per worker → cannot scale concurrency without multiplying processes.

## Decisions (locked)

- **D1 Sequencing:** #2 (shared runtime) first, then #1 (repo review). #2 makes #1 safe to scale.
- **D2 Single, batch-scoped runtime:** exactly **one shared `AcpRuntime`** at a time, multiplexing all in-flight reviews as **one `AcpSessionHandle` per PR** (isolated context, no cross-PR pollution). Lifecycle is tied to the batch, tracked by an in-flight counter under a lock:
  - Lazy `spawn()` on the first review task when no runtime is running.
  - `create_session()` per PR; `handle.destroy()` as each PR finishes (frees that session's memory during large batches).
  - When the in-flight count drains to **zero (all reviews done)**, `shutdown()` the entire runtime (kills the subprocess → reclaims all RSS). A new review task spawns a fresh runtime.
  - This makes the "no per-turn compaction" RSS caveat moot — memory is bounded to a single batch and released on drain. No stale-threshold recycling needed.
- **D3 Concurrency:** default stays **5**, configurable up to **~30** via `review.max_concurrent`. "Review all" does not implicitly raise it.
- **D4 Dedup:** a PR is "not yet reviewed" if absent from the reviewed-index **or its head SHA changed**. Force-all ignores the index.

---

## Phase 0 — Immediate CPU relief (config only, no structural change)

Make the three amplifiers tunable and default them gentler-friendly:
- `review.max_concurrent` (new, default 5) — replaces the hardcoded `MAX_CONCURRENT` constant as source of truth (driver fan-out + pool both read it).
- Confirm `review.max_review_rounds` (exists, default 3) and `review.effort` are surfaced in settings.

Deliverable: config plumbing + settings UI already exposes model/effort; add max_concurrent + max_review_rounds sliders. Ships value even before the re-architecture.

## Phase 1 — #2 Shared AcpRuntime migration (the core CPU fix)

Goal: replace the `AcpClient`-per-worker pool with **one shared, batch-scoped `AcpRuntime`** hosting one `AcpSessionHandle` per PR.

Files:
- `sage_lib/review_pool.py` — rewrite the worker abstraction:
  - A single-runtime holder guarded by a lock + an **in-flight counter**: lazy `spawn()` when the count goes 0→1; `shutdown()` when it drains 1→0 (batch-scoped teardown).
  - `AcpReviewWorker` becomes a thin per-PR wrapper: `runtime.create_session(...)` for the PR, run the review on that handle, `handle.destroy()` on completion.
  - Per-PR isolation is the distinct `sessionId`; no more `reset()` respawn.
  - Concurrency bounded by a `review.max_concurrent` semaphore over session-handles (still one subprocess regardless of width).
- `acp/runtime.py` / `session_handle.py` — reuse as-is (API confirmed: `spawn`, `create_session`, `handle.prompt`, `handle.destroy`, `shutdown`, `has_active_sessions`). No core edits expected.
- `review_driver.py` — dispatch unchanged in shape; fan-out width reads `review.max_concurrent`.

Guard: a stuck session must not block batch teardown — the existing `DEFAULT_TASK_TIMEOUT` bounds each task; on timeout, force `handle.destroy()` and decrement the in-flight count so the runtime can still drain to zero and shut down.

Tests: adapt `code_review_sage/tests/` pool tests to the single-runtime model; add a concurrency test (N sessions on the one runtime), a `destroy()`-on-finish test, and a **batch-drain teardown** test (runtime `shutdown()` fires when the last review completes; a new task spawns a fresh runtime).

Risk: RSS growth (no per-turn compaction) → mitigated by D2 (per-PR `destroy()` + whole-runtime teardown on batch drain).

## Phase 2 — #1 Repo review + reviewed-index

Files:
- `sage_lib/adapters.py` — add repo-URL parse + hostname allowlist (`github.com/<owner>/<repo>` with no `/pull/`), mirroring `detect_platform`.
- `sage_lib/pipeline.py` — `list_open_prs(repo)` via backend `gh api repos/<o>/<r>/pulls?state=open --paginate` (token-free, host `gh` auth), returning `[{url, number, head_sha, title}]`.
- `sage_lib/results.py` (or `store.py`) — new durable **reviewed-index** `data/reviewed.json`: `{ "GH-<owner>-<repo>-<n>": {"head_sha", "reviewed_at", "run_id"} }`, atomic-write + 0600 (mirror existing pattern).
- `backend/routes.py`:
  - `POST /review-repo` `{repo, skip_reviewed=true, max_concurrent?}` → enumerate → dedup (unless `skip_reviewed=false`) → `run_review(changes=...)`.
  - `GET /repo-prs?repo=...` → list open PRs annotated with reviewed/not-reviewed (+ stale-SHA) for the UI.
  - After a run finishes, write reviewed-index entries (change-id + head SHA).

Tests: repo-URL parsing + allowlist; enumeration parsing; dedup by head-SHA (new / unchanged / SHA-changed / force-all); index write/read.

## Phase 3 — Frontend (`website/src/apps/code-review-sage/CodeReviewSagePage.tsx`)

- Add a **repo-link input** alongside the existing paste box.
- On enter: `GET /repo-prs` → render open PRs with **reviewed / not-reviewed / updated** badges.
- **"Review all"** button → `POST /review-repo` with `skip_reviewed=true`; a **"Force review ALL"** secondary → `skip_reviewed=false`.
- A **concurrency control** (slider/select, default 5, max from backend) wired to `review.max_concurrent`.
- Reuse existing `/runs` polling + Focus Report display unchanged.

Verify: `tsc -b` + touched vitest specs.

## Phase 4 — Verify & ship

- Backend: `pytest code_review_sage/tests` + `test_governance_*` unaffected; `flake8` on touched files.
- Frontend: `tsc -b`, `npm run build`.
- Manual: point at a repo with several open PRs; confirm dedup, force-all, and that concurrency cap holds; watch process count (should be ≤ pool size, not per-PR) and CPU.

## Out of scope / caveats

- 30 *simultaneous* agent turns is still real load; the win is process/memory overhead + churn, not eliminating inference cost. Concurrency stays capped/configurable.
- GitLab/other hosts: enumeration is GitHub-only in v1 (matches current `gh`/GitHub-only posting).
