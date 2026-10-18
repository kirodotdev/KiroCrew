"""The spool fixture and builders the MCP Apps render test modules share.

``spool`` (declared ``@pytest.fixture(name="spool")`` and imported under its
function name) points the render spool at a fresh directory under ``tmp_path``;
``_hex`` mints a marker id, ``_write_spool`` writes a spool record with its schema
version defaulted, and ``_FakeState`` records every ``broadcast_ws`` call.
``test_mcp_apps_render.py`` and ``test_mcp_apps_render_claim_recovery.py`` import
them.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from kiro_crew import mcp_apps_render


def _hex() -> str:
    return uuid.uuid4().hex  # 32 lowercase hex chars


@pytest.fixture(name="spool")
def spool_fixture(tmp_path, monkeypatch):
    d = tmp_path / "mcp-apps"
    d.mkdir()
    monkeypatch.setenv("KIROCREW_MCP_APPS_SPOOL", str(d))
    return d


def _write_spool(spool_dir: Path, sid: str, payload: dict) -> None:
    # Readers enforce the schema version — default it so tests exercise the
    # fields they care about; schema-rejection tests set it explicitly.
    payload.setdefault("schema", mcp_apps_render.SPOOL_SCHEMA_VERSION)
    (spool_dir / f"{sid}.json").write_text(json.dumps(payload), encoding="utf-8")


class _FakeState:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def broadcast_ws(self, msg_type: str, data: dict) -> None:
        self.calls.append((msg_type, data))
