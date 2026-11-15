"""A failed agent-spec write is reported, not answered as ``applied``.

The agent-spec sync logged an ``OSError`` from the ``kirocrew.json`` write and
returned, so ``POST /api/mcp/toggle`` answered ``200 applied`` while the spec still
mounted the server. The write now raises like the read does; a held file is the
callers' existing 503 ``mcp_config_busy``, any other write failure their 500
``config_write_failed``.
"""

from __future__ import annotations

import errno
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from handlers_mcp_coverage_helpers import _request as _owner_request
from handlers_mcp_coverage_helpers import sandbox_fixture  # noqa: F401
from handlers_mcp_coverage_helpers import _write_global

from kiro_crew.dashboard.handlers import _shared, agents
from kiro_crew.dashboard.handlers import mcp as mcp_mod
from kiro_crew.dashboard.handlers import mcp_agent_sync

# Taken at import: the sandbox fixture replaces the sync with a recorder.
_REAL_SYNC = mcp_agent_sync._sync_mcp_to_agent
_REAL_WRITE = mcp_agent_sync._atomic_json_write


def _agent_spec(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: OSError) -> Path:
    p = tmp_path / "kirocrew.json"
    p.write_text(
        json.dumps({"mcpServers": {"srv": {"command": "x"}}, "tools": ["@srv"]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(agents, "_installed_agent_config", lambda: p)
    monkeypatch.setattr(mcp_mod, "sel", lambda: MagicMock())

    def _failing_write(path: Any, data: Any) -> None:
        # Only the agent spec fails; every other file the route writes still lands.
        if Path(path) == p:
            raise exc
        _REAL_WRITE(path, data)

    monkeypatch.setattr(mcp_mod, "_atomic_json_write", _failing_write)
    return p


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_the_sync_raises_a_failed_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: bool
) -> None:
    _agent_spec(tmp_path, monkeypatch, OSError(errno.ENOSPC, "No space left on device"))

    raised = None
    try:
        if batch:
            mcp_agent_sync._sync_mcp_to_agent_batch_unlocked(["srv"], False)
        else:
            mcp_agent_sync._sync_mcp_to_agent_unlocked("srv", False)
    except OSError as exc:
        raised = exc

    assert raised is not None, "a failed agent-spec write was swallowed"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("exc", "status", "code"),
    [
        (OSError(errno.ENOSPC, "No space left on device"), 500, "config_write_failed"),
        (PermissionError(errno.EACCES, "Permission denied"), 503, "mcp_config_busy"),
    ],
    ids=["disk-full", "held"],
)
async def test_toggle_reports_a_failed_agent_spec_write(
    sandbox: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc: OSError,
    status: int,
    code: str,
) -> None:
    spec = _agent_spec(tmp_path, monkeypatch, exc)
    _write_global(sandbox, {"srv": {"command": "x"}})
    monkeypatch.setattr(mcp_mod, "_sync_mcp_to_agent", _REAL_SYNC)
    monkeypatch.setattr(mcp_mod, "_agent_home_not_owned", lambda _name: None)
    no_manager = MagicMock()
    no_manager.available.return_value = False
    monkeypatch.setattr(_shared, "_capability_manager", lambda: no_manager)
    before = spec.read_bytes()

    resp = await mcp_mod.api_mcp_toggle(_owner_request({"name": "srv", "enabled": False}))

    assert spec.read_bytes() == before
    assert resp.status == status
    assert json.loads(resp.body).get("code") == code


@pytest.mark.asyncio
async def test_server_register_reports_a_failed_agent_spec_write(
    sandbox: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The PUT route had no arm at all: a failed write now answers a coded 500."""
    _agent_spec(tmp_path, monkeypatch, OSError(errno.ENOSPC, "No space left on device"))
    monkeypatch.setattr(mcp_mod, "_sync_mcp_to_agent", _REAL_SYNC)
    monkeypatch.setattr(mcp_mod, "_agent_home_not_owned", lambda _name: None)

    try:
        resp = await mcp_mod.api_mcp_server_detail(
            _owner_request({"command": "node"}, match_info={"name": "srv"}, method="PUT")
        )
    except OSError as exc:  # main swallowed the write; a bare raise would be a 500 too
        pytest.fail(f"register raised instead of answering: {exc}")

    assert resp.status == 500
    assert json.loads(resp.body).get("code") == "config_write_failed"
