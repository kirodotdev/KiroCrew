"""Member thread projection without an inbox: peer rows on the plain transcript.

Three seams, one PR:

* ``authorize_target`` gains two narrow ``send``-only allows in front of the
  ``not_creator`` fence -- member -> member, and child -> the session that
  created it. Every other operation, and every other caller/target pair, is
  refused exactly as before.
* ``send_to_target`` stamps ``meta.sent_by`` on the delivered row (the text
  prefix the model reads is unchanged) and steers a BUSY member instead of
  queueing behind it.
* ``send_message(session="origin")`` from a non-cron caller reaches the session
  that created the caller, through the same delivery, instead of degrading to
  the bell.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app as _detail_app
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_delivery as cd
from kiro_crew.dashboard import session_control as sc
from kiro_crew.dashboard.chat_utils import slot_history_key
from kiro_crew.members import DM_SLOT_KEY_PREFIX, DM_SLOT_MODE

if "kiro_crew.slack.handler" not in sys.modules:
    _stub = types.ModuleType("kiro_crew.slack.handler")
    _stub.is_allowed_user = lambda uid: False  # type: ignore[attr-defined]
    _stub.is_tracked_channel = lambda cid: False  # type: ignore[attr-defined]
    sys.modules["kiro_crew.slack.handler"] = _stub

from kiro_crew.dashboard.handlers import api_send_message  # noqa: E402

MEMBER_A = DM_SLOT_KEY_PREFIX + "conductor"
MEMBER_B = DM_SLOT_KEY_PREFIX + "autofix"


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setattr(sc, "session_control_enabled", lambda: True)


# ── authorization matrix (fake slots, real gate order) ───────────────────────


def _fake(key: str, *, created_by: str = "", mode: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        workspace="default",
        memory_mode="persistent",
        _app="",
        linked_session_key="",
        _created_by=created_by,
        mode=mode,
        running=False,
        messages=[],
        executor="local",
    )


class _State:
    def __init__(self, slots: dict[str, SimpleNamespace]):
        self._slots = slots

    def get_slot(self, key: str):
        return self._slots.get(key)


def _authorize(state: _State, caller_key: str, target_key: str, operation: str):
    with (
        patch.object(sc, "caller_slot_key", return_value=caller_key),
        patch.object(sc, "member_dispatch_enabled", return_value=True),
        patch.object(sc, "_resolve_slot", return_value=state._slots.get(target_key)),
    ):
        return sc.authorize_target(
            state,
            caller_session_key="dashboard:whatever",
            target=target_key,
            operation=operation,
        )


class TestPeerMemberAllow:
    def _two_members(self) -> _State:
        return _State(
            {
                MEMBER_A: _fake(MEMBER_A, mode=DM_SLOT_MODE),
                MEMBER_B: _fake(MEMBER_B, mode=DM_SLOT_MODE),
            }
        )

    def test_member_may_send_to_another_member(self):
        state = self._two_members()
        slot = _authorize(state, MEMBER_A, MEMBER_B, "send")
        assert slot.key == MEMBER_B

    @pytest.mark.parametrize("operation", ["stop", "close", "read"])
    def test_member_may_not_stop_close_or_read_a_peer(self, operation):
        state = self._two_members()
        with pytest.raises(sc.SessionControlError) as exc_info:
            _authorize(state, MEMBER_A, MEMBER_B, operation)
        assert exc_info.value.code == "not_creator"

    def test_non_member_caller_still_cannot_send_to_a_member(self):
        """An agent-created ordinary session is fenced to what it created; a
        member thread it did not create stays out of reach."""
        state = _State(
            {
                "chat-1-agent": _fake("chat-1-agent", created_by="chat-1-owner"),
                MEMBER_B: _fake(MEMBER_B, mode=DM_SLOT_MODE),
            }
        )
        with pytest.raises(sc.SessionControlError) as exc_info:
            _authorize(state, "chat-1-agent", MEMBER_B, "send")
        assert exc_info.value.code == "not_creator"

    def test_a_squatter_on_a_member_key_is_not_a_member_target(self):
        """The allow needs the member MODE, not only the key prefix."""
        state = _State(
            {
                MEMBER_A: _fake(MEMBER_A, mode=DM_SLOT_MODE),
                MEMBER_B: _fake(MEMBER_B, mode=""),
            }
        )
        with pytest.raises(sc.SessionControlError) as exc_info:
            _authorize(state, MEMBER_A, MEMBER_B, "send")
        assert exc_info.value.code == "not_creator"

    def test_a_member_keyed_caller_without_member_mode_is_not_a_member(self):
        """The allow judges the CALLER by prefix and mode too: a restored or
        squatting `member-` slot that is not in member mode does not reach a
        real member's thread through it."""
        state = _State(
            {
                MEMBER_A: _fake(MEMBER_A, mode=""),
                MEMBER_B: _fake(MEMBER_B, mode=DM_SLOT_MODE),
            }
        )
        with pytest.raises(sc.SessionControlError) as exc_info:
            _authorize(state, MEMBER_A, MEMBER_B, "send")
        assert exc_info.value.code == "not_creator"

    def test_member_still_cannot_send_to_the_users_own_session(self):
        state = _State(
            {
                MEMBER_A: _fake(MEMBER_A, mode=DM_SLOT_MODE),
                "chat-1-user": _fake("chat-1-user"),
            }
        )
        with pytest.raises(sc.SessionControlError) as exc_info:
            _authorize(state, MEMBER_A, "chat-1-user", "send")
        assert exc_info.value.code == "not_creator"


class TestReportToCreatorAllow:
    def _worker_and_creator(self) -> _State:
        return _State(
            {
                MEMBER_A: _fake(MEMBER_A, mode=DM_SLOT_MODE),
                "chat-1-w1": _fake("chat-1-w1", created_by=MEMBER_A),
            }
        )

    def test_child_may_send_to_its_creator(self):
        slot = _authorize(self._worker_and_creator(), "chat-1-w1", MEMBER_A, "send")
        assert slot.key == MEMBER_A

    @pytest.mark.parametrize("operation", ["stop", "close", "read"])
    def test_child_may_not_stop_close_or_read_its_creator(self, operation):
        with pytest.raises(sc.SessionControlError) as exc_info:
            _authorize(self._worker_and_creator(), "chat-1-w1", MEMBER_A, operation)
        assert exc_info.value.code == "not_creator"

    def test_child_may_not_send_to_a_stranger(self):
        state = _State(
            {
                MEMBER_A: _fake(MEMBER_A, mode=DM_SLOT_MODE),
                "chat-1-w1": _fake("chat-1-w1", created_by="chat-9-other"),
            }
        )
        with pytest.raises(sc.SessionControlError) as exc_info:
            _authorize(state, "chat-1-w1", MEMBER_A, "send")
        assert exc_info.value.code == "not_creator"


# ── delivery: steer vs turn vs queue, and the meta shape ─────────────────────


def _key(slot) -> str:
    return slot_history_key(slot)


def _busy(slot):
    task = MagicMock()
    task.done.return_value = False
    slot.task = task
    return slot


def _steerable(accepted: bool = True) -> MagicMock:
    client = MagicMock()
    client.supports_steer = True
    client.steer = AsyncMock(return_value=accepted)
    return client


def _members(state):
    a = state.get_or_create_slot(MEMBER_A, agent="kirocrew-conductor", mode=DM_SLOT_MODE)
    b = state.get_or_create_slot(MEMBER_B, agent="kirocrew-autofix", mode=DM_SLOT_MODE)
    return a, b


class TestSendToMember:
    def test_idle_member_starts_a_turn_with_sent_by_and_the_prefix(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        a, b = _members(state)
        a.title = "Conductor"
        ran: dict[str, str] = {}

        async def _fake_run_chat(_state, slot, prompt):
            ran["prompt"] = prompt

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

        async def _drive():
            out = await sc.send_to_target(
                state, caller_session_key=_key(a), target=MEMBER_B, message="take the first task"
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return out

        out = asyncio.run(_drive())
        assert out == {"ok": True, "target": MEMBER_B, "started": True, "steered": False}
        # The model still reads the exact provenance prefix.
        assert ran["prompt"].startswith(f"[sent by session {MEMBER_A} via session_send]\n\n")
        (row,) = [m for m in b.messages if m.get("role") == "user"]
        assert row["content"].startswith("[sent by session ")
        sent_by = row["meta"]["sent_by"]
        assert sent_by == {
            "session_key": MEMBER_A,
            "via": "session_send",
            "title": "Conductor",
            "agent": "kirocrew-conductor",
            "member_slug": "conductor",
        }

    def test_busy_member_is_steered_not_queued(self, tmp_path):
        state = _make_state(tmp_path)
        a, b = _members(state)
        _busy(b)
        b._acp_client = _steerable()

        out = asyncio.run(
            sc.send_to_target(
                state, caller_session_key=_key(a), target=MEMBER_B, message="one more thing"
            )
        )
        assert out["steered"] is True and out["started"] is False
        b._acp_client.steer.assert_awaited_once()
        assert b._acp_client.steer.await_args.args[0].startswith("[sent by session ")
        assert b._queue == []
        (row,) = [m for m in b.messages if m.get("role") == "user"]
        assert row["meta"]["steer"] is True
        assert row["meta"]["sent_by"]["via"] == "session_send"
        assert row["meta"]["sent_by"]["member_slug"] == "conductor"

    def test_busy_member_without_a_steerable_client_queues_with_meta(self, tmp_path):
        state = _make_state(tmp_path)
        a, b = _members(state)
        _busy(b)
        b._acp_client = None

        out = asyncio.run(
            sc.send_to_target(state, caller_session_key=_key(a), target=MEMBER_B, message="later")
        )
        assert out["steered"] is False and out["started"] is False
        (entry,) = b._queue
        assert "later" in entry["content"]
        assert entry["meta"]["sent_by"]["session_key"] == MEMBER_A

    def test_busy_non_member_target_still_queues(self, tmp_path):
        """The steer path is the member thread's; an ordinary peer keeps the
        queue behaviour and the steer client is never touched."""
        state = _make_state(tmp_path)
        caller = state.get_or_create_slot("chat-1")
        target = state.get_or_create_slot("chat-2")
        _busy(target)
        target._acp_client = _steerable()

        out = asyncio.run(
            sc.send_to_target(state, caller_session_key=_key(caller), target="chat-2", message="hi")
        )
        assert out["steered"] is False and out["started"] is False
        target._acp_client.steer.assert_not_awaited()
        (entry,) = target._queue
        assert entry["meta"]["sent_by"]["via"] == "session_send"
        assert "member_slug" not in entry["meta"]["sent_by"]

    def test_steer_push_frame_carries_the_provenance(self, tmp_path):
        state = _make_state(tmp_path)
        a, b = _members(state)
        _busy(b)
        b._acp_client = _steerable()
        frames: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda kind, payload: frames.append((kind, payload))

        asyncio.run(
            sc.send_to_target(state, caller_session_key=_key(a), target=MEMBER_B, message="x")
        )
        (payload,) = [p for k, p in frames if k == "steer_push"]
        assert payload["sentBy"]["session_key"] == MEMBER_A
        assert payload["sentBy"]["via"] == "session_send"


class TestSentByMeta:
    def test_member_caller_shape(self, tmp_path):
        state = _make_state(tmp_path)
        a, _ = _members(state)
        a.title = "Conductor"
        rec = sc.sent_by_meta(state, MEMBER_A, via=sc.SENT_BY_VIA_SESSION_SEND)
        assert rec == {
            "session_key": MEMBER_A,
            "via": "session_send",
            "title": "Conductor",
            "agent": "kirocrew-conductor",
            "member_slug": "conductor",
        }

    def test_ordinary_caller_has_no_member_slug(self, tmp_path):
        state = _make_state(tmp_path)
        w = state.get_or_create_slot("chat-1-w1", agent="kirocrew-worker")
        rec = sc.sent_by_meta(state, "chat-1-w1", via=sc.SENT_BY_VIA_SEND_MESSAGE_ORIGIN)
        assert rec["via"] == "send_message_origin"
        assert rec["agent"] == "kirocrew-worker"
        assert "member_slug" not in rec
        assert rec["session_key"] == w.key

    def test_child_is_named_by_the_relationship_not_the_door(self, tmp_path):
        """A worker sending to its creator through session_send reads the same as
        one reporting through send_message origin: the record says child."""
        state = _make_state(tmp_path)
        a, _ = _members(state)
        w = state.get_or_create_slot("chat-1-w1", agent="kirocrew-worker")
        w._created_by = a.key
        via_send = sc.sent_by_meta(state, w.key, via=sc.SENT_BY_VIA_SESSION_SEND, target_key=a.key)
        via_origin = sc.sent_by_meta(
            state, w.key, via=sc.SENT_BY_VIA_SEND_MESSAGE_ORIGIN, target_key=a.key
        )
        assert via_send["child"] is True and via_origin["child"] is True
        # The same worker sending to a STRANGER is not that stranger's child.
        assert "child" not in sc.sent_by_meta(
            state, w.key, via=sc.SENT_BY_VIA_SESSION_SEND, target_key="chat-9-other"
        )

    def test_an_untitled_caller_carries_no_title(self, tmp_path):
        """The sidebar placeholder is not a name: the record leaves title empty
        so the transcript's localized "another session" fallback names the sender
        instead of `From "New Session…"`."""
        from kiro_crew.dashboard.state import NEW_SESSION_TITLE

        state = _make_state(tmp_path)
        s = state.get_or_create_slot("chat-7-fresh")
        assert s.display_title == NEW_SESSION_TITLE
        rec = sc.sent_by_meta(state, s.key, via=sc.SENT_BY_VIA_SESSION_SEND)
        assert rec["title"] == ""
        s.title = "Fix the flaky retry test"
        s._titled = True
        assert sc.sent_by_meta(state, s.key, via=sc.SENT_BY_VIA_SESSION_SEND)["title"] == (
            "Fix the flaky retry test"
        )

    def test_unknown_caller_still_yields_a_record(self, tmp_path):
        state = _make_state(tmp_path)
        rec = sc.sent_by_meta(state, "chat-gone", via=sc.SENT_BY_VIA_SESSION_SEND)
        assert rec == {"session_key": "chat-gone", "via": "session_send", "title": "", "agent": ""}

    def test_a_member_keyed_slot_without_member_mode_is_not_named_a_member(self, tmp_path):
        state = _make_state(tmp_path)
        a, _ = _members(state)
        a.mode = ""  # the key still reads member-*, the slot is not a member
        rec = sc.sent_by_meta(state, a.key, via=sc.SENT_BY_VIA_SESSION_SEND)
        assert "member_slug" not in rec
        assert rec["session_key"] == a.key
        # A key wearing the prefix with no live slot at all is not a member either.
        rec = sc.sent_by_meta(state, DM_SLOT_KEY_PREFIX + "ghost", via=sc.SENT_BY_VIA_SESSION_SEND)
        assert "member_slug" not in rec


# ── send_message(session="origin") for a non-cron caller ─────────────────────


class TestDeliverToCreator:
    def test_worker_reports_into_its_creators_thread(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        a, _ = _members(state)
        worker = state.get_or_create_slot("chat-1-w1", agent="kirocrew-worker")
        worker._created_by = a.key
        ran: dict[str, str] = {}

        async def _fake_run_chat(_state, slot, prompt):
            ran["slot"] = slot.key
            ran["prompt"] = prompt

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)

        async def _drive():
            out = await sc.deliver_to_creator(
                state, caller_session_key=_key(worker), text="Triage #42 done: fixed the flake"
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return out

        out = asyncio.run(_drive())
        assert out == {"target": a.key, "started": True, "steered": False}
        assert ran["slot"] == a.key
        assert ran["prompt"].startswith(f"[sent by session {worker.key} via send_message]\n\n")
        (row,) = [m for m in a.messages if m.get("role") == "user"]
        assert row["meta"]["sent_by"]["via"] == "send_message_origin"
        assert row["meta"]["sent_by"]["session_key"] == worker.key
        assert row["meta"]["sent_by"]["child"] is True
        assert "member_slug" not in row["meta"]["sent_by"]

    def test_busy_member_creator_is_steered(self, tmp_path):
        state = _make_state(tmp_path)
        a, _ = _members(state)
        worker = state.get_or_create_slot("chat-1-w1")
        worker._created_by = a.key
        _busy(a)
        a._acp_client = _steerable()

        out = asyncio.run(
            sc.deliver_to_creator(state, caller_session_key=_key(worker), text="done")
        )
        assert out == {"target": a.key, "started": False, "steered": True}

    def test_orphan_caller_has_no_origin(self, tmp_path):
        state = _make_state(tmp_path)
        orphan = state.get_or_create_slot("chat-1-solo")
        assert (
            asyncio.run(sc.deliver_to_creator(state, caller_session_key=_key(orphan), text="x"))
            is None
        )

    def test_unknown_caller_has_no_origin(self, tmp_path):
        state = _make_state(tmp_path)
        assert (
            asyncio.run(sc.deliver_to_creator(state, caller_session_key="cron:abc", text="x"))
            is None
        )

    def test_creator_gone_falls_back(self, tmp_path, monkeypatch):
        state = _make_state(tmp_path)
        worker = state.get_or_create_slot("chat-1-w1")
        worker._created_by = "chat-0-vanished"

        async def _no_rehydrate(_state, _key):
            return None

        monkeypatch.setattr(
            "kiro_crew.dashboard.chat_persistence.rehydrate_slot_from_history_async",
            _no_rehydrate,
        )
        assert (
            asyncio.run(sc.deliver_to_creator(state, caller_session_key=_key(worker), text="x"))
            is None
        )

    def test_creator_in_another_workspace_is_refused(self, tmp_path):
        """The child->creator allow is an exemption from the ownership fence
        only; every other containment refusal still applies."""
        state = _make_state(tmp_path)
        creator = state.get_or_create_slot("chat-0-creator", workspace="alpha")
        worker = state.get_or_create_slot("chat-1-w1", workspace="beta")
        worker._created_by = creator.key
        assert (
            asyncio.run(sc.deliver_to_creator(state, caller_session_key=_key(worker), text="x"))
            is None
        )
        assert not [m for m in creator.messages if m.get("role") == "user"]


@web.middleware
async def _internal_secret_stub(request, handler):
    """What the gateway's auth middleware does for a proven ``X-Internal-Secret``:
    mark the request. The MCP process posts under that secret; an app token does not."""
    if request.headers.get("X-Internal-Secret") == "test-secret":
        request["internal_auth"] = True
    return await handler(request)


def _make_app(state) -> web.Application:
    app = web.Application(middlewares=[_internal_secret_stub])
    app.router.add_post("/api/send-message", api_send_message)
    app["state"] = state
    return app


INTERNAL = {"X-Internal-Secret": "test-secret"}


@pytest.fixture
def mock_sel():
    with patch("kiro_crew.sel.sel") as m:
        m.return_value = MagicMock()
        yield m.return_value


class TestSendMessageOriginRoute:
    def _state(self):
        state = MagicMock()
        state.slack_client = None
        state.owner_id = ""
        state.crons.list_jobs.return_value = []
        return state

    @pytest.mark.asyncio
    async def test_non_cron_caller_with_a_creator_is_delivered_as_session(self, mock_sel):
        state = self._state()
        landed = AsyncMock(return_value={"target": MEMBER_A, "started": True, "steered": False})
        with patch("kiro_crew.dashboard.session_control.deliver_to_creator", landed):
            async with TestClient(TestServer(_make_app(state))) as c:
                resp = await c.post(
                    "/api/send-message",
                    json={"text": "report", "session": "origin"},
                    headers={"X-Session-Key": "dashboard:chat-1-w1", **INTERNAL},
                )
                data = await resp.json()
        assert data["delivered_to"] == "session"
        landed.assert_awaited_once()
        assert landed.await_args.kwargs["caller_session_key"] == "dashboard:chat-1-w1"
        state.notify.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_cron_caller_without_a_creator_falls_back_to_the_bell(self, mock_sel):
        state = self._state()
        landed = AsyncMock(return_value=None)
        with patch("kiro_crew.dashboard.session_control.deliver_to_creator", landed):
            async with TestClient(TestServer(_make_app(state))) as c:
                resp = await c.post(
                    "/api/send-message",
                    json={"text": "report", "session": "origin"},
                    headers={"X-Session-Key": "dashboard:chat-1-solo", **INTERNAL},
                )
                data = await resp.json()
        assert data["delivered_to"] == "notification"
        state.notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_an_app_token_caller_cannot_name_a_worker_for_origin_delivery(self, mock_sel):
        """The session-key header is attested only for the internal-secret
        (MCP) caller. An app-token request naming a worker's key must not run
        its text as that worker's creator's turn; it keeps the bell."""
        state = self._state()
        landed = AsyncMock(return_value={"target": MEMBER_A, "started": True, "steered": False})
        with patch("kiro_crew.dashboard.session_control.deliver_to_creator", landed):
            async with TestClient(TestServer(_make_app(state))) as c:
                resp = await c.post(
                    "/api/send-message",
                    json={"text": "report", "session": "origin"},
                    headers={"X-Session-Key": "dashboard:chat-1-w1"},  # no internal secret
                )
                data = await resp.json()
        landed.assert_not_awaited()
        assert data["delivered_to"] == "notification"
        state.notify.assert_called_once()

    @pytest.mark.asyncio
    async def test_cron_caller_keeps_the_job_origin_path(self, mock_sel):
        """A cron's origin is its job's session, never a creator lookup."""
        state = self._state()
        landed = AsyncMock(return_value=None)
        with patch("kiro_crew.dashboard.session_control.deliver_to_creator", landed):
            async with TestClient(TestServer(_make_app(state))) as c:
                await c.post(
                    "/api/send-message",
                    json={"text": "report", "session": "origin", "caller_session": "cron:job1"},
                    headers={"X-Session-Key": "cron:job1", **INTERNAL},
                )
        landed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_header_means_no_creator_lookup(self, mock_sel):
        state = self._state()
        landed = AsyncMock(return_value=None)
        with patch("kiro_crew.dashboard.session_control.deliver_to_creator", landed):
            async with TestClient(TestServer(_make_app(state))) as c:
                await c.post(
                    "/api/send-message",
                    json={"text": "report", "session": "origin"},
                    headers=INTERNAL,
                )
        landed.assert_not_awaited()


class TestRequeuedPeerSteerKeepsItsAuthor:
    def test_requeue_carries_sent_by_and_is_not_user_speech(self, tmp_path, monkeypatch):
        """A peer's steer the turn never confirmed re-enters the queue WITH its
        author and WITHOUT the human-composer exemption: the drained row must
        still read "From <peer>", and a channel link the target gains before the
        drain must drop it like any other cross-session prompt."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        a, b = _members(state)
        text = "[sent by session member-conductor via session_send]\n\nsteer me"
        sent_by = sc.sent_by_meta(state, a.key, via=sc.SENT_BY_VIA_SESSION_SEND)
        b._pending_steers = [text]
        b._steer_delivery_ids = {text: "did-1"}
        b._steer_sent_by = {text: dict(sent_by)}

        from kiro_crew.dashboard import chat_runner

        chat_runner._requeue_unconsumed_steers(state, b)
        (entry,) = b._queue
        assert entry["meta"]["sent_by"] == sent_by
        assert not entry.get("_directive_user_origin", False)
        assert b._steer_sent_by == {}
        # The card the requeue announces is a peer's too: it carries the record
        # (no edit affordance client-side) and shows the message without the
        # model-facing provenance line, like the card `deliver_sent_by` draws.
        (frame,) = [
            c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_push"
        ]
        assert frame["sent_by"] == sent_by
        assert frame["content"] == "steer me"
        assert frame["queue_id"] == entry["id"]

    def test_requeue_stamps_the_admission_the_steer_was_authorized_under(
        self, tmp_path, monkeypatch
    ):
        """The steer RPC suspended; the turn ended under it; the requeue runs at
        teardown. A link the target gained meanwhile must read as NEWLY held at
        the drain, so the requeued entry carries the admission `authorize_target`
        saw, not a snapshot taken now (which would record the link as admitted)."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        a, b = _members(state)
        text = "[sent by session member-conductor via session_send]\n\nsteer me"
        sent_by = sc.sent_by_meta(state, a.key, via=sc.SENT_BY_VIA_SESSION_SEND)
        admission = sc.containment_meta(state, b)  # unlinked at authorization
        b._pending_steers = [text]
        b._steer_delivery_ids = {text: "did-1"}
        b._steer_sent_by = {text: dict(sent_by)}
        b._steer_admission = {text: dict(admission)}
        b.linked_session_key = "slack:C1:T1"  # gained during the RPC

        from kiro_crew.dashboard import chat_runner

        chat_runner._requeue_unconsumed_steers(state, b)
        (entry,) = b._queue
        recorded = entry["meta"][sc.QUEUED_CONTAINMENT_META_KEY]
        assert recorded["linked"] is False
        assert b._steer_admission == {}
        now = sc.containment_snapshot(state, b, on_probe_failure=True)
        assert "linked" in sc.newly_held_constraints(now, entry["meta"])

    def test_the_admission_travels_with_the_pending_steer_and_is_released_with_it(self, tmp_path):
        state = _make_state(tmp_path)
        a, b = _members(state)
        _busy(b)
        b._acp_client = _steerable()
        asyncio.run(
            sc.send_to_target(state, caller_session_key=_key(a), target=MEMBER_B, message="x")
        )
        (text,) = b._pending_steers
        assert sc.QUEUED_CONTAINMENT_META_KEY in b._steer_admission[text]
        # Settled by the consumed echo: released with the author.
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        _settle_consumed_steers(b, text, state)  # the bare (KAS-shaped) echo
        assert b._steer_admission == {} and b._steer_sent_by == {}

    def test_a_human_steer_requeue_is_unchanged(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("chat-1")
        slot._pending_steers = ["typed here"]
        slot._steer_delivery_ids = {"typed here": "did-1"}

        from kiro_crew.dashboard import chat_runner

        chat_runner._requeue_unconsumed_steers(state, slot)
        (entry,) = slot._queue
        assert "sent_by" not in entry["meta"]
        assert entry.get("_directive_user_origin") is True
        (frame,) = [
            c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "queue_push"
        ]
        assert "sent_by" not in frame
        assert frame["content"] == "typed here"

    def test_an_accepted_steer_keeps_its_author_until_the_echo(self, tmp_path):
        state = _make_state(tmp_path)
        a, b = _members(state)
        _busy(b)
        b._acp_client = _steerable()
        asyncio.run(
            sc.send_to_target(state, caller_session_key=_key(a), target=MEMBER_B, message="x")
        )
        # The accepted steer stamped its own row and dropped its send id, but the
        # author stays registered with the pending steer: the consumed-echo settle
        # reads it (a peer's steer retires no question card) and releases it.
        assert b._steer_send_ids == {}
        assert len(b._pending_steers) == 1
        (text,) = b._pending_steers
        assert b._steer_sent_by[text]["member_slug"] == "conductor"


class TestBusyMeansMidPlanToo:
    def test_a_mid_plan_target_queues_instead_of_starting_a_turn(self, tmp_path, monkeypatch):
        """Between stages of a multi-stage plan `task` is None (so `running` is
        False) while `_in_stage_execution` is set; a turn started there would
        overwrite the plan's state. The delivery queues, exactly like the
        composer's own send path."""
        state = _make_state(tmp_path)
        a, b = _members(state)
        b._in_stage_execution = True
        ran: list[str] = []

        async def _fake_run_chat(_state, slot, prompt):
            ran.append(slot.key)

        monkeypatch.setattr("kiro_crew.dashboard.chat_runner._run_chat", _fake_run_chat)
        out = asyncio.run(
            sc.send_to_target(
                state, caller_session_key=_key(a), target=MEMBER_B, message="mid-plan"
            )
        )
        assert out["started"] is False and out["steered"] is False
        (entry,) = b._queue
        assert entry["meta"]["sent_by"]["session_key"] == MEMBER_A
        assert ran == []

    def test_a_worker_report_into_a_mid_plan_creator_queues(self, tmp_path):
        state = _make_state(tmp_path)
        a, _ = _members(state)
        worker = state.get_or_create_slot("chat-1-w1")
        worker._created_by = a.key
        a._in_stage_execution = True
        out = asyncio.run(
            sc.deliver_to_creator(state, caller_session_key=_key(worker), text="done")
        )
        assert out == {"target": a.key, "started": False, "steered": False}
        (entry,) = a._queue
        assert entry["meta"]["sent_by"]["via"] == "send_message_origin"


class TestPeerRowsNeverMerge:
    """The queue drain may merge several queued user messages into one row whose
    meta is a last-writer-wins union. A peer-authored entry drains alone, so no
    merged row ever carries the wrong author."""

    def _slot_with_queue(self, tmp_path):
        from kiro_crew.dashboard import chat_utils

        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        return chat_utils, slot

    def test_two_peers_do_not_merge(self, tmp_path):
        cu, slot = self._slot_with_queue(tmp_path)
        slot.queue_append(
            "from a", meta={"sent_by": {"session_key": "member-a", "via": "session_send"}}
        )
        slot.queue_append(
            "from b", meta={"sent_by": {"session_key": "member-b", "via": "session_send"}}
        )
        content, consumed = cu._dequeue_next_message(slot, merge_enabled=True)
        assert content == "from a" and len(consumed) == 1
        assert consumed[0]["meta"]["sent_by"]["session_key"] == "member-a"
        content, consumed = cu._dequeue_next_message(slot, merge_enabled=True)
        assert content == "from b" and len(consumed) == 1

    def test_a_peer_row_stops_a_human_merge_run_and_is_not_folded_into_it(self, tmp_path):
        cu, slot = self._slot_with_queue(tmp_path)
        slot.queue_append("typed one")
        slot.queue_append("typed two")
        slot.queue_append(
            "from a", meta={"sent_by": {"session_key": "member-a", "via": "session_send"}}
        )
        content, consumed = cu._dequeue_next_message(slot, merge_enabled=True)
        assert content.startswith("[2 queued messages merged]") and len(consumed) == 2
        assert all("sent_by" not in (c.get("meta") or {}) for c in consumed)
        content, consumed = cu._dequeue_next_message(slot, merge_enabled=True)
        assert content == "from a" and len(consumed) == 1

    def test_plain_human_messages_still_merge(self, tmp_path):
        cu, slot = self._slot_with_queue(tmp_path)
        slot.queue_append("typed one")
        slot.queue_append("typed two")
        content, consumed = cu._dequeue_next_message(slot, merge_enabled=True)
        assert content.startswith("[2 queued messages merged]") and len(consumed) == 2

    def test_carries_sent_by_shape(self):
        from kiro_crew.dashboard.chat_utils import carries_sent_by

        assert carries_sent_by({"meta": {"sent_by": {"session_key": "x", "via": "session_send"}}})
        assert not carries_sent_by({"meta": {"sent_by": "member-a"}})
        assert not carries_sent_by({"meta": {}})
        assert not carries_sent_by({})


class TestQueuePopFrameCarriesTheAuthor:
    @pytest.mark.asyncio
    async def test_drained_peer_entry_broadcasts_sent_by(self, tmp_path, monkeypatch):
        """The client rebuilds a drained queue entry as a user row from the
        `queue_pop` frame alone, so the author has to ride that frame or the
        peer's message renders as the person's own until a reload."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        frames: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda kind, payload: frames.append((kind, payload))
        state.subagents = None
        slot = state.get_or_create_slot("chat-1")
        sent_by = {
            "session_key": "member-conductor",
            "via": "session_send",
            "member_slug": "conductor",
        }
        slot.queue_append(
            "[sent by session member-conductor via session_send]\n\nhello",
            meta={"sent_by": sent_by},
        )

        from kiro_crew.dashboard import chat_runner

        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True
        (pop,) = [p for k, p in frames if k == "queue_pop"]
        assert pop["meta"]["sent_by"] == sent_by
        # The persisted row carries it too (the drain unions entry meta onto the row).
        (row,) = [m for m in slot.messages if m.get("role") == "user"]
        assert row["meta"]["sent_by"] == sent_by

    @pytest.mark.asyncio
    async def test_a_plain_entry_keeps_the_prior_frame_shape(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        frames: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda kind, payload: frames.append((kind, payload))
        state.subagents = None
        slot = state.get_or_create_slot("chat-1")
        slot.queue_append("typed here")

        from kiro_crew.dashboard import chat_runner

        with (
            patch.object(chat_runner, "spawn_guarded_turn", return_value=MagicMock()),
            patch.object(chat_runner, "_run_chat", return_value=MagicMock()),
        ):
            assert await chat_runner._start_next_queued_turn(state, slot) is True
        (pop,) = [p for k, p in frames if k == "queue_pop"]
        assert "meta" not in pop


class TestSteerCarriesSentBy:
    def test_steer_row_and_frame_without_sent_by_are_unchanged(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        _busy(slot)
        slot._acp_client = _steerable()
        frames: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda kind, payload: frames.append((kind, payload))
        asyncio.run(cd.steer_into_running_turn(state, slot, "plain steer"))
        (row,) = [m for m in slot.messages if m.get("role") == "user"]
        assert "sent_by" not in row["meta"]
        (payload,) = [p for k, p in frames if k == "steer_push"]
        assert "sentBy" not in payload


class TestOriginToolForwardsOnlyAStrictKey:
    """The MCP tool side of ``send_message(session="origin")`` for a non-cron
    caller. The gateway acts on the ``X-Session-Key`` it receives, so the tool
    forwards the STRICT key when there is one and NO key when there is not --
    never the lenient ancestor walk, under which an unidentified sub-agent
    resolves to its parent and would report into the parent's creator."""

    def test_strict_key_is_forwarded(self):
        from kiro_crew.mcp_tools import messaging as tool

        with (
            patch.object(tool.mcp_core, "_resolve_session_key", return_value="dashboard:chat-1-w1"),
            patch.object(
                tool.mcp_core,
                "require_strict_session_key",
                return_value=("dashboard:chat-1-w1", ""),
            ),
            patch.object(tool.mcp_core, "_post") as post,
        ):
            post.return_value = {"ok": True, "delivered_to": "session"}
            out = tool.send_message("send_message", {"text": "report", "session": "origin"})
        assert out == "Message injected into target session."
        assert post.call_args.kwargs["session_key"] == "dashboard:chat-1-w1"
        assert post.call_args.args[1]["caller_session"] == "dashboard:chat-1-w1"

    def test_no_strict_key_sends_no_header_and_degrades_to_the_bell(self):
        from kiro_crew.mcp_tools import messaging as tool

        with (
            patch.object(
                tool.mcp_core, "_resolve_session_key", return_value="dashboard:parent-of-subagent"
            ),
            patch.object(
                tool.mcp_core, "require_strict_session_key", return_value=("", "Error: refused")
            ),
            patch.object(tool.mcp_core, "_post") as post,
        ):
            post.return_value = {"ok": True, "delivered_to": "notification"}
            out = tool.send_message("send_message", {"text": "report", "session": "origin"})
        assert post.call_args.kwargs["session_key"] == ""
        assert "caller_session" not in post.call_args.args[1]
        assert "delivered as notification" in out

    def test_a_cron_origin_send_is_untouched(self):
        from kiro_crew.mcp_tools import messaging as tool

        with (
            patch.object(tool.mcp_core, "_resolve_session_key", return_value="cron:job1"),
            patch.object(tool.mcp_core, "require_strict_session_key") as strict,
            patch.object(tool.mcp_core, "_post") as post,
        ):
            post.return_value = {"ok": True, "delivered_to": "session"}
            tool.send_message("send_message", {"text": "report", "session": "origin"})
        strict.assert_not_called()
        assert "session_key" not in post.call_args.kwargs
        assert post.call_args.args[1]["caller_session"] == "cron:job1"


class TestSessionSendToolReadsSteered:
    def _call(self, resp):
        from kiro_crew import mcp_dashboard as md

        with (
            patch.object(md, "_post", return_value=resp),
            patch.object(
                md, "require_strict_session_key", return_value=("dashboard:member-conductor", "")
            ),
        ):
            return md._call_tool_inner("session_send", {"target": MEMBER_B, "message": "hi"})

    def test_steered_is_reported_as_steered(self):
        out = self._call({"ok": True, "target": MEMBER_B, "started": False, "steered": True})
        assert "Steered into" in out and "when the current turn ends" not in out

    def test_started_and_queued_wording_unchanged(self):
        assert "Delivered to" in self._call(
            {"ok": True, "target": MEMBER_B, "started": True, "steered": False}
        )
        assert "Queued for" in self._call(
            {"ok": True, "target": MEMBER_B, "started": False, "steered": False}
        )
        # An older gateway without the field still reads as queued.
        assert "Queued for" in self._call({"ok": True, "target": MEMBER_B, "started": False})


class TestRosterPreviewStrip:
    def test_prefix_is_stripped_before_any_cap(self):
        assert (
            sc.strip_sent_by_prefix("[sent by session member-a via session_send]\n\nhello there")
            == "hello there"
        )
        assert sc.strip_sent_by_prefix("[sent by session chat-1 via send_message] done") == "done"
        assert sc.strip_sent_by_prefix("plain text") == "plain text"
        # Only the leading line; a bracket line later in the body is content.
        assert (
            sc.strip_sent_by_prefix("x\n[sent by session a via b] y")
            == "x\n[sent by session a via b] y"
        )


class TestMidPlanIsBusyForEveryCaller:
    def test_a_plain_prompt_queues_behind_a_mid_plan_slot(self, tmp_path, monkeypatch):
        """The slot-level predicate, exercised the way a pre-existing caller (a
        heartbeat prompt, the workflow-finished auto-turn) uses it: no meta, no
        peer -- between plan stages the prompt queues instead of starting a turn
        over the plan, and it starts once the plan is over."""
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot._in_stage_execution = True
        ran: list[str] = []

        async def _fake_run_chat(_state, _slot, prompt):
            ran.append(prompt)

        async def _drive():
            started = slot.enqueue_or_run_prompt("heartbeat: anything new?", _fake_run_chat, state)
            await asyncio.sleep(0)
            return started

        assert asyncio.run(_drive()) is False
        assert [q["content"] for q in slot._queue] == ["heartbeat: anything new?"]
        assert ran == []

        slot._in_stage_execution = False

        async def _drive_again():
            started = slot.enqueue_or_run_prompt("heartbeat: again", _fake_run_chat, state)
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return started

        assert asyncio.run(_drive_again()) is True
        assert ran == ["heartbeat: again"]


class TestClientCannotForgeSentBy:
    """``meta.sent_by`` is the gateway's stamp; a ``POST /api/chat`` body that
    names it must not reach the persisted row."""

    def test_the_reserved_key_is_dropped_and_the_rest_kept(self):
        forged = {"session_key": "member-kirocrew-conductor", "via": "session_send"}
        meta = {"sendId": "s-1", "sent_by": forged, "files": ["a.txt"]}
        assert sc.strip_reserved_client_meta(meta) == {"sendId": "s-1", "files": ["a.txt"]}

    def test_a_body_that_is_only_the_forgery_becomes_no_meta(self):
        assert sc.strip_reserved_client_meta({"sent_by": {"session_key": "x", "via": "y"}}) is None

    def test_an_honest_body_passes_through_unchanged(self):
        meta = {"sendId": "s-1"}
        assert sc.strip_reserved_client_meta(meta) is meta
        assert sc.strip_reserved_client_meta(None) is None
        assert sc.strip_reserved_client_meta({}) == {}

    def test_api_chat_strips_before_it_reads_or_persists_the_meta(self):
        """Source pin: the strip sits at the ingress, ahead of every reader of
        ``user_meta`` in the handler (steer send id, queue attachments, the
        persisted row). A later refactor that moves the read above the strip
        re-opens the forgery with every unit test still green."""
        import inspect

        from kiro_crew.dashboard import chat_handlers

        src = inspect.getsource(chat_handlers.api_chat)
        strip_at = src.index("strip_reserved_client_meta(user_meta)")
        first_read = min(
            i
            for i in (
                src.find("user_meta.get("),
                src.find("attachment_meta(user_meta)"),
                src.find("_redact_meta(user_meta)"),
            )
            if i >= 0
        )
        assert strip_at < first_read


class TestPeerRowsSpendNoQuestionCard:
    """An unanswered STATELESS question card is contracted to "the user's next
    message". A row another session authored wears role ``user`` but is not
    that message, so it must retire nothing -- backend gate here, the frontend
    mirror is pinned in ``chatSlice.questionCard.test.ts``."""

    def test_a_peer_row_keeps_the_card_and_announces_nothing(self):
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("member-kirocrew-autofix")
        announced: list = []
        slot._on_question_retired = lambda key, ids: announced.append((key, list(ids)))
        slot._question_pending = {"card-a": {"ts": 0.0, "blocking": False}}
        slot.append(
            "user",
            "[sent by session member-kirocrew-conductor via session_send]\n\nping",
            "msg msg-u",
            broadcast_user=True,
            meta={"sent_by": {"session_key": "member-kirocrew-conductor", "via": "session_send"}},
        )
        assert slot._question_pending == {"card-a": {"ts": 0.0, "blocking": False}}
        assert announced == []
        assert slot.to_dict()["needs_input"] is True

    def test_the_users_own_row_still_retires_it(self):
        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot("member-kirocrew-autofix")
        slot._question_pending = {"card-a": {"ts": 0.0, "blocking": False}}
        slot.append("user", "option A", "msg msg-u", meta={"sendId": "s-1"})
        assert slot._question_pending == {}

    def test_the_predicate_matches_the_queue_drain_rule(self):
        from kiro_crew.dashboard.chat_utils import carries_sent_by
        from kiro_crew.dashboard.state import authored_by_another_session

        rec = {"session_key": "chat-9", "via": "send_message_origin"}
        assert authored_by_another_session({"sent_by": rec}) is True
        assert carries_sent_by({"meta": {"sent_by": rec}}) is True
        assert authored_by_another_session({"sent_by": "chat-9"}) is False
        assert authored_by_another_session(None) is False


class TestPeerSteersSpendNoQuestionCard:
    """The steer twin of ``TestPeerRowsSpendNoQuestionCard``: the consumed echo
    that settles a steer is the point where the runner retires an unanswered
    stateless card (the card may have been posted between the steer's row and
    its consumption). A peer's steer is not the user's next message, so the
    settle must leave the card alone; the user's own steer still retires it."""

    def _slot(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        slot._question_pending = {"card-a": {"ts": 0.0, "blocking": False}}
        return state, slot

    def test_a_settled_peer_steer_keeps_the_card_and_releases_its_author(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        state, slot = self._slot(tmp_path, monkeypatch)
        slot._pending_steers = ["PEER-HELLO from conductor"]
        slot._steer_sent_by = {
            "PEER-HELLO from conductor": {"session_key": "member-kirocrew-conductor", "via": "x"}
        }
        _settle_consumed_steers(
            slot, "<user_message>\nPEER-HELLO from conductor\n</user_message>", state
        )
        assert slot._pending_steers == []
        assert slot._question_pending == {"card-a": {"ts": 0.0, "blocking": False}}
        # Released with the pending steer, so the map is bounded by what is in flight.
        assert slot._steer_sent_by == {}

    def test_the_users_own_settled_steer_still_retires_it(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        state, slot = self._slot(tmp_path, monkeypatch)
        slot._pending_steers = ["option A, go"]
        _settle_consumed_steers(slot, "<user_message>\noption A, go\n</user_message>", state)
        assert slot._question_pending == {}

    def test_a_peer_steer_that_stays_pending_keeps_its_author(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard.chat_runner import _settle_consumed_steers

        state, slot = self._slot(tmp_path, monkeypatch)
        slot._pending_steers = ["from the peer", "from the user"]
        slot._steer_sent_by = {"from the peer": {"session_key": "member-x", "via": "session_send"}}
        # The echo accounts for the user's steer only: the card is spent by it,
        # and the peer's still-pending steer keeps its author for the requeue.
        _settle_consumed_steers(slot, "<user_message>\nfrom the user\n</user_message>", state)
        assert slot._pending_steers == ["from the peer"]
        assert slot._question_pending == {}
        assert "from the peer" in slot._steer_sent_by

    def test_the_steered_path_keeps_the_author_until_the_echo(self):
        """Source pin on ``steer_into_running_turn``: the persisting tail releases
        the delivery and send ids but NOT ``_steer_sent_by`` -- the settle needs
        it. A future "lockstep" tidy-up that pops it there would silently turn
        every peer steer back into a card-retiring user message."""
        import inspect

        from kiro_crew.dashboard import chat_delivery

        src = inspect.getsource(chat_delivery.steer_into_running_turn)
        tail = src[src.rindex("slot._steer_send_ids.pop(message, None)") :]
        assert "_steer_sent_by.pop" not in tail


class TestQueuedPeerRowIsAnnounced:
    """A peer's message queued behind a busy target has no composer to draw its
    queue card, and the client rebuilds the drained row FROM that card when
    ``queue_pop`` arrives -- so without a ``queue_push`` the row is missing
    from the live transcript until a reload."""

    def test_queue_push_is_broadcast_with_the_entry_id_and_a_display_safe_body(self, tmp_path):
        state = _make_state(tmp_path)
        a, b = _members(state)
        _busy(b)
        b._acp_client = None  # no steer client -> the queue path
        frames: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda kind, payload: frames.append((kind, payload))

        asyncio.run(
            sc.send_to_target(
                state, caller_session_key=_key(a), target=MEMBER_B, message="later please"
            )
        )
        (entry,) = b._queue
        (payload,) = [p for k, p in frames if k == "queue_push"]
        assert payload["slot"] == b.key
        assert payload["queue_id"] == entry["id"]
        # The card shows the message, not the model-facing provenance line; the
        # persisted entry keeps the line verbatim.
        assert payload["content"] == "later please"
        assert entry["content"].startswith("[sent by session ")
        assert payload["ts"]

    def test_an_idle_target_broadcasts_no_queue_card(self, tmp_path):
        state = _make_state(tmp_path)
        a, b = _members(state)
        frames: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda kind, payload: frames.append((kind, payload))
        with patch("kiro_crew.dashboard.chat_runner._run_chat", new=AsyncMock()):
            asyncio.run(
                sc.send_to_target(state, caller_session_key=_key(a), target=MEMBER_B, message="now")
            )
        assert [k for k, _ in frames if k == "queue_push"] == []


class TestPeerSteerRevalidatesContainment:
    """The queue path re-validates admission-time containment at the drain; the
    steer path has no drain, so it re-validates in the same loop tick as the
    steer RPC. A constraint newly held since ``authorize_target`` admitted the
    target refuses the message instead of steering it."""

    def _admitted_then_linked(self, tmp_path):
        state = _make_state(tmp_path)
        a, b = _members(state)
        _busy(b)
        b._acp_client = _steerable()
        admission = sc.containment_meta(state, b)
        # Between authorization and the steer, the target gained a channel link.
        b.linked_session_key = "slack:C1:T1"
        return state, a, b, admission

    def test_a_newly_linked_target_refuses_the_steer_with_a_notice(self, tmp_path):
        state, a, b, admission = self._admitted_then_linked(tmp_path)
        frames: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda kind, payload: frames.append((kind, payload))
        sent_by = sc.sent_by_meta(state, a.key, via=sc.SENT_BY_VIA_SESSION_SEND, target_key=b.key)

        landed = asyncio.run(
            sc.deliver_sent_by(
                state,
                b,
                "[sent by session x via session_send]\n\nhi",
                sent_by=sent_by,
                admission=admission,
            )
        )
        assert landed == {"started": False, "steered": False, "refused": ["linked"]}
        b._acp_client.steer.assert_not_awaited()
        assert b._queue == []
        notice = b.messages[-1]
        assert notice["role"] == "notice"
        assert "dropped" in notice["content"] and "authorized" in notice["content"]
        assert [k for k, _ in frames if k == "steer_push"] == []

    def test_send_to_target_reports_the_refusal_to_the_caller(self, tmp_path):
        """`authorize_target` itself refuses a target that is ALREADY linked, so
        the window under test is a link landing between authorization and the
        steer: the admission snapshot reads unlinked, the same-tick re-check
        reads linked."""
        state = _make_state(tmp_path)
        a, b = _members(state)
        _busy(b)
        b._acp_client = _steerable()
        base = sc.containment_snapshot(state, b, on_probe_failure=False)
        snaps = iter([dict(base), {**base, "linked": True}])
        with patch.object(sc, "containment_snapshot", side_effect=lambda *_a, **_k: next(snaps)):
            with pytest.raises(sc.SessionControlError) as exc:
                asyncio.run(
                    sc.send_to_target(
                        state, caller_session_key=_key(a), target=MEMBER_B, message="hi"
                    )
                )
        assert exc.value.code == "containment_changed"
        assert exc.value.status == 409
        b._acp_client.steer.assert_not_awaited()
        assert b._queue == []

    def test_an_unchanged_target_still_steers(self, tmp_path):
        state = _make_state(tmp_path)
        a, b = _members(state)
        _busy(b)
        b._acp_client = _steerable()
        sent_by = sc.sent_by_meta(state, a.key, via=sc.SENT_BY_VIA_SESSION_SEND, target_key=b.key)
        landed = asyncio.run(
            sc.deliver_sent_by(
                state,
                b,
                "[sent by session x via session_send]\n\nhi",
                sent_by=sent_by,
                admission=sc.containment_meta(state, b),
            )
        )
        assert landed == {"started": False, "steered": True}
        b._acp_client.steer.assert_awaited_once()

    def test_a_failed_steer_rechecks_before_falling_back_to_the_queue(self, tmp_path):
        """The steer RPC suspends; when it comes back UNAVAILABLE nothing landed
        and the message falls through to the queue. A link that arrived during
        that suspension must refuse the message there too, not ride the queue."""
        state = _make_state(tmp_path)
        a, b = _members(state)
        _busy(b)
        client = _steerable(accepted=False)

        async def _link_during_rpc(_message):
            b.linked_session_key = "slack:C1:T1"
            return False

        client.steer = AsyncMock(side_effect=_link_during_rpc)
        b._acp_client = client
        sent_by = sc.sent_by_meta(state, a.key, via=sc.SENT_BY_VIA_SESSION_SEND, target_key=b.key)
        landed = asyncio.run(
            sc.deliver_sent_by(
                state,
                b,
                "[sent by session x via session_send]\n\nhi",
                sent_by=sent_by,
                admission=sc.containment_meta(state, b),
            )
        )
        assert landed == {"started": False, "steered": False, "refused": ["linked"]}
        assert b._queue == []
        assert b.messages[-1]["role"] == "notice"

    def test_a_queued_peer_entry_carries_the_original_admission(self, tmp_path):
        """The queue entry records what `authorize_target` saw, not a stamp taken
        at enqueue: a fresh stamp after any suspension would record a constraint
        held by then as already admitted and the drain would wave it through.
        Distinguished from a fresh stamp by a marker the containment rules
        ignore (`mirror_unverified` is telemetry, not a constraint)."""
        state = _make_state(tmp_path)
        a, b = _members(state)
        _busy(b)
        b._acp_client = None  # queue path
        admission = sc.containment_meta(state, b)
        admission[sc.QUEUED_CONTAINMENT_META_KEY]["mirror_unverified"] = "admission-marker"
        sent_by = sc.sent_by_meta(state, a.key, via=sc.SENT_BY_VIA_SESSION_SEND, target_key=b.key)
        landed = asyncio.run(
            sc.deliver_sent_by(
                state,
                b,
                "[sent by session x via session_send]\n\nhi",
                sent_by=sent_by,
                admission=admission,
            )
        )
        assert landed == {"started": False, "steered": False}
        (entry,) = b._queue
        assert (
            entry["meta"][sc.QUEUED_CONTAINMENT_META_KEY]["mirror_unverified"] == "admission-marker"
        )
        assert entry["meta"]["sent_by"] == sent_by

    def test_the_check_sits_in_the_steer_rpcs_tick(self):
        """Source pin: no ``await`` between the re-validation and the steer call
        in ``deliver_sent_by``, and none in ``steer_into_running_turn`` before
        ``client.steer`` -- the two facts the same-tick argument rests on."""
        import inspect

        from kiro_crew.dashboard import chat_delivery

        src = inspect.getsource(sc.deliver_sent_by)
        check_at = src.index('_refuse_if_changed("steer")')
        steer_at = src.index("await steer_into_running_turn(")
        assert "await " not in src[check_at:steer_at]
        # The helper itself is synchronous.
        helper = src[src.index("def _refuse_if_changed") : src.index("if slot.running")]
        assert "await " not in helper
        body = inspect.getsource(chat_delivery.steer_into_running_turn)
        first_await = body.index("await client.steer(message)")
        head = body[:first_await]
        # Strip comment lines: prose mentions "await" while explaining the design.
        code_only = "\n".join(ln for ln in head.splitlines() if not ln.strip().startswith("#"))
        assert "await " not in code_only.split('"""')[-1]


class TestPeerQueueEntryIsNotEditable:
    """A queued entry ANOTHER SESSION authored keeps its ``sent_by`` record, so
    an edit would attribute the replacement text to the peer (and stamp the
    human-origin flag onto a peer's message). Refused at the repository and
    named at the endpoint; the card offers no edit (``QueueStack.test.tsx``)."""

    def test_the_repository_refuses_and_leaves_the_entry_untouched(self, tmp_path):
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        peer = {"session_key": "member-kirocrew-conductor", "via": "session_send"}
        qid = slot.queue_append(
            "[sent by session x via session_send]\n\nping", meta={"sent_by": peer}
        )
        assert slot.queue_edit_by_id(qid, "rewritten", directive_user_origin=True) is False
        (entry,) = slot._queue
        assert entry["content"].endswith("ping")
        assert "_directive_user_origin" not in entry
        # The user's own entry still edits.
        own = slot.queue_append("mine")
        assert slot.queue_edit_by_id(own, "mine, edited", directive_user_origin=True) is True

    @pytest.mark.asyncio
    async def test_the_endpoint_answers_409_peer_authored(self, tmp_path, monkeypatch):
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        state.broadcast_ws = MagicMock()
        slot = state.get_or_create_slot("chat-1")
        qid = slot.queue_append(
            "[sent by session x via session_send]\n\nping",
            meta={"sent_by": {"session_key": "member-kirocrew-conductor", "via": "session_send"}},
        )
        from kiro_crew.dashboard.chat_handlers import api_chat_slot_queue_edit

        app = web.Application()
        app["state"] = state
        app.router.add_patch("/api/chat/slots/{slot}/queue/{queue_id}", api_chat_slot_queue_edit)
        with patch("kiro_crew.sel.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            async with TestClient(TestServer(app)) as client:
                resp = await client.patch(
                    f"/api/chat/slots/chat-1/queue/{qid}",
                    json={"content": "rewritten"},
                )
                assert resp.status == 409
                assert (await resp.json())["code"] == "peer_authored"
        assert slot._queue[0]["content"].endswith("ping")
        assert not any(c.args[0] == "queue_edit" for c in state.broadcast_ws.call_args_list)

    @pytest.mark.asyncio
    async def test_slot_detail_hydration_carries_the_record(self, tmp_path, monkeypatch):
        """Run 3 found the hydrated card losing its record: the detail handler
        snapshots the queue before rendering and the snapshot carried id and
        content only, so a reload drew the peer's card as the user's own (prefix
        visible, edit offered). The snapshot keeps ``meta`` now."""
        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        state = _make_state(tmp_path)
        slot = state.get_or_create_slot("chat-1")
        peer = {"session_key": "member-kirocrew-conductor", "via": "session_send"}
        qid = slot.queue_append(
            "[sent by session x via session_send]\n\nping", meta={"sent_by": peer}
        )
        own = slot.queue_append("mine")
        async with TestClient(TestServer(_detail_app(state))) as client:
            resp = await client.get("/api/chat/slots/chat-1")
            assert resp.status == 200
            queue = (await resp.json())["queue"]
        by_id = {q["id"]: q for q in queue}
        assert by_id[qid]["sent_by"] == peer
        assert "sent_by" not in by_id[own]

    def test_slot_detail_queue_items_carry_the_record(self):
        from kiro_crew.dashboard.chat_handlers import _queue_item_view

        peer = {"session_key": "member-kirocrew-conductor", "via": "session_send"}
        view = _queue_item_view({"id": "q1", "content": "hello", "meta": {"sent_by": peer}})
        assert view == {"id": "q1", "content": "hello", "sent_by": peer}
        assert _queue_item_view({"id": "q2", "content": "mine", "meta": {}}) == {
            "id": "q2",
            "content": "mine",
        }


class TestPeerCrewRowsCannotCarrySentBy:
    """A connected peer crew's rows are relayed (`remote_relay._apply_row`) or
    adopted (`remote_adopt.prepare_backfill_rows`) into local slots WITH their
    meta. `sent_by` names a LOCAL member as the author, so a peer-stamped copy
    would draw "From <member>" on a transcript that member never wrote into;
    the keys only this gateway may stamp are dropped at both doors."""

    def test_the_shared_key_list_and_the_client_ingress_agree(self):
        from kiro_crew.dashboard.state import GATEWAY_STAMPED_META_KEYS

        assert sc.SENT_BY_META_KEY in GATEWAY_STAMPED_META_KEYS
        assert sc.strip_reserved_client_meta({"sent_by": {"session_key": "x", "via": "y"}}) is None

    def test_peer_row_meta_drops_the_provenance_and_the_delivery_id(self):
        from kiro_crew.dashboard.remote_relay import peer_row_meta

        kept = peer_row_meta(
            {
                "mid": "peer-77",
                "tool_name": "fs_read",
                "sent_by": {"session_key": "member-kirocrew-conductor", "via": "session_send"},
            }
        )
        assert kept == {"tool_name": "fs_read"}
        assert peer_row_meta({"sent_by": {"session_key": "x", "via": "y"}}) is None
        assert peer_row_meta("not a dict") is None

    def test_adopted_backfill_rows_carry_no_sent_by(self):
        from kiro_crew.dashboard import remote_adopt as ra

        rows, _, _in_flight = ra.prepare_backfill_rows(
            [
                {
                    "role": "user",
                    "content": "[sent by session member-kirocrew-conductor via session_send]\n\nhi",
                    "cls": "",
                    "ts": "2026-09-14T00:00:00Z",
                    "meta": {
                        "sent_by": {
                            "session_key": "member-kirocrew-conductor",
                            "via": "session_send",
                        }
                    },
                }
            ]
        )
        (row,) = rows
        assert row["role"] == "user"
        assert row["meta"] is None
        from kiro_crew.dashboard.state import authored_by_another_session

        assert not authored_by_another_session(row["meta"])


class TestMirroredPeerFramesCarryNoProvenance:
    """Live frames mirrored from a peer crew (`remote_relay._replay_mirrored_frame`)
    are re-broadcast under a local slot key, and the frontend reads `sent_by` /
    `sentBy` / `meta.sent_by` as "another session on THIS gateway wrote this".
    A peer's record names the peer's members; it is dropped before the broadcast."""

    def test_the_stripper_covers_every_carrier(self):
        from kiro_crew.dashboard.remote_relay import _strip_peer_frame_provenance

        rec = {"session_key": "member-kirocrew-conductor", "via": "session_send"}
        queue_push = {"slot": "peer-1", "content": "x", "queue_id": "q1", "sent_by": rec}
        _strip_peer_frame_provenance(queue_push)
        assert queue_push == {"slot": "peer-1", "content": "x", "queue_id": "q1"}

        steer_push = {"slot": "peer-1", "content": "x", "sentBy": rec, "steerState": "written"}
        _strip_peer_frame_provenance(steer_push)
        assert steer_push == {"slot": "peer-1", "content": "x", "steerState": "written"}

        chat_message = {"slot": "peer-1", "role": "user", "meta": {"sent_by": rec, "mid": "m1"}}
        _strip_peer_frame_provenance(chat_message)
        assert chat_message["meta"] == {"mid": "m1"}

        only_provenance = {"slot": "peer-1", "role": "user", "meta": {"sent_by": rec}}
        _strip_peer_frame_provenance(only_provenance)
        assert "meta" not in only_provenance

    def test_a_mirrored_queue_push_is_broadcast_without_the_record(self, tmp_path):
        from kiro_crew.dashboard.remote_relay import _replay_mirrored_frame

        state = _make_state(tmp_path)
        frames: list[tuple[str, dict]] = []
        state.broadcast_ws = lambda kind, payload: frames.append((kind, payload))
        slot = state.get_or_create_slot("chat-1")
        encoded = json.dumps(
            {
                "slot": "peer-chat-9",
                "content": "PEER hello",
                "queue_id": "q1",
                "sent_by": {"session_key": "member-kirocrew-conductor", "via": "session_send"},
            }
        )
        _replay_mirrored_frame(state, slot, "queue_push", encoded)
        ((kind, payload),) = frames
        assert kind == "queue_push"
        assert payload["slot"] == "chat-1"
        assert "sent_by" not in payload
        assert payload["content"] == "PEER hello"
