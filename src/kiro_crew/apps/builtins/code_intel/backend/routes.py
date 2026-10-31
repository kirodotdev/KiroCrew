"""Code-intel's routes, registered in the gateway at startup (``backend.routes``).

Two endpoints, both owner-only and both SEL-audited:

``POST /api/apps/code-intel/binding`` answers the app's ``contributes.editorBindings``.
Core POSTs ``{item_id, path, line, character, dirty, sha256, trigger, text?}``;
``item_id`` names the binding (``definition``, ``definition-click``,
``references``, ``hover``, ``symbols``), ``line``/``character`` are 0-based with
``character`` in UTF-16 units, ``sha256`` is the buffer's hash. The answer is
the host's own vocabulary: ``open`` (one location, or a list the host offers in
its picker), ``markdown`` (a hover's text, or why nothing was found), ``title``,
and ``stale`` when the server holds neither the hashed buffer nor a matching
file on disk, which makes core send ``text`` once, because this app holds
``permissions.editor: ["content"]``.

``POST /api/apps/code-intel/pool`` is the Language servers tab's. Body
``{"op", ...}`` with ``op`` one of:

- ``servers`` -- the pool: every server, its state, and recent failed starts.
- ``detect`` with ``path`` a directory -- the languages under it, each with its
  project root and whether a server is installed. Starts nothing.
- ``status`` with ``path`` a file -- which server would answer for it.
- ``symbols`` with ``path`` a file -- the file's outline.
- ``workspace_symbols`` with ``server``, ``root`` and ``query``.
- ``workspace_symbols_all`` with ``query`` -- every running server, merged.
- ``open_class`` with ``server``, ``root`` and ``uri`` (``jdt://``) -- writes a
  library class's source out from the running server and returns its ``path``.
- ``start`` / ``restart`` / ``stop`` with ``server`` and ``root``.

Sentences the reader sees (why there is no answer) come from the app's own
catalogs through :mod:`..i18n`, in the dashboard's UI language. The work is
done by :mod:`kiro_crew.apps.builtins.code_intel`; see
``docs/system-specs/modules/code-intel.md``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from functools import wraps
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from aiohttp import web

from kiro_crew.apps.builtins.code_intel import i18n
from kiro_crew.apps.manager import is_app_enabled
from kiro_crew.context import normalize_ui_language_tag, ui_language_tag
from kiro_crew.dashboard.handlers._shared import (
    read_bounded_json,
    require_owner_dashboard_request,
)

if TYPE_CHECKING:
    from kiro_crew.apps.builtins.code_intel import CodeIntelError

logger = logging.getLogger(__name__)

APP_NAME = "code-intel"
Handler = Callable[[web.Request], Awaitable[web.Response]]

# The body may carry the whole buffer (the retry after `stale`), so it may be as
# large as the biggest document the manager opens, plus room for the other fields.
_MAX_REQUEST_BYTES = 4 * 1024 * 1024 + 64 * 1024
_MAX_POSITION = 10_000_000
_MAX_QUERY_CHARS = 256
# A jdt:// class URI is a jar path plus a package and class name.
_MAX_CLASS_URI_CHARS = 4096
_SHA256 = re.compile(r"[0-9a-f]{64}")
# Indentation of an outline row in the host's flat picker: one figure space per level.
_INDENT = "\u2007\u2007"

# Which manager operation each binding runs.
_BINDING_OPS = {
    "definition": "definition",
    "definition-click": "definition",
    "references": "references",
    "hover": "hover",
    "symbols": "symbols",
}
# The card's heading per operation, a catalog key.
_TITLE_KEYS = {
    "definition": "definitions",
    "references": "references",
    "hover": "symbol_info",
    "symbols": "outline",
}

# HTTP status per manager error code. Anything unlisted is a 502: the server
# answered badly, not the caller.
_STATUS_BY_CODE = {
    "bad_request": 400,
    "unsupported": 422,
    "no_server": 422,
    "server_refused": 422,
    "too_large": 413,
    "not_readable": 404,
    "server_starting": 503,
    "shutting_down": 503,
    "timeout": 504,
    "text_required": 409,
    "not_running": 409,
    "no_source": 404,
}


def _sel():
    """Late-binding ``sel()``, for test monkeypatch compatibility."""
    import kiro_crew.dashboard.handlers as _pkg

    return _pkg.sel()


def _configured_language() -> str:
    from kiro_crew.config.loader import KiroCrewConfig

    try:
        return ui_language_tag(KiroCrewConfig.load())
    except Exception:
        logger.debug("code-intel: UI language lookup failed; answering in English")
        return ""


async def _lang(body: dict[str, Any]) -> str:
    """The language for the sentences this backend writes.

    The dashboard sends the language it resolved for the reader as ``lang`` on
    every request (core adds it to binding requests; the tab adds it to pool
    calls). It passes the same gate as ``dashboard.language``, which is the
    fallback for a request without it; English after that.
    """
    hint = normalize_ui_language_tag(body.get("lang"), source="code-intel lang")
    return hint or await asyncio.to_thread(_configured_language) or "en"


def _resolve_file(raw: str) -> tuple[str, str]:
    """``(canonical path, refusal code)``; the code is '' when the path is usable.

    Blocking (validation canonicalizes and walks ancestors) -- run off the loop.
    """
    from kiro_crew.dashboard.handlers.files import _validate_dashboard_path
    from kiro_crew.security import is_sensitive_path

    validated = _validate_dashboard_path(raw)
    if validated is None:
        return "", "sensitive_path"
    path = os.path.realpath(os.path.expanduser(validated))
    if is_sensitive_path(path):
        return "", "sensitive_path"
    if not os.path.isfile(path):
        return path, "not_found"
    return path, ""


def _resolve_dir(raw: str) -> tuple[str, str]:
    """``(canonical directory, refusal code)``; the code is '' when the directory is usable.

    Blocking -- run off the loop.
    """
    from kiro_crew.dashboard.handlers.files import _validate_dashboard_path
    from kiro_crew.security import is_sensitive_path

    validated = _validate_dashboard_path(raw)
    if validated is None:
        return "", "sensitive_path"
    path = os.path.realpath(os.path.expanduser(validated))
    if is_sensitive_path(path):
        return "", "sensitive_path"
    if not os.path.isdir(path):
        return path, "not_found"
    return path, ""


def _strings(value: object) -> list[str]:
    """A list of strings carried in an error's extras, or an empty list."""
    return [str(v) for v in value] if isinstance(value, (list, tuple)) else []


def _sentence(lang: str, exc: CodeIntelError) -> str:
    """The reader's sentence for a manager error, from the app's catalogs.

    Codes the catalogs do not name fall back to the server's own words, redacted:
    a start error or a JSON-RPC message can quote the environment or a command line.
    """
    from kiro_crew.security.redaction import redact_credentials

    server = str(exc.extra.get("server", ""))
    if exc.code == "no_server":
        return i18n.t(
            lang, "no_server", server=server, commands=", ".join(_strings(exc.extra.get("install")))
        )
    if exc.code == "server_refused":
        refused = _strings(exc.extra.get("refused")) or [""]
        return i18n.t(lang, "server_refused", reason=refused[0])
    if exc.code == "server_starting":
        return i18n.t(lang, "server_starting")
    if exc.code == "not_running":
        return i18n.t(lang, "not_running", server=server)
    if exc.code == "unsupported":
        return i18n.t(lang, "unsupported")
    if exc.code == "timeout":
        return i18n.t(lang, "timeout")
    return redact_credentials(str(exc))[0] or i18n.t(lang, "failed")


def _error_response(lang: str, exc: CodeIntelError) -> web.Response:
    from kiro_crew.security.redaction import redact_credentials

    status = _STATUS_BY_CODE.get(exc.code, 502)
    return web.json_response(
        {
            "error": _sentence(lang, exc),
            "code": exc.code,
            "detail": redact_credentials(str(exc))[0],
            "server": exc.extra.get("server", ""),
            "root": exc.extra.get("root", ""),
            "install": exc.extra.get("install", []),
            "refused": exc.extra.get("refused", []),
        },
        status=status,
    )


def _bad_request() -> web.Response:
    return web.json_response({"error": "Bad request", "code": "bad_request"}, status=400)


def _require_enabled(handler: Handler) -> Handler:
    """403 while the app is disabled: routes are registered once at startup, so a
    disabled app would otherwise stay callable. ``is_app_enabled`` reads a file;
    off the loop."""

    @wraps(handler)
    async def _wrapped(request: web.Request) -> web.Response:
        if not await asyncio.to_thread(is_app_enabled, APP_NAME):
            return web.json_response(
                {"error": f"{APP_NAME} is disabled", "code": "app_disabled"}, status=403
            )
        return await handler(request)

    return _wrapped


def _int_field(body: dict, name: str) -> int | None:
    value = body.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= _MAX_POSITION:
        return None
    return value


async def _resolve_file_or_refuse(
    request: web.Request, caller: str, raw_path: str
) -> tuple[str, web.Response | None]:
    from kiro_crew.apps.builtins.code_intel import pools

    loop = asyncio.get_running_loop()
    path, refusal = await loop.run_in_executor(pools.control_executor(), _resolve_file, raw_path)
    if refusal == "sensitive_path":
        _sel().log_api_access(
            caller=caller,
            operation="code_intel",
            outcome="denied",
            resources=raw_path,
            error="sensitive path",
        )
        return "", web.json_response(
            {"error": "Access denied", "code": "sensitive_path"}, status=403
        )
    if refusal == "not_found":
        return "", web.json_response({"error": "File not found", "code": "not_found"}, status=404)
    return path, None


def _why_empty(lang: str, result: dict) -> str:
    """Markdown for an answer with nothing to list: the server's notices and the setup hint."""
    lines: list[str] = []
    notices = [n.get("message", "") for n in result.get("notices", []) if isinstance(n, dict)]
    hint = result.get("hint", "")
    if notices or hint:
        lines.append(i18n.t(lang, "server_problem"))
        lines.extend(f"- {m}" for m in [*notices, *([hint] if hint else [])] if m)
    else:
        lines.append(i18n.t(lang, "nothing_here"))
    return "\n".join(lines)


def _footnotes(lang: str, result: dict) -> str:
    notes: list[str] = []
    if result.get("truncated"):
        notes.append(i18n.t(lang, "truncated"))
    external = int(result.get("external", 0) or 0)
    if external > 0:
        notes.append(i18n.plural(lang, "external", external))
    return " ".join(notes)


def _binding_answer(lang: str, op: str, path: str, result: dict) -> dict[str, Any]:
    """The host's `EditorBindingResult` for a manager answer."""
    title = i18n.t(lang, _TITLE_KEYS[op])
    if op == "hover":
        contents = result.get("contents") or ""
        return {"title": title, "markdown": contents or _why_empty(lang, result)}
    if op == "symbols":
        rows = [
            {
                "path": path,
                "line": s["line"],
                "label": f"{_INDENT * int(s.get('depth', 0))}{s.get('name', '')}",
                "detail": " ".join(x for x in (s.get("kind", ""), s.get("detail", "")) if x),
            }
            for s in result.get("symbols", [])
        ]
    else:
        rows = [
            {
                "path": loc["path"],
                "line": loc["line"],
                "character": loc.get("character"),
                "label": f"{os.path.basename(loc['path'])}:{loc['line']}",
                "detail": loc.get("preview", ""),
            }
            for loc in result.get("locations", [])
        ]
    if not rows:
        return {"title": title, "markdown": _why_empty(lang, result)}
    answer: dict[str, Any] = {
        "title": title,
        "open": rows[0] if len(rows) == 1 and op != "symbols" else rows,
    }
    notes = _footnotes(lang, result)
    if notes and len(rows) > 1:
        answer["markdown"] = notes
    return answer


async def api_binding(request: web.Request) -> web.Response:
    """POST /api/apps/code-intel/binding -- an editor binding fired. Owner-only."""
    owner_denied = await require_owner_dashboard_request(request, "code_intel")
    if owner_denied is not None:
        return owner_denied
    caller = str(request.get("user") or "dashboard")
    body, body_err = await read_bounded_json(request, max_bytes=_MAX_REQUEST_BYTES)
    if body_err is not None:
        return body_err
    assert body is not None
    lang = await _lang(body)
    op = _BINDING_OPS.get(str(body.get("item_id") or ""))
    raw_path = str(body.get("path") or "").strip()
    line = _int_field(body, "line")
    character = _int_field(body, "character")
    text = body.get("text")
    sha = body.get("sha256")
    if (
        op is None
        or not raw_path
        or line is None
        or character is None
        or (text is not None and not isinstance(text, str))
        or (sha is not None and not (isinstance(sha, str) and _SHA256.fullmatch(sha)))
    ):
        return _bad_request()
    path, refused = await _resolve_file_or_refuse(request, caller, raw_path)
    if refused is not None:
        return refused

    from kiro_crew.apps.builtins.code_intel import CodeIntelError, code_intel_manager, pools

    loop = asyncio.get_running_loop()
    try:
        manager = code_intel_manager()
        result = await loop.run_in_executor(
            pools.request_executor(), manager.handle, op, path, line, character, text, sha
        )
    except CodeIntelError as exc:
        if exc.code == "text_required":
            # The hash matched neither the server's copy nor the file: ask for the
            # text. Core sends it once because this app holds the editor grant.
            return web.json_response({"stale": True})
        if _STATUS_BY_CODE.get(exc.code, 502) >= 500:
            logger.info("code_intel: %s on %s failed: %s (%s)", op, path, exc, exc.code)
        return _error_response(lang, exc)
    _sel().log_api_access(
        caller=caller,
        operation="code_intel",
        outcome="allowed",
        resources=f"op={op} path={path} server={result.get('server', '')}",
    )
    return web.json_response(_binding_answer(lang, op, path, result))


async def api_pool(request: web.Request) -> web.Response:
    """POST /api/apps/code-intel/pool -- the Language servers tab's operations. Owner-only."""
    owner_denied = await require_owner_dashboard_request(request, "code_intel")
    if owner_denied is not None:
        return owner_denied
    caller = str(request.get("user") or "dashboard")
    body, body_err = await read_bounded_json(request, max_bytes=_MAX_REQUEST_BYTES)
    if body_err is not None:
        return body_err
    assert body is not None
    lang = await _lang(body)
    op = str(body.get("op") or "")

    from kiro_crew.apps.builtins.code_intel import CodeIntelError, code_intel_manager, pools

    loop = asyncio.get_running_loop()
    # Requests that wait on a language server share one pool; the tab's own
    # controls and the path checks run on another, so a slow server never
    # stalls the status poll or a Stop.
    pool = pools.request_executor()
    control = pools.control_executor()
    try:
        manager = code_intel_manager()
    except CodeIntelError as exc:  # the gateway is shutting down
        return _error_response(lang, exc)

    if op in ("status", "symbols"):
        raw_path = str(body.get("path") or "").strip()
        if not raw_path:
            return _bad_request()
        path, refused = await _resolve_file_or_refuse(request, caller, raw_path)
        if refused is not None:
            return refused
        try:
            result = await loop.run_in_executor(
                pool if op == "symbols" else control, manager.handle, op, path, 0, 0, None, None
            )
        except CodeIntelError as exc:
            return _error_response(lang, exc)
        _sel().log_api_access(
            caller=caller,
            operation="code_intel",
            outcome="allowed",
            resources=f"op={op} path={path} server={result.get('server', '')}",
        )
        return web.json_response(result)
    if op == "servers":
        return web.json_response(await loop.run_in_executor(control, manager.servers))
    if op == "workspace_symbols_all":
        query = body.get("query", "")
        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY_CHARS:
            return _bad_request()
        result = await loop.run_in_executor(pool, manager.workspace_symbols_all, query)
        _sel().log_api_access(
            caller=caller,
            operation="code_intel",
            outcome="allowed",
            resources=f"op={op} servers={result['servers']}",
        )
        return web.json_response(result)
    if op not in ("detect", "workspace_symbols", "open_class", "start", "restart", "stop"):
        return _bad_request()

    raw = str((body.get("path") if op == "detect" else body.get("root")) or "").strip()
    if not raw:
        return _bad_request()
    directory, refusal = await loop.run_in_executor(control, _resolve_dir, raw)
    if refusal == "sensitive_path":
        _sel().log_api_access(
            caller=caller,
            operation="code_intel",
            outcome="denied",
            resources=f"op={op} path={raw}",
            error="sensitive path",
        )
        return web.json_response({"error": "Access denied", "code": "sensitive_path"}, status=403)
    # Stop only drops the pool's entry, so it still works once the directory is gone.
    if refusal == "not_found" and op != "stop":
        return web.json_response({"error": "Directory not found", "code": "not_found"}, status=404)

    server = str(body.get("server") or "")
    query = body.get("query", "")
    try:
        if op == "detect":
            result = await loop.run_in_executor(control, manager.detect, directory)
        elif not server:
            return _bad_request()
        elif op == "open_class":
            uri = body.get("uri")
            if not isinstance(uri, str) or len(uri) > _MAX_CLASS_URI_CHARS:
                return _bad_request()
            result = await loop.run_in_executor(pool, manager.open_class, server, directory, uri)
        elif op == "workspace_symbols":
            if not isinstance(query, str) or len(query) > _MAX_QUERY_CHARS:
                return _bad_request()
            result = await loop.run_in_executor(
                pool, manager.workspace_symbols, server, directory, query
            )
        elif op in ("start", "restart"):
            method = manager.start if op == "start" else manager.restart
            await loop.run_in_executor(control, method, server, directory)
            result = {"ok": True}
        else:
            result = {"ok": await loop.run_in_executor(control, manager.stop, server, directory)}
    except CodeIntelError as exc:
        if _STATUS_BY_CODE.get(exc.code, 502) >= 500:
            logger.info("code_intel: %s on %s failed: %s (%s)", op, directory, exc, exc.code)
        return _error_response(lang, exc)
    _sel().log_api_access(
        caller=caller,
        operation="code_intel",
        outcome="allowed",
        resources=f"op={op} path={directory} server={server}",
    )
    return web.json_response(result)


def register_routes(app: web.Application) -> None:
    """Register this app's routes on the gateway's aiohttp Application."""
    app.router.add_post(f"/api/apps/{APP_NAME}/binding", _require_enabled(api_binding))
    app.router.add_post(f"/api/apps/{APP_NAME}/pool", _require_enabled(api_pool))
