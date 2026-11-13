"""``POST /api/mcp/apply`` reports a change that is not an object instead of raising.

Phases 0 and 1 of ``api_mcp_apply`` already skip a change that is not an object,
but the write loop read each change with ``.get`` and raised (500).

Every filesystem seam is redirected into ``tmp_path`` by the shared ``sandbox``
fixture; nothing touches the real ``~/.kiro``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from handlers_mcp_coverage_helpers import sandbox_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _payload, _request

from kiro_crew.dashboard.handlers import mcp as mcp_mod


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["srv", None, 5])
async def test_apply_reports_a_change_that_is_not_an_object(
    sandbox: SimpleNamespace, change: object
) -> None:
    try:
        resp = await mcp_mod.api_mcp_apply(_request({"changes": [change]}))
    except AttributeError as exc:  # main reads the change with .get and raises (500)
        pytest.fail(f"apply raised on a non-object change: {exc}")

    assert resp.status == 200, _payload(resp)
    assert {"error": "change must be an object", "change": change} in _payload(resp).get(
        "results", []
    )
