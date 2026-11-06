"""Opening a chat in kiro-cli: the session kiro-cli resumes, and the chat's own
start racing the open.

kiro-cli 2.21.1's ``chat --resume-id`` runs the agent the session's envelope
(``~/.kiro/sessions/cli/<sid>.json``) names, whatever ``--agent`` says, and keeps
the tools the envelope trusts trusted. Both were checked live on a pty: a resume
with ``--agent`` naming another agent ran the envelope's agent, and one whose
envelope trusted ``executeCmd`` ran a shell command without asking, then asked
once the envelope trusted nothing. So the open points the envelope at its launch
copy, empties its grants for the hold, and puts both back on release.

Split from ``test_kiro_cli_launch.py`` (the file-line cap); the fixtures are
that module's.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pathlib
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from test_kiro_cli_launch import (  # noqa: F401 -- the fixtures are bound for pytest
    KEY,
    SID,
    _isolate,
    _request,
    _slot,
    _state,
    _turn,
    switch_fixture,
    wired_fixture,
)

from kiro_crew import kiro_cli_handoff
from kiro_crew.dashboard import chat_handlers
from kiro_crew.dashboard.handlers import kiro_cli_launch as kcl
from kiro_crew.dashboard.handlers import kiro_cli_launch_v3 as v3
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until

#: The agent the chat's v3 ``session.json`` names before the open.
_SAVED_AGENT = "myagent"


def _session(wired) -> dict:
    return json.loads((wired.v3 / "session.json").read_text())


def _rewrite(wired, change) -> None:
    document = _session(wired)
    change(document)
    (wired.v3 / "session.json").write_text(json.dumps(document))


async def _open(wired, terminal_id="t"):
    (wired.log).touch()
    return await kcl.prepare_kiro_cli_launch(
        _request(_state(_slot(wired.project))), "main", terminal_id
    )


# ── the session kiro-cli resumes ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_terminal_resumes_on_the_launch_copy_and_the_release_puts_the_agent_back(
    wired, switch
):
    launch, code = await _open(wired)
    assert code == "" and launch is not None
    assert _session(wired)["agentMode"] == pathlib.Path(launch.launch_spec).stem
    [record] = [json.loads(p.read_text()) for p in wired.records.iterdir()]
    assert record["session_agent"] == {"agentMode": _SAVED_AGENT}, "a restart can put it back"
    assert record["engine"] == kcl.ENGINE_V3
    launch.announce()

    # kiro-cli rewrites session.json as it runs.
    def in_kiro_cli(document):
        document["title"] = "renamed in kiro-cli"

    _rewrite(wired, in_kiro_cli)
    assert await kiro_cli_handoff.settle_and_release(KEY, "t")
    after = _session(wired)
    assert after["agentMode"] == _SAVED_AGENT
    assert after["title"] == "renamed in kiro-cli", "the rest is kiro-cli's"
    assert list(wired.records.iterdir()) == []


@pytest.mark.asyncio
async def test_the_record_names_what_the_open_writes_before_it_writes_it(
    wired, switch, monkeypatch
):
    """A gateway killed between any two of the open's writes leaves its restart
    a record that can undo what was written: the record goes first."""
    seen = []
    real_write, real_point = kcl._write_launch_spec, v3.point_session_agent

    def on_disk():
        [record] = [json.loads(p.read_text()) for p in wired.records.iterdir()]
        return record

    def write(agent, target):
        record = on_disk()
        seen.append(("copy", record["launch_spec"] == target))
        real_write(agent, target)

    def point(sid, agent):
        record = on_disk()
        seen.append(("session", record["session_agent"]["agentMode"] == _SAVED_AGENT))
        real_point(sid, agent)

    monkeypatch.setattr(kcl, "_write_launch_spec", write)
    monkeypatch.setattr(v3, "point_session_agent", point)
    launch, code = await _open(wired)
    assert code == ""
    assert seen == [("copy", True), ("session", True)]
    # Killed right after the session.json write: the restart undoes both.
    kiro_cli_handoff.reset_holds()
    slot = _slot(wired.project)
    assert await kcl.recover_left_open(_state(slot), slot, KEY)
    assert _session(wired)["agentMode"] == _SAVED_AGENT
    assert [p.name for p in wired.agents.iterdir()] == ["myagent.json"]


@pytest.mark.asyncio
async def test_a_cancelled_open_rolls_back_only_after_its_envelope_write_finishes(
    wired, switch, monkeypatch
):
    """A cancel landing while the envelope write runs in its worker must not
    restore the envelope under it: the worker would then write the pointed
    envelope after the rollback, naming a launch copy that is gone."""
    in_worker = threading.Event()
    go = threading.Event()
    real_point = v3.point_session_agent

    def slow_point(sid, agent):
        in_worker.set()
        go.wait(10)
        real_point(sid, agent)

    monkeypatch.setattr(v3, "point_session_agent", slow_point)
    task = asyncio.ensure_future(_open(wired))
    await async_wait_until(in_worker.is_set, timeout=FALLBACK_TIMEOUT_SECS)
    task.cancel()
    go.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _session(wired)["agentMode"] == _SAVED_AGENT
    assert [p.name for p in wired.agents.iterdir()] == ["myagent.json"]
    assert list(wired.records.iterdir()) == []
    assert not kiro_cli_handoff.is_held(KEY)


@pytest.mark.asyncio
async def test_a_release_that_times_out_while_restoring_keeps_the_record(
    wired, switch, monkeypatch
):
    """The import is owed from the moment the release starts: a settle cut off
    at its deadline while the envelope restore runs must not read as imported."""
    launch, code = await _open(wired)
    assert code == ""
    launch.announce()
    real_restore = v3.restore_session_agent
    in_restore = threading.Event()
    go = threading.Event()

    def slow_restore(sid, saved):
        if not in_restore.is_set():
            in_restore.set()
            go.wait(10)
        real_restore(sid, saved)

    async def deadline_hits(step, timeout):
        # The settle's deadline, reached while the restore is in its worker.
        task = asyncio.ensure_future(step)
        await async_wait_until(in_restore.is_set, timeout=FALLBACK_TIMEOUT_SECS)
        task.cancel()
        go.set()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        raise TimeoutError

    class _DeadlineAsyncio:
        def __getattr__(self, name):
            return deadline_hits if name == "wait_for" else getattr(asyncio, name)

    monkeypatch.setattr(v3, "restore_session_agent", slow_restore)
    monkeypatch.setattr(kiro_cli_handoff, "asyncio", _DeadlineAsyncio())
    with (wired.log).open("a") as fh:
        fh.write(_turn("Prompt", "asked in kiro-cli") + "\n")
    await kiro_cli_handoff.settle_and_release(KEY, "t")
    assert not kiro_cli_handoff.is_held(KEY)
    assert len(list(wired.records.iterdir())) == 1, "kept: the turns were not imported"
    assert _session(wired)["agentMode"] == _SAVED_AGENT


@pytest.mark.asyncio
async def test_an_open_whose_terminal_never_starts_puts_the_envelope_back(wired, switch):
    launch, code = await _open(wired)
    assert code == ""
    launch.release()
    assert _session(wired)["agentMode"] == _SAVED_AGENT
    assert not kiro_cli_handoff.is_held(KEY)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["missing", "directory", "not json"])
async def test_a_session_file_the_open_cannot_rewrite_refuses_it(wired, switch, damage):
    path = wired.v3 / "session.json"
    path.unlink()
    if damage == "directory":
        path.mkdir()
    elif damage == "not json":
        path.write_text("{")
    launch, code = await _open(wired)
    assert (launch, code) == (None, kcl.CODE_SESSION_FILE)
    assert code in kcl.KIRO_CLI_REFUSAL_CODES
    assert not kiro_cli_handoff.is_held(KEY)
    assert [p.name for p in wired.agents.iterdir()] == ["myagent.json"], "no launch copy left"
    assert not wired.records.exists() or list(wired.records.iterdir()) == []


@pytest.mark.asyncio
async def test_after_a_restart_the_chats_next_turn_puts_the_session_back(wired, switch):
    launch, code = await _open(wired)
    assert code == ""
    launch.announce()
    kiro_cli_handoff.reset_holds()  # the gateway stopped: no release ran
    slot = _slot(wired.project)
    assert await kcl.recover_left_open(_state(slot), slot, KEY)
    assert _session(wired)["agentMode"] == _SAVED_AGENT
    assert [p.name for p in wired.agents.iterdir()] == ["myagent.json"]
    assert list(wired.records.iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["", "v2", None])
async def test_a_record_naming_no_kiro_agent_session_is_dropped_not_kept(wired, switch, engine):
    """Only a Kiro Agent chat opens, so a record naming any other store is no
    session to read or put back: kept, it would refuse the chat for good. It
    goes, with the chat told, and the launch copy it names is removed."""
    spec = wired.agents / f"{kcl._LAUNCH_SPEC_PREFIX}0123456789abcdef.json"
    spec.write_text("{}")
    record = {"history_key": KEY, "created_at": "", "log_offset": 0, "sid": SID}
    record["launch_spec"] = str(spec)
    if engine is not None:
        record["engine"] = engine
    kiro_cli_handoff.record_open(KEY, record)
    slot = _slot(wired.project)
    assert await kcl.recover_left_open(_state(slot), slot, KEY)
    assert list(wired.records.iterdir()) == []
    assert not spec.exists()
    assert slot.appended[-1][1] == kcl._UNREADABLE_NOTICE, "said, not read as turns"
    assert _session(wired)["agentMode"] == _SAVED_AGENT, "the v3 session is left alone"
    kiro_cli_handoff.refuse_if_held(KEY)  # the session allocates again


async def _loop_turns(n: int) -> None:
    """Let the event loop run *n* times round, so a delivered cancel is acted on."""
    loop = asyncio.get_running_loop()
    for _ in range(n):
        turn = loop.create_future()
        loop.call_soon(turn.set_result, None)
        await turn


@pytest.mark.asyncio
async def test_a_cancelled_recovery_holds_its_lock_until_its_envelope_restore_finishes(
    wired, switch, monkeypatch
):
    """A recovery cancelled (the chat closed) while its worker restores the
    envelope must not free the session for a new open: that open's envelope
    would then be overwritten by the old worker, original grants and all."""
    from kiro_crew.messaging.link import canonical_key

    launch, code = await _open(wired)
    assert code == ""
    launch.announce()
    kiro_cli_handoff.reset_holds()  # the gateway stopped: the record is left
    real_restore = v3.restore_session_agent
    in_worker = threading.Event()
    go = threading.Event()

    def slow_restore(sid, saved):
        if not in_worker.is_set():
            in_worker.set()
            go.wait(10)
        real_restore(sid, saved)

    monkeypatch.setattr(v3, "restore_session_agent", slow_restore)
    slot = _slot(wired.project)
    task = asyncio.ensure_future(kcl.recover_left_open(_state(slot), slot, KEY))
    await async_wait_until(in_worker.is_set, timeout=FALLBACK_TIMEOUT_SECS)
    task.cancel()
    await _loop_turns(5)
    lock = kcl._recovery_locks[canonical_key(KEY)]
    assert lock.locked() and not task.done(), "held while the worker still writes"
    go.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not lock.locked()
    assert _session(wired)["agentMode"] == _SAVED_AGENT


@pytest.mark.asyncio
async def test_a_session_file_that_cannot_be_put_back_keeps_the_record_for_the_next_turn(
    wired, switch, monkeypatch
):
    launch, code = await _open(wired)
    assert code == ""
    launch.announce()
    real = v3._write_session

    def read_only(*_a, **_k):
        raise OSError("read-only")

    monkeypatch.setattr(v3, "_write_session", read_only)
    await kiro_cli_handoff.settle_and_release(KEY, "t")
    assert _session(wired)["agentMode"] != _SAVED_AGENT
    assert len(list(wired.records.iterdir())) == 1, "kept: session.json still names the copy"
    slot = _slot(wired.project)
    assert await kcl.recover_left_open(_state(slot), slot, KEY)
    assert len(list(wired.records.iterdir())) == 1, "still failing: kept again"
    monkeypatch.setattr(v3, "_write_session", real)
    assert await kcl.recover_left_open(_state(slot), slot, KEY)
    assert _session(wired)["agentMode"] == _SAVED_AGENT
    assert list(wired.records.iterdir()) == []


def test_open_records_live_where_sandboxed_agents_can_only_read():
    """A record names a launch copy the gateway removes and the envelope state
    it writes back, so a sandboxed agent that could plant one (at its chat's
    predictable key) could steer those writes. Records sit in the data home's
    ``run`` subtree, which the sandbox mounts read-only."""
    import os

    from kiro_crew.sandbox_workspace import _voice_runtime_parent_paths

    parent = kiro_cli_handoff._records_dir().parent
    sealed = {os.path.normpath(p) for p in _voice_runtime_parent_paths()}
    assert {os.path.normpath(str(parent)), os.path.realpath(parent)} & sealed


def test_a_crewmate_pinned_to_a_harness_this_gateway_cannot_start_does_not_open(monkeypatch):
    """The backend read passes the crewmate's harness pin like every other
    reader of the selection gate; an unstartable pin means no kiro-cli session."""
    from kiro_crew import crewmate_harness

    def unstartable(cfg, key):
        raise crewmate_harness.CrewmateHarnessUnavailable("mate", "codex")

    monkeypatch.setattr(crewmate_harness, "crewmate_backend_pin", unstartable)
    assert kcl._configured_backend_is_kas(KEY) is False


def _plant_non_regular(path) -> None:
    """A FIFO with no writer where the platform has them, else a directory: a
    session file that is not a regular file."""
    if hasattr(os, "mkfifo"):
        os.mkfifo(path)
    else:
        path.mkdir()


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["envelope", "log"])
async def test_a_fifo_planted_as_a_session_file_is_refused_without_blocking(wired, switch, which):
    """A FIFO with no writer at the envelope's (or log's) path would block the
    open, while the hold is taken, until a restart. It is refused instead."""
    if which == "envelope":
        path = wired.v3 / "session.json"
        path.unlink()
        _plant_non_regular(path)
        # No bound of its own: a blocking open hangs here until the suite's
        # per-test timeout, which is the failure this test exists to show.
        launch, code = await _open(wired)
        assert (launch, code) == (None, kcl.CODE_SESSION_FILE)
        assert not kiro_cli_handoff.is_held(KEY)
        return
    path = wired.log
    path.unlink(missing_ok=True)
    _plant_non_regular(path)
    with pytest.raises(OSError):
        await asyncio.to_thread(v3.read_turns, SID, 0)


def test_a_session_file_opens_where_the_platform_has_no_nonblocking_open(wired, monkeypatch):
    """Windows has no ``O_NONBLOCK`` and refuses ``set_blocking`` on a file:
    ``session.json`` still opens there, so the open is not refused on every
    Windows gateway."""

    def refused(fd, flag):
        raise OSError(87, "The parameter is incorrect")

    class _WindowsOs:
        # The module's view of ``os`` on Windows: no O_NONBLOCK, and a
        # set_blocking that refuses a file descriptor.
        set_blocking = staticmethod(refused)

        def __getattr__(self, name):
            if name == "O_NONBLOCK":
                raise AttributeError(name)
            return getattr(os, name)

    monkeypatch.setattr(kcl, "os", _WindowsOs())
    assert v3.read_session_agent(SID) == {"agentMode": _SAVED_AGENT}


def test_every_imported_row_is_sanitized_whatever_role_it_claims():
    """kiro-cli's log is writable by any sandboxed kiro-cli, so a ``Prompt`` in
    it is not proof the owner typed it, and a user row is stored and shown as
    written: every imported row goes through the delivery sanitizer."""
    key_like = "AKIA" + "Z7QX3MPL4RBN2WKD"
    rows = [
        ("user", f"planted {key_like}", "msg msg-u"),
        ("assistant", f"reply {key_like}", "msg msg-a"),
    ]
    entries = kcl._import_entries(rows, "imp")
    assert [e[0] for e in entries] == ["user", "assistant"]
    assert all(key_like not in e[1] for e in entries)
    assert entries[0][1].startswith("planted ")


class _LazyChat:
    """A chat restored lazily after a restart: its rows are read from disk on
    first use. Reading them on the event loop thread is the stall to avoid, so
    that read fails here; the off-loop load (``load_window_off_loop``) reads
    them on a worker and applies them."""

    def __init__(self, base):
        self._base = base
        self.window_pending = True
        self.read_on = None
        self._lazy_window = SimpleNamespace(read=self._read)

    def _read(self, slot):
        self.read_on = threading.current_thread()
        return ()

    def load_window(self, *prefetched):
        self.window_pending = False

    @property
    def messages(self):
        if self.window_pending:
            raise AssertionError("the transcript was read on the event loop")
        return self._base.messages

    def append(self, *args, **kwargs):
        # A real slot's append reads its rows first.
        if self.window_pending:
            raise AssertionError("the transcript was read on the event loop")
        return self._base.append(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._base, name)


@pytest.mark.asyncio
async def test_a_lazily_restored_chat_is_read_off_the_loop_before_an_import(wired):
    base = _slot(wired.project)
    lazy = _LazyChat(base)
    rows = [("user", "q", "msg msg-u"), ("assistant", "a", "msg msg-a")]
    assert await kcl._import_turns(_state(base), lazy, KEY, "", rows, "imp")
    assert lazy.read_on is not None and lazy.read_on is not threading.main_thread()
    assert [r for r, _t, _m in base.appended] == ["user", "assistant"]


@pytest.mark.asyncio
async def test_a_notice_to_a_lazily_restored_chat_waits_for_its_off_loop_read(wired, switch):
    base = _slot(wired.project, _disk_meta_created_at="2026-11-02T00:00:00Z")
    state = _state(base)
    state._background_tasks = set()
    launch, code = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    assert code == ""
    lazy = _LazyChat(base)  # closed and reopened from disk meanwhile
    state._slots["main"] = lazy
    launch.announce()
    await async_wait_until(lambda: base.appended, timeout=FALLBACK_TIMEOUT_SECS)
    assert lazy.read_on is not threading.main_thread()
    assert "Opened in kiro-cli" in base.appended[-1][1]
    launch.release()


@pytest.mark.asyncio
async def test_a_left_open_whose_log_can_never_be_read_frees_the_chat(wired, switch):
    """A log replaced by something that is not a file can never be read, so a
    retry would keep every allocation of the session refused for good: the chat
    is told, and the record goes."""
    launch, code = await _open(wired)
    assert code == ""
    launch.announce()
    kiro_cli_handoff.reset_holds()  # the gateway stopped: the record is left
    log = wired.log
    log.unlink()
    log.mkdir()
    slot = _slot(wired.project)
    assert await kcl.recover_left_open(_state(slot), slot, KEY)
    assert list(wired.records.iterdir()) == []
    assert "could not be added" in slot.appended[-1][1]
    kiro_cli_handoff.refuse_if_held(KEY)  # the session allocates again
    assert _session(wired)["agentMode"] == _SAVED_AGENT


@pytest.mark.asyncio
async def test_a_release_whose_log_became_unreadable_frees_the_chat(wired, switch):
    launch, code = await _open(wired)
    assert code == ""
    launch.announce()
    log = wired.log
    log.unlink()
    log.mkdir()
    assert await kiro_cli_handoff.settle_and_release(KEY, "t")
    assert list(wired.records.iterdir()) == []
    kiro_cli_handoff.refuse_if_held(KEY)


@pytest.mark.asyncio
async def test_a_transient_read_error_still_keeps_the_record_to_retry(wired, switch, monkeypatch):
    launch, code = await _open(wired)
    assert code == ""
    launch.announce()
    kiro_cli_handoff.reset_holds()

    def eio(*_a, **_k):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(v3, "read_turns", eio)
    slot = _slot(wired.project)
    assert await kcl.recover_left_open(_state(slot), slot, KEY)
    assert len(list(wired.records.iterdir())) == 1


@pytest.mark.asyncio
async def test_a_late_cleanup_of_an_earlier_open_leaves_a_newer_opens_state_alone(wired, switch):
    """A terminal that failed to start is torn down (the hold released through
    ``on_released``) before its handler's own cleanup calls ``release``. A
    newer open of the same chat in between writes its own record and points the
    envelope at its own copy: the late cleanup must touch neither."""
    first, code = await _open(wired, "t1")
    assert code == ""
    assert await kiro_cli_handoff.settle_and_release(KEY, "t1")  # the teardown
    second, code = await _open(wired, "t2")
    assert code == "" and kiro_cli_handoff.holder_of(KEY) == "t2"
    pointed = _session(wired)["agentMode"]
    first.release()  # the failed handler's own cleanup, late
    [record] = [json.loads(p.read_text()) for p in wired.records.iterdir()]
    assert record["import_id"] == second.import_id
    assert _session(wired)["agentMode"] == pointed
    assert pathlib.Path(second.launch_spec).exists()
    second.release()
    assert list(wired.records.iterdir()) == []


def _record_file_work_threads(monkeypatch) -> list:
    """Each thread the open's file cleanup runs on: the envelope restore, the
    launch-copy removal and the record removal."""
    threads: list = []
    for name, owner in (
        ("restore_session_agent", v3),
        ("_remove_launch_spec", kcl),
        ("forget_open", kiro_cli_handoff),
    ):
        real = getattr(owner, name)

        def spy(*a, _real=real, **k):
            threads.append(threading.current_thread())
            return _real(*a, **k)

        monkeypatch.setattr(owner, name, spy)
    return threads


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["never started", "teardown"])
async def test_a_release_puts_the_files_back_off_the_event_loop(wired, switch, monkeypatch, how):
    """The release reads and rewrites the envelope and removes files: none of
    that may run on the gateway's loop, whether the terminal never started
    (``arelease``) or exited (the teardown's ``settle_and_release``)."""
    launch, code = await _open(wired)
    assert code == ""
    if how == "teardown":
        launch.announce()
    threads = _record_file_work_threads(monkeypatch)
    if how == "never started":
        await launch.arelease()
    else:
        assert await kiro_cli_handoff.settle_and_release(KEY, "t")
    assert threads and threading.current_thread() not in threads
    assert not kiro_cli_handoff.is_held(KEY)
    assert _session(wired)["agentMode"] == _SAVED_AGENT
    assert list(wired.records.iterdir()) == []
    assert not pathlib.Path(launch.launch_spec).exists()


def test_forgetting_an_open_by_its_id_leaves_another_opens_record(wired):
    kiro_cli_handoff.record_open(KEY, {"import_id": "newer", "sid": SID})
    kiro_cli_handoff.forget_open(KEY, import_id="earlier")
    assert len(list(wired.records.iterdir())) == 1
    kiro_cli_handoff.forget_open(KEY, import_id="newer")
    assert list(wired.records.iterdir()) == []


# ── nothing taken in while held that cannot run ─────────────────────────────


@pytest.mark.asyncio
async def test_a_responsive_send_to_a_held_chat_is_refused_before_it_is_accepted(
    tmp_path, monkeypatch
):
    """A crewmate composer sends ``responsive: true``. That path accepts and
    queues the message; while held its turn would be refused after the user row
    was written, losing the message. The send is refused before that branch."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_utils, responsive_chat
    from kiro_crew.dashboard.chat import api_chat
    from kiro_crew.dashboard.state import _ChatSlot

    accepted = []
    monkeypatch.setattr(responsive_chat, "eligible", lambda *a, **k: True)

    async def accept(*a, **k):
        accepted.append(a)
        return {}

    monkeypatch.setattr(responsive_chat, "accept", accept)
    state = _make_state(tmp_path)
    slot = _ChatSlot("chat-held")
    state._slots[slot.key] = slot
    kiro_cli_handoff.hold(chat_utils.effective_session_key(slot), "t")

    @web.middleware
    async def _auth(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/chat/send", api_chat)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post(
            "/api/chat/send", json={"slot": slot.key, "message": "hi", "responsive": True}
        )
        assert resp.status == 409
        assert (await resp.json())["code"] == "kiro_cli_open"
    assert accepted == [] and slot.messages == [] and not slot._queue


@pytest.mark.asyncio
async def test_the_queue_drain_leaves_a_held_chats_entries_in_place(tmp_path):
    """Something queued while held (a monitor wake, a delivery) would be popped,
    its row written, then refused at admission: the drain leaves it instead."""
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_utils
    from kiro_crew.dashboard.chat_turn import queue_drain
    from kiro_crew.dashboard.state import _ChatSlot

    state = _make_state(tmp_path)
    slot = _ChatSlot("chat-held")
    state._slots[slot.key] = slot
    slot._queue.append({"message": "wake up", "id": "q1"})
    kiro_cli_handoff.hold(chat_utils.effective_session_key(slot), "t")
    assert await queue_drain._start_next_queued_turn(state, slot) is False
    assert [e["id"] for e in slot._queue] == ["q1"] and slot.messages == []


@pytest.mark.asyncio
async def test_the_hold_s_release_starts_what_was_queued_meanwhile(wired, switch, monkeypatch):
    from kiro_crew.dashboard import chat_runner

    drained = []

    async def drain(state_, keys):
        drained.append((keys, kiro_cli_handoff.is_held(KEY)))

    monkeypatch.setattr(chat_runner, "_drain_parked_queues", drain)
    slot = _slot(wired.project, _queue=[])
    (wired.log).touch()
    launch, code = await kcl.prepare_kiro_cli_launch(_request(_state(slot)), "main", "t")
    assert code == ""
    slot._queue.append({"message": "wake up"})
    launch.release()
    await async_wait_until(lambda: drained, timeout=FALLBACK_TIMEOUT_SECS)
    assert drained == [(["main"], False)], "drained once the hold is gone"


@pytest.mark.asyncio
async def test_an_unreadable_log_size_refuses_the_open_instead_of_offset_zero(
    wired, switch, monkeypatch
):
    """Offset 0 for a log that does exist would bring the whole earlier
    conversation in again as new turns at release: the open is refused."""

    def eio(sid):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(v3, "log_size", eio)
    launch, code = await _open(wired)
    assert (launch, code) == (None, kcl.CODE_SESSION_FILE)
    assert not kiro_cli_handoff.is_held(KEY)
    assert _session(wired)["agentMode"] == _SAVED_AGENT
    assert not wired.records.exists() or list(wired.records.iterdir()) == []


@pytest.mark.asyncio
async def test_a_hold_taken_during_a_sends_admission_refuses_it_before_the_row(
    tmp_path, monkeypatch
):
    """The send's early hold check can be overtaken by an open that completes
    during one of its later awaits (the replay lookup, admission). The send is
    then refused in the step that records its row, not accepted and dropped."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_state

    from kiro_crew.dashboard import chat_utils
    from kiro_crew.dashboard.chat import api_chat
    from kiro_crew.dashboard.state import _ChatSlot
    from kiro_crew.shared_crew import admission_entry

    state = _make_state(tmp_path)
    slot = _ChatSlot("chat-raced")
    state._slots[slot.key] = slot
    key = chat_utils.effective_session_key(slot)
    real_admit = admission_entry.admit_chat_turn
    abandoned = []

    async def admit_then_open(*args, **kwargs):
        admission = await real_admit(*args, **kwargs)
        kiro_cli_handoff.hold(key, "t")  # the open completed meanwhile
        real_abandon = admission.abandon

        async def abandon():
            abandoned.append(True)
            return await real_abandon()

        admission.abandon = abandon
        return admission

    monkeypatch.setattr(admission_entry, "admit_chat_turn", admit_then_open)

    @web.middleware
    async def _auth(request, handler):
        request["app"] = ""
        request["user"] = "local-app"
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/chat/send", api_chat)
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/api/chat/send", json={"slot": slot.key, "message": "hi"})
        assert resp.status == 409
        assert (await resp.json())["code"] == "kiro_cli_open"
    assert slot.messages == [] and slot.task is None
    assert abandoned == [True], "the admission the send took is given back"


# ── the chat's own start racing the open ────────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_that_slipped_in_declines_the_reset_and_gives_the_hold_back(
    wired, switch, monkeypatch
):
    provider = object()
    slot = _slot(wired.project)
    state = _state(slot)
    # No provider at the busy read; one registered by the time the reset runs.
    state.sessions.get_provider = MagicMock(side_effect=[None, provider, provider])

    async def declined(*_a, **_k):
        slot.running = True  # the send that slipped in dispatched its turn
        return False

    monkeypatch.setattr(chat_handlers, "_reset_slot_session", declined)
    assert await kcl.prepare_kiro_cli_launch(_request(state), "main", "t") == (None, kcl.CODE_BUSY)
    assert not kiro_cli_handoff.is_held(KEY)


@pytest.mark.asyncio
async def test_a_pre_spawn_slower_to_unwind_than_five_seconds_still_lets_an_idle_chat_open(
    wired, switch, monkeypatch
):
    """A pre-spawn cancelled mid-spawn kills its process before it ends, which
    can take over 5 s. An open that stops waiting sooner finds the process it
    registered and calls the idle chat busy ("turn running")."""
    # The open's waits at 1/100 scale, so 6 s of unwinding takes 60 ms here.
    scale = 0.01
    real_wait = asyncio.wait

    async def scaled_wait(aws, *, timeout=None, **kw):
        scaled = None if timeout is None else timeout * scale
        return await real_wait(aws, timeout=scaled, **kw)  # flake-ok: the open's own wait, scaled

    class _ScaledAsyncio:
        def __getattr__(self, name):
            return scaled_wait if name == "wait" else getattr(asyncio, name)

    monkeypatch.setattr(kcl, "asyncio", _ScaledAsyncio())
    provider = object()
    state = _state(_slot(wired.project))
    registered: list[object] = []
    state.sessions.get_provider = MagicMock(
        side_effect=lambda key: registered[-1] if registered else None
    )
    started = asyncio.Event()

    async def pre_spawn():
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # It unwinds for 6 s at the scale above, then its process registers.
            await asyncio.sleep(6.0 * scale)
            registered.append(provider)
            raise

    async def reset(state_, slot, key, *, skip_if_busy=False):
        switch.reset.append((key, skip_if_busy))
        registered.clear()  # the reset tears down what is registered now
        await asyncio.sleep(2.0 * scale)  # and its teardown takes a moment
        return True

    monkeypatch.setattr(chat_handlers, "_reset_slot_session", reset)
    task = asyncio.ensure_future(pre_spawn())
    await started.wait()
    state._slots["main"]._eager_spawn_task = task
    launch, code = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    assert code == "" and launch is not None
    assert task.done() and switch.reset == [(KEY, True)]
    launch.release()


@pytest.mark.asyncio
async def test_a_pre_spawn_that_will_not_stop_refuses_as_starting_not_as_a_running_turn(
    wired, switch, monkeypatch
):
    monkeypatch.setattr(kcl, "_EAGER_SPAWN_CANCEL_WAIT_S", 0)
    started = asyncio.Event()
    stop = asyncio.Event()

    async def pre_spawn():
        started.set()
        while not stop.is_set():
            try:
                await stop.wait()
            except asyncio.CancelledError:
                continue  # still killing the process it started

    task = asyncio.ensure_future(pre_spawn())
    await started.wait()
    state = _state(_slot(wired.project, _eager_spawn_task=task))
    try:
        assert await kcl.prepare_kiro_cli_launch(_request(state), "main", "t") == (
            None,
            kcl.CODE_STARTING,
        )
    finally:
        stop.set()
        await task
    assert not kiro_cli_handoff.is_held(KEY)
    assert kcl.CODE_STARTING in kcl.KIRO_CLI_REFUSAL_CODES


@pytest.mark.asyncio
async def test_a_process_registered_with_no_turn_running_refuses_as_starting(wired, switch):
    state = _state(_slot(wired.project))
    state.sessions.get_provider = MagicMock(side_effect=[None, None, object()])
    assert await kcl.prepare_kiro_cli_launch(_request(state), "main", "t") == (
        None,
        kcl.CODE_STARTING,
    )
    assert not kiro_cli_handoff.is_held(KEY)


@pytest.mark.asyncio
async def test_a_pending_pre_spawn_is_cancelled_before_the_reset(wired, switch):
    started = asyncio.Event()

    async def pre_spawn():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.ensure_future(pre_spawn())
    await started.wait()
    state = _state(_slot(wired.project, _eager_spawn_task=task))
    launch, _ = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    assert launch is not None
    assert task.cancelled()


def _rotating_log(tmp_path, monkeypatch):
    """A real transcript store whose rotation keeps only the newest 2 rows."""
    from kiro_crew import history
    from kiro_crew.history import ConversationLog

    monkeypatch.setattr(history, "_SESSION_MAX_BYTES", 600)
    monkeypatch.setattr(history, "_SESSION_KEEP_LINES", 2)
    log = ConversationLog(base_dir=tmp_path / "history")
    log.append(KEY, "user", "earlier")
    return log


def _import_rows(count):
    return [("user", f"q{n} " + "x" * 120, "msg msg-u") for n in range(count)]


def _tagged_rows(log):
    rows = log.read_rotated_messages_chained(KEY) + log.read_messages(KEY)
    return [r for r in rows if kcl._IMPORT_META_KEY in (r.get("meta") or {})]


def test_a_retried_archive_import_after_a_rotation_writes_each_row_once(tmp_path, monkeypatch):
    """Rows that rotation moved into the archive still count as written, so a
    retry after a crash does not add them again."""
    log = _rotating_log(tmp_path, monkeypatch)
    rows = kcl._import_entries(_import_rows(6), "x1")
    assert kcl._append_to_archived_transcript(log, KEY, "", rows) == len(rows)
    assert log.read_rotated_messages_chained(KEY), "the import rotated the transcript"
    assert kcl._append_to_archived_transcript(log, KEY, "", rows) == 0
    assert len(_tagged_rows(log)) == len(rows)


def test_an_unreadable_archive_falls_back_to_the_current_file(tmp_path, monkeypatch):
    log = _rotating_log(tmp_path, monkeypatch)
    rows = kcl._import_entries(_import_rows(1), "x1")
    assert kcl._append_to_archived_transcript(log, KEY, "", rows) == 1

    def unreadable(_key):
        raise OSError("archive gone")

    monkeypatch.setattr(log, "read_rotated_messages_chained", unreadable)
    assert kcl._persisted_import_tags(log, KEY) == {"x1:0"}


@pytest.mark.asyncio
async def test_a_retried_import_into_an_open_chat_skips_rows_rotated_out_of_its_window(
    wired, tmp_path, monkeypatch
):
    """An open chat's window lacks rows an earlier attempt saved and rotation
    archived; they are found on disk and not appended again."""
    log = _rotating_log(tmp_path, monkeypatch)
    rows = _import_rows(6)
    assert kcl._append_to_archived_transcript(log, KEY, "", kcl._import_entries(rows, "x1")) == 6
    slot = _slot(wired.project)  # restored with an empty window
    state = _state(slot)
    state.conversation_log = log
    assert await kcl._import_turns(state, slot, KEY, "", rows, "x1")
    assert slot.appended == []


# ── a chat replaced while an import or notice awaits ────────────────────────


def _swap_during_tag_read(monkeypatch, state, replacement):
    """Make the import's on-disk tag read close the chat and open *replacement*
    under its name, as the slot APIs can while that read is off the loop."""
    real = kcl._persisted_import_tags

    def swapping(log, key):
        state._slots["main"] = replacement
        return real(log, key)

    monkeypatch.setattr(kcl, "_persisted_import_tags", swapping)


@pytest.mark.asyncio
async def test_a_chat_replaced_while_the_release_imports_gets_none_of_its_turns(
    wired, switch, monkeypatch
):
    slot = _slot(wired.project, _disk_meta_created_at="c1")
    state = _state(slot)
    log = wired.log
    log.write_text("")
    launch, _ = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    launch.announce()
    before = len(slot.appended)
    log.write_text(_turn("Prompt", "asked") + "\n" + _turn("AssistantMessage", "a") + "\n")
    newcomer = _slot(wired.project, _disk_meta_created_at="c2")
    _swap_during_tag_read(monkeypatch, state, newcomer)
    assert await kiro_cli_handoff.settle_and_release(KEY, "t")
    assert slot.appended[before:] == [], "nothing through the retired tab"
    assert newcomer.appended == [], "nothing into the new conversation under the name"


@pytest.mark.asyncio
async def test_a_chat_replaced_while_a_restart_import_reads_gets_none_of_its_turns(
    wired, monkeypatch
):
    (wired.log).write_text(_turn("Prompt", "q") + "\n")
    kiro_cli_handoff.record_open(
        KEY, {"history_key": KEY, "created_at": "c1", "log_offset": 0, "sid": SID}
    )
    slot = _slot(wired.project, _disk_meta_created_at="c1")
    state = _state(slot)
    newcomer = _slot(wired.project, _disk_meta_created_at="c2")
    _swap_during_tag_read(monkeypatch, state, newcomer)
    assert await kcl.recover_left_open(state, slot, KEY)
    assert slot.appended == [] and newcomer.appended == []


@pytest.mark.asyncio
async def test_a_notice_waits_for_its_read_and_skips_a_chat_replaced_meanwhile(wired, switch):
    base = _slot(wired.project, _disk_meta_created_at="2026-11-02T00:00:00Z")
    state = _state(base)
    state._background_tasks = set()
    launch, code = await kcl.prepare_kiro_cli_launch(_request(state), "main", "t")
    assert code == ""
    newcomer = _slot(wired.project, _disk_meta_created_at="2026-11-04T00:00:00Z")

    class _ReplacedWhileLoading(_LazyChat):
        def load_window(self, *prefetched):
            super().load_window(*prefetched)
            state._slots["main"] = newcomer

    state._slots["main"] = _ReplacedWhileLoading(base)
    launch.announce()
    await async_wait_until(lambda: not state._background_tasks, timeout=FALLBACK_TIMEOUT_SECS)
    assert base.appended == [] and newcomer.appended == []
    launch.release()
