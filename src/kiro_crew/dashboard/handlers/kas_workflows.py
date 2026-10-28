"""Dashboard routes for KAS's own workflow engine (``/api/workflows/kas/*``).

Served only while the configured agent backend brings its own engine AND the
operator turned on ``agent.kas_workflows`` (the single opt-in,
:func:`kiro_crew.kas_workflows.engine.kas_workflows_enabled`); otherwise every
route answers 409 ``engine_not_kas`` and Crew's own ``/api/workflows/*`` routes
apply.

A run's parent is a visible chat. The client opens that chat the normal way
(``POST /api/chat/slots`` with the run's title, folder and project), then posts
here naming it; this route registers the launch and sends the chat its launch
message, and the chat's own turn starts the run on its own Kiro Agent session
(:func:`kiro_crew.kas_workflows.router.launch_from_turn`).

Every route is the dashboard OWNER's: these start runs that execute tools, and
answer the permission prompts those tools raise. App tokens are refused.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.dashboard.handlers.workflows import (
    _audit_authorization,
    _error,
    _json_response_off_loop,
    _redact_obj,
)

logger = logging.getLogger(__name__)

#: Most inputs one launch may carry, and the longest value.
_MAX_INPUTS = 64
_MAX_INPUT_CHARS = 16 * 1024


#: Operations that only READ a run the router already holds. When the switch is
#: off these stay available (together with Stop) so a run still in flight can be
#: viewed and halted, while launching and controlling it are refused (gap 5).
_READ_ONLY_WHEN_HELD = frozenset(
    {
        "workflow.kas.state",
        "workflow.kas.run",
        "workflow.kas.slot_runs",
        "workflow.kas.step_transcript",
        "workflow.kas.runs",
    }
)


async def _refusal(request: web.Request, operation: str) -> web.Response | None:
    """Owner-only, and only while KAS owns workflows AND the operator opted in.

    When the switch is off we are normally exactly origin/main: every route
    answers 409 ``engine_not_kas``. The one exception (gap 5): on a Kiro Agent
    backend where the router still holds a run the operator turned the feature
    off under, the run's READS and its Stop stay open so it can be viewed and
    halted; launch/resume/retry/grant/answers are still refused. With no held
    run — or on any other backend — nothing is open, so OFF is origin/main.
    """
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.dashboard.handlers._shared import require_owner_dashboard_request
    from kiro_crew.kas_workflows.engine import backend_brings_kas_workflows, kas_workflows_enabled

    if request.get("app"):
        return _error("dashboard user required", "dashboard_user_required", 403)
    denied = await require_owner_dashboard_request(request, operation)
    if denied is not None:
        return denied
    config = await asyncio.to_thread(KiroCrewConfig.load)
    if kas_workflows_enabled(config):
        return None
    # Switch off. Read-only-held-run mode only on a Kiro Agent backend that still
    # holds at least one run; otherwise refuse everything (origin/main shape).
    if backend_brings_kas_workflows(getattr(config.agent, "acp_backend", "")):
        from kiro_crew.kas_workflows.router import get_router

        if get_router().rows():
            if operation in _READ_ONLY_WHEN_HELD:
                return None
            if operation == "workflow.kas.control" and request.match_info.get("action") == "cancel":
                return None
            return _error(
                "Kiro Workflows is turned off, so this run can be viewed but not controlled",
                "engine_not_kas",
                409,
            )
    return _error(
        "Kiro Agent is not the selected agent, so its workflows are unavailable",
        "engine_not_kas",
        409,
    )


def _project_dir(raw: Any) -> str | None:
    """An existing, non-sensitive absolute directory, or None."""
    from kiro_crew.security import is_sensitive_path

    if not isinstance(raw, str) or not raw or not os.path.isabs(raw):
        return None
    path = os.path.realpath(raw)
    if not os.path.isdir(path) or is_sensitive_path(path):
        return None
    return path


def _router_error(exc: Exception) -> web.Response:
    from kiro_crew.kas_workflows.router import KasWorkflowError

    if isinstance(exc, KasWorkflowError):
        return _error(str(_redact_obj(str(exc))), "kas_workflow_error", 409)
    logger.warning("KAS workflow route failed", exc_info=True)
    return _error("Kiro Workflows request failed", "kas_workflow_failed", 500)


async def _body(request: web.Request) -> dict[str, Any] | None:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return None
    return body if isinstance(body, dict) else None


def _send_reconnect(state: Any, router: Any, slot_key: str) -> bool:
    """Send *slot_key*'s chat a reconnect turn; False when the chat is gone."""
    from kiro_crew.dashboard.chat import _run_chat

    slot = state.get_slot(slot_key) if state is not None else None
    if slot is None:
        return False
    slot.enqueue_or_run_prompt(router.prepare_reconnect(slot_key), _run_chat, state)
    state.push_slots_update()
    return True


def boot_recovery_enabled(config: Any) -> bool:
    """Boot recovery runs only on KAS, and only with the KAS workflows switch on.

    One gateway setting now, ``agent.kas_workflows``, read through the shared
    :func:`kiro_crew.kas_workflows.engine.kas_workflows_enabled` gate (there is no
    longer a per-device preview flag to miss).
    """
    from kiro_crew.kas_workflows.engine import kas_workflows_enabled

    return kas_workflows_enabled(config)


async def recover_runs_on_boot(
    state: Any,
    *,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    load_config: Callable[[], Any] | None = None,
) -> list[tuple[str, str, str]]:
    """At gateway start, reattach the chats of this gateway's interrupted runs.

    Waits out KAS's owner-abandonment window first, so the previous gateway's
    runs read stale to KAS. Then re-reads the gate (the operator may have
    changed it meanwhile) and hands the router this gateway's chats: one
    reconnect turn per chat, awaited, so the router's concurrency bound holds.
    Never calls ``resumeAll`` (see :meth:`KasWorkflowRouter.recover_on_boot`).
    """
    from kiro_crew.config import KiroCrewConfig
    from kiro_crew.kas_workflows import router as router_mod

    reader = load_config or KiroCrewConfig.load
    if not boot_recovery_enabled(await asyncio.to_thread(reader)):
        return []
    await sleep(router_mod.OWNER_ABANDON_SECS + router_mod.BOOT_RECOVERY_SLACK_SECS)
    if not boot_recovery_enabled(await asyncio.to_thread(reader)):
        return []
    router = router_mod.get_router()

    def chat_workspace(slot_key: str) -> str | None:
        slot = state.get_slot(slot_key) if state is not None else None
        return None if slot is None else str(getattr(slot, "workspace", "") or "")

    async def reconnect(slot_key: str) -> bool:
        slot = state.get_slot(slot_key) if state is not None else None
        if slot is None:
            return False
        if getattr(slot, "running", False):
            # Its own turn reattaches it (KasWorkflowRouter.schedule_reattach).
            return False
        if not _send_reconnect(state, router, slot_key):
            return False
        task = getattr(slot, "task", None)
        if isinstance(task, asyncio.Future):
            try:
                await asyncio.wait_for(
                    asyncio.shield(task), timeout=router_mod.BOOT_RECOVERY_TURN_SECS
                )
            except Exception:  # noqa: BLE001 -- a timeout or a failed turn reports itself
                logger.debug("boot reconnect turn for %s did not settle", slot_key)
        return True

    return await router.recover_on_boot(chat_workspace, reconnect)


#: The one boot-recovery task this process started (cancelled at shutdown).
_BOOT_RECOVERY: list[asyncio.Task] = []


def start_kas_boot_recovery(state: Any) -> None:
    """Start :func:`recover_runs_on_boot` once per process, off the boot path.

    It waits out KAS's owner window before doing anything, and a failure only
    leaves interrupted runs for their chats' next turn or poll to reconnect.
    """
    if _BOOT_RECOVERY:
        return

    async def _go() -> None:
        try:
            await recover_runs_on_boot(state)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- best effort; reconnect-on-turn still works
            logger.info("KAS workflow boot recovery failed", exc_info=True)

    task = asyncio.create_task(_go(), name="kas-workflow-boot-recovery")
    _BOOT_RECOVERY.append(task)
    tasks = getattr(state, "_background_tasks", None)
    if isinstance(tasks, set):
        tasks.add(task)
        task.add_done_callback(tasks.discard)


def cancel_kas_boot_recovery() -> None:
    for task in _BOOT_RECOVERY:
        if not task.done():
            task.cancel()


def observe_turn_starter(state: Any) -> Any:
    """How the KAS router sends a chat the observe turn for a run's end.

    KAS wakes a run's parent with a model turn only when that session is idle;
    a chat mid-turn has KAS's nudge folded into the turn it is running, so there
    is no separate engine turn to read and the chat is skipped (an observe
    queued behind that turn would only read nothing and say so). A closed chat
    is skipped too.
    """

    def start(slot_key: str, message: str) -> bool:
        from kiro_crew.dashboard.chat import _run_chat

        slot = state.get_slot(slot_key)
        if slot is None or getattr(slot, "running", False):
            return False
        started = bool(slot.enqueue_or_run_prompt(message, _run_chat, state))
        state.push_slots_update()
        return started

    return start


async def _auto_reconnect(request: web.Request, router: Any) -> None:
    """While the dashboard is watching, reconnect runs that lost their process.

    A run's process can die mid-run (a chat runtime recycled or crashed, the
    gateway restarted). Each poll sends the chat of any such run a short,
    visible reconnect turn, bounded per chat by the router's interval and
    try count, so a run another process still holds is not chased forever.
    A busy chat is skipped: its own turn re-attaches it.
    """
    try:
        await router.ensure_ledger()
        state = request.app.get("state")
        for slot_key in router.due_reconnects():
            slot = state.get_slot(slot_key) if state is not None else None
            if slot is None or getattr(slot, "running", False):
                continue
            _send_reconnect(state, router, slot_key)
    except Exception:  # noqa: BLE001 -- a poll must not fail on a reconnect
        logger.debug("automatic KAS workflow reconnect failed", exc_info=True)


#: A model id or effort level a launch may name: KAS treats an unknown model as a
#: warning and an unsupported effort as the model's default, so only the shape
#: is checked here.
_OPTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")


def _launch_options(body: dict[str, Any]) -> dict[str, str] | web.Response:
    """``model`` / ``effort`` from a launch body, or a 400.

    ``creatorEffort`` is IGNORED here: KAS reads the creator step's effort from
    the PARENT session's settings (``settings.workflows.creatorEffort`` on
    ``session/new``/``session/load``), not from ``_kiro/workflow/new``, so it is
    the gateway setting ``agent.kas_creator_effort`` (kas_workflows.chat), not a
    per-launch option.
    """
    out: dict[str, str] = {}
    for key in ("model", "effort"):
        raw = body.get(key)
        if raw is None or raw == "" or raw == "auto":
            continue
        if not isinstance(raw, str) or not _OPTION_RE.match(raw):
            return _error(f"{key} must be a model or effort id", f"invalid_{key}", 400)
        out[key] = raw
    return out


#: KAS's own run-label rules (acp-type-covenant ``WorkflowRunLabelSchema``):
#: at most 100 characters, no control characters, line/paragraph separators or
#: double quotes. KAS REJECTS a label that breaks them, and the launch only
#: reaches KAS on the chat's turn, after this route has answered 202, so the
#: person's display name is made to fit here instead (the same rewrite KAS's
#: ``sanitizeRunLabelForDisplay`` applies to labels it composes itself).
_RUN_LABEL_MAX_CHARS = 100
_RUN_LABEL_FORBIDDEN_RE = re.compile("[\u0000-\u001f\u007f-\u009f\u2028\u2029]")


def _run_label(raw: Any) -> str | None:
    """A launch's ``runLabel`` made to pass KAS's rules, or None when blank."""
    if not isinstance(raw, str):
        return None
    cleaned = _RUN_LABEL_FORBIDDEN_RE.sub(" ", raw).replace('"', "'").strip()
    return cleaned[:_RUN_LABEL_MAX_CHARS].strip() or None


def _inputs(raw: Any) -> dict[str, str] | None:
    raw = raw or {}
    if not isinstance(raw, dict) or len(raw) > _MAX_INPUTS:
        return None
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str) or len(value) > _MAX_INPUT_CHARS:
            return None
        out[key] = value
    return out


#: A recipe's file lives under ``<root>/.kiro/workflows``. A new recipe's name
#: is also its file stem, so it must be a safe basename: a leading alphanumeric
#: then letters, digits, dots, dashes and underscores (no ``/``, no ``..`` — the
#: leading-alnum rule rejects a name that starts with a dot). Mirrors the
#: extension's ``RECIPE_NAME_PATTERN`` (``workflow-recipe-create.ts``).
_RECIPE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_RECIPE_FILE_SUFFIX = ".workflow.json"
_WORKFLOWS_DIR = (".kiro", "workflows")


def _recipe_name(raw: Any) -> str | None:
    """A recipe name that is a safe file stem, or None."""
    if not isinstance(raw, str):
        return None
    name = raw.strip()
    return name if _RECIPE_NAME_RE.match(name) else None


def _workflows_dir(scope: Any, cwd: str) -> str | None:
    """The ``.kiro/workflows`` directory a new recipe of *scope* is written to.

    ``workspace`` is under the project *cwd*; ``global`` is under the user's
    home. The path is composed from fixed segments and an already-validated
    name, never from client text, so nothing can escape the workflows dir.
    """
    if scope == "workspace":
        return os.path.join(cwd, *_WORKFLOWS_DIR)
    if scope == "global":
        return os.path.join(os.path.expanduser("~"), *_WORKFLOWS_DIR)
    return None


def _global_workflows_root() -> str:
    """The real ``~/.kiro/workflows`` a global recipe must sit under."""
    return os.path.join(os.path.realpath(os.path.expanduser("~")), *_WORKFLOWS_DIR)


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def _recipe_dir_allowed(resolved_dir: str, cwd: str, scope: str | None = None) -> bool:
    """Whether the REAL directory *resolved_dir* may hold a recipe Crew writes or removes.

    ``os.makedirs``/``open``/``os.remove`` follow symlinks, so a symlinked
    ``.kiro`` or ``workflows`` could lead them out of the project (workspace) or
    the user's workflows dir (global). *cwd* is already realpath'd by
    :func:`_project_dir`. With no *scope*, either root is allowed (a delete,
    where the recipe's own listing says which it is).
    """
    roots = {
        "workspace": [cwd],
        "global": [_global_workflows_root()],
        None: [cwd, _global_workflows_root()],
    }.get(scope, [])
    return any(_under(resolved_dir, root) for root in roots)


def _recipe_scaffold(name: str) -> str:
    """A minimal recipe that passes KAS validation: one coder step over a prompt.

    The same shape the extension writes (``recipeScaffold``), so a created
    recipe is immediately launchable and editable.
    """
    import json

    recipe = {
        "name": name,
        "description": "Describe what this workflow does.",
        "inputs": {"task": "prompt"},
        "steps": [{"type": "step", "id": "main", "agent": "wf-coder", "prompt": "{{task}}"}],
    }
    return json.dumps(recipe, indent=2) + "\n"


async def api_kas_recipe_detail(request: web.Request) -> web.Response:
    """GET /api/workflows/kas/recipes/detail?cwd=&source= — one recipe with its steps.

    ``source`` must be a recipe KAS lists for ``cwd`` right now (the router
    checks), so no caller can name an arbitrary file to read. A ``.workflow.json``
    recipe answers its steps' agent and prompt text; a bundled or YAML recipe
    answers ``definitionAvailable: false`` with no steps (gap 27).
    """
    refusal = await _refusal(request, "workflow.kas.recipe_detail")
    if refusal is not None:
        return refusal
    cwd = _project_dir(request.query.get("cwd"))
    if cwd is None:
        return _error("cwd must be an existing project directory", "invalid_cwd", 400)
    source = request.query.get("source")
    if not isinstance(source, str) or not source.strip():
        return _error("source is required", "invalid_source", 400)
    from kiro_crew.kas_workflows.router import get_router

    try:
        detail = await get_router().recipe_detail(cwd, source.strip())
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    return web.json_response(_redact_obj(detail))


async def api_kas_recipe_create(request: web.Request) -> web.Response:
    """POST /api/workflows/kas/recipes — {cwd, name, scope: workspace|global}.

    Writes ``<root>/.kiro/workflows/<name>.workflow.json`` with a minimal valid
    scaffold, WITHOUT overwriting an existing file (create-exclusive). ``name``
    must be a safe basename; the directory is composed from fixed segments, so
    the path cannot escape the workflows directory (gap 18).
    """
    refusal = await _refusal(request, "workflow.kas.recipe_create")
    if refusal is not None:
        return refusal
    body = await _body(request)
    if body is None:
        return _error("a JSON object body is required", "invalid_body", 400)
    cwd = _project_dir(body.get("cwd"))
    if cwd is None:
        return _error("cwd must be an existing project directory", "invalid_cwd", 400)
    name = _recipe_name(body.get("name"))
    if name is None:
        return _error(
            "name must start with a letter or digit and use only letters, digits, dots, "
            "dashes and underscores",
            "invalid_name",
            400,
        )
    scope = body.get("scope")
    directory = _workflows_dir(scope, cwd)
    if directory is None:
        return _error("scope must be 'workspace' or 'global'", "invalid_scope", 400)
    # Confine the REAL target. ``os.makedirs`` + ``open('x')`` follow symlinks,
    # so a symlinked ``.kiro`` or ``workflows`` could lead the write out of the
    # project (workspace) or the user's workflows dir (global). Resolve the
    # directory and refuse unless it still sits under the allowed root (``cwd``
    # is already realpath'd by ``_project_dir``).
    resolved_dir = os.path.realpath(directory)
    if not _recipe_dir_allowed(resolved_dir, cwd, scope):
        return _error(
            "the recipe directory resolves outside the allowed location",
            "recipe_path_escape",
            400,
        )
    path = os.path.join(resolved_dir, name + _RECIPE_FILE_SUFFIX)

    def _write() -> str | None:
        os.makedirs(resolved_dir, exist_ok=True)
        try:
            # Exclusive create: never clobber an existing recipe.
            with open(path, "x", encoding="utf-8") as handle:
                handle.write(_recipe_scaffold(name))
        except FileExistsError:
            return None
        return path

    try:
        written = await asyncio.to_thread(_write)
    except OSError:
        logger.warning("KAS recipe create failed", exc_info=True)
        return _error("The recipe file could not be written", "recipe_write_failed", 500)
    if written is None:
        return _error("A recipe with that name already exists", "recipe_exists", 409)
    _audit_authorization(request, "workflow.kas.recipe_create", f"allowed:{name}")
    return web.json_response({"name": name, "source": written}, status=201)


async def api_kas_recipe_delete(request: web.Request) -> web.Response:
    """DELETE /api/workflows/kas/recipes?cwd=&source= — remove a recipe file.

    ``source`` must be a recipe KAS lists for ``cwd`` right now and NOT read-only
    (built-in / bundled recipes have no file to remove), so only a real,
    editable recipe file is deleted — no caller can name an arbitrary path
    (gap 18).
    """
    refusal = await _refusal(request, "workflow.kas.recipe_delete")
    if refusal is not None:
        return refusal
    cwd = _project_dir(request.query.get("cwd"))
    if cwd is None:
        return _error("cwd must be an existing project directory", "invalid_cwd", 400)
    source = request.query.get("source")
    if not isinstance(source, str) or not source.strip():
        return _error("source is required", "invalid_source", 400)
    source = source.strip()
    from kiro_crew.kas_workflows.router import get_router

    try:
        recipes = await get_router().recipes(cwd)
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    match = next((r for r in recipes if r.get("source") == source), None)
    if match is None:
        return _error("That recipe is not listed for this project", "recipe_unknown", 404)
    if match.get("readOnly"):
        return _error("A built-in recipe cannot be deleted", "recipe_read_only", 409)
    if not (
        isinstance(source, str) and os.path.isabs(source) and source.endswith(_RECIPE_FILE_SUFFIX)
    ):
        return _error("That recipe has no file to delete", "recipe_no_file", 409)
    # KAS lists what it finds under ``.kiro/workflows``, symlinks followed, so a
    # listed file is not yet proof it lives where Crew may remove files: confine
    # the file's REAL directory exactly as create confines its write. Removing
    # a symlinked recipe removes the link, never its target.
    if not _recipe_dir_allowed(os.path.realpath(os.path.dirname(source)), cwd):
        return _error(
            "the recipe file resolves outside the allowed location",
            "recipe_path_escape",
            400,
        )

    def _remove() -> bool:
        try:
            os.remove(source)
        except FileNotFoundError:
            return True
        except OSError:
            return False
        return True

    if not await asyncio.to_thread(_remove):
        logger.warning("KAS recipe delete failed", exc_info=True)
        return _error("The recipe file could not be removed", "recipe_delete_failed", 500)
    _audit_authorization(request, "workflow.kas.recipe_delete", "allowed")
    return web.json_response({"deleted": True})


async def api_kas_recipes(request: web.Request) -> web.Response:
    """GET /api/workflows/kas/recipes?cwd= — the recipes KAS offers for a project."""
    refusal = await _refusal(request, "workflow.kas.recipes")
    if refusal is not None:
        return refusal
    cwd = _project_dir(request.query.get("cwd"))
    if cwd is None:
        return _error("cwd must be an existing project directory", "invalid_cwd", 400)
    from kiro_crew.kas_workflows.router import get_router

    try:
        recipes = await get_router().recipes(cwd)
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    return web.json_response(_redact_obj({"recipes": recipes}))


async def api_kas_runs(request: web.Request) -> web.Response:
    """GET /api/workflows/kas/runs?cwd= — a project's runs, from disk."""
    refusal = await _refusal(request, "workflow.kas.runs")
    if refusal is not None:
        return refusal
    cwd = _project_dir(request.query.get("cwd"))
    if cwd is None:
        return _error("cwd must be an existing project directory", "invalid_cwd", 400)
    from kiro_crew.kas_workflows.router import get_router

    router = get_router()
    try:
        runs = await router.runs(cwd)
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    await _auto_reconnect(request, router)
    return web.json_response(
        _redact_obj(
            {
                "runs": runs,
                "approvals": router.approvals(),
                "questions": router.questions(),
                "revision": router.revision,
            }
        )
    )


async def api_kas_state(request: web.Request) -> web.Response:
    """GET /api/workflows/kas/state — cheap poll: revision, known runs, approvals.

    Reads only what the router already holds and starts no process; it may send
    a reconnect turn to the chat of a run whose process died (see
    :func:`_auto_reconnect`).
    """
    refusal = await _refusal(request, "workflow.kas.state")
    if refusal is not None:
        return refusal
    from kiro_crew.kas_workflows.router import get_router

    router = get_router()
    await _auto_reconnect(request, router)
    try:
        # A chat switched to Trust after a step asked: the held step follows it.
        await router.recheck_pending()
    except Exception:  # noqa: BLE001 -- the poll still answers
        logger.debug("KAS approval recheck failed", exc_info=True)
    return web.json_response(
        _redact_obj(
            {
                "revision": router.revision,
                "runs": router.rows(),
                "approvals": router.approvals(),
                "questions": router.questions(),
                # The recipe list's own poll key: bumped on every KAS
                # ``recipes_changed`` push, so a client invalidates its cached
                # recipe list the moment the set on disk changes (gap 16).
                "recipesRevision": router.recipes_revision,
            }
        )
    )


async def api_kas_run_start(request: web.Request) -> web.Response:
    """POST /api/workflows/kas/runs — {slot, source, inputs?, runLabel?, name?}.

    ``slot`` is the chat that will parent the run; its project is the run's
    project. Answers 202: the run starts when that chat's launch turn runs.
    """
    refusal = await _refusal(request, "workflow.kas.start")
    if refusal is not None:
        return refusal
    body = await _body(request)
    if body is None:
        return _error("a JSON object body is required", "invalid_body", 400)
    slot_key = body.get("slot")
    state = request.app.get("state")
    slot = state.get_slot(slot_key) if state is not None and isinstance(slot_key, str) else None
    if slot is None:
        return _error("slot must name an open chat", "invalid_slot", 400)
    cwd = _project_dir(getattr(slot, "project", ""))
    if cwd is None:
        return _error("the chat must have a project directory", "invalid_cwd", 400)
    source = body.get("source")
    if not isinstance(source, str) or not source.strip():
        return _error("source is required", "invalid_source", 400)
    inputs = _inputs(body.get("inputs"))
    if inputs is None:
        return _error("inputs must be an object of strings", "invalid_inputs", 400)
    run_label = _run_label(body.get("runLabel"))
    name = body.get("name") if isinstance(body.get("name"), str) else ""
    options = _launch_options(body)
    if isinstance(options, web.Response):
        return options
    from kiro_crew.dashboard.chat import _run_chat
    from kiro_crew.kas_workflows.router import get_router

    try:
        launch = get_router().prepare_launch(
            slot.key,
            cwd,
            source.strip(),
            inputs,
            run_label=run_label,
            model=options.get("model"),
            effort=options.get("effort"),
        )
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    started = slot.enqueue_or_run_prompt(
        launch.message(run_label or name or source.strip()), _run_chat, state
    )
    state.push_slots_update()
    _audit_authorization(request, "workflow.kas.start", "allowed")
    return web.json_response({"slot": slot.key, "queued": not started}, status=202)


async def api_kas_run_get(request: web.Request) -> web.Response:
    """GET /api/workflows/kas/runs/{workflow_id} — one run with its approvals."""
    refusal = await _refusal(request, "workflow.kas.run")
    if refusal is not None:
        return refusal
    from kiro_crew.kas_workflows.router import get_router

    try:
        row = get_router().run(request.match_info["workflow_id"])
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    return web.json_response(_redact_obj(row))


async def api_kas_slot_runs(request: web.Request) -> web.Response:
    """GET /api/workflows/kas/slots/{slot}/runs — one chat's runs, reloaded after a restart."""
    refusal = await _refusal(request, "workflow.kas.slot_runs")
    if refusal is not None:
        return refusal
    from kiro_crew.kas_workflows.router import get_router

    router = get_router()
    try:
        runs = await router.chat_runs(request.match_info["slot"])
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    return web.json_response(
        _redact_obj(
            {
                "runs": runs,
                "approvals": router.approvals(),
                "questions": router.questions(),
                "revision": router.revision,
            }
        )
    )


async def api_kas_step_transcript(request: web.Request) -> web.Response:
    """GET /api/workflows/kas/runs/{workflow_id}/steps/{node_id}/transcript — a step's chat.

    ``?path=`` (the node's runtime path) picks one iteration of a loop's step.
    """
    refusal = await _refusal(request, "workflow.kas.step_transcript")
    if refusal is not None:
        return refusal
    from kiro_crew.kas_workflows.router import StepNotStarted, get_router

    path = (request.query.get("path") or "")[:2000] or None
    try:
        body = await get_router().step_transcript(
            request.match_info["workflow_id"], request.match_info["node_id"], path
        )
    except StepNotStarted as exc:
        return _error(str(_redact_obj(str(exc))), "step_unknown", 404)
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    # Like every other KAS read: the transcript builder redacts its text fields,
    # but names, kinds and session metadata ride through raw, so the whole body
    # takes the same recursive key-and-value pass (off the loop: it can be big).
    return await _json_response_off_loop(body)


async def api_kas_run_control(request: web.Request) -> web.Response:
    """POST /api/workflows/kas/runs/{workflow_id}/{action} — pause|resume|cancel|delete."""
    refusal = await _refusal(request, "workflow.kas.control")
    if refusal is not None:
        return refusal
    from kiro_crew.kas_workflows.router import CONTROL_METHODS, get_router

    action = request.match_info["action"]
    if action not in CONTROL_METHODS:
        return _error("unknown action", "invalid_action", 400)
    body = await _body(request) or {}
    reason = body.get("reason") if isinstance(body.get("reason"), str) else None
    workflow_id = request.match_info["workflow_id"]
    router = get_router()
    try:
        await router.control(workflow_id, action, reason)
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    _audit_authorization(request, f"workflow.kas.{action}", "allowed")
    return web.json_response(
        _redact_obj({"workflowId": workflow_id, "row": router.run_row(workflow_id)})
    )


async def api_kas_step_reply(request: web.Request) -> web.Response:
    """POST /api/workflows/kas/runs/{workflow_id}/steps/{node_id}/reply — {text, path?}.

    Sends the person's reply to a step parked for their next message.
    """
    refusal = await _refusal(request, "workflow.kas.step_reply")
    if refusal is not None:
        return refusal
    body = await _body(request) or {}
    text = body.get("text")
    if not isinstance(text, str):
        return _error("text is required", "invalid_reply", 400)
    raw_path = body.get("path")
    path = raw_path[:2000] if isinstance(raw_path, str) and raw_path else None
    from kiro_crew.kas_workflows.router import StepNotStarted, get_router

    workflow_id = request.match_info["workflow_id"]
    try:
        await get_router().reply_to_step(workflow_id, request.match_info["node_id"], path, text)
    except StepNotStarted as exc:
        return _error(str(_redact_obj(str(exc))), "step_unknown", 404)
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    _audit_authorization(request, "workflow.kas.step_reply", "allowed")
    return web.json_response({"sent": True})


async def api_kas_approval_answer(request: web.Request) -> web.Response:
    """POST /api/workflows/kas/approvals/{key} — {optionId}: answer a step's prompt."""
    refusal = await _refusal(request, "workflow.kas.approve")
    if refusal is not None:
        return refusal
    body = await _body(request)
    option_id = body.get("optionId") if body else None
    if not isinstance(option_id, str) or not option_id:
        return _error("optionId is required", "invalid_option", 400)
    from kiro_crew.kas_workflows.router import get_router

    try:
        await get_router().answer(request.match_info["key"], option_id)
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    _audit_authorization(request, "workflow.kas.approve", f"answered:{option_id[:64]}")
    return web.json_response({"answered": True})


async def api_kas_slot_reconnect(request: web.Request) -> web.Response:
    """POST /api/workflows/kas/slots/{slot}/reconnect — re-attach a chat's runs now.

    Sends the chat a reconnect turn, which loads its unfinished runs on the
    chat's live Kiro Agent process and resumes any a restart interrupted.
    Resets the automatic-reconnect try count for that chat.
    """
    refusal = await _refusal(request, "workflow.kas.reconnect")
    if refusal is not None:
        return refusal
    from kiro_crew.kas_workflows.router import get_router

    router = get_router()
    await router.ensure_ledger()
    slot_key = request.match_info["slot"]
    router.reset_reconnect_tries(slot_key)
    if not _send_reconnect(request.app.get("state"), router, slot_key):
        return _error("slot must name an open chat", "invalid_slot", 400)
    _audit_authorization(request, "workflow.kas.reconnect", "allowed")
    return web.json_response({"slot": slot_key}, status=202)


def _body_path(body: dict[str, Any]) -> str | None:
    raw = body.get("path")
    if not isinstance(raw, str):
        return None
    return raw.strip("/")[:2000] or None


def _body_reason(body: dict[str, Any]) -> str | None:
    raw = body.get("reason")
    return raw if isinstance(raw, str) and raw.strip() else None


def _row_response(router: Any, workflow_id: str, **extra: Any) -> web.Response:
    return web.json_response(
        _redact_obj({"workflowId": workflow_id, "row": router.run_row(workflow_id), **extra})
    )


async def api_kas_run_retry(request: web.Request) -> web.Response:
    """POST /api/workflows/kas/runs/{workflow_id}/retry — {nodeId?, path?}.

    Retries every failed/aborted step of a finished run, or the one named
    (KAS ``_kiro/workflow/retry``).
    """
    refusal = await _refusal(request, "workflow.kas.retry")
    if refusal is not None:
        return refusal
    body = await _body(request) or {}
    raw_node = body.get("nodeId")
    if raw_node is not None and (not isinstance(raw_node, str) or not raw_node):
        return _error("nodeId must be a step id", "invalid_node", 400)
    from kiro_crew.kas_workflows.router import get_router

    workflow_id = request.match_info["workflow_id"]
    router = get_router()
    try:
        retried = await router.retry(workflow_id, raw_node, _body_path(body))
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    _audit_authorization(request, "workflow.kas.retry", "allowed")
    return _row_response(router, workflow_id, retriedNodeIds=retried)


async def api_kas_run_restart(request: web.Request) -> web.Response:
    """POST /api/workflows/kas/runs/{workflow_id}/restart — run a finished run again.

    KAS ``_kiro/workflow/invoke`` on a terminal run resets every step and starts
    from scratch.
    """
    refusal = await _refusal(request, "workflow.kas.restart")
    if refusal is not None:
        return refusal
    from kiro_crew.kas_workflows.router import get_router

    workflow_id = request.match_info["workflow_id"]
    router = get_router()
    try:
        await router.restart(workflow_id)
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    _audit_authorization(request, "workflow.kas.restart", "allowed")
    return _row_response(router, workflow_id)


async def api_kas_step_grant(request: web.Request) -> web.Response:
    """POST /api/workflows/kas/runs/{workflow_id}/steps/{node_id}/grant — {iterations, path?}.

    Grants an exhausted loop more passes (KAS ``_kiro/workflow/resume`` with
    ``extendRepeat``); done iterations are kept and KAS caps the total.
    """
    refusal = await _refusal(request, "workflow.kas.grant")
    if refusal is not None:
        return refusal
    body = await _body(request) or {}
    iterations = body.get("iterations")
    from kiro_crew.kas_workflows import wire

    if (
        isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or not 1 <= iterations <= wire.MAX_REPEAT_ITERATIONS
    ):
        return _error(
            f"iterations must be a whole number from 1 to {wire.MAX_REPEAT_ITERATIONS}",
            "invalid_iterations",
            400,
        )
    from kiro_crew.kas_workflows.router import get_router

    workflow_id = request.match_info["workflow_id"]
    router = get_router()
    try:
        await router.grant(
            workflow_id, request.match_info["node_id"], iterations, _body_reason(body)
        )
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    _audit_authorization(request, "workflow.kas.grant", f"allowed:{iterations}")
    return _row_response(router, workflow_id)


async def api_kas_step_complete(request: web.Request) -> web.Response:
    """POST /api/workflows/kas/runs/{workflow_id}/steps/{node_id}/complete — {status, reason?, path?}.

    Ends a paused step as ``completed`` or ``failed``, or ends the loop the run
    is parked on at its limit and continues (``completed`` only). KAS
    ``_kiro/workflow/update`` ``update_status`` with ``nodeId``.
    """
    refusal = await _refusal(request, "workflow.kas.complete")
    if refusal is not None:
        return refusal
    body = await _body(request) or {}
    status = body.get("status")
    if status not in ("completed", "failed"):
        return _error("status must be completed or failed", "invalid_status", 400)
    from kiro_crew.kas_workflows.router import get_router

    workflow_id = request.match_info["workflow_id"]
    router = get_router()
    try:
        message = await router.end_step(
            workflow_id,
            request.match_info["node_id"],
            status,
            _body_reason(body),
            _body_path(body),
        )
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    _audit_authorization(request, "workflow.kas.complete", f"allowed:{status}")
    return _row_response(router, workflow_id, message=message)


async def api_kas_step_cancel(request: web.Request) -> web.Response:
    """POST /api/workflows/kas/runs/{workflow_id}/steps/{node_id}/cancel — {path?}.

    Stops one running step (``session/cancel`` on its session); parallel
    siblings keep running and the stopped step can be resumed.
    """
    refusal = await _refusal(request, "workflow.kas.step_cancel")
    if refusal is not None:
        return refusal
    body = await _body(request) or {}
    from kiro_crew.kas_workflows.router import StepNotStarted, get_router

    workflow_id = request.match_info["workflow_id"]
    router = get_router()
    try:
        await router.cancel_step(workflow_id, request.match_info["node_id"], _body_path(body))
    except StepNotStarted as exc:
        return _error(str(_redact_obj(str(exc))), "step_unknown", 404)
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    _audit_authorization(request, "workflow.kas.step_cancel", "allowed")
    return _row_response(router, workflow_id)


#: Most answers one question card may carry, and the longest answer.
_MAX_ANSWERS = 16
_MAX_ANSWER_CHARS = 2000


def _answers(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict) or not raw or len(raw) > _MAX_ANSWERS:
        return None
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not key:
            return None
        values = value if isinstance(value, list) else [value]
        if not values or len(values) > _MAX_ANSWERS:
            return None
        if not all(isinstance(v, str) and 0 < len(v) <= _MAX_ANSWER_CHARS for v in values):
            return None
        out[key] = value
    return out


async def api_kas_question_answer(request: web.Request) -> web.Response:
    """POST /api/workflows/kas/questions/{key} — {answers: {id: str|[str]}} or {dismiss: true}.

    Answers a step's question (its ``user_input`` tool) with one of the offered
    answers, or dismisses it so the step goes on without one. A question is
    never answered by a chat's approval mode.
    """
    refusal = await _refusal(request, "workflow.kas.question")
    if refusal is not None:
        return refusal
    body = await _body(request)
    if body is None:
        return _error("a JSON object body is required", "invalid_body", 400)
    dismiss = body.get("dismiss") is True
    answers = None if dismiss else _answers(body.get("answers"))
    if not dismiss and answers is None:
        return _error("answers must map a question id to its answer", "invalid_answers", 400)
    from kiro_crew.kas_workflows.router import get_router

    try:
        await get_router().answer_question(request.match_info["key"], answers, dismiss=dismiss)
    except Exception as exc:  # noqa: BLE001
        return _router_error(exc)
    _audit_authorization(request, "workflow.kas.question", "dismissed" if dismiss else "answered")
    return web.json_response({"answered": not dismiss, "dismissed": dismiss})
