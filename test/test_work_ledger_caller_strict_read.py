"""A transient I/O fault on a worker binding or a conductor ledger is a 503,
not a false ``not_bound`` / ``no_ledger``.

``_worker_binding`` and ``_own_ledger`` read strictly, so a present-but-unreadable
file (an EACCES/EIO blip, a Windows rename race -- reproduced here by making the
path a directory, which raises ``IsADirectoryError``) refuses with 503
``ledger_unreadable`` instead of telling a bound worker it has no task or a
conductor it owns no ledger.
"""

from __future__ import annotations

import json

import pytest
import work_ledger_helpers

from kiro_crew import work_ledger
from kiro_crew.dashboard.handlers import work_ledger_caller as wlc

# The data-home pin every store suite runs under, bound into this module so
# pytest collects it as an autouse fixture here.
_isolated_home = work_ledger_helpers._isolated_home

CONDUCTOR = "chat-1-conductor"
WORKER = "chat-2-worker"


def _ledger_key(session_key: str) -> str:
    from kiro_crew import session_ledger

    return session_ledger.ledger_key(session_key)


@pytest.mark.asyncio
async def test_a_worker_binding_io_fault_is_503_not_not_bound():
    key = _ledger_key(WORKER)
    path = work_ledger.binding_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.mkdir()  # a directory where the binding file would be -> read raises

    binding, refusal = await wlc._worker_binding(key, "work_brief")
    assert binding is None
    assert refusal is not None and refusal.status == 503
    assert json.loads(refusal.text)["code"] == "ledger_unreadable"


@pytest.mark.asyncio
async def test_an_absent_binding_is_still_not_bound():
    # The missing-file case must stay 403 not_bound, not become a 503.
    key = _ledger_key(WORKER)
    binding, refusal = await wlc._worker_binding(key, "work_brief")
    assert binding is None
    assert refusal is not None and refusal.status == 403
    assert json.loads(refusal.text)["code"] == wlc.CODE_NOT_BOUND


@pytest.mark.asyncio
async def test_a_conductor_ledger_io_fault_is_503_not_no_ledger():
    key = _ledger_key(CONDUCTOR)
    cpath = work_ledger.conductor_dir(key) / work_ledger._CONDUCTOR_FILE
    cpath.parent.mkdir(parents=True, exist_ok=True)
    cpath.mkdir()  # a directory where conductor.json would be -> read raises

    record, refusal = await wlc._own_ledger(key, "work_ledger_read")
    assert record is None
    assert refusal is not None and refusal.status == 503
    assert json.loads(refusal.text)["code"] == "ledger_unreadable"


@pytest.mark.asyncio
async def test_an_absent_ledger_is_still_no_ledger():
    key = _ledger_key(CONDUCTOR)
    record, refusal = await wlc._own_ledger(key, "work_ledger_read")
    assert record is None
    assert refusal is not None and refusal.status == 404
    assert json.loads(refusal.text)["code"] == work_ledger.CODE_NO_LEDGER


@pytest.mark.asyncio
async def test_a_dispatch_ledger_io_fault_is_503_not_no_ledger():
    # work_dispatch's own no-ledger guard reads the same header, so it must
    # refuse an I/O fault the same way instead of saying "open one".
    from kiro_crew.dashboard.handlers import work_ledger_conductor as wlcond

    key = _ledger_key(CONDUCTOR)
    cpath = work_ledger.conductor_dir(key) / work_ledger._CONDUCTOR_FILE
    cpath.parent.mkdir(parents=True, exist_ok=True)
    cpath.mkdir()

    refusal = await wlcond._refuse_dispatch_without_ledger(key)
    assert refusal is not None and refusal.status == 503
    assert json.loads(refusal.text)["code"] == "ledger_unreadable"

    cpath.rmdir()  # absent again -> still the 404 no_ledger remedy
    refusal = await wlcond._refuse_dispatch_without_ledger(key)
    assert refusal is not None and refusal.status == 404
    assert json.loads(refusal.text)["code"] == work_ledger.CODE_NO_LEDGER
