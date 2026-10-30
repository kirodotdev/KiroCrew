"""The learning namespaces: their CRUD, the learnings view and the consolidation.

Listing, creating and deleting namespaces, the read-only learnings view, and the
one-shot merge of staged candidates into the ruleset. New learning-namespace
endpoints go here.

Composed onto :mod:`kiro_crew.apps.builtins.code_review_sage.backend.routes`;
see this package's ``__init__``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.apps.builtins.code_review_sage.backend.routes import (
        _CONSOLIDATE_STATE,
        _CONSOLIDATING,
        _MERGE_MAX_BYTES,
        _NS_OPS_LOCK,
        _TASKS,
        _load_review_section,
        _write_review_section,
        hooks,
        learning,
        logger,
        require_owner_dashboard_request,
        review_driver,
        review_pool,
        web,
    )


async def _handle_namespaces(request: web.Request) -> web.Response:
    # fmt: off
    """GET    -> {namespaces:[{name, patterns, candidate, active}], active:[...]}
       POST   body {name}            -> create a namespace
       DELETE body {name}            -> delete a (non-default) namespace."""
    # fmt: on
    if request.method == "GET":
        # list_namespaces() walks a dir and the per-namespace loop does a
        # synchronous read_text()+parse for EACH namespace — unbounded sync IO.
        # Offload the whole build to a thread so it can't freeze the event loop.
        def _build_ns_response() -> dict:
            names = learning.list_namespaces()
            active = set(learning.get_active_namespaces())
            out = []
            for n in names:
                out.append(
                    {
                        "name": n,
                        "patterns": len(learning.list_patterns(namespace=n)),
                        "candidate": learning.candidate_count(namespace=n),
                        "active": n in active,
                    }
                )
            return {"namespaces": out, "active": sorted(active)}

        try:
            return web.json_response(await asyncio.to_thread(_build_ns_response))
        except Exception as exc:
            logger.warning("namespace list failed: %s", exc, exc_info=True)
            return web.json_response({"namespaces": [], "active": ["default"]})

    owner_denied = await require_owner_dashboard_request(
        request, "code_review_sage.namespaces_write"
    )
    if owner_denied is not None:
        return owner_denied
    try:
        body = await request.json()
    except Exception:
        body = {}
    name = str((body or {}).get("name", "")).strip()
    if not name:
        return web.json_response(
            {"code": "name_required", "ok": False, "error": "name required"}, status=400
        )

    # Audit namespace create/delete (filesystem ops that change the learning
    # scope). log_api_access does synchronous file IO (appends to the audit
    # ledger), so it is offloaded off the event loop. Audit failure is swallowed
    # so it can never break the response.
    async def _audit(operation: str, ok: bool) -> None:
        def _emit() -> None:
            from kiro_crew.sel import sel  # circular import: sel->config->apps cycle

            sel().log_api_access(
                caller="code-review-sage",
                operation=operation,
                outcome="success" if ok else "denied",
                resources=f"learnings/namespaces/{name}",
            )

        try:
            await asyncio.to_thread(_emit)
        except Exception as exc:
            logger.warning("SEL audit failed for %s: %s", operation, exc)

    if request.method == "POST":
        res = await asyncio.to_thread(learning.create_namespace, name)
        await _audit("create_namespace", bool(res.get("ok")))
        return web.json_response(res, status=200 if res.get("ok") else 400)
    if request.method == "DELETE":
        # Deleting a namespace and starting a consolidation for it are mutually
        # exclusive, and BOTH sides take `_NS_OPS_LOCK` so there is no interleaving
        # to reason about. Two earlier shapes of this guard were wrong in ways worth
        # recording: a single check before the prune left a window during the
        # prune's await, and adding a SECOND check after the prune closed that
        # window but could reject the delete with the namespace already pruned from
        # the active list -- refusing the request while silently deactivating the
        # namespace it refused to delete. Holding the lock across check + prune +
        # rmtree removes the window instead of splitting it.
        async with _NS_OPS_LOCK:
            if name in _CONSOLIDATING:
                return web.json_response(
                    {
                        "code": "consolidation_in_progress",
                        "error": "a consolidation is running for this namespace — "
                        "wait for it to finish, then delete",
                    },
                    status=409,
                )

            # Delete FIRST, prune only on success -- one offloaded helper, since both
            # halves are synchronous file IO that must never run on the event loop.
            # Order matters: `delete_namespace` refuses the default namespace, an
            # invalid name, an out-of-tree path and a missing directory, and none of
            # those refusals depend on the active list. Pruning first would therefore
            # deactivate a namespace the API then reports it could not delete --
            # reviews quietly stop using it while the caller is told nothing changed.
            def _delete_then_prune() -> dict:
                res = learning.delete_namespace(name)
                if not res.get("ok"):
                    return res
                try:
                    sec = _load_review_section()
                    if name in (sec.get("active_namespaces") or []):
                        remaining = [n for n in sec["active_namespaces"] if n != name]
                        _write_review_section({"active_namespaces": remaining or ["default"]})
                except Exception:
                    logger.debug("could not prune deleted ns from active list", exc_info=True)
                return res

            res = await asyncio.to_thread(_delete_then_prune)
        await _audit("delete_namespace", bool(res.get("ok")))  # destructive rmtree
        return web.json_response(res, status=200 if res.get("ok") else 400)
    return web.json_response(
        {"code": "method_not_allowed", "error": "method not allowed"}, status=405
    )


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
        state = dict(_CONSOLIDATE_STATE.get(ns) or {})
        return {
            "namespace": ns,
            "patterns": patterns,
            "candidate": candidate,
            "consolidating": bool(state.get("running")),
            "consolidate_error": state.get("error"),
        }

    try:
        # Namespace-scoped file reads + markdown parse are synchronous IO — keep
        # them off the shared gateway event loop.
        return web.json_response(await asyncio.to_thread(_build))
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("learnings view failed for ns=%s: %s", ns, exc, exc_info=True)
        return web.json_response(
            {
                "namespace": ns,
                "patterns": [],
                "candidate": [],
                "consolidating": False,
                "consolidate_error": None,
            }
        )


async def _consolidate_bg(ns: str) -> None:
    """Run the one-shot AI merge, then apply it deterministically.

    The worker writes a candidate merge to a scratch file; this reads that file
    and hands it to ``learning.consolidate_apply``, which refuses empty content.
    The model never writes ``learned-patterns.md`` itself, so a truncated or
    chatty turn cannot destroy the ruleset — it just fails to produce a merge.
    """
    out_path = ""
    try:
        ns_dir = await asyncio.to_thread(learning._namespace_dir, ns, None)
        live = await asyncio.to_thread(learning.common_file, None, ns)
        cand = await asyncio.to_thread(learning.candidate_file, None, ns)
        out_path = str(Path(ns_dir) / "learned-patterns.merge.md")
        # The candidates this merge is allowed to consume, captured BEFORE the
        # worker runs. A review that stages a learning while the merge is in
        # flight must not have it cleared by that merge — the worker never saw it,
        # so it is not represented in the merged ruleset and a blanket clear would
        # destroy the only copy.
        # Each entry's equivalence key and `seen` count, not a set of ids: the
        # COUNT is what tells clear_candidate how many sightings this merge is
        # entitled to drop, so one staged again mid-merge is kept.
        cand_ids = await asyncio.to_thread(learning.candidate_snapshot, None, ns)
        # Clear any residue BEFORE dispatching. The worker writes this path and
        # the backend applies it afterwards; a crash between those two steps
        # leaves a stale merge on disk, and the next consolidation whose
        # worker produces nothing would apply that stale output over the live
        # ruleset and clear the candidate that was never merged.
        try:
            await asyncio.to_thread(Path(out_path).unlink, missing_ok=True)
        except OSError:
            pass

        loop = asyncio.get_running_loop()
        pool = review_pool.get_pool()
        dispatch = review_pool.make_sync_dispatch(loop, pool)
        await pool.begin_batch()
        try:
            spawn = await asyncio.to_thread(
                dispatch,
                review_driver.build_consolidation_task(ns, str(live), str(cand), out_path),
            )
        finally:
            await pool.end_batch()

        # The merge file is written by the WORKER, which has shell and file
        # tools, so both its path and its content are LLM-influenced. Reading it
        # with a plain open() would follow a symlink planted at that path and
        # copy whatever it points at into learned-patterns.md — a file that is
        # rendered in the UI and injected into every later review prompt. Route
        # the read through the hooks chokepoint, which opens with O_NOFOLLOW,
        # validates the opened inode against the app's own data root, rejects
        # sensitive paths and hardlinks, and caps the size.
        merged = ""
        try:
            raw = await asyncio.to_thread(
                hooks.safe_read_file_bytes_nolink,
                out_path,
                str(ns_dir),
                max_bytes=_MERGE_MAX_BYTES,
            )
        except Exception:
            logger.debug("merge read rejected", exc_info=True)
            raw = None
        if raw is not None:
            merged = raw.decode("utf-8", errors="replace")

        if not merged.strip():
            why = str(spawn.get("error") or "").strip()
            _CONSOLIDATE_STATE[ns] = {
                "running": False,
                "error": why or "the merge produced no file; the ruleset is unchanged",
            }
            return

        # A file on disk is not evidence the merge SUCCEEDED. The worker can fail
        # partway — timeout, cancellation, a tool error — after writing some of the
        # ruleset, and whatever it managed to emit parses fine. Applying that
        # replaces the full ruleset with a truncated one and clears the staged
        # candidates, so the omitted rules are gone from both copies. `spawn["ok"]`
        # was only consulted on the empty path above; a partial file skipped it.
        if not spawn.get("ok"):
            why = str(spawn.get("error") or "").strip()
            _CONSOLIDATE_STATE[ns] = {
                "running": False,
                "error": (why or "the merge did not finish")
                + "; the ruleset and the staged candidates are unchanged",
            }
            return

        # Non-empty is not the same as usable. The merge worker is an LLM, so it
        # can answer in prose ("nothing to merge here") instead of the pattern
        # format. That text is non-empty, so the emptiness check above passes it
        # through, and consolidate_apply would then replace the ENTIRE ruleset
        # with commentary and clear the staged candidates that were the only copy
        # of the pending learnings. Require at least one parseable pattern.
        if not await asyncio.to_thread(learning.parse_patterns, merged):
            _CONSOLIDATE_STATE[ns] = {
                "running": False,
                "error": (
                    "the merge produced no recognizable patterns; "
                    "the ruleset and the staged candidates are unchanged"
                ),
            }
            return

        # No existence re-check here on purpose. Every learning writer mkdirs its
        # parents, so a late apply WOULD resurrect a deleted namespace -- but the
        # `_CONSOLIDATING` claim already brackets this worker's entire lifetime: the
        # handler adds it before its first await and only this function's `finally`
        # releases it, and the delete handler refuses with 409 for that whole span.
        # A second check here would be unreachable, and an unreachable guard implies
        # protection that never actually runs.
        applied = await asyncio.to_thread(learning.consolidate_apply, merged, None, ns, cand_ids)
        _CONSOLIDATE_STATE[ns] = {
            "running": False,
            "error": None if applied.get("ok") else str(applied.get("error") or "merge rejected"),
            "patterns_now": applied.get("patterns_now"),
            "consolidated_from_candidate": applied.get("consolidated_from_candidate"),
        }
    except Exception as exc:
        logger.exception("consolidation failed for ns=%s", ns)
        _CONSOLIDATE_STATE[ns] = {"running": False, "error": str(exc)}
    finally:
        _CONSOLIDATING.discard(ns)
        if out_path:
            # The scratch merge is an intermediate; leaving it behind would make
            # the next run's "did the worker write a file?" check read a stale one
            # as success.
            try:
                await asyncio.to_thread(Path(out_path).unlink, True)
            except OSError:
                pass


async def _handle_consolidate(request: web.Request) -> web.Response:
    """POST .../learnings/consolidate — merge staged candidates into the ruleset.

    The merge is a judgment call (is this candidate already covered? do two rules
    collapse?), so it runs as one worker turn on the same pool reviews use. The
    APPLY is deterministic and refuses empty content, which is what keeps a bad
    merge from wiping the reviewer's memory.
    """
    owner_denied = await require_owner_dashboard_request(request, "code_review_sage.consolidate")
    if owner_denied is not None:
        return owner_denied
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    ns = str(body.get("namespace") or learning.DEFAULT_NAMESPACE)
    if not (ns == learning.DEFAULT_NAMESPACE or learning._is_valid_ns_name(ns)):
        return web.json_response(
            {"code": "invalid_namespace", "error": f"invalid namespace {ns!r}"}, status=400
        )
    # Claim the namespace BEFORE the first await. Checking membership and then
    # awaiting the staged count left a window where two concurrent requests for
    # one namespace both passed the guard, then both dispatched a merge against
    # the same scratch path — the second overwriting or unlinking the first's
    # output. The check-and-add below has no await between its two halves, so on
    # the single event loop it is atomic; the claim is given back on every early
    # return.
    async with _NS_OPS_LOCK:
        if ns in _CONSOLIDATING:
            return web.json_response(
                {
                    "code": "consolidation_in_progress",
                    "error": "a consolidation is already running for this namespace",
                },
                status=409,
            )
        # Taken under the lock so an in-flight DELETE cannot land between this
        # check and the claim; the delete handler holds the same lock across its
        # whole check + prune + rmtree.
        _CONSOLIDATING.add(ns)
    try:
        staged = await asyncio.to_thread(learning.candidate_count, None, ns)
    except Exception:
        _CONSOLIDATING.discard(ns)
        raise
    if staged == 0:
        _CONSOLIDATING.discard(ns)
        return web.json_response(
            {
                "code": "nothing_to_consolidate",
                "error": "nothing to consolidate — no learnings are staged",
            },
            status=409,
        )

    _CONSOLIDATE_STATE[ns] = {"running": True, "error": None}
    # Strong ref, as with the review and post paths: a collected merge task would
    # leave `_CONSOLIDATING` claimed with nothing to release it, locking this
    # namespace out of consolidation until a restart.
    task = asyncio.create_task(_consolidate_bg(ns))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return web.json_response({"ok": True, "namespace": ns, "staged": staged, "running": True})
