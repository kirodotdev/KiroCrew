"""Every error body from the MCP toggle/remove/register/apply handlers carries a ``code``.

``error-code-baseline.json`` listed ``dashboard/handlers/mcp.py`` with 17
``missing_code`` sites and one ``dynamic_status`` site: error bodies that held
English prose and nothing a client could switch on; three of them returned the
raw ``str(exc)`` of a failed ``_write_mcp_json``. This suite pins the contract
each branch keeps now:

* the status code is unchanged;
* the body keeps its ``error`` string, so a screen that shows ``res.error`` still
  shows something, and gains a stable lower_snake ``code``;
* a write failure answers a fixed sentence, and the exception's own text goes
  to the server log.

Every filesystem seam is redirected into ``tmp_path`` by the shared ``sandbox``
fixture; nothing touches the real ``~/.kiro``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from handlers_mcp_coverage_helpers import sandbox_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _payload, _request, _write_global

from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.mcp_discovery import McpServerInfo

# A file name only this suite uses, so it can only reach a body through the
# exception's own text.
_EXC_FILE = "error-codes-suite-only/mcp.json"

Handler = Callable[[web.Request], Awaitable[web.Response]]


def _no_other_servers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Discovery sees no server in any other scope, so an unknown name is a 404."""
    import kiro_crew.mcp_discovery as disc

    monkeypatch.setattr(disc, "list_servers", lambda *a, **k: [McpServerInfo(name="other")])


def _corrupt(sandbox: SimpleNamespace) -> None:
    sandbox.global_json.write_text("{not json", encoding="utf-8")


# (id, handler, request kwargs, global servers or a setup callable, status, code)
_CASES: list[tuple[str, Handler, dict[str, Any], Any, int, str]] = [
    ("toggle-no-name", mcp_mod.api_mcp_toggle, {"body": {"enabled": False}}, None, 400,
     "name_required"),
    ("toggle-corrupt", mcp_mod.api_mcp_toggle, {"body": {"name": "srv"}}, _corrupt, 500,
     "config_corrupt"),
    ("toggle-unknown", mcp_mod.api_mcp_toggle, {"body": {"name": "ghost"}}, {}, 404,
     "mcp_server_not_found"),
    ("toggle-bad-spec", mcp_mod.api_mcp_toggle, {"body": {"name": "srv"}}, {"srv": [1]}, 500,
     "mcp_server_config_invalid"),
    ("tool-no-tool", mcp_mod.api_mcp_toggle_tool, {"body": {"server": "srv"}}, None, 400,
     "server_and_tool_required"),
    ("tool-corrupt", mcp_mod.api_mcp_toggle_tool, {"body": {"server": "s", "tool": "t"}},
     _corrupt, 500, "config_corrupt"),
    ("tool-unknown", mcp_mod.api_mcp_toggle_tool, {"body": {"server": "ghost", "tool": "t"}},
     {}, 404, "mcp_server_not_found"),
    ("tool-bad-spec", mcp_mod.api_mcp_toggle_tool, {"body": {"server": "srv", "tool": "t"}},
     {"srv": 7}, 500, "mcp_server_config_invalid"),
    ("all-corrupt", mcp_mod.api_mcp_toggle_all, {"body": {"enabled": False}}, _corrupt, 500,
     "config_corrupt"),
    ("remove-no-name", mcp_mod.api_mcp_remove, {"body": {}}, None, 400, "name_required"),
    ("detail-blank-name", mcp_mod.api_mcp_server_detail,
     {"body": None, "match_info": {"name": "  "}, "method": "DELETE"}, None, 400,
     "name_required"),
    ("put-no-command", mcp_mod.api_mcp_server_detail,
     {"body": {"args": ["x"]}, "match_info": {"name": "srv"}, "method": "PUT"}, None, 400,
     "command_required"),
    ("apply-not-list", mcp_mod.api_mcp_apply, {"body": {"changes": "srv"}}, None, 400,
     "invalid_changes"),
    ("apply-too-many", mcp_mod.api_mcp_apply,
     {"body": {"changes": [{"name": f"s{i}"} for i in range(mcp_mod._MCP_APPLY_MAX_CHANGES + 1)]}},
     None, 400, "too_many_changes"),
]  # fmt: skip


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "req", "setup", "status", "code"),
    [pytest.param(*case[1:], id=case[0]) for case in _CASES],
)
async def test_error_branch_keeps_status_and_error_and_gains_a_code(
    sandbox: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    handler: Handler,
    req: dict[str, Any],
    setup: Any,
    status: int,
    code: str,
) -> None:
    _no_other_servers(monkeypatch)
    if callable(setup):
        setup(sandbox)
    elif setup is not None:
        _write_global(sandbox, setup)
    kwargs = dict(req)
    body = kwargs.pop("body")
    resp = await handler(_request(body, **kwargs))
    payload = _payload(resp)
    assert resp.status == status, payload
    assert payload.get("code") == code, payload
    assert isinstance(payload.get("error"), str) and payload["error"], payload


@pytest.mark.asyncio
async def test_delete_of_an_absent_server_is_a_coded_404(sandbox: SimpleNamespace) -> None:
    """The DELETE miss keeps its old fields and status and gains the same code."""
    _write_global(sandbox, {})
    resp = await mcp_mod.api_mcp_server_detail(
        _request(None, match_info={"name": "ghost"}, method="DELETE")
    )
    assert resp.status == 404
    assert _payload(resp) == {
        "ok": False,
        "name": "ghost",
        "removed": False,
        "code": "mcp_server_not_found",
    }


@pytest.mark.asyncio
async def test_delete_of_a_present_server_body_is_unchanged(sandbox: SimpleNamespace) -> None:
    _write_global(sandbox, {"srv": {"command": "x"}})
    resp = await mcp_mod.api_mcp_server_detail(
        _request(None, match_info={"name": "srv"}, method="DELETE")
    )
    assert resp.status == 200
    assert _payload(resp) == {"ok": True, "name": "srv", "removed": True}


_WRITE_CASES: list[tuple[str, Handler, dict[str, Any]]] = [
    ("toggle", mcp_mod.api_mcp_toggle, {"name": "srv", "enabled": False}),
    ("toggle-tool", mcp_mod.api_mcp_toggle_tool, {"server": "srv", "tool": "t", "enabled": False}),
    ("toggle-all", mcp_mod.api_mcp_toggle_all, {"enabled": False}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler", "body"), [pytest.param(h, b, id=i) for i, h, b in _WRITE_CASES]
)
async def test_write_failure_answers_a_code_and_logs_the_exception(
    sandbox: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    handler: Handler,
    body: dict[str, Any],
) -> None:
    _write_global(sandbox, {"srv": {"command": "x"}})

    def _boom(_data: dict) -> None:
        raise PermissionError(13, "Permission denied", _EXC_FILE)

    monkeypatch.setattr(mcp_mod, "_write_mcp_json", _boom)
    with caplog.at_level(logging.WARNING, logger=mcp_mod.logger.name):
        resp = await handler(_request(body))

    payload = _payload(resp)
    assert resp.status == 500
    assert payload == {"error": "failed to write mcp.json", "code": "config_write_failed"}
    assert _EXC_FILE not in resp.text
    assert "Permission denied" not in resp.text
    # The detail is not lost: the server log keeps the exception.
    logged = [r for r in caplog.records if r.exc_info and _EXC_FILE in str(r.exc_info[1])]
    assert logged, [r.getMessage() for r in caplog.records]
    assert sandbox.synced == [] and sandbox.batched == []
