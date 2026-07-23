"""Project management API handlers — thin alias over task runner runs."""

from aiohttp import web

from kiro_crew.security import redact_credentials, redact_exfiltration_urls

_VISIBLE_SOURCES = {"text", "spec", "file", "chat", "dashboard", "mcp"}


def _runner(request):
    return request.app["state"].task_runner


def _is_hidden(run) -> bool:
    return run.source not in _VISIBLE_SOURCES


def _redact(text: str) -> str:
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    return text


def _run_to_project(run) -> dict:
    desc = getattr(run, "description", None) or run.spec_content or run.original_input or ""
    return {
        "id": run.task_id,
        "name": _redact(run.name or run.task_id),
        "description": _redact(desc[:4000]),
        "status": run.status,
        "created_at": run.started_at or 0,
        "updated_at": getattr(run, "updated_at", run.started_at) or 0,
    }


async def api_projects_list(request):
    tr = _runner(request)
    if not tr:
        return web.json_response([])
    runs = sorted(
        (r for r in tr._runs.values() if r.source in _VISIBLE_SOURCES),
        key=lambda r: r.started_at or 0,
        reverse=True,
    )
    return web.json_response([_run_to_project(r) for r in runs])


async def api_project_get(request):
    tr = _runner(request)
    pid = request.match_info["id"]
    run = tr._runs.get(pid) if tr else None
    if not run or _is_hidden(run):
        raise web.HTTPNotFound(text=f"Project {pid} not found")
    return web.json_response(_run_to_project(run))


async def api_project_create(request):
    return web.json_response({"error": "Use 'task run <spec>' to create projects"}, status=400)


async def api_project_update(request):
    tr = _runner(request)
    pid = request.match_info["id"]
    data = await request.json()
    run = tr._runs.get(pid) if tr else None
    if not run or _is_hidden(run):
        raise web.HTTPNotFound(text=f"Project {pid} not found")
    if "name" in data:
        run.name = data["name"]
        await tr._apersist_runs()
    return web.json_response(_run_to_project(run))


async def api_project_delete(request):
    tr = _runner(request)
    pid = request.match_info["id"]
    if not tr or not await tr.delete_run(pid):
        raise web.HTTPNotFound(text=f"Project {pid} not found")
    return web.json_response({"ok": True})


async def api_activities_list(request):
    return web.json_response([])


async def api_comment_add(request):
    return web.json_response({"ok": True}, status=201)


async def api_comments_list(request):
    return web.json_response([])


async def api_comment_delete(request):
    return web.json_response({"ok": True})
