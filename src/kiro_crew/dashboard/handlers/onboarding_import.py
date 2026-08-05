"""Authenticated API handlers for foreign-agent onboarding import."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from types import ModuleType
from typing import Any

from aiohttp import web

from kiro_crew.config.loader import KiroCrewConfig

logger = logging.getLogger(__name__)

_SOURCE_IDS = frozenset(
    {
        "codex",
        "claude_code",
        "meshclaw",
        "openclaw",
        "hermes",
    }
)
_CATEGORY_IDS = frozenset(
    {
        "instructions",
        "memories",
        "workspaces",
        "mcp_servers",
        "skills",
        "schedules",
        "settings",
    }
)
_SOURCE_NAMES = {
    "codex": "Codex",
    "claude_code": "Claude Code",
    "meshclaw": "MeshClaw",
    "openclaw": "OpenClaw",
    "hermes": "Hermes Agent",
}
_CATEGORY_NAMES = {
    "memories": "Memories",
    "workspaces": "Workspaces",
    "mcp_servers": "MCP servers",
    "skills": "Skills",
    "schedules": "Schedules",
    "settings": "Settings",
    "hooks": "Hooks",
    "agents": "Agents and personas",
    "instructions": "Instructions",
    "credentials": "Credentials",
    "runtime": "Runtime state",
    "sessions": "Conversation history",
}
_CATEGORY_DESCRIPTIONS = {
    "instructions": "Your own rules, as high-priority memory",
    "memories": "Durable preferences and memories",
    "workspaces": "Existing local project folders",
    "mcp_servers": "Server definitions, imported disabled",
    "skills": "User-authored skills and supporting files",
    "schedules": "Compatible schedules, imported paused",
    "settings": "Compatible display and timezone settings",
}
_CONFLICT_STRATEGIES = frozenset({"skip", "rename", "overwrite"})
_ITEM_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ITEM_OUTCOMES = frozenset({"accepted", "deduplicated", "rejected"})
_IMPORT_LOCK = asyncio.Lock()


class _InvalidSelection(ValueError):
    """The submitted selection is invalid or stale."""


def _backend() -> ModuleType:
    from kiro_crew import onboarding_import

    return onboarding_import


def _sel() -> Any:
    from kiro_crew.sel import sel

    return sel()


def _audit(
    *,
    caller: str,
    operation: str,
    outcome: str,
    error: str = "",
    resources: str = "",
) -> None:
    """Write a credential-free, best-effort API outcome."""
    try:
        event = {
            "caller": caller,
            "operation": operation,
            "outcome": outcome,
            "source": "dashboard",
        }
        if error:
            event["error"] = error
        if resources:
            event["resources"] = resources
        _sel().log_api_access(
            **event,
        )
    except Exception:
        logger.debug("Onboarding import SEL event failed", exc_info=True)


def _caller(request: web.Request, operation: str) -> tuple[str | None, web.Response | None]:
    if "user" not in request or not request["user"]:
        _audit(
            caller="anonymous",
            operation=operation,
            outcome="denied",
            error="authentication_required",
        )
        return None, web.json_response({"error": "authentication required"}, status=401)
    return str(request["user"]), None


def _parse_conflict_strategy(body: object) -> str:
    """Read the requested strategy, rejecting anything unrecognized.

    Absent means ``skip`` (the safe default), but a PRESENT-but-unknown value is
    a hard 400: silently downgrading "overwrite" to "skip" would tell the client
    its destructive request succeeded when nothing was replaced.
    """

    if not isinstance(body, dict):
        raise _InvalidSelection
    # ABSENT means the safe default. A PRESENT null is a malformed value like any
    # other and must 400 -- silently defaulting it contradicts the documented
    # contract and would tell a client its request was understood.
    if "conflict_strategy" not in body:
        return "skip"
    raw = body["conflict_strategy"]
    if not isinstance(raw, str) or raw not in _CONFLICT_STRATEGIES:
        raise _InvalidSelection
    return raw


def _parse_selection(body: object) -> tuple[list[str], set[tuple[str, str]]]:
    if not isinstance(body, dict):
        raise _InvalidSelection
    sources = body.get("sources")
    if not isinstance(sources, list) or not sources or len(sources) > len(_SOURCE_IDS):
        raise _InvalidSelection

    source_ids: list[str] = []
    selected: set[tuple[str, str]] = set()
    seen_sources: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            raise _InvalidSelection
        source_id = source.get("id")
        categories = source.get("categories")
        if (
            not isinstance(source_id, str)
            or source_id not in _SOURCE_IDS
            or source_id in seen_sources
            or not isinstance(categories, list)
            or not categories
            or len(categories) > len(_CATEGORY_IDS)
        ):
            raise _InvalidSelection

        seen_categories: set[str] = set()
        for category_id in categories:
            if (
                not isinstance(category_id, str)
                or category_id not in _CATEGORY_IDS
                or category_id in seen_categories
            ):
                raise _InvalidSelection
            seen_categories.add(category_id)
            selected.add((source_id, category_id))

        seen_sources.add(source_id)
        source_ids.append(source_id)

    return source_ids, selected


def _select_fresh_plan(
    plan: object,
    selected: set[tuple[str, str]],
) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise RuntimeError("invalid import preview")
    selection = plan.get("selection")
    sources = plan.get("sources")
    if not isinstance(selection, list) or not isinstance(sources, list):
        raise RuntimeError("invalid import preview")

    available: set[tuple[str, str]] = set()
    selected_items: list[dict[str, Any]] = []
    for item in selection:
        if not isinstance(item, dict):
            raise RuntimeError("invalid import preview")
        source_id = item.get("source_id")
        category_id = item.get("category_id")
        if not isinstance(source_id, str) or not isinstance(category_id, str):
            raise RuntimeError("invalid import preview")
        pair = (source_id, category_id)
        available.add(pair)
        if pair in selected:
            selected_items.append(item)

    if not selected.issubset(available):
        raise _InvalidSelection

    for source in sources:
        if not isinstance(source, dict):
            raise RuntimeError("invalid import preview")
        source_id = source.get("id")
        categories = source.get("categories")
        if not isinstance(source_id, str) or not isinstance(categories, list):
            raise RuntimeError("invalid import preview")
        for category in categories:
            if not isinstance(category, dict) or not isinstance(category.get("id"), str):
                raise RuntimeError("invalid import preview")
            category["selected"] = (source_id, category["id"]) in selected

    plan["selection"] = selected_items
    return plan


def _nonnegative_count(value: object, *, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RuntimeError("invalid import result")
    return value


def _safe_reason(value: object) -> str:
    if (
        isinstance(value, str)
        and 0 < len(value) <= 64
        and all(
            character.islower() or character.isdigit() or character == "_" for character in value
        )
    ):
        return value
    return "not_imported"


def _scan_response(plan: object) -> dict[str, Any]:
    """Project an internal plan to the content-free browser contract."""
    if not isinstance(plan, dict) or not isinstance(plan.get("sources"), list):
        raise RuntimeError("invalid import preview")

    sources: list[dict[str, Any]] = []
    for source in plan["sources"]:
        if not isinstance(source, dict):
            raise RuntimeError("invalid import preview")
        source_id = source.get("id")
        categories = source.get("categories")
        if source_id not in _SOURCE_IDS or not isinstance(categories, list):
            raise RuntimeError("invalid import preview")
        projected_categories: list[dict[str, Any]] = []
        for category in categories:
            if not isinstance(category, dict):
                raise RuntimeError("invalid import preview")
            category_id = category.get("id")
            if category_id not in _CATEGORY_IDS:
                raise RuntimeError("invalid import preview")
            projected_categories.append(
                {
                    "id": category_id,
                    "label": _CATEGORY_NAMES[category_id],
                    "count": _nonnegative_count(category.get("count")),
                    "description": _CATEGORY_DESCRIPTIONS[category_id],
                }
            )
        sources.append(
            {
                "id": source_id,
                "name": _SOURCE_NAMES[source_id],
                "detected": True,
                "categories": projected_categories,
            }
        )

    skipped: list[dict[str, Any]] = []
    raw_skipped = plan.get("skipped", [])
    if not isinstance(raw_skipped, list):
        raise RuntimeError("invalid import preview")
    for item in raw_skipped:
        if not isinstance(item, dict):
            raise RuntimeError("invalid import preview")
        source_id = item.get("source_id")
        category_id = item.get("category_id")
        source_name = (
            _SOURCE_NAMES.get(source_id, "Unknown source")
            if isinstance(source_id, str)
            else "Unknown source"
        )
        category_name = (
            _CATEGORY_NAMES.get(category_id, "General")
            if isinstance(category_id, str)
            else "General"
        )
        projected: dict[str, Any] = {
            "source": source_name,
            "category": category_name,
            "reason": _safe_reason(item.get("reason")),
        }
        if "count" in item:
            projected["count"] = _nonnegative_count(item["count"])
        skipped.append(projected)

    return {
        "sources": sources,
        "skipped": skipped,
        "merge_only": True,
    }


def _apply_response(result: object) -> dict[str, Any]:
    """Project importer details to the stable aggregate browser contract."""
    if not isinstance(result, dict):
        raise RuntimeError("invalid import result")

    imported_count = result.get("imported_count")
    if imported_count is None:
        imported = result.get("imported", {})
        if not isinstance(imported, dict):
            raise RuntimeError("invalid import result")
        imported_count = sum(_nonnegative_count(value) for value in imported.values())

    skipped = result.get("skipped", [])
    conflicts = result.get("conflicts", [])
    if not isinstance(skipped, list) or not isinstance(conflicts, list):
        raise RuntimeError("invalid import result")

    # A conflict the user can act on (rename/overwrite) vs one they cannot.
    # Counts only — never the restore/rename PATHS: those are filesystem details
    # and this response crosses into the browser.
    resolvable = sum(
        1 for entry in conflicts if isinstance(entry, dict) and entry.get("resolvable")
    )
    strategy = result.get("conflict_strategy")
    return {
        "ok": True,
        "conflict_strategy": strategy if isinstance(strategy, str) else "skip",
        "summary": {
            "imported": _nonnegative_count(imported_count),
            "deduplicated": _nonnegative_count(
                result.get("already_imported"),
                default=0,
            ),
            "skipped": (
                len(skipped)
                + len(conflicts)
                + _nonnegative_count(result.get("secret_count"), default=0)
            ),
            "conflicts": len(conflicts),
            # How many of those a retry with rename/overwrite could clear.
            "resolvable_conflicts": resolvable,
        },
    }


def _apply_import(
    source_ids: list[str],
    selected: set[tuple[str, str]],
    cron_service: object | None,
    vector_store: object | None,
    lesson_store: object | None,
    conflict_strategy: str,
) -> object:
    backend = _backend()
    plan = _select_fresh_plan(
        backend.preview_import(source_ids=source_ids),
        selected,
    )
    return backend.apply_import(
        plan,
        cron_service=cron_service,
        vector_store=vector_store,
        lesson_store=lesson_store,
        conflict_strategy=conflict_strategy,
    )


def _merge_import_results(
    results: list[object],
    conflict_strategy: str = "skip",
) -> dict[str, Any]:
    merged: dict[str, Any] = {
        "conflict_strategy": conflict_strategy,
        "imported": {},
        "imported_count": 0,
        "already_imported": 0,
        "embedding_backfill_pending": 0,
        "secret_count": 0,
        "skipped": [],
        "conflicts": [],
        "item_outcomes": [],
    }
    seen_diagnostics: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            raise RuntimeError("invalid import result")
        imported = result.get("imported", {})
        if isinstance(imported, dict):
            for category, count in imported.items():
                if isinstance(count, int) and not isinstance(count, bool):
                    merged["imported"][category] = merged["imported"].get(category, 0) + max(
                        0, count
                    )
        for key in ("imported_count", "already_imported", "embedding_backfill_pending"):
            value = result.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                merged[key] += max(0, value)
        secret_count = result.get("secret_count")
        if isinstance(secret_count, int) and not isinstance(secret_count, bool):
            merged["secret_count"] = max(merged["secret_count"], max(0, secret_count))
        for key in ("skipped", "conflicts", "item_outcomes"):
            value = result.get(key)
            if isinstance(value, list):
                for item in value:
                    marker = json.dumps(item, sort_keys=True, separators=(",", ":"))
                    if key == "item_outcomes" or marker not in seen_diagnostics:
                        merged[key].append(item)
                        if key != "item_outcomes":
                            seen_diagnostics.add(marker)
    if not merged["imported_count"]:
        merged["imported_count"] = sum(merged["imported"].values())
    return merged


def _get_config_lock() -> asyncio.Lock:
    # Circular import: handlers.agents is re-exported by the package that imports us.
    from kiro_crew.dashboard.handlers.agents import _get_config_lock as get_lock

    return get_lock()


def _audit_item_outcomes(caller: str, result: object) -> None:
    if not isinstance(result, dict):
        return
    outcomes = result.get("item_outcomes")
    if not isinstance(outcomes, list):
        return
    for item in outcomes:
        if not isinstance(item, dict):
            continue
        source_id = item.get("source_id")
        category_id = item.get("category_id")
        item_hash = item.get("item_hash")
        outcome = item.get("outcome")
        if (
            source_id not in _SOURCE_IDS
            or category_id not in _CATEGORY_IDS
            or not isinstance(item_hash, str)
            or not _ITEM_HASH_RE.fullmatch(item_hash)
            or outcome not in _ITEM_OUTCOMES
        ):
            continue
        _audit(
            caller=caller,
            operation="onboarding.import.item",
            outcome=str(outcome),
            resources=f"{source_id}:{category_id}:{item_hash}",
        )


def _backfill_embeddings(vector_store: object) -> int:
    """Embed the NULL-embedding rows import just wrote (worker-thread body).

    Import defers embedding so the apply request returns promptly: inference
    costs ~0.4s per 2000-char chunk on CPU, and an import writes hundreds. The
    rows are FTS5 keyword-searchable the moment they land; this sweep is what
    makes them semantically searchable, so it must actually run — a row left
    NULL is silently absent from vector search.

    Waits for the model to finish loading first: ``backfill_missing_embeddings``
    embeds zero rows against a still-warming model, and nothing would re-schedule
    it before the next gateway boot.
    """
    backfill = getattr(vector_store, "backfill_missing_embeddings", None)
    if not callable(backfill):
        return 0
    try:
        from kiro_crew.embeddings import get_shared_embedder, model_file_present

        if not model_file_present():
            # Still downloading — the gateway's own boot sweep backfills later.
            return 0
        embedder = get_shared_embedder()
        # wait_ready() is on the llama.cpp backend but not the EmbeddingBackend
        # ABC (a swapped-in backend need not support blocking-wait).
        wait_ready = getattr(embedder, "wait_ready", None)
        ready = wait_ready(timeout=120) if callable(wait_ready) else embedder.is_ready()
        if not ready:
            logger.info("Embedding model not ready; deferring import backfill to next boot")
            return 0
        return int(backfill())
    except Exception:
        # Never surface as an apply failure: the memories ARE imported and
        # keyword-searchable; only the vectors are missing, and the gateway's
        # boot sweep is the standing retry.
        logger.warning("Import embedding backfill failed", exc_info=True)
        return 0


def _schedule_embedding_backfill(vector_store: object | None) -> None:
    """Run the import backfill on the maintenance executor, off the request."""
    if vector_store is None:
        return
    from kiro_crew.executors import maintenance_executor

    loop = asyncio.get_running_loop()
    task = loop.run_in_executor(maintenance_executor(), _backfill_embeddings, vector_store)
    # Fire-and-forget, but retrieve the result so a failure is logged rather than
    # resurfacing later as an unretrieved-future warning far from its cause.
    task.add_done_callback(_log_backfill_result)


def _log_backfill_result(task: asyncio.Future[int]) -> None:
    try:
        embedded = task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.warning("Import embedding backfill task failed", exc_info=True)
        return
    if embedded:
        logger.info("Embedded %d imported memories in the background", embedded)


def _rebuild_agent_config() -> None:
    # Circular import: agent imports dashboard handlers during gateway startup.
    from kiro_crew.agent import rebuild_agent_config

    rebuild_agent_config()


def _persist_state(completed: bool) -> None:
    config = KiroCrewConfig.load()
    config.dashboard.import_onboarded = completed
    config.save()


async def api_onboarding_import_scan(request: web.Request) -> web.Response:
    """GET /api/onboarding/import/scan."""
    operation = "onboarding.import.scan"
    caller, error_response = _caller(request, operation)
    if error_response is not None:
        return error_response
    assert caller is not None

    try:
        result = await asyncio.to_thread(_backend().preview_import, source_ids=None)
        response = web.json_response(_scan_response(result))
    except Exception:
        logger.exception("Onboarding import scan failed")
        _audit(caller=caller, operation=operation, outcome="failed", error="scan_failed")
        return web.json_response({"error": "request failed"}, status=500)

    _audit(caller=caller, operation=operation, outcome="completed")
    return response


async def api_onboarding_import_apply(request: web.Request) -> web.Response:
    """POST /api/onboarding/import/apply."""
    operation = "onboarding.import.apply"
    caller, error_response = _caller(request, operation)
    if error_response is not None:
        return error_response
    assert caller is not None

    try:
        body = await request.json()
        source_ids, selected = _parse_selection(body)
        conflict_strategy = _parse_conflict_strategy(body)
    except (ValueError, TypeError):
        _audit(caller=caller, operation=operation, outcome="failed", error="invalid_request")
        return web.json_response({"error": "invalid request"}, status=400)

    state = request.app.get("state")
    cron_service = getattr(state, "crons", None)
    lesson_store = getattr(state, "lessons", None)
    vector_store = getattr(state, "vector_memory", None)
    if vector_store is None:
        context_builder = getattr(state, "context_builder", None)
        memory = getattr(context_builder, "memory", None)
        vector_store = getattr(memory, "vector_store", None)
    try:
        async with _IMPORT_LOCK:
            config_selected = {pair for pair in selected if pair[1] != "mcp_servers"}
            mcp_selected = {pair for pair in selected if pair[1] == "mcp_servers"}
            results: list[object] = []
            if config_selected:
                async with _get_config_lock():
                    results.append(
                        await asyncio.to_thread(
                            _apply_import,
                            source_ids,
                            config_selected,
                            cron_service,
                            vector_store,
                            lesson_store,
                            conflict_strategy,
                        )
                    )
            if mcp_selected:
                # MCP handlers acquire the MCP file lock before the config lock.
                # Keep this phase outside the config lock to avoid lock inversion.
                results.append(
                    await asyncio.to_thread(
                        _apply_import,
                        source_ids,
                        mcp_selected,
                        cron_service,
                        vector_store,
                        lesson_store,
                        conflict_strategy,
                    )
                )
                await asyncio.to_thread(_rebuild_agent_config)
            result = _merge_import_results(results, conflict_strategy)
            response = web.json_response(_apply_response(result))
            # Import wrote episodic rows without vectors so this request could
            # return in ~1s instead of minutes. Embed them on a worker thread now
            # — inside the import lock's scope but not awaited, so the response
            # goes out immediately.
            if result.get("embedding_backfill_pending"):
                _schedule_embedding_backfill(vector_store)
    except _InvalidSelection:
        _audit(caller=caller, operation=operation, outcome="failed", error="invalid_request")
        return web.json_response({"error": "invalid request"}, status=400)
    except Exception:
        logger.exception("Onboarding import apply failed")
        _audit(caller=caller, operation=operation, outcome="failed", error="apply_failed")
        return web.json_response({"error": "request failed"}, status=500)

    _audit_item_outcomes(caller, result)
    _audit(caller=caller, operation=operation, outcome="completed")
    return response


async def api_onboarding_import_state(request: web.Request) -> web.Response:
    """PUT /api/onboarding/import/state."""
    operation = "onboarding.import.state"
    caller, error_response = _caller(request, operation)
    if error_response is not None:
        return error_response
    assert caller is not None

    try:
        body = await request.json()
        if not isinstance(body, dict) or not isinstance(body.get("completed"), bool):
            raise _InvalidSelection
    except (ValueError, TypeError):
        _audit(caller=caller, operation=operation, outcome="failed", error="invalid_request")
        return web.json_response({"error": "invalid request"}, status=400)

    try:
        async with _get_config_lock():
            await asyncio.to_thread(_persist_state, body["completed"])
    except Exception:
        logger.exception("Onboarding import state update failed")
        _audit(caller=caller, operation=operation, outcome="failed", error="state_failed")
        return web.json_response({"error": "request failed"}, status=500)

    _audit(caller=caller, operation=operation, outcome="completed")
    return web.json_response({"ok": True})
