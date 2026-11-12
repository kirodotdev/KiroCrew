"""A dashboard auto-approve the batch write guard refused records its SEL row
under the guard's own outcome, not the transport floor's.

``approve_tool`` answers both a security-floor refusal and a batch-guard refusal
with ``False``; the dashboard consumer tells them apart with ``take_batch_refusal``
so its own audit row says which refused. The batch_guard helper is pinned directly
in ``test_batch_write_guard.py``; these tests pin the dashboard consumer end to end.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dashboard_approval_helpers import (
    _complete_event,
    _context_builder,
    _make_slot,
    _make_state,
    _permission_event,
    _set_stream,
)

from kiro_crew.dashboard.chat import _run_chat

#: The guard's public SEL outcome and mechanism, as literals so this file binds no
#: kiro_crew.acp name; the strings are pinned against the batch_guard constants in
#: test_batch_write_guard.py.
_BATCH_GUARD_OUTCOME = "rejected_batch_guard"
_BATCH_GUARD_MECHANISM = "batch_write_guard"


def _dashboard_rows(mock_sel: MagicMock, request_id: object) -> list[dict]:
    return [
        c.kwargs
        for c in mock_sel.return_value.log_tool_invocation.call_args_list
        if c.kwargs.get("source") == "dashboard" and c.kwargs.get("request_id") == request_id
    ]


@pytest.mark.asyncio
async def test_trust_auto_approve_refused_by_the_guard_is_labelled_batch_guard(tmp_path):
    state, client = _make_state(tmp_path, context_builder=_context_builder())
    slot = _make_slot(trust=True)
    evt = _permission_event()
    _set_stream(client, [evt, _complete_event()])
    # The transport answered the auto-approve with False: the batch guard refused
    # it (its reason is kept on the transport), not the floor.
    client.approve_tool = AsyncMock(return_value=False)
    client.take_batch_refusal = MagicMock(return_value="Blocked by the batch guard: ...")

    with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
        mock_sel.return_value = MagicMock()
        await _run_chat(state, slot, "hello")
        rows = _dashboard_rows(mock_sel, evt.request_id)

    assert rows, "the refused auto-approve wrote no dashboard SEL row"
    assert rows[-1]["outcome"] == _BATCH_GUARD_OUTCOME
    assert rows[-1]["outcome"] != "rejected_transport_floor"
    assert rows[-1].get("metadata", {}).get("mechanism") == _BATCH_GUARD_MECHANISM


@pytest.mark.asyncio
async def test_trust_auto_approve_refused_by_the_floor_stays_transport_floor(tmp_path):
    state, client = _make_state(tmp_path, context_builder=_context_builder())
    slot = _make_slot(trust=True)
    evt = _permission_event()
    _set_stream(client, [evt, _complete_event()])
    # The floor refused: no batch-guard reason is kept for the request.
    client.approve_tool = AsyncMock(return_value=False)
    client.take_batch_refusal = MagicMock(return_value=None)

    with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
        mock_sel.return_value = MagicMock()
        await _run_chat(state, slot, "hello")
        rows = _dashboard_rows(mock_sel, evt.request_id)

    assert rows, "the refused auto-approve wrote no dashboard SEL row"
    assert rows[-1]["outcome"] == "rejected_transport_floor"
    assert "metadata" not in rows[-1] or not rows[-1]["metadata"]
