"""A JSON boolean ``budget_total`` on a workflow run is no ceiling, not a ceiling of 1.

JSON ``true`` and ``false`` arrive as a Python ``bool``, which is an ``int``
subclass. ``POST /api/workflows/run`` and ``/run_intent`` took any ``int`` as the
run's token ceiling, so ``true`` started a run with a budget of 1 and ``false``
one of 0, and either run ended at its first agent call. The saved-definition
run route already ignored a bool; these two now read the field the same way,
through ``_opt_int``. A real integer still reaches the service unchanged.
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
        **{method: AsyncMock(return_value={"run_id": "new-run"}) for *_, method in _ROUTES}
    )


async def _post(env, entry, path, body):
    service = _service()
    env.state.workflow_service = service
    request = make_request(
        env.state, path, method="POST", body=body, owner=True, session="dashboard:global"
    )
    response = await getattr(workflows, f"api_workflow_{entry}")(request)
    return response, service


@pytest.mark.asyncio
@pytest.mark.parametrize("entry,path,body,method", _ROUTES)
@pytest.mark.parametrize("flag", [True, False])
async def test_a_boolean_budget_is_no_ceiling(env, entry, path, body, method, flag):
    response, service = await _post(env, entry, path, {**body, "budget_total": flag})
    assert response.status == 200
    assert getattr(service, method).await_args.kwargs["budget_total"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("entry,path,body,method", _ROUTES)
async def test_an_integer_budget_still_reaches_the_service(env, entry, path, body, method):
    response, service = await _post(env, entry, path, {**body, "budget_total": 5000})
    assert response.status == 200
    assert getattr(service, method).await_args.kwargs["budget_total"] == 5000
