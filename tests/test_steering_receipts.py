"""Steering receipt safety: delivery, retries and terminal admission fences."""
from __future__ import annotations

from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pytest

from kiro_byo.acp_server import AcpServer, Session


def _make():
    out = StringIO()
    server = AcpServer(stdin=StringIO(), stdout=out)
    session = Session('steering-unit', str(Path.cwd()), 'echo')
    session.active_turn = True
    server._put_session(session)
    return server, session, out


def _rpc(server, output, rid, text, ident):
    server.handle({
        'jsonrpc': '2.0', 'id': rid, 'method': '_session/steer',
        'params': {'sessionId': 'steering-unit', 'message': text, 'messageId': ident},
    })
    import json
    return next(m for m in (json.loads(x) for x in output.getvalue().splitlines()) if m.get('id') == rid)


def _updates(out):
    import json
    return [m['params']['update']['_meta']['kiro'] for m in
            (json.loads(line) for line in out.getvalue().splitlines())
            if m.get('method') == 'session/update'
            and m['params']['update'].get('sessionUpdate') == 'session_info_update'
            and 'kiro' in m['params']['update'].get('_meta', {})]


def test_retry_is_idempotent_and_reusing_id_for_different_text_is_rejected():
    server, session, out = _make()
    try:
        assert _rpc(server, out, 1, 'change plan', 'msg1')['result'] == {'queued': True, 'messageId': 'msg1'}
        retry = _rpc(server, out, 2, 'change plan', 'msg1')['result']
        assert retry['queued'] and retry['duplicate'] == 'queued'
        assert list(session.steer_queue) == ['change plan']
        assert len([e for e in _updates(out) if e['kind'] == 'steering_queued']) == 1
        conflict = _rpc(server, out, 3, 'another instruction', 'msg1')
        assert conflict['error']['code'] == -32602
        assert list(session.steer_queue) == ['change plan']
    finally:
        server._executor.shutdown(wait=True)


def test_injected_receipt_is_durable_before_event_and_retry_is_not_replayed():
    server, session, out = _make()
    try:
        assert _rpc(server, out, 1, 'new direction', 'msg2')['result']['queued']
        snapshots = []
        with patch.object(server, '_persist_session', side_effect=lambda s: snapshots.append([m.copy() for m in s.messages])):
            assert server._consume_queued_steers_for_turn(session, 'partially written')
        assert snapshots and snapshots[-1][-1]['content'] == 'new direction'
        assert [m['content'] for m in session.messages] == ['partially written', 'new direction']
        assert not session.steer_event.is_set()
        assert not session.steer_queue
        info = _updates(out)
        assert [e['kind'] for e in info] == ['steering_queued', 'steering_injected']
        assert all(e['messageId'] == 'msg2' for e in info)
        assert _rpc(server, out, 2, 'new direction', 'msg2')['result']['duplicate'] == 'injected'
        assert len(session.messages) == 2
        assert len(_updates(out)) == 2
    finally:
        server._executor.shutdown(wait=True)


def test_terminal_fence_rejects_late_steer_instead_of_acking_then_dropping():
    server, session, out = _make()
    try:
        assert not server._consume_queued_steers_for_turn(session, close_if_empty=True)
        result = _rpc(server, out, 1, 'late steer', 'late')['result']
        assert result == {'queued': False, 'messageId': 'late', 'dropped': 'session_not_accepting'}
        assert not session.steer_queue
        assert not any(e['kind'] == 'steering_queued' for e in _updates(out))
    finally:
        server._executor.shutdown(wait=True)


def test_final_clear_emits_recovery_event_and_retry_can_be_queued_by_crew():
    server, session, out = _make()
    try:
        assert _rpc(server, out, 1, 'do this', 'msg3')['result']['queued']
        assert server._clear_steers(session)
        assert session.steer_receipts['msg3'][1] == 'cleared'
        assert [e['kind'] for e in _updates(out)] == ['steering_queued', 'steering_cleared']
        assert _rpc(server, out, 2, 'do this', 'msg3')['result'] == {
            'queued': False, 'messageId': 'msg3', 'duplicate': 'cleared',
        }
    finally:
        server._executor.shutdown(wait=True)


def test_cancelled_turn_does_not_inject_a_pending_correction():
    server, session, out = _make()
    try:
        assert _rpc(server, out, 1, 'do not inject', 'cancel')['result']['queued']
        session.cancel_event.set()
        assert not server._consume_queued_steers_for_turn(session)
        assert session.steer_queue
        assert server._clear_steers(session)
        assert session.steer_receipts['cancel'][1] == 'cleared'
        assert session.messages == []
    finally:
        server._executor.shutdown(wait=True)


def test_bounded_queue_never_acknowledges_unstored_message():
    server, session, out = _make()
    try:
        for i in range(64):
            assert server._queue_steer(session, f'fix {i}', f'id-{i}')[0]
        accepted, reason = server._queue_steer(session, 'overflow', 'overflow')
        assert not accepted and reason == 'steer_queue_full'
        assert len(session.steer_queue) == len(session.steer_pending_ids) == 64
    finally:
        server._executor.shutdown(wait=True)
