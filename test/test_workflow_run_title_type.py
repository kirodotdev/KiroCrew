"""A workflow run's fallback chat title is text, never the repr of an object.

The three launch routes read ``title`` through ``str(body.get("title") or "")``,
so a JSON object titled the run's fallback chat ``{'a': 1}``, a list ``['x']``
and ``true`` ``True``. Those now read as absent. Nothing is refused: the run
still starts, a string title reaches the service unchanged, and a number keeps
the text it has always produced.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import make_request

from kiro_crew.dashboard.handlers import workflows

pytestmark = pytest.mark.xdist_group("memory_workflow_http")
env = _member_env

_ROUTES = [
    ("api_workflow_run", "/api/workflows/run", {"source": "source"}, "start", None),
    (
        "api_workflow_run_intent",
        "/api/workflows/run_intent",
        {"intent": "summarize"},
        "start_from_intent",
        None,
    ),
    (
        "api_workflow_definition_run",
        "/api/workflows/definitions/nightly/run",
        {},
        "start_definition",
        {"workflow_ref": "nightly"},
    ),
]


async def _call(env, handler, path, body, method, match_info):
    service = SimpleNamespace(**{method: AsyncMock(return_value={"run_id": "new-run"})})
    env.state.workflow_service = service
    request = make_request(
        env.state, path, method="POST", body=body, owner=True, session="dashboard:global"
    )
    if match_info:
        request.match_info.update(match_info)
    response = await getattr(workflows, handler)(request)
    return response, getattr(service, method)


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,path,body,method,match_info", _ROUTES)
@pytest.mark.parametrize("title", [{"a": 1}, ["x"], True, False, None, 0, 0.0, ""])
async def test_a_title_that_is_not_text_reads_as_absent(
    env, handler, path, body, method, match_info, title
):
    response, start = await _call(env, handler, path, {**body, "title": title}, method, match_info)
    assert response.status == 200
    assert start.await_args.kwargs["fallback_title"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("handler,path,body,method,match_info", _ROUTES)
@pytest.mark.parametrize(
    "title,expected", [("Nightly report", "Nightly report"), (2026, "2026"), (1.5, "1.5")]
)
async def test_a_text_or_number_title_reaches_the_service(
    env, handler, path, body, method, match_info, title, expected
):
    response, start = await _call(env, handler, path, {**body, "title": title}, method, match_info)
    assert response.status == 200
    assert start.await_args.kwargs["fallback_title"] == expected
