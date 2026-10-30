"""The script-hook routes, and the SEL accessor and store-failure guard the hooks routes share.

``/api/hooks`` (list, create, update, delete, toggle, test) and the read-only
``/api/kiro-hooks`` view of the kiro-cli agent hooks.

:mod:`kiro_crew.dashboard.handlers.hooks` is this module's facade: callers and tests import
and patch every name through it, and a write there -- ``monkeypatch.setattr``,
``mock.patch`` -- reaches this module too (:mod:`kiro_crew._facade_mirror`). This module
never imports the facade.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os

from aiohttp import web

from kiro_crew import webhooks
from kiro_crew.agent import (
    _VALID_HOOK_EVENTS,
    _shipped_defaults,
    kiro_agents_dir_path,
    unsent_kiro_hook_rows,
)
from kiro_crew.agent_discovery import _read_agent_spec
from kiro_crew.dashboard.handlers.hooks_webhook_intake import _json_object
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.validation import sanitize_string

logger = logging.getLogger("kiro_crew.dashboard.handlers.hooks")


def _sel():
    """Late-binding _sel() for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg  # noqa: F811

    return _pkg.sel()


# ── Script Hooks ──


def _get_hook_store(state: DashboardState):
    """Lazy-init ScriptHookStore on DashboardState."""
    if state._hook_store is None:
        from kiro_crew.hooks import (  # noqa: F811  # circular import
            ScriptHookStore,
            set_global_hook_store,
        )

        state._hook_store = ScriptHookStore()
        set_global_hook_store(state._hook_store)
    return state._hook_store


def _store_failure_guard(handler):
    """Map a webhook/script-hook store failure to 503 instead of a 500.

    Every store this module touches can REFUSE rather than silently report an
    empty file: reads raise ``WebhookStoreUnreadable`` when the file exists but
    cannot be parsed, and the shared ``hooks.json`` write refuses rather than
    erasing the webhook contexts stored alongside the script hooks. Writes can also
    fail outright on a full or read-only disk (``OSError``).

    One wrapper on every store-touching handler closes the class rather than
    catching those refusals a handler at a time: a handler that already returns a
    more specific 503 still does (its own guard runs first), and anything that
    would otherwise escape as an unhandled 500 — which
    reads to the operator as a gateway fault rather than "your store needs
    repair" — becomes the shared, machine-readable response.
    """

    @functools.wraps(handler)
    async def _guarded(request: web.Request) -> web.Response:
        try:
            return await handler(request)
        except (webhooks.WebhookStoreUnreadable, OSError) as exc:
            logger.warning(
                "webhook store unavailable in %s: %s", getattr(handler, "__name__", "?"), exc
            )
            return _store_unavailable_response()

    return _guarded


def _store_unavailable_response() -> web.Response:
    """The shared 503 for a script-hook mutation that could not be persisted."""
    return web.json_response(
        {
            "error": "the hook store is unavailable, so nothing was changed",
            "code": "store_unavailable",
        },
        status=503,
    )


@_store_failure_guard
async def api_hooks(request: web.Request) -> web.Response:
    """GET /api/hooks — list all script hooks."""
    store = _get_hook_store(request.app["state"])
    return web.json_response({"hooks": [h.to_dict() for h in store.list_all()]})


class _StoreUnavailable(Exception):
    """Raised internally when a script-hook mutation could not be persisted."""


# Named by the path callers import it from: an error chain renders a raised class as
# ``module.qualname``, so the owner module stays out of a traceback or a log line.
_StoreUnavailable.__module__ = "kiro_crew.dashboard.handlers.hooks"


async def _mutate_hook_store(operation, *args):
    """Run a script-hook store mutation off the event loop, failing loudly.

    Persistence takes the shared ``hooks.json`` lock and fsyncs, so it cannot run
    inline: a concurrent ``register_hook`` holding that lock would stall the whole
    gateway loop.

    ``ScriptHookStore._save`` refuses to overwrite an unreadable ``hooks.json``
    (overwriting would erase the webhook contexts kept in the same file), and the
    write itself can fail on a full or read-only disk. Both arrive here as
    :class:`_StoreUnavailable` so each caller answers 503 instead of leaking an
    unhandled 500 that reads as a gateway fault.
    """
    try:
        return await asyncio.to_thread(operation, *args)
    except (webhooks.WebhookStoreUnreadable, OSError) as exc:
        logger.warning("script-hook store mutation failed: %s", exc)
        raise _StoreUnavailable(str(exc)) from exc


@_store_failure_guard
async def api_hooks_create(request: web.Request) -> web.Response:
    """POST /api/hooks — create a new script hook."""
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "hooks.create")
    if denied is not None:
        return denied
    from kiro_crew.validation import (  # noqa: F811
        HOOK_CREATE_SCHEMA,
        ValidationError,
        validate_tool_args,
    )

    store = _get_hook_store(request.app["state"])
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)

    # Validate via schema (rejects wrong types, enforces length limits, sanitizes)
    try:
        validated = validate_tool_args(body, HOOK_CREATE_SCHEMA)
    except ValidationError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_hook"}, status=400)

    try:
        hook = await _mutate_hook_store(store.create, validated)
    except _StoreUnavailable:
        return _store_unavailable_response()
    except ValueError as exc:
        # store.create enforces the same invariants as store.update via the
        # shared validator, so it can raise ValueError. The HOOK_CREATE_SCHEMA
        # check above normally rejects bad input first, but catch it here too so
        # any schema/validator drift surfaces as a 400 (like the update handler)
        # rather than an unhandled 500.
        return web.json_response({"error": str(exc), "code": "invalid_hook"}, status=400)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="hook.create",
        outcome="success",
        source="dashboard",
        resources=f"hook:{hook.id}:{hook.name}:{hook.event}",
    )
    return web.json_response({"ok": True, "hook": hook.to_dict()})


@_store_failure_guard
async def api_hook_detail(request: web.Request) -> web.Response:
    """PUT/DELETE /api/hooks/{hook_id}."""
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "hooks.update")
    if denied is not None:
        return denied
    from kiro_crew.validation import (  # noqa: F811
        HOOK_UPDATE_SCHEMA,
        ValidationError,
        validate_tool_args,
    )

    store = _get_hook_store(request.app["state"])
    hook_id = request.match_info["hook_id"]
    if request.method == "DELETE":
        hook = store.get(hook_id)
        try:
            deleted = await _mutate_hook_store(store.delete, hook_id)
        except _StoreUnavailable:
            return _store_unavailable_response()
        if deleted:
            _sel().log_api_access(
                caller=request.get("user", "dashboard"),
                operation="hook.delete",
                outcome="success",
                source="dashboard",
                resources=f"hook:{hook_id}:{hook.name if hook else 'unknown'}",
            )
            return web.json_response({"ok": True})
        return web.json_response({"error": "not found", "code": "hook_not_found"}, status=404)

    # PUT — update
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON", "code": "invalid_json"}, status=400)

    # Validate via schema (rejects wrong types, enforces length limits, sanitizes)
    try:
        validated = validate_tool_args(body, HOOK_UPDATE_SCHEMA)
    except ValidationError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_hook"}, status=400)

    try:
        hook = await _mutate_hook_store(store.update, hook_id, validated)
    except _StoreUnavailable:
        return _store_unavailable_response()
    except ValueError as exc:
        return web.json_response({"error": str(exc), "code": "invalid_hook"}, status=400)
    if not hook:
        return web.json_response({"error": "not found", "code": "hook_not_found"}, status=404)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="hook.update",
        outcome="success",
        source="dashboard",
        resources=f"hook:{hook_id}:{hook.name}:{hook.event}",
    )
    return web.json_response({"ok": True, "hook": hook.to_dict()})


@_store_failure_guard
async def api_hook_toggle(request: web.Request) -> web.Response:
    """POST /api/hooks/{hook_id}/toggle — enable/disable."""
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "hooks.toggle")
    if denied is not None:
        return denied

    store = _get_hook_store(request.app["state"])
    hook_id = request.match_info["hook_id"]
    try:
        hook = await _mutate_hook_store(store.toggle, hook_id)
    except _StoreUnavailable:
        return _store_unavailable_response()
    if not hook:
        return web.json_response({"error": "not found", "code": "hook_not_found"}, status=404)
    _sel().log_api_access(
        caller=request.get("user", "dashboard"),
        operation="hook.toggle",
        outcome="success",
        source="dashboard",
        resources=f"hook:{hook_id}:{hook.name}:enabled={hook.enabled}",
    )
    return web.json_response({"ok": True, "hook": hook.to_dict()})


@_store_failure_guard
async def api_hook_test(request: web.Request) -> web.Response:
    """POST /api/hooks/{hook_id}/test — execute hook and return output."""
    # Body-scope import, like the sibling gates in this package
    # (``connections.py``, ``mcp_apps.py``, ``files.py``): ``source_providers``
    # reaches back into sibling handler modules, so importing the helper at
    # module scope from here would close a cycle.
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request

    denied = await require_owner_dashboard_request(request, "hooks.test")
    if denied is not None:
        return denied
    # circular import: kiro_crew.hooks pulls dashboard state at module load, so
    # this handler defers the import to call time (matches _get_hook_store above).
    from kiro_crew.hooks import (  # noqa: F811
        HOOK_EVENT_STOP,
        HOOK_EVENT_USER_PROMPT_SUBMIT,
        run_script_hook,
    )
    from kiro_crew.platform import redact_via_context

    store = _get_hook_store(request.app["state"])
    hook_id = request.match_info["hook_id"]
    hook = store.get(hook_id)
    if not hook:
        return web.json_response({"error": "not found", "code": "hook_not_found"}, status=404)
    body, body_error = await _json_object(request, default_empty=True)
    if body_error is not None:
        return body_error
    assert body is not None  # (dict, None) on success
    raw_context = body.get("context", "test")
    context = sanitize_string(raw_context)
    if len(context) > 10000:  # Max context length for hook test
        context = context[:10000]
    # Mirror ScriptHookStore.fire()'s per-event stdin payload so the FULL
    # context reaches the hook here exactly as it does on a live fire. The env
    # var KIROCREW_HOOK_CONTEXT is capped at 500 chars in run_script_hook for
    # every event (ARG_MAX safety), so an event whose context travels on stdin
    # (UserPromptSubmit -> ``prompt``, Stop -> ``assistant_text``) must carry it
    # here too, or a >500-char test context is truncated on both paths and the
    # hook receives incomplete input. Events fire() sends context only via the
    # env var keep the default payload (run_script_hook builds it when None),
    # matching production.
    hook_event = None
    if hook.event == HOOK_EVENT_USER_PROMPT_SUBMIT:
        hook_event = {
            "hook_event_name": hook.event,
            "cwd": os.getcwd(),
            "prompt": context,
        }
    elif hook.event == HOOK_EVENT_STOP:
        hook_event = {
            "hook_event_name": hook.event,
            "cwd": os.getcwd(),
            "assistant_text": context,
        }
    result = await run_script_hook(hook, context, hook_event)
    _sel().log_tool_invocation(
        session_key="dashboard:hook_test",
        agent="kirocrew",
        source="dashboard",
        tool_name=f"hook:{hook.name}",
        tool_kind="script_hook",
        outcome="tested",
        metadata={
            "hook_id": hook.id,
            "hook_event": hook.event,
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
            "context": redact_via_context(context),
        },
    )
    return web.json_response(
        {
            "ok": True,
            "result": {
                "stdout": result.stdout,
                "stderr": result.stderr,
                "exit_code": result.exit_code,
                "error": result.error,
                "duration_ms": result.duration_ms,
            },
        }
    )


#: The ``unsent`` row fields the author wrote, which may hold a credential.
_UNSENT_AUTHORED = frozenset({"name", "matcher", "command"})


@_store_failure_guard
async def api_kiro_hooks(request: web.Request) -> web.Response:
    """GET /api/kiro-hooks — read-only view of kiro-cli agent hooks from kirocrew.json."""
    from kiro_crew.platform import redact_via_context as redact

    agent_cfg = kiro_agents_dir_path() / "kirocrew.json"
    # ``kirocrew.json`` lives in the user-writable, tool-shared agents dir, so
    # the read goes through the hardened agents-dir reader (size cap, symlink
    # and sensitive-target screens, explicit UTF-8, non-object rejection).
    # ``None`` covers every unreadable case, including the ones an
    # ``except (OSError, JSONDecodeError)`` misses (e.g. non-UTF-8 bytes, which
    # would escape as an unhandled 500), and degrades one way: no user hooks.
    # Off-loop: the reader stats + reads up to the size cap, and this handler
    # runs on the gateway event loop (no-blocking-call rule).
    # The labels are passed explicitly: they name the SEL denial event's
    # operation and interface channel, and without them a refusal here is
    # recorded under the reader's ``list_agents`` defaults -- attributing a
    # hooks request's denial to an agent-listing cache warm, the exact
    # misattribution the labels exist to prevent.
    raw = await asyncio.to_thread(
        _read_agent_spec,
        agent_cfg,
        operation="api_kiro_hooks",
        source="dashboard",
    )
    # The reader guarantees the TOP level is an object, not the "hooks" value:
    # a user-writable {"hooks": []} would reach hooks.items() below and escape
    # as a 500. Same degrade-as-absent rule as every other unusable shape.
    hooks = raw.get("hooks") if raw is not None else None
    if not isinstance(hooks, dict):
        hooks = {}
    # Load bundled defaults to tag source. Explicit UTF-8: the shipped file
    # carries non-ASCII text, and the locale code page (cp932/cp936/cp949 on CJK
    # Windows) cannot decode it. ``ValueError`` covers that UnicodeDecodeError
    # and JSONDecodeError, so an unreadable file degrades to no bundled tags.
    # Off-loop, with the path lookup (it probes a project-dir override).
    try:
        raw = json.loads(
            await asyncio.to_thread(lambda: _shipped_defaults().read_text(encoding="utf-8"))
        )
        bundled = raw.get("hooks", {}) if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        bundled = {}
    bundled_keys: set[tuple[str, str, str]] = set()
    for event, entries in bundled.items():
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict):
                bundled_keys.add((event, e.get("command") or "", e.get("matcher") or ""))
    result: dict[str, list[dict]] = {}
    for event, entries in hooks.items():
        if event not in _VALID_HOOK_EVENTS:
            continue  # drop unknown/injected event keys
        tagged = []
        for e in entries if isinstance(entries, list) else []:
            if isinstance(e, dict):
                key = (event, e.get("command") or "", e.get("matcher") or "")
                # Context-aware redact(): runs the exfil-URL + credential passes
                # and applies a loaded companion's extra regexes (so an internal
                # token in a hook command is scrubbed on this egress surface too).
                tagged.append(
                    {
                        "command": redact(e.get("command") or ""),
                        "matcher": redact(e.get("matcher") or ""),
                        "source": "bundled" if key in bundled_keys else "user",
                    }
                )
        if tagged:
            result[event] = tagged
    unsent = await asyncio.to_thread(unsent_kiro_hook_rows)  # off-loop: reads config.json
    unsent = [{k: redact(v) if k in _UNSENT_AUTHORED else v for k, v in r.items()} for r in unsent]
    return web.json_response({"hooks": result, "unsent": unsent})
