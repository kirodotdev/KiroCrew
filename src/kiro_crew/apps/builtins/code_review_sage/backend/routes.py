"""Deterministic backend route for the Code Review Sage builtin.

Kicks off the two-stage review driver **in-process** (no LLM orchestrator), so
the Phase 1 -> Phase 2 switch AND the finalize step (report -> archive -> clean)
are guaranteed to run as Python, never left to an LLM session that can deviate.
The only LLM work is the per-change gate/deep tasks the driver dispatches to a
reusable worker pool (``sage_lib/review_pool.py``) — long-lived ACP sessions reused
across CRs, not per-CR ``/api/spawn`` sub-agents — so reviews run silently.

Why this exists: when an LLM orchestrator session was merely *asked* to shell
``review_driver.py``, it could instead run Phase 1 inline and stop — leaving a
gate-only record, a stale report, and no Phase 2 (observed in practice).
Routing the kickoff through this route removes that discretion entirely.

Registered at gateway startup by ``apps/routes.py:register_app_routes`` (loaded
by file path because the app dir name ``code-review-sage`` is hyphenated and
cannot be imported as a Python package).

Routes (browser-facing, same-origin authed exactly like ``/config``):
  POST /api/apps/code-review-sage/review   {links | changes} -> {run_id, changes}
  GET  /api/apps/code-review-sage/runs                       -> {runs: [...]}

The ``/runs`` registry lets the page render live status and reconstruct it after
navigating away (the backend owns the run, not ephemeral React state). Per-change
detail still comes from the on-disk result records the driver writes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from aiohttp import web

logger = logging.getLogger("kirocrew.app.code-review-sage")

# The app root (.../apps/builtins/code-review-sage) holds ``sage_lib/`` next to this
# ``backend/`` dir. Put it on sys.path so ``from sage_lib import review_driver`` works
# the same way the driver resolves its own siblings.
_APP_ROOT = Path(__file__).resolve().parent.parent
if str(_APP_ROOT) not in sys.path:
    sys.path.insert(0, str(_APP_ROOT))

# Sibling app modules — importable now that the app root is on sys.path (the app
# dir is hyphenated, so these are not auto-discovered packages). Imported at the
# top per the top-level-imports guideline; the sys.path setup above executes at
# module load, so this resolves on first import.
from sage_lib import (  # noqa: E402,E501
    adapters,
    learning,
    pipeline,
    results,
    review_driver,
    review_pool,
    store,
)

# In-memory run registry, most-recent first. Bounded so it can't grow unbounded.
# Holds lightweight run descriptors the page polls; on-disk result records carry
# per-change detail.
_RUNS: list[dict[str, Any]] = []
_RUNS_MAX = 25
_LOCK = asyncio.Lock()
# Serializes whole review runs. run_review clears + writes the SHARED data/results
# dir and the report index, so two overlapping runs (from /review and /review-repo,
# or two /review-repo calls) would clobber each other's records/report. Concurrent
# starts queue on this lock instead of interleaving.
_RUN_LOCK = asyncio.Lock()
# Guards copy-on-write updates to a run's per-change ``progress`` map, which the
# (threaded) driver writes and the /runs handler reads concurrently.
_PROGRESS_LOCK = threading.Lock()
# Keep strong refs to background tasks so they aren't garbage-collected mid-flight.
_TASKS: set[asyncio.Task] = set()  # type: ignore[type-arg]


def _make_progress(run: dict):
    """Build a thread-safe progress callback the driver calls as each change moves
    through its phases (queued -> gating -> deep -> done/blocked/failed). Updates
    are copy-on-write so the /runs reader never sees a half-mutated dict."""
    def cb(change_id: str, phase: str, extra: dict | None = None) -> None:
        with _PROGRESS_LOCK:
            prog = dict(run.get("progress") or {})
            entry = {"phase": phase}
            if extra:
                entry.update(extra)
            prog[str(change_id)] = entry
            run["progress"] = prog
    return cb


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# --- Durable job-status store -------------------------------------------------
# The run registry is the ONLY thing we persist: it lets the page reflect the
# current/last status of review jobs across navigation AND gateway restarts,
# instead of starting from scratch. Per-change result records are transient
# scratch the driver clears after each run — they are NOT a durable store and
# are never used for cross-run dedup (re-reviewing the same change is expected).


def _runs_file() -> Path:
    return store.data_dir() / "runs.json"


def _save_runs() -> None:
    """Atomically persist the run registry (0600). Never raises."""
    try:
        f = _runs_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(_RUNS, indent=2), encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, f)
    except Exception:  # pragma: no cover - defensive
        logger.warning("failed to persist runs.json", exc_info=True)


def _load_runs() -> None:
    """Load persisted runs on startup. Any run still marked ``running`` is
    re-marked ``interrupted`` — its in-process driver thread did not survive the
    restart, so it can't be resumed and must not show as live."""
    global _RUNS
    try:
        f = _runs_file()
        if not f.is_file():
            return
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return
        for r in data:
            if isinstance(r, dict) and r.get("status") == "running":
                r["status"] = "interrupted"
                r["error"] = "Interrupted by a gateway restart — re-run the review."
                r.setdefault("finished_at", _now())
        _RUNS = data[:_RUNS_MAX]
    except Exception:  # pragma: no cover - defensive
        logger.warning("failed to load runs.json", exc_info=True)


async def _record(run: dict) -> None:
    async with _LOCK:
        _RUNS.insert(0, run)
        del _RUNS[_RUNS_MAX:]
        _save_runs()


def _dedup_changes_under_lock(run: dict, changes: list[str]) -> list[str]:
    """Re-filter a repo-review run's ``changes`` against the CURRENT reviewed index,
    dropping any PR already reviewed at the exact head SHA this run intended to
    review. Called under ``_RUN_LOCK`` to close the TOCTOU where two concurrent
    repo-reviews both deduped against a stale index before the lock (the second
    would otherwise re-review + re-post a PR the first just delivered).

    No-op for forced runs and pasted-link runs (no ``head_shas``). Keeps
    ``run['changes'|'change_ids'|'head_shas']`` consistent with the kept subset so
    ``_record_reviewed`` still round-trips. Sync (reads the index) — call via a
    thread from the event loop.
    """
    head_shas = run.get("head_shas") or {}
    if run.get("force") or not head_shas:
        return changes
    index = results.read_reviewed()
    kept: list[str] = []
    kept_shas: dict[str, str] = {}
    for url in changes:
        rkey = review_driver.reviewed_key_for(url)
        intended = head_shas.get(rkey, "")
        rec = index.get(rkey) or {}
        if intended and rec.get("head_sha") == intended:
            continue   # a concurrent run already reviewed this exact head
        kept.append(url)
        if rkey in head_shas:
            kept_shas[rkey] = head_shas[rkey]
    run["changes"] = kept
    run["change_ids"] = [review_driver.change_id_for(c) for c in kept]
    run["head_shas"] = kept_shas
    return kept


async def _run_review_bg(run: dict, changes: list[str]) -> None:
    """Run the (blocking) driver in a worker thread; update the run record.

    The driver owns the whole lifecycle: gate task -> Python phase switch ->
    deep task -> report -> archive -> clean. Each task is dispatched to the
    shared, reusable worker pool (``review_pool``) — long-lived ACP sessions,
    not per-CR ``/api/spawn`` sub-agents — so reviews run silently (no agent
    card, no ``:lock:``, no Slack relay) and warm workers are reused across CRs
    with a clean-slate reset between them. We just surface the driver's summary."""
    try:
        # Serialize whole runs (see _RUN_LOCK): concurrent starts queue here rather
        # than interleaving over the shared results dir / report index.
        async with _RUN_LOCK:
            # TOCTOU guard: the dedup in _handle_review_repo ran
            # BEFORE this lock, so a concurrent repo-review that finished first may
            # have just recorded some of these PRs. Re-dedup against the now-current
            # reviewed index under the lock so we never re-review + re-post a PR that
            # another run already delivered. No-op for forced / pasted-link runs.
            changes = await asyncio.to_thread(_dedup_changes_under_lock, run, changes)
            if not changes:
                run["summary"] = {"ok": True, "changes": 0,
                                  "note": "all PRs already reviewed by a concurrent run"}
                run["status"] = "done"
                return
            # Bridge the threaded driver to the async pool running on THIS (gateway)
            # event loop. The pool is a lazily-created process-wide singleton.
            loop = asyncio.get_running_loop()
            pool = review_pool.get_pool()
            dispatch = review_pool.make_sync_dispatch(loop, pool)

            # Bracket the batch: begin_batch() lazily spawns the ONE shared runtime;
            # end_batch() (in finally) kills it once this run's reviews all drain — so
            # the subprocess (and its memory) lives exactly as long as the batch.
            await pool.begin_batch()
            try:
                summary = await asyncio.to_thread(
                    review_driver.run_review, changes,  # type: ignore[attr-defined]
                    dispatch=dispatch, progress=_make_progress(run),
                )
            finally:
                await pool.end_batch()
            run["summary"] = summary
            run["report_slug"] = summary.get("report_slug") or run.get("report_slug")
            # "done" even if some changes failed — the failures are surfaced in the
            # summary; "error" is reserved for the driver itself blowing up.
            run["status"] = "done" if summary.get("ok") else "error"
            if not summary.get("ok"):
                run["error"] = summary.get("error", "review failed")
            else:
                # Durable dedup index: record each reviewed PR's head SHA so a later
                # repo-review skips it until its head changes. Only repo-review runs
                # carry head_shas; pasted-link runs skip this (no-op).
                await asyncio.to_thread(_record_reviewed, run)
    except Exception as exc:  # pragma: no cover - defensive
        run["status"] = "error"
        run["error"] = str(exc)
        logger.warning("code-review-sage run %s failed: %s", run.get("run_id"), exc, exc_info=True)
    finally:
        run["finished_at"] = _now()
        async with _LOCK:
            _save_runs()


async def _handle_review(request: web.Request) -> web.Response:
    """POST /api/apps/code-review-sage/review — start a deterministic review run.

    Body: ``{"links": "<pasted CR links>"}`` or ``{"changes": ["CR-1", ...]}``.
    Returns immediately with a ``run_id``; poll ``/runs`` for status."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    changes: list[str] = []
    raw = body.get("changes")
    if isinstance(raw, list):
        changes = [str(c).strip() for c in raw if str(c).strip()]
    links = body.get("links") or body.get("input") or ""
    if not changes and links:
        changes = pipeline.parse_batch(str(links))

    if not changes:
        return web.json_response(
            {"error": "no reviewable changes — paste one or more PR/CR links"},
            status=400,
        )

    run: dict[str, Any] = {
        "run_id": uuid.uuid4().hex[:12],
        "changes": changes,
        # Same keys the driver writes progress under (GH-<owner>-<repo>-<n>), so the
        # dashboard can align each row with its live phase instead of falling back to
        # a permanent "queued". Paired positionally with ``changes`` for row hrefs.
        "change_ids": [review_driver.change_id_for(c) for c in changes],
        "status": "running",
        "started_at": _now(),
        "progress": {},
    }
    await _record(run)

    task = asyncio.create_task(_run_review_bg(run, changes))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)

    return web.json_response(
        {"run_id": run["run_id"], "changes": changes, "status": "running"}
    )


def _record_reviewed(run: dict) -> None:
    """Upsert this run's SUCCESSFULLY-reviewed PRs into the durable dedup index
    (by head SHA), so a later repo-review skips them until their head changes.

    Only PRs whose deep review actually ran + recorded are marked — a gate crash,
    no-verdict, or dispatch failure must NOT be written to the index, otherwise the
    next non-forced repo-review would silently skip a PR that was never really
    reviewed. (``run_review`` returns ``ok=True`` for any run with >=1 change even
    when individual changes failed, so we intersect ``head_shas`` with the
    per-change outcomes rather than trusting the run-level ok.) Best-effort — a
    failure here never fails the run. No-op for runs without a ``head_shas`` map
    (i.e. pasted-link reviews, where we don't know each PR's head SHA)."""
    shas = run.get("head_shas") or {}
    if not shas:
        return
    per_change = (run.get("summary") or {}).get("per_change") or []
    # per_change records carry the (lossy) change_id; head_shas is keyed by the
    # collision-free reviewed key. Map change_id -> reviewed key via the run's
    # parallel `changes` (URLs) so we can intersect the two keyings and index
    # ONLY the PRs whose deep review recorded a result AND whose draft was
    # actually delivered are indexed. `post_ok` alone is insufficient: it only
    # means the poster AGENT's turn ended normally, not that `gh api` succeeded
    # (a 422 / network error can still end the turn cleanly). So additionally
    # require the poster to have persisted at least the EXPECTED comment count
    # (`posted_comments >= posting_expected`, both written by the deep-review
    # branch). A failed/partial post leaves posted_comments below expected -> the
    # PR is NOT indexed and is re-reviewed next time (fail-safe: re-review, never
    # silent-skip a PR that was never really delivered).
    changes = run.get("changes") or []
    cid_to_rkey = {
        review_driver.change_id_for(u): review_driver.reviewed_key_for(u)
        for u in changes
    }
    reviewed_ok = {
        cid_to_rkey.get(r.get("change_id"))
        for r in per_change
        if r.get("deep_reviewed") and r.get("post_ok")
        and (r.get("posted_comments") or 0) >= (r.get("posting_expected") or 1)
    }
    reviewed_ok.discard(None)
    now = _now()
    rid = run.get("run_id", "")
    entries = {rkey: {"head_sha": sha, "reviewed_at": now, "run_id": rid}
               for rkey, sha in shas.items() if sha and rkey in reviewed_ok}
    if not entries:
        return
    try:
        results.mark_reviewed(entries)
    except Exception:  # pragma: no cover - defensive
        logger.warning("code-review-sage: failed to update reviewed index", exc_info=True)


async def _list_repo_prs(repo: str) -> tuple[str, list[dict]]:
    """Resolve owner/repo from a repo URL and enumerate its OPEN PRs (via `gh`).
    Returns ``("<owner>/<repo>", prs)``. Raises ValueError for a bad URL and
    RuntimeError for a `gh` failure (mapped to 400/502 by the handlers)."""
    owner, name = adapters.parse_repo_url(repo)   # raises on non-GitHub / no owner/repo
    prs = await asyncio.to_thread(pipeline.list_open_prs, owner, name)
    return f"{owner}/{name}", prs


async def _handle_repo_prs(request: web.Request) -> web.Response:
    """GET .../repo-prs?repo=<url> — list a repo's OPEN PRs annotated with
    reviewed / not-reviewed / stale (by head SHA). Does NOT start a review."""
    repo = (request.query.get("repo") or "").strip()
    if not repo:
        return web.json_response({"error": "missing ?repo=<github repo url>"}, status=400)
    try:
        slug, prs = await _list_repo_prs(repo)
    except (adapters.AdapterParseError, adapters.UnsupportedPlatform, ValueError) as e:
        return web.json_response({"error": f"invalid repo url: {e}"}, status=400)
    except Exception as e:  # gh not authed / network / repo not found
        logger.warning("repo PR list failed: %s", e, exc_info=True)
        return web.json_response({"error": "upstream service error"}, status=502)
    index = await asyncio.to_thread(results.read_reviewed)
    out = []
    for pr in prs:
        url = pr.get("url", "")
        cid = review_driver.change_id_for(url)      # display / response field only
        # Read the dedup index with the SAME collision-free key it is written
        # under (reviewed_key_for), NOT the lossy change-id — otherwise every
        # reviewed PR reads back as "new" (read/write key mismatch).
        rkey = review_driver.reviewed_key_for(url)
        rec = index.get(rkey) or {}
        stored = rec.get("head_sha") or ""
        cur = pr.get("head_sha") or ""
        out.append({
            **pr, "change_id": cid,
            "reviewed": bool(stored) and stored == cur,
            "reviewed_stale": bool(stored) and stored != cur,
            "reviewed_at": rec.get("reviewed_at", ""),
        })
    return web.json_response({"repo": slug, "prs": out, "count": len(out)})


async def _handle_review_repo(request: web.Request) -> web.Response:
    """POST .../review-repo — review all OPEN PRs of a repo in one batch.

    Body: ``{"repo": "<github repo url>", "force": bool}``. By default only PRs
    NOT yet reviewed at their current head SHA are queued; ``force=true`` reviews
    ALL open PRs regardless of the dedup index."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    repo = str(body.get("repo") or "").strip()
    # Strict boolean: only a literal JSON `true` forces a full re-review. A string
    # ("false"), object, or number must NOT accidentally bypass dedup and trigger a
    # costly review of every open PR.
    force = body.get("force") is True
    if not repo:
        return web.json_response({"error": "missing 'repo' (a github repo url)"}, status=400)
    try:
        slug, prs = await _list_repo_prs(repo)
    except (adapters.AdapterParseError, adapters.UnsupportedPlatform, ValueError) as e:
        return web.json_response({"error": f"invalid repo url: {e}"}, status=400)
    except Exception as e:
        logger.warning("repo review-repo failed: %s", e, exc_info=True)
        return web.json_response({"error": "upstream service error"}, status=502)

    index = await asyncio.to_thread(results.read_reviewed)
    changes: list[str] = []
    head_shas: dict[str, str] = {}
    skipped = 0
    for pr in prs:
        url = pr.get("url") or ""
        if not url:
            continue
        # Dedup by a COLLISION-FREE canonical key (NOT the lossy change-id, which
        # sanitizes '-'->'_' and would let acme/service-api and acme/service_api
        # share one reviewed.json entry). head_shas is keyed the same way so
        # _record_reviewed round-trips against this index.
        rkey = review_driver.reviewed_key_for(url)
        cur = pr.get("head_sha") or ""
        if not force:
            rec = index.get(rkey) or {}
            if rec.get("head_sha") and rec.get("head_sha") == cur:
                skipped += 1
                continue   # already reviewed at this exact head SHA
        changes.append(url)
        head_shas[rkey] = cur

    if not changes:
        return web.json_response({
            "repo": slug, "changes": [], "skipped": skipped, "status": "noop",
            "message": "all open PRs already reviewed at their current head "
                       "(use force=true to re-review all)",
        })

    run: dict[str, Any] = {
        "run_id": uuid.uuid4().hex[:12],
        "repo": slug,
        "changes": changes,
        "change_ids": [review_driver.change_id_for(c) for c in changes],
        "head_shas": head_shas,        # consumed by _record_reviewed on success
        "force": force,                # skip the under-lock re-dedup for a forced run
        "status": "running",
        "started_at": _now(),
        "progress": {},
    }
    await _record(run)
    task = asyncio.create_task(_run_review_bg(run, changes))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return web.json_response({
        "run_id": run["run_id"], "repo": slug,
        "changes": changes, "skipped": skipped, "status": "running",
    })


async def _handle_runs(request: web.Request) -> web.Response:
    """GET /api/apps/code-review-sage/runs — current + recent runs (most-recent
    first) plus live worker-pool occupancy, so the page can show rich progress."""
    async with _LOCK:
        runs = list(_RUNS)
    pool: dict | None = None
    try:
        pool = review_pool.pool_stats()
    except Exception:  # pragma: no cover - defensive
        pool = None
    reviewer: dict | None = None
    try:
        # reviewer_info() reads config.json + the agent json — offload its sync
        # file IO off the event loop (consistent with _handle_settings).
        reviewer = await asyncio.to_thread(review_pool.reviewer_info)
    except Exception:  # pragma: no cover - defensive
        reviewer = None
    return web.json_response({"runs": runs, "pool": pool, "reviewer": reviewer})


# --- Settings (model / effort / active namespaces) ---------------------------
# These let the dashboard read + write the review knobs that live in config.json
# under the "review" section. The generic GET /api/apps/{name}/config already
# exposes the full config (read), so this route is the WRITE path plus a focused
# settings view that also enumerates available models, efforts, and namespaces.

def _load_known_models() -> list[str]:
    """Selectable models for the review-settings dropdown — the registry's
    CANONICAL keys (e.g. ``opus-4.8-1m``), which are the wire/persisted format
    the review worker consumes, NOT the provider ids from ``available_models``.

    Provider ids carry a ``[1m]`` capability suffix (e.g.
    ``global.anthropic.claude-opus-4-8[1m]``); the brackets fail ``_valid_model``'s
    safe-token check, so sourcing the dropdown from provider ids made every 1M
    variant unselectable (the PUT 400'd and the dropdown snapped back — only the
    bracket-free plain ids survived). Canonical keys are bracket-free tokens that
    both pass validation AND match what ``review_pool`` writes into the worker's
    cli.json overlay. Empty on failure; the UI still offers 'Default (agent config)'."""
    try:
        from kiro_crew import model_registry
        return [row["model_name"] for row in model_registry.display_list("claude_code")]
    except Exception:  # pragma: no cover - defensive
        return []


# Computed once at import (the registry is immutable after load). A module-level
# constant so the settings validator and the /settings enumerator share one list.
_KNOWN_MODELS: list[str] = _load_known_models()


def _known_models() -> list[str]:
    """Back-compat accessor for the known-model allowlist (the constant above)."""
    return _KNOWN_MODELS


def _valid_model(m: str) -> bool:
    """A model id is acceptable if it is a safe token (it becomes a cli.json
    overlay key for the worker subprocess) and, when the registry is available,
    is one it knows."""
    if not m or len(m) > 64 or not all(c.isalnum() or c in "._-" for c in m):
        return False
    known = _KNOWN_MODELS
    return (m in known) if known else True


def _load_review_section() -> dict:
    """Read the persisted review settings (model/effort/active_namespaces)."""
    try:
        cfg = store.load_config()
    except Exception:
        cfg = {}
    review = cfg.get("review") if isinstance(cfg, dict) else None
    if not isinstance(review, dict):
        review = {}
    return {
        "model": review.get("model") or None,
        "effort": review.get("effort", ""),
        "active_namespaces": review.get("active_namespaces") or ["default"],
        "max_concurrent": review_pool.effective_max_concurrent(),
    }


def _write_review_section(patch: dict) -> dict:
    """Merge a partial review-settings patch into config.json atomically. Only
    the model/effort/active_namespaces keys are writable; everything else in the
    config is preserved. Returns the resulting review section."""
    cfg_path = store.data_dir() / "config.json"
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except (json.JSONDecodeError, OSError, FileNotFoundError):
        store.ensure_layout()
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    review = cfg.get("review")
    if not isinstance(review, dict):
        review = {}

    if "model" in patch:
        m = patch["model"]
        # Empty/None clears the override (inherits the system/agent default model).
        # A non-empty value is validated (safe token + known to the registry when
        # available) before persisting, since it later becomes a cli.json overlay
        # key for the review worker subprocess — raw request input must not reach it.
        if not m:
            review["model"] = None
        elif _valid_model(str(m)):
            review["model"] = str(m)
        else:
            raise ValueError(f"unknown or invalid model {str(m)!r}")
    if "effort" in patch:
        eff = str(patch["effort"]).lower()
        # "" = inherit the model/provider default; otherwise a concrete level.
        review["effort"] = eff if (eff == "" or eff in review_pool.VALID_EFFORTS) else ""
    if "active_namespaces" in patch:
        ns = patch["active_namespaces"]
        if isinstance(ns, list) and ns:
            avail = set(learning.list_namespaces())
            cleaned = [str(n) for n in ns if str(n) in avail]
            review["active_namespaces"] = cleaned or ["default"]
    if "max_concurrent" in patch:
        # How many reviews run at once on the shared runtime. Clamped to
        # [1, MAX_CONCURRENT_CEIL] so "review all" can fan out without letting a
        # user set an unbounded value that would saturate the host.
        try:
            mc = int(patch["max_concurrent"])
        except (TypeError, ValueError):
            raise ValueError("max_concurrent must be an integer")
        review["max_concurrent"] = max(1, min(mc, review_pool.MAX_CONCURRENT_CEIL))

    cfg["review"] = review
    tmp = cfg_path.with_name(cfg_path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, cfg_path)
    return review


async def _handle_settings(request: web.Request) -> web.Response:
    """GET  -> {settings, models, efforts, namespaces}
       PUT  body {model?, effort?, active_namespaces?} -> {ok, settings}."""
    if request.method == "GET":
        # All of this is synchronous file IO (config read + namespaces dir walk +
        # reviewer_info file read) — offload to a thread so it never blocks the
        # shared gateway event loop.
        def _build_settings_response() -> dict:
            try:
                namespaces = learning.list_namespaces()
            except Exception:
                namespaces = ["default"]
            reviewer = None
            if hasattr(review_pool, "reviewer_info"):
                try:
                    reviewer = review_pool.reviewer_info()
                except Exception:
                    reviewer = None
            return {
                "settings": _load_review_section(),
                "models": _known_models(),
                "efforts": list(review_pool.VALID_EFFORTS),
                "namespaces": namespaces,
                "reviewer": reviewer,
                "max_concurrent_max": review_pool.MAX_CONCURRENT_CEIL,
            }
        return web.json_response(await asyncio.to_thread(_build_settings_response))
    # PUT
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    # SEL audit for this write path: it changes security-relevant subprocess
    # config — the model/effort flow into the review worker's cli.json overlay,
    # and the active namespaces select which learnings the worker loads. Both the
    # applied write (success) AND a rejected attempt (denied, e.g. an invalid/
    # injected model) are audited. Offloaded (ledger does sync file IO); failure
    # is swallowed so it can never break the response.
    async def _audit_settings(outcome: str) -> None:
        def _emit() -> None:
            from kiro_crew.sel import sel  # circular import: sel->config->apps cycle
            sel().log_api_access(
                caller="code-review-sage", operation="update_review_settings",
                outcome=outcome,
                resources="config.json#review(" + ",".join(sorted(body)) + ")")
        try:
            await asyncio.to_thread(_emit)
        except Exception as exc:
            logger.warning("SEL audit failed for update_review_settings (%s): %s", outcome, exc)

    try:
        review = await asyncio.to_thread(_write_review_section, body)
        await _audit_settings("success")
        return web.json_response({"ok": True, "settings": {
            "model": review.get("model") or None,
            "effort": review.get("effort", ""),
            "active_namespaces": review.get("active_namespaces") or ["default"],
            "max_concurrent": review.get("max_concurrent") or review_pool.effective_max_concurrent(),
        }})
    except ValueError as exc:
        # Bad client input (e.g. unknown model) — a 4xx, not a server fault.
        # Audit the rejected attempt (security-relevant: invalid model injection).
        await _audit_settings("denied")
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except Exception as exc:
        corr = uuid.uuid4().hex[:12]
        logger.warning("settings write failed [%s]: %s", corr, exc, exc_info=True)
        return web.json_response(
            {"ok": False, "error": "internal error", "id": corr}, status=500
        )


async def _handle_namespaces(request: web.Request) -> web.Response:
    """GET    -> {namespaces:[{name, patterns, candidate, active}], active:[...]}
       POST   body {name}            -> create a namespace
       DELETE body {name}            -> delete a (non-default) namespace."""
    if request.method == "GET":
        # list_namespaces() walks a dir and the per-namespace loop does a
        # synchronous read_text()+parse for EACH namespace — unbounded sync IO.
        # Offload the whole build to a thread so it can't freeze the event loop.
        def _build_ns_response() -> dict:
            names = learning.list_namespaces()
            active = set(learning.get_active_namespaces())
            out = []
            for n in names:
                out.append({
                    "name": n,
                    "patterns": len(learning.list_patterns(namespace=n)),
                    "candidate": learning.candidate_count(namespace=n),
                    "active": n in active,
                })
            return {"namespaces": out, "active": sorted(active)}
        try:
            return web.json_response(await asyncio.to_thread(_build_ns_response))
        except Exception as exc:
            logger.warning("namespace list failed: %s", exc, exc_info=True)
            return web.json_response({"namespaces": [], "active": ["default"]})

    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str((body or {}).get("name", "")).strip()
    if not name:
        return web.json_response({"ok": False, "error": "name required"}, status=400)

    # Audit namespace create/delete (filesystem ops that change the learning
    # scope). log_api_access does synchronous file IO (appends to the audit
    # ledger), so it is offloaded off the event loop. Audit failure is swallowed
    # so it can never break the response.
    async def _audit(operation: str, ok: bool) -> None:
        def _emit() -> None:
            from kiro_crew.sel import sel  # circular import: sel->config->apps cycle
            sel().log_api_access(
                caller="code-review-sage", operation=operation,
                outcome="success" if ok else "denied",
                resources=f"learnings/namespaces/{name}")
        try:
            await asyncio.to_thread(_emit)
        except Exception as exc:
            logger.warning("SEL audit failed for %s: %s", operation, exc)

    if request.method == "POST":
        res = await asyncio.to_thread(learning.create_namespace, name)
        await _audit("create_namespace", bool(res.get("ok")))
        return web.json_response(res, status=200 if res.get("ok") else 400)
    if request.method == "DELETE":
        # Drop it from active_namespaces first so reviews don't reference a gone
        # ns. Both the read AND the conditional write are synchronous file IO, so
        # run the whole prune in one offloaded helper (never on the event loop).
        def _prune_ns_from_active() -> None:
            sec = _load_review_section()
            if name in (sec.get("active_namespaces") or []):
                remaining = [n for n in sec["active_namespaces"] if n != name]
                _write_review_section({"active_namespaces": remaining or ["default"]})
        try:
            await asyncio.to_thread(_prune_ns_from_active)
        except Exception:
            logger.debug("could not prune deleted ns from active list", exc_info=True)
        res = await asyncio.to_thread(learning.delete_namespace, name)
        await _audit("delete_namespace", bool(res.get("ok")))  # destructive rmtree
        return web.json_response(res, status=200 if res.get("ok") else 400)
    return web.json_response({"error": "method not allowed"}, status=405)


async def _handle_learnings(request: web.Request) -> web.Response:
    """GET /api/apps/code-review-sage/learnings?namespace=<ns>

    Read-only view of a namespace's self-learning state so the dashboard can show
    what the reviewer has actually learned:
      - ``patterns``   — the consolidated heuristics reviews load (learned-patterns.md)
      - ``candidate``  — pending learnings staged during reviews, awaiting the
                         human-triggered ``learn-from-sage`` consolidation
    Both come from on-disk markdown; consolidation itself is an AI merge run via
    the skill (never a blind REST overwrite), so this endpoint deliberately does
    not mutate anything. ``?namespace=`` defaults to 'default'."""
    ns = request.query.get("namespace") or learning.DEFAULT_NAMESPACE

    def _build() -> dict:
        try:
            patterns = learning.list_patterns(namespace=ns)
        except Exception:
            patterns = []
        try:
            candidate = learning.list_candidate(namespace=ns)
        except Exception:
            candidate = []
        return {"namespace": ns, "patterns": patterns, "candidate": candidate}

    try:
        # Namespace-scoped file reads + markdown parse are synchronous IO — keep
        # them off the shared gateway event loop.
        return web.json_response(await asyncio.to_thread(_build))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("learnings view failed for ns=%s: %s", ns, exc, exc_info=True)
        return web.json_response({"namespace": ns, "patterns": [], "candidate": []})


def register_routes(app: web.Application) -> None:
    """Register the deterministic review routes on the gateway app."""
    # Self-heal: ensure the data layout (dirs + config.json with resolved_paths)
    # exists at startup. Without this, the UI gets {} from the generic config
    # endpoint and shows a perpetual "Initializing…" message because it needs
    # resolved_paths.reports to render the main view.
    try:
        store.ensure_layout()
    except Exception:  # pragma: no cover - never break gateway startup
        logger.warning("code-review-sage: ensure_layout failed at startup", exc_info=True)
    _load_runs()  # restore durable job status (mark orphaned 'running' as 'interrupted')
    app.router.add_post("/api/apps/code-review-sage/review", _handle_review)
    app.router.add_post("/api/apps/code-review-sage/review-repo", _handle_review_repo)
    app.router.add_get("/api/apps/code-review-sage/repo-prs", _handle_repo_prs)
    app.router.add_get("/api/apps/code-review-sage/runs", _handle_runs)
    app.router.add_get("/api/apps/code-review-sage/settings", _handle_settings)
    app.router.add_put("/api/apps/code-review-sage/settings", _handle_settings)
    app.router.add_get("/api/apps/code-review-sage/namespaces", _handle_namespaces)
    app.router.add_post("/api/apps/code-review-sage/namespaces", _handle_namespaces)
    app.router.add_delete("/api/apps/code-review-sage/namespaces", _handle_namespaces)
    app.router.add_get("/api/apps/code-review-sage/learnings", _handle_learnings)

    async def _shutdown_pool(_app: web.Application) -> None:
        """Retire the reusable review workers when the gateway shuts down."""
        try:
            await review_pool.shutdown_pool()
        except Exception:  # pragma: no cover - defensive
            logger.warning("failed to shut down review pool", exc_info=True)

    # register_app_routes runs before runner.setup() freezes the signal lists,
    # so this append is safe; guarded anyway so it can never break startup.
    try:
        app.on_cleanup.append(_shutdown_pool)
    except Exception:  # pragma: no cover - defensive
        logger.warning("could not register review-pool cleanup hook", exc_info=True)

    logger.info("code-review-sage backend routes registered (deterministic review kickoff)")
