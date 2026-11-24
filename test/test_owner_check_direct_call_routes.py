"""Who the owner check admits on the routes that call the shared gate directly.

Each handler is judged once per caller kind, and three things are compared with
the table below: the status, the whole error body, and the operation name the
refusal is audited under.

Two gates are in play. ``require_owner_dashboard_request`` admits the owner and
refuses everyone else. ``require_owner_for_dashboard_caller`` (the task-runner
writes and every knowledge route) judges a dashboard session the same way and leaves an app token, an
internal caller and a request with no claims to their own controls.

No handler does its work here. An admitted owner stops at the owner predicate.
Any other admitted request stops at the dashboard state or the request body,
both of which are tripwires, or is answered by a later check of the handler's
own, which is not the owner check's refusal.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request

from kiro_crew.apps import routes_bot_grants
from kiro_crew.connected_computers import remote_routes
from kiro_crew.dashboard import handlers_system, slack_shared_access
from kiro_crew.dashboard.handlers import guide, knowledge
from kiro_crew.dashboard.handlers import source_providers as source
from kiro_crew.dashboard.handlers import taskrunner, wakatime

OWNER = "U_OWNER"

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class _PastTheGate(Exception):
    """The request was admitted and the handler went on."""


class _State:
    """Dashboard state that answers the owner id and nothing else."""

    owner_id = OWNER

    def __getattr__(self, name: str) -> Any:
        raise _PastTheGate(name)


# name -> (user claim, app claim, internal mark; ``None`` leaves a claim absent)
_CALLERS: dict[str, tuple[str | None, str | None, bool]] = {
    "owner": (OWNER, "", False),
    "non_owner_dashboard": ("U_OTHER", "", False),
    "stale_session": ("local-startup", "", False),
    "app_token": ("app:some-app", "some-app", False),
    "internal": (None, None, True),
    "no_claims": (None, None, False),
}

_OWNER_ONLY = (403, {"error": "owner authorization required", "code": "owner_only"})
_STALE = (
    401,
    {
        "error": "this session predates the configured owner; sign in again",
        "code": source.STALE_OWNER_SESSION_CODE,
    },
)
_OWNER_CHECK_CODES = {"owner_only", source.STALE_OWNER_SESSION_CODE}

# gate -> caller -> the refusal, or ``None`` when the gate admits the caller
_EXPECTED: dict[str, dict[str, tuple[int, dict[str, str]] | None]] = {
    "owner": {
        "owner": None,
        "non_owner_dashboard": _OWNER_ONLY,
        "stale_session": _STALE,
        "app_token": _OWNER_ONLY,
        "internal": _OWNER_ONLY,
        "no_claims": _OWNER_ONLY,
    },
    "dashboard_caller": {
        "owner": None,
        "non_owner_dashboard": _OWNER_ONLY,
        "stale_session": _STALE,
        "app_token": None,
        "internal": None,
        "no_claims": None,
    },
}

# (handler, HTTP method, gate, audited operation)
_ROUTES: list[tuple[Handler, str, str, str]] = [
    (guide.api_guide_pending, "GET", "owner", "guide.pending"),
    (guide.api_guide_observe, "POST", "owner", "guide.observe_reply"),
    (guide.api_guide_claim, "POST", "owner", "guide.claim"),
    (guide.api_guide_progress, "POST", "owner", "guide.progress"),
    (guide.api_guide_heartbeat, "POST", "owner", "guide.heartbeat"),
    (guide.api_guide_cancel, "POST", "owner", "guide.cancel_by_user"),
    (guide.api_guide_dismiss, "POST", "owner", "guide.dismiss"),
    (guide.api_guide_refuse, "POST", "owner", "guide.refuse"),
    (guide.api_guide_replay, "POST", "owner", "guide.replay"),
    (guide.api_guide_replan, "POST", "owner", "guide.replan"),
    (handlers_system.api_leaked_runtimes_reclaim, "POST", "owner", "leaked_runtimes.reclaim"),
    (wakatime.api_wakatime_oauth_authorize, "POST", "owner", "wakatime.oauth.authorize"),
    (wakatime.api_wakatime_oauth_callback, "GET", "owner", "wakatime.oauth.callback"),
    (wakatime.api_wakatime_oauth_disconnect, "POST", "owner", "wakatime.oauth.disconnect"),
    (wakatime.api_wakatime_oauth_status, "GET", "owner", "wakatime.oauth.status"),
    (slack_shared_access.api_shared_access_get, "GET", "owner", "slack.shared_access.read"),
    (slack_shared_access.api_shared_access_put, "PUT", "owner", "slack.shared_access.switch"),
    (slack_shared_access.api_shared_access_add, "POST", "owner", "slack.shared_access.add"),
    (slack_shared_access.api_shared_access_remove, "DELETE", "owner", "slack.shared_access.remove"),
    (remote_routes.api_devices, "GET", "owner", "connected_computers.devices"),
    (remote_routes.api_device_revoke, "POST", "owner", "connected_computers.revoke"),
    (remote_routes.api_device_remove, "POST", "owner", "connected_computers.remove"),
    (routes_bot_grants.handle_bot_grants, "GET", "owner", "apps.bot_grants.read"),
    (routes_bot_grants.handle_bot_grants, "PUT", "owner", "apps.bot_grants.write"),
    (taskrunner.api_taskrunner_start, "POST", "dashboard_caller", "taskrunner.start"),
    (taskrunner.api_taskrunner_cancel, "POST", "dashboard_caller", "taskrunner.cancel"),
    (taskrunner.api_taskrunner_pause, "POST", "dashboard_caller", "taskrunner.pause"),
    (taskrunner.api_taskrunner_delete, "DELETE", "dashboard_caller", "taskrunner.delete"),
    (taskrunner.api_taskrunner_rename, "PATCH", "dashboard_caller", "taskrunner.rename"),
    (taskrunner.api_taskrunner_update_task, "PATCH", "dashboard_caller", "taskrunner.update_task"),
    (taskrunner.api_taskrunner_retry, "POST", "dashboard_caller", "taskrunner.retry"),
    (taskrunner.api_taskrunner_to_chat, "POST", "dashboard_caller", "taskrunner.to_chat"),
    (taskrunner.api_taskrunner_plan, "POST", "dashboard_caller", "taskrunner.plan"),
    (taskrunner.api_taskrunner_plan_cancel, "POST", "dashboard_caller", "taskrunner.plan_cancel"),
    (taskrunner.api_taskrunner_update_plan, "PUT", "dashboard_caller", "taskrunner.update_plan"),
    (taskrunner.api_taskrunner_execute_plan, "POST", "dashboard_caller", "taskrunner.execute"),
    (taskrunner.api_taskrunner_from_chat, "POST", "dashboard_caller", "taskrunner.from_chat"),
    (taskrunner.api_taskrunner_refine, "POST", "dashboard_caller", "taskrunner.refine"),
    (
        taskrunner.api_taskrunner_refine_cancel,
        "POST",
        "dashboard_caller",
        "taskrunner.refine_cancel",
    ),
    (
        taskrunner.api_taskrunner_refine_answer,
        "POST",
        "dashboard_caller",
        "taskrunner.refine_answer",
    ),
    (knowledge.get_config, "GET", "dashboard_caller", "knowledge.config.read"),
    (knowledge.list_items, "GET", "dashboard_caller", "knowledge.items.read"),
    (knowledge.list_namespaces, "GET", "dashboard_caller", "knowledge.namespaces.read"),
    (knowledge.get_stats, "GET", "dashboard_caller", "knowledge.stats.read"),
    (knowledge.list_sources, "GET", "dashboard_caller", "knowledge.sources.read"),
    (knowledge.source_counts, "GET", "dashboard_caller", "knowledge.source_counts.read"),
    (knowledge.add_source, "POST", "dashboard_caller", "knowledge.source.add"),
    (knowledge.pick_folder, "POST", "dashboard_caller", "knowledge.source.pick_folder"),
    (knowledge.sync_source, "POST", "dashboard_caller", "knowledge.source.sync"),
    (knowledge.confirm_source, "POST", "dashboard_caller", "knowledge.source.confirm"),
    (knowledge.pause_source, "POST", "dashboard_caller", "knowledge.source.pause"),
    (knowledge.resume_source, "POST", "dashboard_caller", "knowledge.source.resume"),
    (knowledge.list_source_files, "GET", "dashboard_caller", "knowledge.source.files.read"),
    (knowledge.retry_file, "POST", "dashboard_caller", "knowledge.source.file_retry"),
    (knowledge.skip_file, "POST", "dashboard_caller", "knowledge.source.file_skip"),
    (knowledge.ingest_text, "POST", "dashboard_caller", "knowledge.source.ingest_text"),
    (knowledge.delete_source, "DELETE", "dashboard_caller", "knowledge.source.delete"),
    (knowledge.rename_source, "PATCH", "dashboard_caller", "knowledge.source.rename"),
    (knowledge.list_entities, "GET", "dashboard_caller", "knowledge.entities.read"),
    (knowledge.get_full_graph, "GET", "dashboard_caller", "knowledge.graph.read"),
    (knowledge.export_all, "GET", "dashboard_caller", "knowledge.export"),
    (knowledge.ingest_file, "POST", "dashboard_caller", "knowledge.ingest"),
    (
        knowledge.add_agent_document_route,
        "POST",
        "dashboard_caller",
        "knowledge.agent_document.add",
    ),
    (knowledge.import_bundle, "POST", "dashboard_caller", "knowledge.import"),
    (knowledge.get_item, "GET", "dashboard_caller", "knowledge.item.read"),
    (knowledge.update_item, "PATCH", "dashboard_caller", "knowledge.item.update"),
    (knowledge.delete_item, "DELETE", "dashboard_caller", "knowledge.item.delete"),
    (knowledge.get_item_content, "GET", "dashboard_caller", "knowledge.item.content.read"),
    (knowledge.get_related_items, "GET", "dashboard_caller", "knowledge.item.related.read"),
    (knowledge.export_item, "GET", "dashboard_caller", "knowledge.item.export"),
    (knowledge.get_entity_items, "GET", "dashboard_caller", "knowledge.entity.items.read"),
    (knowledge.get_entity_graph, "GET", "dashboard_caller", "knowledge.entity.graph.read"),
    (knowledge.get_job, "GET", "dashboard_caller", "knowledge.job.read"),
    (
        knowledge.get_embedding_status,
        "GET",
        "dashboard_caller",
        "knowledge.embedding.status.read",
    ),
    (knowledge.batch_embed_items, "POST", "dashboard_caller", "knowledge.embedding.generate"),
    (
        knowledge.search_for_context,
        "GET",
        "dashboard_caller",
        "knowledge.search_for_context.read",
    ),
]

_CASES = [
    pytest.param(handler, method, gate, operation, caller, id=f"{operation}-{method}-{caller}")
    for handler, method, gate, operation in _ROUTES
    for caller in _CALLERS
]


@pytest.fixture(autouse=True)
def _audit(_floor_monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """The security-event log is a double; a refusal's record is read from it."""
    audit = MagicMock()
    _floor_monkeypatch.setattr("kiro_crew.sel.sel", lambda: audit)
    return audit


@pytest.fixture(autouse=True)
def _stop_at_the_gate(_floor_monkeypatch: pytest.MonkeyPatch) -> None:
    """Stop an admitted owner at the predicate, before the handler does any work."""
    is_owner = source.is_owner_dashboard_request

    def _owner_or_stop(request: web.Request) -> bool:
        if is_owner(request):
            raise _PastTheGate("owner")
        return False

    _floor_monkeypatch.setattr(source, "is_owner_dashboard_request", _owner_or_stop)


def _request(method: str, caller: str) -> web.Request:
    user, app_claim, internal = _CALLERS[caller]
    app = web.Application()
    app["state"] = _State()
    request = make_mocked_request(
        method, "/x", app=app, match_info={"name": "demo", "task_id": "t-1", "id": "1"}
    )
    if user is not None:
        request["user"] = user
    if app_claim is not None:
        request["app"] = app_claim
    if internal:
        request["internal_auth"] = True
    for reader in ("json", "read", "text", "post"):
        setattr(request, reader, AsyncMock(side_effect=_PastTheGate(reader)))
    return request


@pytest.mark.parametrize(("handler", "method", "gate", "operation", "caller"), _CASES)
@pytest.mark.asyncio
async def test_each_route_judges_each_caller_as_the_table_says(
    handler: Handler, method: str, gate: str, operation: str, caller: str, _audit: MagicMock
) -> None:
    expected = _EXPECTED[gate][caller]
    request = _request(method, caller)
    if expected is None:
        # Past the gate a handler either trips a wire or answers from a later
        # check of its own; neither is the owner check's refusal.
        try:
            later = await handler(request)
        except _PastTheGate:
            later = None
        if later is not None:
            assert isinstance(later, web.Response) and later.body is not None
            assert json.loads(bytes(later.body).decode()).get("code") not in _OWNER_CHECK_CODES
        for call in _audit.log_api_access.call_args_list:
            assert call.kwargs.get("resources") != "non_owner_block"
        return
    resp = await handler(request)
    status, body = expected
    assert isinstance(resp, web.Response) and resp.body is not None
    assert resp.status == status
    assert json.loads(bytes(resp.body).decode()) == body
    _audit.log_api_access.assert_called_once_with(
        caller=request.get("user") or "unknown",
        operation=operation,
        outcome="denied",
        source="dashboard",
        resources="non_owner_block",
    )
