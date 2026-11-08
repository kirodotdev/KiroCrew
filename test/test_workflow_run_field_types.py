"""Loosely typed workflow launch fields are read leniently, never refused.

``POST /api/workflows/run`` and ``/run_intent`` handed any truthy ``name`` to the
service, so a JSON object or list became the run's name. ``POST
/api/workflows/runs/{id}/rerun`` took a JSON ``true`` (a Python ``bool``, an
``int`` subclass) as ``from_index`` 1, and labelled a negative index rerun for
an index it never ran from. Each now reads a wrong-typed value as absent and
clamps a negative index to 0; the request still succeeds, and a well-typed value
reaches the service unchanged.
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
    ("run", "/api/workflows/run", {"source": "source"}, "start"),
    ("run_intent", "/api/workflows/run_intent", {"intent": "summarize"}, "start_from_intent"),
]


def _service():
    return SimpleNamespace(
        rerun_subtree=AsyncMock(return_value={"run_id": "new-run"}),
        **{method: AsyncMock(return_value={"run_id": "new-run"}) for *_, method in _ROUTES},
    )


async def _call(env, handler, path, body, match_info=None):
    service = _service()
    env.state.workflow_service = service
    request = make_request(
        env.state, path, method="POST", body=body, owner=True, session="dashboard:global"
    )
    if match_info:
        request.match_info.update(match_info)
    response = await getattr(workflows, handler)(request)
    return response, service


@pytest.mark.asyncio
@pytest.mark.parametrize("entry,path,body,method", _ROUTES)
@pytest.mark.parametrize("name", [{"a": 1}, ["x"], 7, True])
async def test_a_name_that_is_not_text_reads_as_absent(env, entry, path, body, method, name):
    response, service = await _call(env, f"api_workflow_{entry}", path, {**body, "name": name})
    assert response.status == 200
    assert getattr(service, method).await_args.kwargs["name"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("entry,path,body,method", _ROUTES)
async def test_a_text_name_still_reaches_the_service(env, entry, path, body, method):
    response, service = await _call(
        env, f"api_workflow_{entry}", path, {**body, "name": "Nightly report"}
    )
    assert response.status == 200
    assert getattr(service, method).await_args.kwargs["name"] == "Nightly report"


_RERUN = "/api/workflows/runs/old-run/rerun"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sent,expected",
    [(True, 0), (False, 0), ("3", 0), (1.5, 0), (None, 0), (-4, 0), (0, 0), (3, 3)],
)
async def test_a_rerun_index_is_a_whole_number_from_zero(env, sent, expected):
    response, service = await _call(
        env, "api_workflow_run_rerun", _RERUN, {"from_index": sent}, {"run_id": "old-run"}
    )
    assert response.status == 200
    args = service.rerun_subtree.await_args
    assert args.args == ("old-run", expected)


@pytest.mark.asyncio
async def test_a_bodyless_rerun_still_starts_from_zero(env):
    response, service = await _call(
        env, "api_workflow_run_rerun", _RERUN, {}, {"run_id": "old-run"}
    )
    assert response.status == 200
    assert service.rerun_subtree.await_args.args == ("old-run", 0)
