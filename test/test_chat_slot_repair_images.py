"""POST /api/chat/slots/{slot}/repair-images: the dashboard recovery endpoint.

The endpoint composes three existing pieces rather than inventing a teardown:
it runs the shared ``reload_slot_session`` (which carries every busy/children/
rebind guard), passes it an ``after_reset_in_lock`` hook that repairs the
transcript WHILE the kiro-cli process is down, and then re-issues the
interrupted turn with ``continue_slot_turn``. These tests pin that composition
and the repair hook, mocking the two shared helpers so this file does not
re-test the reload two-lock dance (``test_chat_slot_reload`` owns that) or the
continuation machinery (``continue_turn`` owns that).
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from kiro_crew.config import paths as cfg_paths
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.chat_api.continue_turn import SlotContinueRefusal
from kiro_crew.dashboard.chat_handlers import api_chat_slot_repair_images
from kiro_crew.dashboard.state import DashboardState, _ChatSlot


@pytest.fixture
def sessions_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "sessions" / "cli"
    d.mkdir(parents=True)
    monkeypatch.setattr(cfg_paths, "_sessions_dir_override", lambda: d)
    return d


def _oversized_transcript(path: Path) -> None:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (3000, 10), (1, 2, 3)).save(buf, format="PNG")
    payload = list(buf.getvalue())
    record = {
        "data": {
            "content": [
                {"kind": "text", "text": "hi"},
                {
                    "kind": "image",
                    "data": {"format": "png", "source": {"kind": "bytes", "data": payload}},
                },
            ]
        }
    }
    path.write_text(json.dumps(record) + "\n")


def _longest_edge(path: Path) -> int:
    from PIL import Image

    record = json.loads(path.read_text().splitlines()[0])
    block = record["data"]["content"][1]
    with Image.open(io.BytesIO(bytes(block["data"]["source"]["data"]))) as im:
        return max(im.size)


def _state(slot: _ChatSlot, sid: str) -> DashboardState:
    state = MagicMock(spec=DashboardState)
    state._slots = {slot.key: slot}
    state.sessions = MagicMock()
    state.sessions.resumable_sid = MagicMock(return_value=sid)
    state.sessions.get_provider = MagicMock(return_value=None)
    return state


def _app(state: DashboardState) -> web.Application:
    app = web.Application()
    app["state"] = state

    @web.middleware
    async def _publish_app(request: web.Request, handler):
        request["app"] = ""  # a dashboard user
        return await handler(request)

    app.middlewares.append(_publish_app)
    app.router.add_post("/api/chat/slots/{slot}/repair-images", api_chat_slot_repair_images)
    return app


async def _post(state: DashboardState, slot: str):
    async with TestClient(TestServer(_app(state))) as client:
        resp = await client.post(f"/api/chat/slots/{slot}/repair-images")
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_hook_repairs_transcript_then_turn_is_reissued(
    sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    t = sessions_dir / "sid-1.jsonl"
    _oversized_transcript(t)
    assert _longest_edge(t) == 3000

    slot = _ChatSlot("chat-1-foo")
    state = _state(slot, "sid-1")

    # Mock reload to RUN the hook it is handed (the behaviour the real reload
    # gives after a successful teardown), then return a 200.
    async def _fake_reload(_state, _slot, _name, *, after_reset_in_lock=None, **_kw):
        if after_reset_in_lock is not None:
            await after_reset_in_lock("chat-1-foo")
        return web.json_response({"ok": True})

    continue_mock = AsyncMock()
    monkeypatch.setattr(chat_handlers, "reload_slot_session", _fake_reload)
    monkeypatch.setattr(chat_handlers, "continue_slot_turn", continue_mock)

    status, body = await _post(state, "chat-1-foo")

    assert status == 200
    assert body == {"slot": "chat-1-foo", "repaired": True, "resumed": True}
    # The transcript was downscaled in place, with a backup sidecar.
    assert _longest_edge(t) <= 2000
    assert (sessions_dir / "sid-1.jsonl.pre-image-repair.bak").exists()
    # The interrupted turn was re-issued once.
    continue_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_reload_refusal_is_returned_and_no_turn_is_reissued(
    sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    slot = _ChatSlot("chat-1-foo")
    state = _state(slot, "sid-1")

    async def _fake_reload(*_a, **_kw):
        # The reload guards refused (e.g. a turn in flight): the hook never runs.
        return web.json_response(
            {"error": "a turn is in flight", "code": "turn_in_flight"}, status=409
        )

    continue_mock = AsyncMock()
    monkeypatch.setattr(chat_handlers, "reload_slot_session", _fake_reload)
    monkeypatch.setattr(chat_handlers, "continue_slot_turn", continue_mock)

    status, body = await _post(state, "chat-1-foo")

    assert status == 409
    assert body["code"] == "turn_in_flight"
    continue_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_continue_refusal_reports_repaired_but_not_resumed(
    sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    t = sessions_dir / "sid-1.jsonl"
    _oversized_transcript(t)
    slot = _ChatSlot("chat-1-foo")
    state = _state(slot, "sid-1")

    async def _fake_reload(_state, _slot, _name, *, after_reset_in_lock=None, **_kw):
        if after_reset_in_lock is not None:
            await after_reset_in_lock("chat-1-foo")
        return web.json_response({"ok": True})

    async def _refuse(*_a, **_kw):
        raise SlotContinueRefusal("slot is running", "slot_running")

    monkeypatch.setattr(chat_handlers, "reload_slot_session", _fake_reload)
    monkeypatch.setattr(chat_handlers, "continue_slot_turn", _refuse)

    status, body = await _post(state, "chat-1-foo")

    # The repair landed; only the re-issue was refused, so it is reported without
    # undoing the repair.
    assert status == 200
    assert body["repaired"] is True
    assert body["resumed"] is False
    # The refusal is logged, not returned: the body carries only repaired/resumed.
    assert "continue_code" not in body
    assert "continue_error" not in body
    assert _longest_edge(t) <= 2000


@pytest.mark.asyncio
async def test_no_resumable_sid_skips_repair_but_still_reissues(
    sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    slot = _ChatSlot("chat-1-foo")
    state = _state(slot, "")  # no resumable sid -> nothing to repair

    hook_seen: dict[str, bool] = {}

    async def _fake_reload(_state, _slot, _name, *, after_reset_in_lock=None, **_kw):
        if after_reset_in_lock is not None:
            await after_reset_in_lock("chat-1-foo")
            hook_seen["ran"] = True
        return web.json_response({"ok": True})

    continue_mock = AsyncMock()
    monkeypatch.setattr(chat_handlers, "reload_slot_session", _fake_reload)
    monkeypatch.setattr(chat_handlers, "continue_slot_turn", continue_mock)

    status, body = await _post(state, "chat-1-foo")

    assert status == 200
    # Nothing was rewritten (no transcript), so repaired is False -- but the turn
    # is still re-issued, since the wedge may have cleared by hand.
    assert body == {"slot": "chat-1-foo", "repaired": False, "resumed": True}
    assert hook_seen.get("ran") is True
    continue_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_nothing_over_cap_reports_not_repaired_but_still_reissues(
    sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    # A transcript that is already within the cap: scan_file returns lines=None,
    # so nothing is rewritten. repaired=False, but the turn is still re-issued.
    import json

    t = sessions_dir / "sid-1.jsonl"
    t.write_text(json.dumps({"data": {"content": [{"kind": "text", "text": "hi"}]}}) + "\n")
    slot = _ChatSlot("chat-1-foo")
    state = _state(slot, "sid-1")

    async def _fake_reload(_state, _slot, _name, *, after_reset_in_lock=None, **_kw):
        if after_reset_in_lock is not None:
            await after_reset_in_lock("chat-1-foo")
        return web.json_response({"ok": True})

    continue_mock = AsyncMock()
    monkeypatch.setattr(chat_handlers, "reload_slot_session", _fake_reload)
    monkeypatch.setattr(chat_handlers, "continue_slot_turn", continue_mock)

    status, body = await _post(state, "chat-1-foo")
    assert status == 200
    assert body == {"slot": "chat-1-foo", "repaired": False, "resumed": True}
    assert not (sessions_dir / "sid-1.jsonl.pre-image-repair.bak").exists()
    continue_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_scan_tripwire_is_refused_and_the_turn_is_not_reissued(
    sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    # scan_file returns lines=None AND fires its tripwire (it saw image-shaped
    # blocks the anchored walk did not): "nothing over the cap" cannot be
    # trusted, so re-issuing would replay the same wedge. The route refuses with
    # a 409 instead and does NOT re-issue.
    import json

    from kiro_crew import session_image_repair

    t = sessions_dir / "sid-1.jsonl"
    t.write_text(json.dumps({"data": {"content": []}}) + "\n")
    slot = _ChatSlot("chat-1-foo")
    state = _state(slot, "sid-1")

    report = session_image_repair.FileReport(path=t, unanchored_images=1)
    monkeypatch.setattr(session_image_repair, "scan_file", lambda _p: (report, None))

    async def _fake_reload(_state, _slot, _name, *, after_reset_in_lock=None, **_kw):
        if after_reset_in_lock is not None:
            await after_reset_in_lock("chat-1-foo")
        return web.json_response({"ok": True})

    continue_mock = AsyncMock()
    monkeypatch.setattr(chat_handlers, "reload_slot_session", _fake_reload)
    monkeypatch.setattr(chat_handlers, "continue_slot_turn", continue_mock)

    status, body = await _post(state, "chat-1-foo")
    assert status == 409
    assert body["code"] == "image_repair_failed"
    continue_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_second_wedge_is_refused_so_the_first_backup_is_preserved(
    sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    # A prior repair left a `.pre-image-repair.bak` (the single recovery copy of
    # the original bytes). The module refuses to overwrite it, so a second wedge
    # is refused with 409 rather than rewritten without a backup; the operator
    # remedy is to move the old sidecar aside and repair again. The hook raises,
    # so the respawn is skipped and no turn is re-issued, and the transcript and
    # the first backup are both left untouched.
    t = sessions_dir / "sid-1.jsonl"
    _oversized_transcript(t)
    bak = sessions_dir / "sid-1.jsonl.pre-image-repair.bak"
    bak.write_text("the original pre-repair bytes\n")
    bak_before = bak.read_text()
    t_before = t.read_bytes()
    slot = _ChatSlot("chat-1-foo")
    state = _state(slot, "sid-1")

    async def _fake_reload(_state, _slot, _name, *, after_reset_in_lock=None, **_kw):
        if after_reset_in_lock is not None:
            await after_reset_in_lock("chat-1-foo")
        return web.json_response({"ok": True})

    continue_mock = AsyncMock()
    monkeypatch.setattr(chat_handlers, "reload_slot_session", _fake_reload)
    monkeypatch.setattr(chat_handlers, "continue_slot_turn", continue_mock)

    status, body = await _post(state, "chat-1-foo")

    assert status == 409
    assert body["code"] == "image_repair_backup_exists"
    continue_mock.assert_not_awaited()
    assert t.read_bytes() == t_before  # transcript untouched
    assert bak.read_text() == bak_before  # the first backup is preserved


@pytest.mark.asyncio
async def test_a_repair_failure_raises_so_the_turn_is_not_reissued(
    sessions_dir: Path, monkeypatch: pytest.MonkeyPatch
):
    # A transcript that moved under the repair (TranscriptMovedError) is a real
    # failure: the hook raises, the real reload propagates it, the respawn is
    # SKIPPED, no turn is re-issued, and the route answers 409.
    import kiro_crew.session_image_repair as sir

    t = sessions_dir / "sid-1.jsonl"
    _oversized_transcript(t)
    slot = _ChatSlot("chat-1-foo")
    state = _state(slot, "sid-1")

    def _boom(*_a, **_kw):
        raise sir.TranscriptMovedError("the transcript changed under the repair")

    monkeypatch.setattr(sir, "apply_repair", _boom)

    async def _fake_reload(_state, _slot, _name, *, after_reset_in_lock=None, **_kw):
        if after_reset_in_lock is not None:
            await after_reset_in_lock("chat-1-foo")
        return web.json_response({"ok": True})

    continue_mock = AsyncMock()
    monkeypatch.setattr(chat_handlers, "reload_slot_session", _fake_reload)
    monkeypatch.setattr(chat_handlers, "continue_slot_turn", continue_mock)

    status, body = await _post(state, "chat-1-foo")

    assert status == 409
    assert body["code"] == "image_repair_failed"
    continue_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_missing_slot_is_404():
    state = MagicMock(spec=DashboardState)
    state._slots = {}
    status, _body = await _post(state, "no-such-slot")
    assert status == 404
