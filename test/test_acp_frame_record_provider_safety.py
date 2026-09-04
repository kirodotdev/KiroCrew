"""PROVIDER SAFETY: the frame recorder can never take a live provider down.

Read this file as the answer to one question: "does turning this change on, or
having it misbehave, break any provider that is running right now?" Every test
below drives a REAL ``AcpClient._read_message`` for EVERY known backend id --
kiro-cli (``""``), ``kas``, ``claude``, ``codex`` -- and asserts that the frame
still comes back as a ``JsonRpcMessage`` no matter what state the recorder is
in. If one of these tests fails, a provider's reader loop would have raised or
hung on a frame, and every session on that provider would have died with it.

The states covered, each on every provider:

* recorder OFF (the default for every ordinary run and every CI run) --
  ``record_frame`` returns after one env lookup and the frame is untouched;
* recorder ON and healthy -- the frame is delivered to the caller unchanged and
  the recording is a side effect, never a dependency;
* recorder ON but the writer thread never started -- stands down, frame flows;
* recorder ON but the destination is unwritable -- stands down, frame flows;
* recorder ON but the queue is full (count AND byte bound) -- drops, frame flows;
* recorder ON but the writer thread is wedged on the filesystem -- the reader
  does not wait on it, frame flows;
* recorder already stood down -- zero work, frame flows;
* ``record_frame`` itself raising an unexpected exception -- the reader loop
  must NOT propagate it (defence in depth: the function is written never to
  raise, and this pins that a future bug there still cannot reach a provider);
* the backend id mapping to a corpus file name for every known backend, so a
  ``KeyError`` cannot surface at write time for a real provider.

Both reader paths are driven. ``AcpClient._read_message`` is the per-session
reader; ``AcpRuntime._reader_loop`` is the MULTIPLEXED reader that every session
on a provider shares, and it matters more: an exception escaping it lands in a
catch-all that ``_mark_dead``s the runtime, taking every session on that
provider down at once. So the runtime tests assert three things after each
fault: the frame reached its session queue, the runtime is not dead, and the
reader task is still running.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.skipif(os.name == "nt", reason="ACP frame recorder is Linux-only")

from kiro_crew.acp import _frame_record  # noqa: E402
from kiro_crew.acp.client import AcpClient, JsonRpcMessage  # noqa: E402
from kiro_crew.acp.runtime import AcpRuntime  # noqa: E402
from kiro_crew.acp.types import METHOD_SESSION_UPDATE  # noqa: E402
from kiro_crew.acp_backends import ACP_BACKENDS_KNOWN, POLICY_ID_BY_BACKEND  # noqa: E402

# Every backend id this build can construct a provider for. The empty string is
# kiro-cli, and it is deliberately in the list: a test that forgot it would pass
# while the default provider broke.
PROVIDERS = sorted(ACP_BACKENDS_KNOWN, key=lambda b: (b == "", b))
assert "" in PROVIDERS, "kiro-cli (backend id '') must be under test"
assert len(PROVIDERS) >= 4, PROVIDERS

WIRE_FRAME = {
    "jsonrpc": "2.0",
    "method": "session/update",
    "params": {"sessionId": "s-1", "update": {"sessionUpdate": "agent_message_chunk"}},
}


def _client_with_wire(backend: str, frame: dict) -> AcpClient:
    """An ``AcpClient`` for *backend* whose stdout holds exactly one JSON line.

    No subprocess: ``_process.stdout`` is a real ``asyncio.StreamReader`` so
    ``_read_message`` runs the genuine decode -> parse -> record -> return path.
    """
    client = AcpClient(acp_backend=backend)
    reader = asyncio.StreamReader()
    reader.feed_data((json.dumps(frame) + "\n").encode())
    reader.feed_eof()
    process = MagicMock()
    process.stdout = reader
    process.returncode = None
    client._process = process
    return client


async def _read_one(backend: str, frame: dict = WIRE_FRAME) -> JsonRpcMessage:
    """Push one frame through the provider's real reader path, bounded in time.

    The 5s bound is the test's own guard against a reader that blocks: the
    recorder must never make ``_read_message`` wait, so a timeout here IS the
    failure this module exists to catch.
    """
    client = _client_with_wire(backend, frame)
    msg = await asyncio.wait_for(client._read_message(timeout=5.0), timeout=5.0)
    assert msg is not None, f"{backend or 'kiro-cli'}: frame was swallowed"
    assert msg.method == frame["method"], msg
    assert msg.params == frame["params"], msg
    return msg


class _LiveRuntime:
    """An ``AcpRuntime`` for *backend* with a fake process and a live reader task.

    Same shape as ``test_acp_runtime._make_runtime``: stdout is a real
    ``asyncio.StreamReader``, so ``_reader_loop`` runs the genuine decode ->
    record -> route path. ``feed`` pushes a frame; ``expect`` waits for it on
    the session's queue and asserts the runtime survived.
    """

    def __init__(self, backend: str, session_id: str = "s-rt") -> None:
        self.rt = AcpRuntime(work_dir="/tmp", acp_backend=backend)
        self.reader = asyncio.StreamReader()
        proc = MagicMock()
        proc.stdout = self.reader
        proc.stdin = MagicMock()
        proc.stdin.write = MagicMock()
        proc.stdin.drain = AsyncMock()
        proc.returncode = None
        proc.pid = 4242
        self.rt._process = proc
        self.rt._pid = 4242
        self.rt._initialized = True
        self.session_id = session_id
        self.queue: asyncio.Queue = asyncio.Queue()
        self.rt._session_queues[session_id] = self.queue
        self.task: asyncio.Task | None = None

    async def __aenter__(self) -> "_LiveRuntime":
        self.task = asyncio.ensure_future(self.rt._reader_loop())
        await asyncio.sleep(0)
        return self

    async def __aexit__(self, *_exc) -> None:
        assert self.task is not None
        self.task.cancel()
        try:
            await self.task
        except (asyncio.CancelledError, Exception):
            pass

    def feed(self, frame: dict | None = None) -> None:
        frame = frame or dict(
            WIRE_FRAME, params={**WIRE_FRAME["params"], "sessionId": self.session_id}
        )
        self.reader.feed_data((json.dumps(frame) + "\n").encode())

    async def expect(self) -> JsonRpcMessage:
        """The frame reached its session; the runtime and its reader survived."""
        msg = await asyncio.wait_for(self.queue.get(), timeout=5.0)
        assert msg.method == METHOD_SESSION_UPDATE, msg
        assert not self.rt._dead, "runtime was marked dead by a recorder fault"
        assert self.task is not None and not self.task.done(), "reader loop exited"
        return msg


def _runtime_frame(session_id: str, **extra_params) -> dict:
    return dict(
        WIRE_FRAME, params={**WIRE_FRAME["params"], "sessionId": session_id, **extra_params}
    )


@pytest.fixture(autouse=True)
def _fresh_recorder(monkeypatch):
    _frame_record._reset_for_tests()
    monkeypatch.delenv(_frame_record.ENV_RECORD_FRAMES, raising=False)
    yield
    # Never leave a writer thread behind for the next test module.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_frame_record.stop_for_tests())
    finally:
        loop.close()
    _frame_record._reset_for_tests()


# ── 1. OFF: the default state of every real deployment ──────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_provider_reads_frames_normally_with_the_recorder_off(monkeypatch, backend):
    """PROVIDER SAFETY: with the env var unset (every ordinary run), each
    provider's reader returns the frame and the recorder does no work at all."""
    touched: list = []
    monkeypatch.setattr(_frame_record, "_enqueue", lambda *a: touched.append(a))
    monkeypatch.setattr(_frame_record, "write_frame", lambda *a: touched.append(a))
    await _read_one(backend)
    assert touched == [], f"{backend or 'kiro-cli'}: recorder did work while off"
    assert not _frame_record._stood_down


# ── 2. ON and healthy: recording is a side effect, never a dependency ────────


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_provider_reads_frames_normally_with_the_recorder_on(monkeypatch, tmp_path, backend):
    """PROVIDER SAFETY: with recording on and healthy, the provider gets the
    same frame back, and the recording lands as a side effect."""
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    assert _frame_record.start_recorder() is True
    await _read_one(backend)
    await _frame_record.flush_for_tests()
    out = tmp_path / f"{POLICY_ID_BY_BACKEND[backend]}.jsonl"
    assert out.exists(), f"{backend or 'kiro-cli'}: recording did not land"
    assert json.loads(out.read_text().splitlines()[-1])["method"] == "session/update"
    assert not _frame_record._stood_down


# ── 3. ON but broken, every way it can break ────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_provider_survives_a_recorder_whose_thread_never_started(
    monkeypatch, tmp_path, backend
):
    """PROVIDER SAFETY: env var set after import, ``start_recorder`` never
    called. The recorder stands down; the provider's frame still flows."""
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    assert _frame_record._writer is None
    await _read_one(backend)
    assert _frame_record._stood_down, "misconfiguration must stand the recorder down"
    assert list(tmp_path.iterdir()) == []
    # And every later frame on every provider still flows with zero work.
    for other in PROVIDERS:
        await _read_one(other)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_provider_survives_an_unwritable_destination(monkeypatch, tmp_path, backend):
    """PROVIDER SAFETY: destination is a regular FILE, so the directory pin
    fails on the writer thread. The provider never sees the error."""
    bad = tmp_path / "not-a-dir"
    bad.write_text("x", encoding="utf-8")
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(bad))
    assert _frame_record.start_recorder() is True
    await _read_one(backend)
    await _frame_record.flush_for_tests()
    assert _frame_record._stood_down
    assert bad.read_text(encoding="utf-8") == "x", "the bad destination was modified"
    await _read_one(backend)  # still flows after stand-down


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_provider_survives_a_full_queue_by_count(monkeypatch, tmp_path, backend):
    """PROVIDER SAFETY: the backlog count bound is hit. The frame is dropped and
    the recorder stands down; the provider's read returns normally."""
    monkeypatch.setattr(_frame_record, "QUEUE_LIMIT", 0)
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    assert _frame_record.start_recorder() is True
    await _read_one(backend)
    await _frame_record.flush_for_tests()
    assert _frame_record._stood_down
    await _read_one(backend)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_provider_survives_a_full_queue_by_bytes(monkeypatch, tmp_path, backend):
    """PROVIDER SAFETY: the backlog BYTE bound is hit by one big frame. Dropped,
    stood down, and the provider still gets its message."""
    monkeypatch.setattr(_frame_record, "QUEUE_BYTES_LIMIT", 1)
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    assert _frame_record.start_recorder() is True
    big = dict(WIRE_FRAME, params={"sessionId": "s-1", "blob": "x" * 4096})
    await _read_one(backend, big)
    await _frame_record.flush_for_tests()
    assert _frame_record._stood_down
    await _read_one(backend)


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_provider_never_waits_on_a_wedged_writer_thread(monkeypatch, tmp_path, backend):
    """PROVIDER SAFETY: the writer thread is stuck inside the filesystem call.
    The provider's read must return immediately -- a reader that waited here
    would stall every multiplexed session on that provider."""
    release = threading.Event()

    def wedged_write(*_a, **_k):
        release.wait(30)

    monkeypatch.setattr(_frame_record, "write_frame", wedged_write)
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    assert _frame_record.start_recorder() is True
    try:
        # Wedge the writer with a first frame, then time the next reads.
        await _read_one(backend)
        for _ in range(20):
            await asyncio.sleep(0.01)
            if _frame_record._writer.in_flight:
                break
        started = time.monotonic()
        for _ in range(50):
            await _read_one(backend)
        elapsed = time.monotonic() - started
        assert (
            elapsed < 2.0
        ), f"{backend or 'kiro-cli'}: reader waited on the writer ({elapsed:.2f}s)"
        assert not _frame_record._stood_down
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_provider_is_untouched_once_the_recorder_has_stood_down(
    monkeypatch, tmp_path, backend
):
    """PROVIDER SAFETY: after a stand-down, recording costs one flag read and
    the provider never reaches the enqueue path again."""
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    assert _frame_record.start_recorder() is True
    _frame_record._stand_down(RuntimeError("simulated earlier fault"))
    touched: list = []
    monkeypatch.setattr(_frame_record, "_enqueue", lambda *a: touched.append(a))
    await _read_one(backend)
    assert touched == []
    assert _frame_record.recording_destination() == ""


# ── 4. Defence in depth: even a BUG in record_frame cannot reach a provider ──


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_provider_reader_does_not_propagate_an_unexpected_recorder_exception(
    monkeypatch, tmp_path, backend
):
    """PROVIDER SAFETY (defence in depth): ``record_frame`` is written to never
    raise. This pins the contract from the recorder's side -- an unexpected
    exception inside ``_enqueue`` is caught and turned into a stand-down, so
    the provider's frame still returns and nothing propagates into the loop."""
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    assert _frame_record.start_recorder() is True

    def boom(*_a, **_k):
        raise ZeroDivisionError("unexpected recorder bug")

    monkeypatch.setattr(_frame_record, "_enqueue", boom)
    await _read_one(backend)  # would raise ZeroDivisionError if not contained
    assert _frame_record._stood_down
    await _read_one(backend)


# ── 4b. The MULTIPLEXED runtime reader: one fault here would kill every session ──


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_runtime_routes_frames_normally_with_the_recorder_off(monkeypatch, backend):
    """PROVIDER SAFETY (runtime): env var unset, the shared reader loop routes
    the frame to its session and the recorder does no work."""
    touched: list = []
    monkeypatch.setattr(_frame_record, "_enqueue", lambda *a: touched.append(a))
    async with _LiveRuntime(backend) as live:
        live.feed()
        await live.expect()
    assert touched == []
    assert not _frame_record._stood_down


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_runtime_routes_frames_normally_with_the_recorder_on(monkeypatch, tmp_path, backend):
    """PROVIDER SAFETY (runtime): recording on and healthy; the frame is routed
    and the recording lands under the provider's corpus name."""
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    assert _frame_record.start_recorder() is True
    async with _LiveRuntime(backend) as live:
        live.feed()
        await live.expect()
    await _frame_record.flush_for_tests()
    out = tmp_path / f"{POLICY_ID_BY_BACKEND[backend]}.jsonl"
    assert out.exists(), f"{backend or 'kiro-cli'}: runtime recording did not land"
    assert not _frame_record._stood_down


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_runtime_survives_a_recorder_whose_thread_never_started(
    monkeypatch, tmp_path, backend
):
    """PROVIDER SAFETY (runtime): switch set, no writer started. Stand down;
    the multiplexed loop keeps routing."""
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    async with _LiveRuntime(backend) as live:
        live.feed()
        await live.expect()
        assert _frame_record._stood_down
        live.feed()
        await live.expect()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_runtime_survives_a_full_queue(monkeypatch, tmp_path, backend):
    """PROVIDER SAFETY (runtime): backlog full by count on the very first
    frame. Dropped, stood down, runtime alive, frame routed."""
    monkeypatch.setattr(_frame_record, "QUEUE_LIMIT", 0)
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    assert _frame_record.start_recorder() is True
    async with _LiveRuntime(backend) as live:
        live.feed()
        await live.expect()
        await _frame_record.flush_for_tests()
        assert _frame_record._stood_down
        live.feed()
        await live.expect()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_runtime_never_waits_on_a_wedged_writer_thread(monkeypatch, tmp_path, backend):
    """PROVIDER SAFETY (runtime): writer stuck in the filesystem call; the
    shared loop must keep routing frames for every session without waiting."""
    release = threading.Event()

    def wedged_write(*_a, **_k):
        release.wait(30)

    monkeypatch.setattr(_frame_record, "write_frame", wedged_write)
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    assert _frame_record.start_recorder() is True
    try:
        async with _LiveRuntime(backend) as live:
            live.feed()
            await live.expect()
            started = time.monotonic()
            for _ in range(50):
                live.feed()
                await live.expect()
            elapsed = time.monotonic() - started
            assert (
                elapsed < 2.0
            ), f"{backend or 'kiro-cli'}: runtime waited on the writer ({elapsed:.2f}s)"
        assert not _frame_record._stood_down
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_runtime_does_not_die_on_an_unexpected_recorder_exception(
    monkeypatch, tmp_path, backend
):
    """PROVIDER SAFETY (runtime, defence in depth): an exception escaping
    ``record_frame`` would reach ``_reader_loop``'s catch-all and ``_mark_dead``
    the runtime -- every session on this provider gone. The recorder contains
    it; this pins that the runtime stays alive and keeps routing."""
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    assert _frame_record.start_recorder() is True

    def boom(*_a, **_k):
        raise ZeroDivisionError("unexpected recorder bug")

    monkeypatch.setattr(_frame_record, "_enqueue", boom)
    async with _LiveRuntime(backend) as live:
        live.feed()
        await live.expect()
        assert _frame_record._stood_down
        live.feed()
        await live.expect()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
async def test_runtime_recorder_fault_does_not_leak_across_sessions(monkeypatch, tmp_path, backend):
    """PROVIDER SAFETY (runtime): the whole point of the multiplexed loop. A
    recorder fault triggered by session A's frame must not cost session B its
    next frame."""
    monkeypatch.setattr(_frame_record, "QUEUE_LIMIT", 0)
    monkeypatch.setenv(_frame_record.ENV_RECORD_FRAMES, str(tmp_path))
    assert _frame_record.start_recorder() is True
    async with _LiveRuntime(backend, "sA") as live:
        q_b: asyncio.Queue = asyncio.Queue()
        live.rt._session_queues["sB"] = q_b
        live.feed(_runtime_frame("sA"))
        await live.expect()
        assert _frame_record._stood_down
        live.feed(_runtime_frame("sB"))
        msg = await asyncio.wait_for(q_b.get(), timeout=5.0)
        assert msg.params["sessionId"] == "sB"
        assert not live.rt._dead


# ── 5. Every provider has a corpus file name ────────────────────────────────


@pytest.mark.parametrize("backend", PROVIDERS, ids=lambda b: b or "kiro-cli")
def test_every_known_provider_maps_to_a_corpus_file_name(backend):
    """PROVIDER SAFETY: ``fixture_dir_name`` must not KeyError for any backend a
    real provider can be built with. kiro-cli's id is ``""``, which is not a
    filename, so the mapping is the thing to pin."""
    name = _frame_record.fixture_dir_name(backend)
    assert name and "/" not in name and name.strip() == name, (backend, name)


def test_provider_set_under_test_matches_the_build():
    """If a new backend id is added, this module must grow with it. A backend
    that a provider can be constructed for but that is not under test here is a
    provider whose reader loop this file says nothing about."""
    assert set(PROVIDERS) == set(ACP_BACKENDS_KNOWN)
    assert set(PROVIDERS) <= set(POLICY_ID_BY_BACKEND)


# ── 6. Import-time: loading the module never raises, on or off ──────────────


def test_importing_the_recorder_with_the_switch_unset_starts_nothing(monkeypatch):
    """PROVIDER SAFETY: every provider imports ``_frame_record`` transitively
    through ``acp.client``. With the switch unset, import-time
    ``start_recorder()`` must be a no-op -- no thread, no stand-down."""
    monkeypatch.delenv(_frame_record.ENV_RECORD_FRAMES, raising=False)
    _frame_record._reset_for_tests()
    assert _frame_record.start_recorder() is False
    assert _frame_record._writer is None
    assert not _frame_record._stood_down
