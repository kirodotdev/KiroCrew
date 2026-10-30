"""pi's mid-turn steer: the sealed steer channel and ``AcpClient``'s half of it.

The extension's half runs under Node in ``test_pi_gate_extension_steer.py``; the live
evidence is ``fixtures/acp_frames/pi/steering-live.jsonl``, replayed here through the
client's real read loop.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import stat
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from kiro_crew.acp import pi_steer_turn
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import pi as pi_mod
from kiro_crew.acp.harness.pi_steer import (
    PiSteerChannel,
    new_pi_steer_channel,
    steer_echo_seq,
)
from kiro_crew.acp.types import (
    ACP_BACKEND_PI,
    ACP_BACKENDS_STEER,
    ACP_BACKENDS_STEERING_REQUEST,
    EVENT_COMPLETE,
    EVENT_STEER_CONSUMED,
    EVENT_TEXT_CHUNK,
    MAX_STEERING_ANSWERS,
    MAX_STEERING_TEXT_CHARS,
)
from kiro_crew.testing.wait import await_until, until_parked

_SID = "pi-session-1"
_NONCE = "0123456789abcdef0123456789abcdef"
_PROMPT_ID = 7
_FIXTURE = Path(__file__).parent / "fixtures" / "acp_frames" / "pi" / "steering-live.jsonl"


@pytest.fixture
def gate_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> str:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("KIROCREW_HOME", str(home))
    return pi_mod._pi_gate_artifact_dir()


def _client(tmp_path: Path, channel: PiSteerChannel | None) -> tuple[AcpClient, Any]:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.returncode = None
    client._process = proc
    client._session_id = _SID
    client._pi_steer_channel = channel
    if channel is not None:
        client._pi_gate_nonce = channel.nonce
    return client, reader


def _frame(obj: dict[str, Any]) -> bytes:
    return (json.dumps({"jsonrpc": "2.0", **obj}) + "\n").encode()


def _chunk(text: str, *, notify: bool) -> bytes:
    update: dict[str, Any] = {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": text},
    }
    if notify:
        update["_meta"] = {"piAcp": {"notify": {"level": "info"}}}
    return _frame({"method": "session/update", "params": {"sessionId": _SID, "update": update}})


def _echo(seq: int, nonce: str = _NONCE) -> str:
    return json.dumps({"kiro-crew-steer": 1, "nonce": nonce, "seq": seq}, separators=(",", ":"))


def _complete(stop: str = "end_turn") -> bytes:
    return _frame({"id": _PROMPT_ID, "result": {"stopReason": stop}})


def _open(path: str, key: bytes, nonce: str, session: str) -> dict[str, Any]:
    """Open a steer file the way the extension does (``readSteers``)."""
    record = json.loads(Path(path).read_text())
    seq = record["seq"]
    aad = "\0".join(("kiro-crew-steer", nonce, session, str(seq))).encode()
    sealed = base64.b64decode(record["sealed"])
    plain = AESGCM(key).decrypt(base64.b64decode(record["iv"]), sealed, aad)
    return json.loads(plain)


def _path(channel: PiSteerChannel, seq: int) -> str:
    """Where *channel* writes steer *seq*."""
    return f"{channel._stem()}{seq:010d}.json"


def _steer_files(gate: str) -> list[str]:
    return sorted(n for n in os.listdir(gate) if "_steer_" in n)


# ── the channel ──────────────────────────────────────────────────────────────


def test_a_steer_file_is_sealed_to_its_launch_session_and_sequence(gate_dir: str) -> None:
    channel = new_pi_steer_channel(_NONCE, gate_dir)
    assert channel.publish(_SID, "use blue") == 1
    assert channel.publish(_SID, "and green") == 2
    names = _steer_files(gate_dir)
    assert names == [
        f"kirocrew_pi_gate_{os.getpid()}_steer_{_NONCE}_{seq:010d}.json" for seq in (1, 2)
    ]
    first = os.path.join(gate_dir, names[0])
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(first).st_mode) == 0o400
    body = json.loads(Path(first).read_text())
    assert set(body) == {"kiro-crew-steer", "nonce", "seq", "iv", "sealed"}
    assert "use blue" not in Path(first).read_text(), "the text is sealed, not plain"
    assert _open(first, channel.key, _NONCE, _SID) == {
        "seq": 1,
        "session": _SID,
        "text": "use blue",
    }
    # Bound to the session and the key: either one differing does not open.
    from cryptography.exceptions import InvalidTag

    with pytest.raises(InvalidTag):
        _open(first, channel.key, _NONCE, "another-session")
    with pytest.raises(InvalidTag):
        _open(first, bytes(range(32, 64)), _NONCE, _SID)
    with pytest.raises(InvalidTag):
        _open(first, channel.key, "f" * 32, _SID)
    channel.discard()
    assert _steer_files(gate_dir) == []


def test_the_child_is_named_the_channel_and_given_its_key(gate_dir: str) -> None:
    channel = new_pi_steer_channel(_NONCE, gate_dir)
    env = channel.env()
    assert env["KIROCREW_PI_STEER"] == os.path.join(
        gate_dir, f"kirocrew_pi_gate_{os.getpid()}_steer_{_NONCE}_"
    )
    assert bytes.fromhex(env["KIROCREW_PI_STEER_KEY"]) == channel.key
    assert len(channel.key) == 32
    assert new_pi_steer_channel(_NONCE, gate_dir).key != channel.key


def test_a_moved_gate_directory_is_refused(gate_dir: str, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    channel = new_pi_steer_channel(_NONCE, str(elsewhere))
    with pytest.raises(OSError):
        channel.publish(_SID, "use blue")
    assert _steer_files(gate_dir) == [] and _steer_files(str(elsewhere)) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes and symlinks")
def test_a_steer_is_written_only_into_a_real_owner_only_gate_leaf(
    gate_dir: str, tmp_path: Path
) -> None:
    channel = new_pi_steer_channel(_NONCE, gate_dir)
    # Loosened between spawn and steer (the case under test): the write re-secures it.
    os.chmod(gate_dir, 0o710)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions  # noqa: E501  # fmt: skip
    channel.publish(_SID, "use blue")
    assert stat.S_IMODE(os.stat(gate_dir).st_mode) == 0o700
    channel.discard()
    # Swapped for a link to somewhere else: refused, nothing written there.
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    os.rename(gate_dir, str(tmp_path / "parked"))
    os.symlink(target, gate_dir)
    with pytest.raises(pi_mod.AcpToolGateUnroutable):
        channel.publish(_SID, "use blue")
    assert os.listdir(target) == []


@pytest.mark.parametrize(
    ("text", "seq"),
    [
        (_echo(3), 3),
        (_echo(3, nonce="f" * 32), None),
        (json.dumps({"kiro-crew-steer": 1, "nonce": _NONCE, "seq": 3, "x": 1}), None),
        (json.dumps({"kiro-crew-steer": 1, "nonce": _NONCE, "seq": True}), None),
        (json.dumps({"kiro-crew-steer": True, "nonce": _NONCE, "seq": 3}), None),
        (json.dumps({"kiro-crew-steer": 1, "nonce": _NONCE, "seq": 0}), None),
        (json.dumps({"kiro-crew-steer": 2, "nonce": _NONCE, "seq": 3}), None),
        ("Steer received: PINEAPPLE.", None),
        ("{not json", None),
    ],
)
def test_only_the_extensions_own_envelope_reads_as_consumed(text: str, seq: int | None) -> None:
    assert steer_echo_seq(text, _NONCE) == seq
    assert steer_echo_seq(text, "") is None


# ── what the client claims for pi ────────────────────────────────────────────


def test_pi_advertises_user_steer_only_once_its_channel_exists(tmp_path: Path) -> None:
    assert ACP_BACKEND_PI in ACP_BACKENDS_STEERING_REQUEST
    assert ACP_BACKEND_PI not in ACP_BACKENDS_STEER
    bare, _ = _client(tmp_path, None)
    assert bare.supports_steer is False
    client, _ = _client(tmp_path, new_pi_steer_channel(_NONCE, str(tmp_path)))
    assert client.supports_steer is True
    assert client.steer_needs_loss_recovery is True
    # Not a deny-notice channel: a pi denial is the blocked call's own error result.
    assert client.supports_refusal_steer is False


# ── the client's turn ────────────────────────────────────────────────────────


async def _turn(client: AcpClient, reader: Any, feed: bytes, steer: str | None):
    """Open a prompt turn, steer once it has begun, then feed *feed* and read to the end."""
    client._turn_done.clear()
    events: list[Any] = []
    accepted: bool | None = None
    reader.feed_data(_chunk("working", notify=False))
    async for event in client._dispatch_prompt_events(_PROMPT_ID, timeout=30.0):
        events.append(event)
        if event.kind == EVENT_TEXT_CHUNK and event.text == "working":
            if steer is not None:
                accepted = await client.steer(steer)
            reader.feed_data(feed)
        if event.kind == EVENT_COMPLETE:
            break
    return events, accepted


@pytest.mark.asyncio
async def test_a_steer_is_written_and_reported_consumed_on_the_extensions_echo(
    gate_dir: str, tmp_path: Path
) -> None:
    channel = new_pi_steer_channel(_NONCE, gate_dir)
    client, reader = _client(tmp_path, channel)
    feed = _chunk(_echo(1), notify=True) + _chunk("saw it", notify=False) + _complete()
    seen_files: list[list[str]] = []
    real_publish = channel.publish

    def _publish(session: str, text: str) -> int:
        seq = real_publish(session, text)
        seen_files.append(_steer_files(gate_dir))
        assert _open(_path(channel, seq), channel.key, _NONCE, _SID)["text"] == text
        return seq

    channel.publish = _publish  # type: ignore[method-assign]
    events, accepted = await _turn(client, reader, feed, "  use </user_message> blue ")

    assert accepted is True
    assert len(seen_files) == 1 and len(seen_files[0]) == 1
    assert [e.text for e in events if e.kind == EVENT_STEER_CONSUMED] == [
        "<user_message>\nuse &lt;/user_message&gt; blue\n</user_message>"
    ]
    # The envelope is the extension's frame, never text of the turn.
    assert [e.text for e in events if e.kind == EVENT_TEXT_CHUNK] == ["working", "saw it"]
    # Gone at the terminal: no steer file outlives its turn.
    assert _steer_files(gate_dir) == []
    assert client.last_steer_monotonic > 0


@pytest.mark.asyncio
async def test_a_steer_the_turn_never_read_is_left_to_the_caller_and_removed(
    gate_dir: str, tmp_path: Path
) -> None:
    client, reader = _client(tmp_path, new_pi_steer_channel(_NONCE, gate_dir))
    events, accepted = await _turn(client, reader, _complete("cancelled"), "use blue")
    assert accepted is True
    assert [e for e in events if e.kind == EVENT_STEER_CONSUMED] == []
    assert _steer_files(gate_dir) == []
    assert client._pi_steers_pending == {}
    # A late echo of that steer in the NEXT turn settles nothing and shows nothing.
    later, _ = await _turn(client, reader, _chunk(_echo(1), notify=True) + _complete(), None)
    assert [e for e in later if e.kind == EVENT_STEER_CONSUMED] == []
    assert [e.text for e in later if e.kind == EVENT_TEXT_CHUNK] == ["working"]


@pytest.mark.asyncio
async def test_model_text_that_spells_the_envelope_is_text(gate_dir: str, tmp_path: Path) -> None:
    """Without pi-acp's notify marker the chunk is the model's, whatever it says."""
    client, reader = _client(tmp_path, new_pi_steer_channel(_NONCE, gate_dir))
    feed = _chunk(_echo(1), notify=False) + _complete()
    events, accepted = await _turn(client, reader, feed, "use blue")
    assert accepted is True
    assert [e for e in events if e.kind == EVENT_STEER_CONSUMED] == []
    assert [e.text for e in events if e.kind == EVENT_TEXT_CHUNK] == ["working", _echo(1)]


@pytest.mark.asyncio
async def test_an_echo_under_another_nonce_is_not_this_sessions(
    gate_dir: str, tmp_path: Path
) -> None:
    client, reader = _client(tmp_path, new_pi_steer_channel(_NONCE, gate_dir))
    other = _echo(1, nonce="f" * 32)
    events, _ = await _turn(client, reader, _chunk(other, notify=True) + _complete(), "use blue")
    assert [e for e in events if e.kind == EVENT_STEER_CONSUMED] == []
    assert [e.text for e in events if e.kind == EVENT_TEXT_CHUNK] == ["working", other]


@pytest.mark.asyncio
async def test_no_steer_is_written_without_an_open_turn(gate_dir: str, tmp_path: Path) -> None:
    client, _ = _client(tmp_path, new_pi_steer_channel(_NONCE, gate_dir))
    assert await client.steer("use blue") is False
    pi_steer_turn.open_turn(client, _PROMPT_ID)
    client._turn_done.set()
    assert await client.steer("use blue") is False
    client._turn_done.clear()
    client._cancelled = True
    assert await client.steer("use blue") is False
    client._cancelled = False
    await pi_steer_turn.close_turn(client, _PROMPT_ID)
    assert await client.steer("use blue") is False
    assert _steer_files(gate_dir) == []


@pytest.mark.asyncio
async def test_a_steer_over_the_bounds_is_queued_instead(gate_dir: str, tmp_path: Path) -> None:
    client, _ = _client(tmp_path, new_pi_steer_channel(_NONCE, gate_dir))
    client._turn_done.clear()
    pi_steer_turn.open_turn(client, _PROMPT_ID)
    assert await client.steer("x" * (MAX_STEERING_TEXT_CHARS + 1)) is False
    for _ in range(MAX_STEERING_ANSWERS):
        assert await client.steer("use blue") is True
    assert await client.steer("one too many") is False
    await pi_steer_turn.close_turn(client, _PROMPT_ID)
    assert _steer_files(gate_dir) == []


def _gate_publish(channel: PiSteerChannel) -> tuple[asyncio.Event, threading.Event]:
    """Hold *channel*'s writes in their thread: ``entered`` is set once a write is
    inside ``publish``, and it lands only after ``release`` is set."""
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    real_publish = channel.publish

    def _held(session: str, text: str) -> int:
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(timeout=120), "the test never released the write"
        return real_publish(session, text)

    channel.publish = _held  # type: ignore[method-assign]
    return entered, release


async def _parked_behind(lock: asyncio.Lock, count: int) -> asyncio.Task[None]:
    """A task queued on *lock* behind every task already runnable; returns once
    *count* waiters are parked there, so every earlier ready step has run."""

    async def _probe() -> None:
        async with lock:
            pass

    probe = asyncio.ensure_future(_probe())
    await until_parked(lock, count=count)
    return probe


@pytest.mark.asyncio
async def test_a_write_in_flight_lands_before_the_close_removes_it(
    gate_dir: str, tmp_path: Path
) -> None:
    """The close waits for a write it races, so the file cannot appear after it."""
    channel = new_pi_steer_channel(_NONCE, gate_dir)
    client, _ = _client(tmp_path, channel)
    client._turn_done.clear()
    pi_steer_turn.open_turn(client, _PROMPT_ID)
    entered, release = _gate_publish(channel)
    steering = asyncio.ensure_future(client.steer("use blue"))
    await await_until(entered, what="the write entering publish")
    closing = asyncio.ensure_future(pi_steer_turn.close_turn(client, _PROMPT_ID))
    await until_parked(client._pi_steer_lock)
    assert not closing.done(), "the close must wait for the write it races"
    release.set()
    assert await await_until(steering, what="the steer call") is True
    await await_until(closing, what="the close")
    assert _steer_files(gate_dir) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("cancels", [1, 2])
async def test_a_cancelled_steer_waits_out_its_write_and_undoes_it(
    gate_dir: str, tmp_path: Path, cancels: int
) -> None:
    """A caller cancelled mid-write -- once, or again while it waits -- queues its
    text, so the file must not stay behind, and the lock is held until it is gone."""
    channel = new_pi_steer_channel(_NONCE, gate_dir)
    client, _ = _client(tmp_path, channel)
    client._turn_done.clear()
    pi_steer_turn.open_turn(client, _PROMPT_ID)
    entered, release = _gate_publish(channel)
    steering = asyncio.ensure_future(client.steer("use blue"))
    await await_until(entered, what="the write entering publish")
    probes = []
    for n in range(cancels):
        steering.cancel()
        # Queued behind the cancel: once it is parked, the cancel has been handled.
        probes.append(await _parked_behind(client._pi_steer_lock, n + 1))
    closing = asyncio.ensure_future(pi_steer_turn.close_turn(client, _PROMPT_ID))
    await until_parked(client._pi_steer_lock, count=cancels + 1)
    assert not steering.done(), "the cancelled call waits for its write"
    assert not closing.done(), "the lock is held until the write is undone"
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await await_until(steering, what="the cancelled steer call")
    await await_until(closing, what="the close")
    for probe in probes:
        await await_until(probe, what="the lock probe")
    assert _steer_files(gate_dir) == []
    assert client._pi_steers_pending == {}


@pytest.mark.asyncio
async def test_a_late_close_of_an_old_turn_leaves_the_open_turns_steers(
    gate_dir: str, tmp_path: Path
) -> None:
    """The ``finally`` of a turn can run after the next turn opened."""
    client, _ = _client(tmp_path, new_pi_steer_channel(_NONCE, gate_dir))
    client._turn_done.clear()
    pi_steer_turn.open_turn(client, _PROMPT_ID + 1)
    assert await client.steer("use blue") is True
    await pi_steer_turn.close_turn(client, _PROMPT_ID)
    assert len(_steer_files(gate_dir)) == 1
    assert client._pi_steers_pending and client._pi_steer_turn == _PROMPT_ID + 1
    await pi_steer_turn.close_turn(client, _PROMPT_ID + 1)
    assert _steer_files(gate_dir) == []


def test_discard_removes_every_file_of_the_launch_and_no_other(gate_dir: str) -> None:
    channel = new_pi_steer_channel(_NONCE, gate_dir)
    channel.publish(_SID, "use blue")
    stray = channel._stem() + "0000000009.json"
    Path(stray).write_text("{}")
    other = new_pi_steer_channel("f" * 32, gate_dir)
    theirs = other.publish(_SID, "theirs")
    channel.discard()
    assert _steer_files(gate_dir) == [os.path.basename(_path(other, theirs))]


def test_a_steer_at_the_text_bound_fits_the_size_the_extension_reads(gate_dir: str) -> None:
    channel = new_pi_steer_channel(_NONCE, gate_dir)
    # Astral characters: 4 bytes in UTF-8, 12 as a JSON escape.
    seq = channel.publish(_SID, "\U0001f34d" * MAX_STEERING_TEXT_CHARS)
    size = os.path.getsize(_path(channel, seq))
    assert size < 512 * 1024
    assert "MAX_STEER_FILE_BYTES = 512 * 1024" in Path(pi_mod.pi_gate_extension_path()).read_text()


@pytest.mark.asyncio
async def test_a_failed_write_is_queued_and_holds_nothing(gate_dir: str, tmp_path: Path) -> None:
    channel = new_pi_steer_channel(_NONCE, gate_dir)
    client, _ = _client(tmp_path, channel)
    client._turn_done.clear()
    pi_steer_turn.open_turn(client, _PROMPT_ID)

    def _fail(session: str, text: str) -> int:
        raise OSError("disk full")

    channel.publish = _fail  # type: ignore[method-assign]
    assert await client.steer("use blue") is False
    assert client._pi_steers_pending == {}


# ── the live capture, through the client's real read loop ────────────────────


@pytest.mark.asyncio
async def test_the_live_capture_reports_the_steer_consumed_and_shows_no_envelope(
    tmp_path: Path,
) -> None:
    lines = _FIXTURE.read_text().splitlines()
    meta = json.loads(lines[0])["_meta"]
    frames = [json.loads(line) for line in lines[1:]]
    nonce = meta["gate_envelope_nonce"]
    # The turn begins after the session/new and set-model answers (ids 1-3).
    start = next(i for i, f in enumerate(frames) if f.get("id") == 3) + 1
    turn = frames[start:]
    prompt_id = turn[-1]["id"]
    channel = PiSteerChannel(nonce=nonce, artifact_dir=str(tmp_path), key=bytes(range(32)))
    client, reader = _client(tmp_path, channel)
    client._session_id = "pi-session-1"
    client._turn_done.clear()
    pi_steer_turn.open_turn(client, prompt_id)
    # The steer this capture's client wrote: sequence 1 of its launch.
    client._pi_steers_pending[1] = (
        "<user_message>\nAlso tell me the word PINEAPPLE.\n</user_message>"
    )
    for frame in turn:
        reader.feed_data((json.dumps(frame) + "\n").encode())
    events = []
    async for event in client._dispatch_events(prompt_id, timeout=30.0):
        events.append(event)
        if event.kind == EVENT_COMPLETE:
            break
    kinds = [e.kind for e in events]
    assert kinds.count(EVENT_STEER_CONSUMED) == 1
    consumed_at = kinds.index(EVENT_STEER_CONSUMED)
    texts_after = [e.text for e in events[consumed_at:] if e.kind == EVENT_TEXT_CHUNK]
    assert "".join(texts_after) == "Steer received: PINEAPPLE."
    assert not any(
        "kiro-crew-steer" in (e.text or "") for e in events if e.kind == EVENT_TEXT_CHUNK
    )
    assert events[-1].stop_reason == "end_turn"
