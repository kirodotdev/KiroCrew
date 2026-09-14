"""Phase 3 (J3): a supervised gateway recognises the supervising CLI's
``kiro-cli:<sessionId>`` keys as established sessions; an unsupervised one does not.

Regression for the D3 live finding where every memory/lesson/ledger/cron write
from the Kiro CLI was refused ``unknown_session`` (the CLI's sessions have no
slot, restricted-key entry or JSONL in the sidecar).
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kiro_crew.dashboard.handlers import cron as cron_handlers
from kiro_crew.validation import SUPERVISOR_SESSION_KEY_PREFIX


def _state(*, supervised: bool) -> SimpleNamespace:
    return SimpleNamespace(_slots={}, _restricted_keys=set(), supervised=supervised)


@pytest.fixture(autouse=True)
def _quiet_sel(monkeypatch):
    calls: list[dict] = []

    class _Sel:
        def log_api_access(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(cron_handlers, "_sel", lambda: _Sel())
    # No JSONL on disk for any probed slot name.
    monkeypatch.setattr(cron_handlers, "_probe_persisted_session", lambda _name: (False, None))
    return calls


@pytest.mark.asyncio
async def test_supervised_gateway_recognises_kiro_cli_namespace(_quiet_sel) -> None:
    refusal = await cron_handlers._recognize_session(
        _state(supervised=True),
        f"{SUPERVISOR_SESSION_KEY_PREFIX}sess-42",
        "learn_add",
        blocks_persisted_mode=lambda _m: True,
    )
    assert refusal is None
    assert any(
        c.get("resources") == "supervisor_session" and c.get("outcome") == "allowed"
        for c in _quiet_sel
    )


@pytest.mark.asyncio
async def test_unsupervised_gateway_still_refuses_kiro_cli_namespace(_quiet_sel) -> None:
    refusal = await cron_handlers._recognize_session(
        _state(supervised=False),
        f"{SUPERVISOR_SESSION_KEY_PREFIX}sess-42",
        "learn_add",
        blocks_persisted_mode=lambda _m: True,
    )
    assert refusal is not None
    assert refusal.status == 400
    assert json.loads(refusal.text)["code"] == "unknown_session"


@pytest.mark.asyncio
async def test_supervised_gateway_does_not_widen_other_unknown_keys(_quiet_sel) -> None:
    refusal = await cron_handlers._recognize_session(
        _state(supervised=True),
        "some-random-key",
        "learn_add",
        blocks_persisted_mode=lambda _m: True,
    )
    assert refusal is not None
    assert json.loads(refusal.text)["code"] == "unknown_session"


@pytest.mark.asyncio
async def test_missing_key_still_refused_when_supervised(_quiet_sel) -> None:
    refusal = await cron_handlers._recognize_session(
        _state(supervised=True), "", "learn_add", blocks_persisted_mode=lambda _m: True
    )
    assert refusal is not None
    assert json.loads(refusal.text)["code"] == "missing_session_key"
