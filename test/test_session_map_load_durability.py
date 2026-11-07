"""A failed startup read of the session map must never erase the map on disk.

``SessionMap._load`` reads the file from the constructor (an async write re-reads
it once if that failed), and every later
mutation rewrites the WHOLE file from memory. If an ``OSError`` on that read
(EIO, EACCES, or a Windows sharing violation from AV, an indexer or a sync
client) left
the map empty in memory, the next ordinary write would replace the file with
that empty map and drop every session_key -> sid binding.

A file that reads but is not usable JSON starts the map empty too; its bytes are
moved to ``session_map.json.corrupt-<utc>`` by the first write, and one leading
UTF-8 byte-order mark reads normally.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import threading

import pytest

from kiro_crew import session_map_store
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.session_map import SessionMap
from kiro_crew.testing.wait import async_wait_until

_SEED = {
    "dashboard:alice": {"sid": "sid-ALICE", "slack_thread_ts": None, "slack_channel_id": None},
    "dashboard:bob": {"sid": "sid-BOB", "slack_thread_ts": None, "slack_channel_id": None},
}


@pytest.fixture()
def map_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.session_map.config_dir", lambda: tmp_path)
    (tmp_path / "session_map.json").write_text(json.dumps(_SEED), encoding="utf-8")
    return tmp_path


@pytest.fixture()
def unreadable_once(monkeypatch):
    """Fail the next map read only; set ``["on"]`` again to fail the read after it too."""
    armed = {"on": True}

    def flaky(self, *args, **kwargs):
        if self.name == "session_map.json" and armed.pop("on", False):
            raise OSError("EIO: handle held by a scanner")
        return real(self, *args, **kwargs)

    real = pathlib.Path.read_text
    monkeypatch.setattr(pathlib.Path, "read_text", flaky)
    return armed


def _raw(map_dir: pathlib.Path) -> bytes:
    """The file's bytes, read without the patched ``Path`` methods."""
    with open(map_dir / "session_map.json", "rb") as handle:
        return handle.read()


def _assert_bindings_intact(map_dir: pathlib.Path) -> None:
    reloaded = SessionMap()
    assert reloaded._path == map_dir / "session_map.json"
    assert reloaded._data["dashboard:alice"]["sid"] == "sid-ALICE"
    assert reloaded._data["dashboard:bob"]["sid"] == "sid-BOB"


def test_a_write_after_a_failed_read_raises_and_leaves_the_file(map_dir, unreadable_once) -> None:
    before = _raw(map_dir)
    sm = SessionMap()
    assert sm._path == map_dir / "session_map.json"

    with pytest.raises(OSError, match="not readable at load"):
        sm.set_agent_override("dashboard:carol", "reviewer")

    assert _raw(map_dir) == before
    _assert_bindings_intact(map_dir)


@pytest.mark.asyncio
async def test_aflush_after_a_failed_read_fails_and_reowes_the_state(
    map_dir, unreadable_once
) -> None:
    """A persist-before-publish caller sees the failure, not a silent success."""
    before = _raw(map_dir)
    sm = SessionMap()
    sm.set_agent_override("dashboard:carol", "reviewer")
    task = sm._flush_task
    assert task is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    unreadable_once["on"] = True  # the one re-read fails too
    with pytest.raises(OSError, match="not readable at load"):
        await sm.aflush()

    assert sm._dirty
    assert _raw(map_dir) == before
    unreadable_once["on"] = True
    with pytest.raises(OSError, match="not readable at load"):
        await sm.aflush()
    assert unreadable_once.pop("on", False), "a second re-read ran"
    _assert_bindings_intact(map_dir)


@pytest.mark.asyncio
@pytest.mark.parametrize("land", ["deferred flush", "aflush"])
async def test_a_file_readable_again_is_adopted_by_the_first_async_write(
    map_dir, unreadable_once, land
) -> None:
    """A key written since the failed load joins the file's bindings; writes resume."""
    sm = SessionMap()
    sm.set_agent_override("dashboard:carol", "reviewer")
    task = sm._flush_task
    assert task is not None
    if land == "aflush":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await sm.aflush()
    else:
        await task

    assert not sm._dirty
    _assert_bindings_intact(map_dir)
    assert SessionMap().get_agent_override("dashboard:carol") == "reviewer"


@pytest.mark.asyncio
async def test_an_aflush_during_the_reread_waits_for_it(
    map_dir, unreadable_once, monkeypatch
) -> None:
    """A snapshot claimed before the adopt lacks the file's bindings; none is claimed."""
    reading, release = threading.Event(), threading.Event()
    real = session_map_store._read_map_file

    def held(path: pathlib.Path) -> dict:
        reading.set()
        release.wait(timeout=30)
        return real(path)

    monkeypatch.setattr(session_map_store, "_read_map_file", held)
    sm = SessionMap()
    sm.set_agent_override("dashboard:carol", "reviewer")
    task = sm._flush_task
    assert task is not None
    try:
        await async_wait_until(reading.is_set, describe=lambda: "the deferred flush never re-read")
        durable = asyncio.ensure_future(sm.aflush())
    finally:
        release.set()
    await durable
    await task

    _assert_bindings_intact(map_dir)
    assert SessionMap().get_agent_override("dashboard:carol") == "reviewer"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "write",
    [
        lambda sm: sm.set_agent_override("dashboard:alice", "reviewer"),
        lambda sm: sm.set_slack_link("dashboard:carol", "ts-1", "C1"),
        lambda sm: sm.set_mirror_link(
            "dashboard:carol", ChannelLink(channel_type="telegram", channel_id="99")
        ),
    ],
    ids=["a-file-key", "a-slack-thread", "a-mirror"],
)
async def test_a_binding_written_since_the_failed_load_that_may_collide_keeps_the_file(
    map_dir, unreadable_once, write
) -> None:
    """A blank ``dashboard:alice`` would replace the file's sid, and a Slack thread or mirror
    has one owner the file may already name, so nothing is adopted."""
    before = _raw(map_dir)
    sm = SessionMap()
    write(sm)
    task = sm._flush_task
    assert task is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(OSError, match="not readable at load"):
        await sm.aflush()

    assert _raw(map_dir) == before
    _assert_bindings_intact(map_dir)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "seed",
    [
        {"unified:agent:gen1": {"sid": "sid-OLD"}},
        {"unified:agent": {"sid": "sid-OLD", "generation_floor": 5}},
    ],
    ids=["a-generation-key", "a-generation-floor"],
)
async def test_a_file_with_dm_generations_is_not_adopted(map_dir, unreadable_once, seed) -> None:
    """A ``/new`` counter seeded from the empty map is behind the file's generations, so
    adopting them would make a later ``/new`` resume an old conversation."""
    (map_dir / "session_map.json").write_text(json.dumps({**_SEED, **seed}), encoding="utf-8")
    before = _raw(map_dir)
    sm = SessionMap()
    sm.set_agent_override("dashboard:carol", "reviewer")
    task = sm._flush_task
    assert task is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with pytest.raises(OSError, match="not readable at load"):
        await sm.aflush()

    assert _raw(map_dir) == before


def _corrupt_copies(map_dir: pathlib.Path) -> list[pathlib.Path]:
    return sorted(map_dir.glob("session_map.json.corrupt-*"))


@pytest.mark.parametrize(
    "content",
    [b"{not json", b"[]", b"\xff\xfe not utf-8"],
    ids=["not-json", "a-list", "not-utf8"],
)
def test_an_unparseable_file_is_kept_aside_before_the_first_write(map_dir, content) -> None:
    """Writes proceed from an empty map, and the old bytes survive in a ``.corrupt-`` copy."""
    (map_dir / "session_map.json").write_bytes(content)
    sm = SessionMap()
    assert sm._data == {}
    assert _corrupt_copies(map_dir) == []

    sm.set_agent_override("dashboard:carol", "reviewer")
    sm.flush()

    copies = _corrupt_copies(map_dir)
    assert len(copies) == 1
    with open(copies[0], "rb") as handle:
        assert handle.read() == content
    on_disk = json.loads((map_dir / "session_map.json").read_text(encoding="utf-8"))
    assert list(on_disk) == ["dashboard:carol"]

    sm.set_agent_override("dashboard:dave", "reviewer")
    sm.flush()
    assert len(_corrupt_copies(map_dir)) == 1


def test_a_read_only_instance_never_moves_an_unparseable_file(map_dir) -> None:
    (map_dir / "session_map.json").write_bytes(b"{not json")
    SessionMap()
    assert _raw(map_dir) == b"{not json"
    assert _corrupt_copies(map_dir) == []


def test_a_bom_file_keeps_every_binding_on_the_next_write(map_dir) -> None:
    (map_dir / "session_map.json").write_bytes(b"\xef\xbb\xbf" + json.dumps(_SEED).encode())
    sm = SessionMap()
    sm.set_agent_override("dashboard:carol", "reviewer")
    sm.flush()

    _assert_bindings_intact(map_dir)
    assert _corrupt_copies(map_dir) == []


def test_a_failed_move_aside_refuses_the_write_until_one_succeeds(map_dir, monkeypatch) -> None:
    (map_dir / "session_map.json").write_bytes(b"{not json")
    sm = SessionMap()
    real = session_map_store.replace_with_retry

    def refuse(src, dst, **kwargs):
        raise PermissionError("EACCES: rename refused")

    monkeypatch.setattr(session_map_store, "replace_with_retry", refuse)
    with pytest.raises(PermissionError):
        sm.set_agent_override("dashboard:carol", "reviewer")
    assert _raw(map_dir) == b"{not json"
    assert _corrupt_copies(map_dir) == []

    monkeypatch.setattr(session_map_store, "replace_with_retry", real)
    sm.flush()
    copies = _corrupt_copies(map_dir)
    assert len(copies) == 1
    with open(copies[0], "rb") as handle:
        assert handle.read() == b"{not json"
    on_disk = json.loads((map_dir / "session_map.json").read_text(encoding="utf-8"))
    assert list(on_disk) == ["dashboard:carol"]


async def _api_status_body(monkeypatch, map_dir: pathlib.Path, session_map: SessionMap) -> dict:
    """``GET /api/status`` from a real ``DashboardState`` whose session manager holds *session_map*."""
    from aiohttp import web
    from aiohttp.test_utils import make_mocked_request
    from test_handlers_system_meminfo_unavailable import _status_state

    from kiro_crew.dashboard.handlers_system import api_status

    state = _status_state(monkeypatch, map_dir)
    state.sessions._session_map = session_map
    app = web.Application()
    app["state"] = state
    resp = await api_status(make_mocked_request("GET", "/api/status", app=app))
    return json.loads(resp.body.decode())


@pytest.mark.asyncio
async def test_api_status_names_the_refuse_writes_state_and_its_remedy(
    map_dir, unreadable_once, monkeypatch
) -> None:
    """The scattered write failures after a failed read have one place that explains them."""
    refusing = await _api_status_body(monkeypatch, map_dir, SessionMap())
    assert refusing["session_map_writes_refused"] is True
    assert "restart the gateway once the file is readable" in refusing["session_map_remedy"]

    healthy = await _api_status_body(monkeypatch, map_dir, SessionMap())
    assert healthy["session_map_writes_refused"] is False
    assert healthy["session_map_remedy"] == ""
