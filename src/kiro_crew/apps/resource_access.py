"""Explicit resource delegation without dashboard-owner impersonation.

Installed API grants name a whole resource operation, not a file path or a
wildcard. Providers resolve native IDs themselves; the gateway never turns a
caller-authored path, command or URL into a resource.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps.document_store import DocumentStore, DocumentStoreError

_SEGMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_REVISION = re.compile(r"[a-fA-F0-9]{64}\Z")
MAX_RESOURCE_BYTES = 1024 * 1024
MAX_RESOURCE_WIRE_BYTES = MAX_RESOURCE_BYTES * 2 + 4096


class ResourceAccessError(Exception):
    """A stable public refusal without upstream exception text."""

    def __init__(self, code: str, status: int = 400):
        self.code = code
        self.status = status
        super().__init__(code)


def resource_path(
    workspace: str, provider: str, resource_id: str, document: str, operation: str
) -> str:
    if operation not in {"read", "create", "update"}:
        raise ResourceAccessError("unsupported_operation")
    parts = (workspace, provider, resource_id, document, operation)
    if any(not isinstance(p, str) or not _SEGMENT.fullmatch(p) or ".." in p for p in parts):
        raise ResourceAccessError("invalid_resource")
    return "/api/resource-access/" + "/".join(parts)


@dataclass(frozen=True)
class BoundDocument:
    root: Path
    relative: str

    def store(self) -> DocumentStore:
        return DocumentStore(self.root)


Callback = Callable[[Path, str, str, str, dict], Awaitable[dict]]
OwnerLocator = Callable[[Path], Awaitable[BoundDocument | None]]


@dataclass(frozen=True)
class _Provider:
    callback: Callback
    enabled: Callable[[], bool]
    owner_locator: OwnerLocator | None = None


def _workspace_root(name: str) -> Path | None:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.config.loader_locations import workspace_dir_from_entry

    entry = KiroCrewConfig.load().workspaces.get(name)
    if entry is None:
        return None
    root = workspace_dir_from_entry(entry)
    return root if root.is_dir() else None


def _grants(app_name: str) -> tuple[str, ...]:
    from kiro_crew.apps.host_exec_grants import host_exec_granted
    from kiro_crew.apps.manager import is_app_enabled
    from kiro_crew.dashboard.token_auth_app_scope import _app_api_allowlist

    if not host_exec_granted(app_name) or not is_app_enabled(app_name):
        return ()
    return _app_api_allowlist(app_name)


def _owner(request: web.Request) -> bool:
    from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request

    return is_owner_dashboard_request(request)


def _validate_body(operation: str, body: dict) -> None:
    if not isinstance(body, dict):
        raise ResourceAccessError("invalid_body")
    if operation == "read":
        if body:
            raise ResourceAccessError("invalid_body")
        return
    if set(body) != {"content", "expectedRevision"} or not isinstance(body["content"], str):
        raise ResourceAccessError("invalid_body")
    try:
        if len(body["content"].encode("utf-8")) > MAX_RESOURCE_BYTES:
            raise ResourceAccessError("size_limit", 413)
    except UnicodeEncodeError as exc:
        raise ResourceAccessError("invalid_content") from exc
    snapshot_wire = json.dumps(
        {"content": body["content"], "revision": "0" * 64}, ensure_ascii=False
    )
    if len(snapshot_wire.encode("utf-8")) > MAX_RESOURCE_WIRE_BYTES - 4096:
        raise ResourceAccessError("size_limit", 413)
    revision = body["expectedRevision"]
    if operation == "create" and revision is not None:
        raise ResourceAccessError("invalid_revision")
    if operation == "update" and (
        not isinstance(revision, str) or not _REVISION.fullmatch(revision)
    ):
        raise ResourceAccessError("invalid_revision")


class ResourceRegistry:
    """One gateway's native providers; no caller state or credentials are retained."""

    def __init__(self, workspace_resolver=_workspace_root, grants=_grants, owner_check=_owner):
        self.workspace_resolver = workspace_resolver
        self.grants = grants
        self.owner_check = owner_check
        self.providers: dict[str, _Provider] = {}

    def register(
        self,
        name: str,
        callback: Callback,
        *,
        enabled=lambda: True,
        owner_locator: OwnerLocator | None = None,
    ) -> None:
        resource_path("workspace", name, "resource", "document", "read")
        previous = self.providers.get(name)
        if previous is not None and (
            previous.callback.__module__,
            previous.callback.__qualname__,
        ) != (callback.__module__, callback.__qualname__):
            raise ValueError("duplicate_resource_provider")
        self.providers[name] = _Provider(callback, enabled, owner_locator)

    async def dispatch(
        self,
        request: web.Request,
        workspace: str,
        provider: str,
        resource_id: str,
        document: str,
        operation: str,
        body: dict,
    ) -> dict:
        grant = resource_path(workspace, provider, resource_id, document, operation)
        if not request.get("user"):
            raise ResourceAccessError("resource_forbidden", 403)
        app_name = request.get("app")
        if app_name:
            admitted = await asyncio.to_thread(self.grants, app_name)
            if grant not in admitted:
                raise ResourceAccessError("resource_forbidden", 403)
        elif app_name != "" or not self.owner_check(request):
            raise ResourceAccessError("resource_forbidden", 403)
        root = await asyncio.to_thread(self.workspace_resolver, workspace)
        if root is None:
            raise ResourceAccessError("workspace_forbidden", 403)
        binding = self.providers.get(provider)
        if binding is None or not await asyncio.to_thread(binding.enabled):
            raise ResourceAccessError("resource_unavailable", 404)
        _validate_body(operation, body)
        try:
            result = await binding.callback(Path(root), resource_id, document, operation, body)
        except ResourceAccessError:
            raise
        except DocumentStoreError as exc:
            code = exc.code
            if code == "conflict":
                code = "name_collision" if operation == "create" else "revision_conflict"
            status = 409 if code in {"revision_conflict", "name_collision"} else 400
            if exc.code == "not_found":
                status = 404
            if exc.code == "document_too_large":
                code, status = "size_limit", 413
            if exc.code == "write_unverified":
                code, status = "write_outcome_unknown", 500
            raise ResourceAccessError(code, status) from exc
        except ValueError as exc:
            code = str(exc)
            allowed = {
                "unsupported_operation",
                "resource_not_found",
                "invalid_document",
                "invalid_body",
                "resource_scope_mismatch",
            }
            if code not in allowed:
                code = "resource_unavailable"
            raise ResourceAccessError(
                code, 404 if code.endswith(("not_found", "unavailable")) else 400
            ) from exc
        except Exception as exc:
            raise ResourceAccessError("resource_unavailable", 500) from exc
        if not isinstance(result, dict):
            raise ResourceAccessError("invalid_resource_result", 500)
        if (
            len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            > MAX_RESOURCE_WIRE_BYTES - 1024
        ):
            if operation != "read":
                raise ResourceAccessError("write_outcome_unknown", 500)
            raise ResourceAccessError("size_limit", 413)
        return result

    async def owner_document(self, path: Path) -> BoundDocument | None:
        for binding in self.providers.values():
            if binding.owner_locator is not None:
                found = await binding.owner_locator(path)
                if found is not None:
                    return found
        return None


REGISTRY = web.AppKey("resource_access_registry", ResourceRegistry)


def registry_for(app: web.Application) -> ResourceRegistry:
    return app[REGISTRY]


def register_provider(app: web.Application, name: str, callback: Callback, **kwargs: Any) -> None:
    if REGISTRY not in app:
        app[REGISTRY] = ResourceRegistry()
    registry_for(app).register(name, callback, **kwargs)


async def _handle(request: web.Request) -> web.Response:
    try:
        if request.content_length is not None and request.content_length > MAX_RESOURCE_BYTES * 2:
            raise ResourceAccessError("size_limit", 413)
        from kiro_crew.dashboard.handlers._shared import read_bounded_json

        body, error = await read_bounded_json(request, max_bytes=MAX_RESOURCE_BYTES * 2)
        if error is not None:
            return error
        if not isinstance(body, dict):
            raise ResourceAccessError("invalid_body")
        match = request.match_info
        fields = {
            key: match[key]
            for key in ("workspace", "provider", "resource_id", "document", "operation")
        }
        result = await registry_for(request.app).dispatch(request, **fields, body=body)
        await asyncio.to_thread(_audit, request, "allowed")
        return web.json_response(
            {
                "workspace": fields["workspace"],
                "provider": fields["provider"],
                "resourceId": fields["resource_id"],
                "document": fields["document"],
                "operation": fields["operation"],
                "result": result,
            }
        )
    except ResourceAccessError as exc:
        await asyncio.to_thread(_audit, request, "denied")
        return web.json_response({"error": exc.code}, status=exc.status)


def _audit(request: web.Request, outcome: str) -> None:
    from kiro_crew.sel import sel

    try:
        sel().log_api_access(
            caller=str(request.get("app") or "dashboard"),
            operation="resource_access",
            outcome=outcome,
            source="dashboard",
            resources=hashlib.sha256(request.path.encode()).hexdigest(),
        )
    except Exception:
        pass


def register_routes(app: web.Application) -> None:
    if REGISTRY not in app:
        app[REGISTRY] = ResourceRegistry()
    app.router.add_post(
        "/api/resource-access/{workspace}/{provider}/{resource_id}/{document}/{operation}",
        _handle,
    )
