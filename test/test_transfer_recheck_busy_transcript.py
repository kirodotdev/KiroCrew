"""The recheck just before a session transfer's POST answers by the error it met.

``api_instances_send_session`` takes the transcript's publication hold before the
send, and ``SshTunnelManager.send_session_bundle`` runs the same check again (the
handler's ``recheck`` hook) after it serialises the bundle and before it posts.
``TranscriptBusy`` subclasses ``TranscriptWithheld``, so the recheck must catch the
busy case first: a busy transcript is the retryable ``503
transfer_snapshot_unstable``, and only a restricted line is ``400
transfer_slot_not_persistent``. In both the peer receives nothing, and a line that
did not change lets the transfer through. Every case drives the real manager.
"""

from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import pytest
from test_session_transfer import (
    _async_value,
    _FakeLog,
    _identity_exchange,
    _OwnerReq,
    _peer_answering,
    _slot,
)

from kiro_crew.history import TranscriptBusy, TranscriptWithheld


class _ChangesAtRecheck(_FakeLog):
    """A transcript whose publication hold raises *error* once the bundle is serialised.

    The serialised body exists in the egress dir from the manager's serialise step
    until its attempt ends, so a hold taken while it exists is the recheck. With
    *error* ``None`` the line never changes. ``timeline`` records each hold in
    order: ``"held"`` or ``"refused"``.
    """

    def __init__(self, messages, egress, error):
        super().__init__(messages)
        self._egress = egress
        self._error = error
        self.timeline: list[str] = []

    @contextlib.contextmanager
    def publication_hold(self, key, *, expected_keys=None):
        if self._error is not None and any(self._egress.glob("*.json")):
            self.timeline.append("refused")
            raise self._error("fake: met at the final recheck")
        self.timeline.append("held")
        yield


async def _send_through_recheck(monkeypatch, tmp_path, error):
    """Send one slot to a CONNECTED peer through the real tunnel manager.

    The peer answers 200 to any POST and counts them. Returns the fake log, the
    POST counter and the handler's response.
    """
    from kiro_crew.dashboard import handlers_instances as hi
    from kiro_crew.dashboard import session_transfer as st
    from kiro_crew.instances import ssh_tunnel_manager as mod
    from kiro_crew.instances.ssh_tunnel_manager import (
        SshTunnelManager,
        TunnelState,
        TunnelStatus,
    )

    monkeypatch.setattr(
        hi.KiroCrewConfig,
        "load",
        staticmethod(lambda: SimpleNamespace(instances=SimpleNamespace(enabled=True))),
    )
    monkeypatch.setattr(st, "_egress_tmp_dir", lambda: tmp_path)

    mgr = SshTunnelManager.__new__(SshTunnelManager)
    # The fields send_session_bundle reads to resolve the forward and the
    # credential, for a manager assembled without `__init__`.
    mgr._tunnel_epoch = {}
    mgr._tokens = {"peer": "tok"}
    mgr._peer_sessions = {}
    mgr._chained_far_end = {}
    mgr._exchange_link = _identity_exchange  # type: ignore[method-assign]
    mgr.status = lambda _id: TunnelStatus(  # type: ignore[method-assign]
        instance_id="peer", state=TunnelState.CONNECTED, local_port=7778
    )
    session, posts = _peer_answering(200, body={"key": "remote-1"})
    monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda *a, **k: session())

    slot = _slot([{"role": "user", "content": "hello", "ts": ""}])
    log = _ChangesAtRecheck(slot.messages, tmp_path, error)
    state = SimpleNamespace(
        _slots={"slot-1": slot},
        conversation_log=log,
        instances_manager=mgr,
        instances_registry=SimpleNamespace(get=lambda _i: SimpleNamespace(id="peer")),
        owner_id="owner",
    )
    request = _OwnerReq(
        app={"state": state},
        match_info={"id": "peer"},
        headers={},
        json=_async_value({"slot": "slot-1"}),
    )

    resp = await hi.api_instances_send_session(request)
    return log, posts, resp


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (TranscriptBusy, 503, "transfer_snapshot_unstable"),
        (TranscriptWithheld, 400, "transfer_slot_not_persistent"),
    ],
    ids=["busy-is-retryable", "withheld-is-refused"],
)
async def test_the_final_recheck_answers_by_the_error_it_met(
    monkeypatch, tmp_path, error, status, code
):
    log, posts, resp = await _send_through_recheck(monkeypatch, tmp_path, error)

    assert posts["n"] == 0, "the bundle was posted after the recheck refused it"
    assert resp.status == status, resp.body
    assert json.loads(resp.body)["code"] == code
    # The handler's check before the send held; the recheck after serialising refused.
    assert log.timeline == ["held", "refused"]


@pytest.mark.asyncio
async def test_the_final_recheck_lets_an_unchanged_transcript_through(monkeypatch, tmp_path):
    log, posts, resp = await _send_through_recheck(monkeypatch, tmp_path, None)

    assert resp.status == 200, resp.body
    assert json.loads(resp.body)["remote_key"] == "remote-1"
    assert posts["n"] == 1
    assert log.timeline == ["held", "held"]
