"""A request that spells a switch as a string gets the switch it names.

``bool("false")`` is ``True``, so ``POST /api/diagnostics/collect
{"include_logs": "false"}`` still bundled the logs, and ``PATCH
/api/chat/folders/{id} {"hidden": "false"}`` hid the folder. A spelled
``"false"``/``"0"``/``"no"``/``"off"`` (any case) now reads as ``False`` and
its ``true`` spellings as ``True``. Every other value reads as it always has,
so nothing that works today is refused.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_folder_app, _make_state
from test_diagnostics import _CollectReq

from kiro_crew.dashboard.handlers import diagnostics as dh
from kiro_crew.diagnostics import BundleResult

_CASES = [
    ("false", False),
    ("False", False),
    ("0", False),
    ("off", False),
    ("no", False),
    ("true", True),
    (True, True),
    (False, False),
    (1, True),
    (0, False),
    (None, False),  # not a spelling: read by truthiness, as before
    ("yes please", True),  # not a spelling: read by truthiness, as before
]


def _collect_include_logs(tmp_path: Path, monkeypatch, body: dict) -> object:
    """Run the collect handler on *body*; return the include_logs it passed on."""
    monkeypatch.setattr("kiro_crew.dashboard.handlers.diagnostics.config_dir", lambda: tmp_path)
    seen: dict[str, Any] = {}

    def _collect(**kw: Any) -> BundleResult:
        seen.update(kw)
        return BundleResult(
            zip_path=tmp_path / "b.zip",
            filename="b.zip",
            included=[],
            skipped=[],
            redaction_summary={},
            github_issue_url="https://github.com/kirodotdev/KiroCrew/issues/new",
        )

    monkeypatch.setattr(dh.diagnostics, "collect_bundle", _collect)
    resp = asyncio.run(dh.api_diagnostics_collect(_CollectReq(body)))
    if resp.status != 200:
        pytest.fail(f"diagnostics collect returned {resp.status}")
    return seen["include_logs"]


@pytest.mark.parametrize(("sent", "read"), _CASES)
def test_include_logs_reads_what_it_spells(tmp_path: Path, monkeypatch, sent, read) -> None:
    assert _collect_include_logs(tmp_path, monkeypatch, {"include_logs": sent}) is read


def test_include_logs_still_defaults_on(tmp_path: Path, monkeypatch) -> None:
    assert _collect_include_logs(tmp_path, monkeypatch, {}) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["collapsed", "hidden"])
@pytest.mark.parametrize(("sent", "saved"), _CASES)
async def test_a_folder_switch_saves_what_it_spells(
    tmp_path: Path, monkeypatch, field, sent, saved
) -> None:
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    state = _make_state(tmp_path)
    client = TestClient(TestServer(_make_folder_app(state)))
    await client.start_server()
    try:
        created = await client.post("/api/chat/folders", json={"name": "Work"})
        if created.status != 201:
            pytest.fail(f"folder create returned {created.status}")
        fid = (await created.json())["id"]
        resp = await client.patch(f"/api/chat/folders/{fid}", json={field: sent})
        if resp.status != 200:
            pytest.fail(f"folder update returned {resp.status}: {await resp.text()}")
    finally:
        await client.close()

    folder = next(f for f in state._folders if f["id"] == fid)
    assert folder[field] is saved
