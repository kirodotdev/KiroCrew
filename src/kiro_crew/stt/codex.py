"""Optional Codex dictation using its existing ChatGPT login and voice transport."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import json
import logging
import os
from typing import Any, AsyncIterator

from kiro_crew import github_runner, sandbox
from kiro_crew.stt import codex_media, codex_rpc
from kiro_crew.stt.codex_rpc import (
    CODE_FAILED,
    CODE_SANDBOX,
    CODE_UNAVAILABLE,
    CodexError,
    CodexRpc,
)
from kiro_crew.stt.engine import Availability

logger = logging.getLogger(__name__)
PROVIDER = "codex"
CODE_MISSING = "stt_codex_missing"
CODE_EXTRA = "stt_codex_extra_missing"
MAX_AUDIO_SECS = 300
_TAIL_SECS = 4
_FINAL_SECS = 15
_CONNECT_SECS = 30
_MAX_EVENTS = 128
_MAX_TEXT_CHARS = 32000
_MAX_ITEMS = 10000
#: Item ids are opaque server tokens; anything longer is not a real id.
_MAX_ITEM_ID_CHARS = 256
_DECODE_SECS = 60


def codex_executable() -> str | None:
    """The first ``codex`` whose provenance the provider-CLI trust policy accepts.

    Never a bare ``PATH`` lookup. The well-known install directories come first,
    so a system copy wins over an earlier ``PATH`` entry such as ``~/.local/bin``,
    then every ``PATH`` hit unless strict provider binaries are configured. Each
    candidate must pass the ownership and writability walk the other provider CLIs
    use, which refuses another account's binary, a world-writable one, or one in
    the agent's project or workspace. The validation deliberately does not use
    ``require_protected=True`` because the child is confined. The child also runs
    inside the agent sandbox (:mod:`kiro_crew.stt.codex_rpc`), so a same-user copy
    this walk accepts is still confined.
    """
    well_known = tuple(
        os.path.join(directory, "codex") for directory in github_runner.PROVIDER_EXECUTABLE_DIRS
    )
    candidates = dict.fromkeys(well_known + github_runner.provider_executable_candidates("codex"))
    for candidate in candidates:
        if not os.path.isfile(candidate):
            continue
        try:
            return github_runner.validate_provider_executable(candidate)
        except (ValueError, OSError) as exc:
            logger.debug("skipping untrusted codex candidate %s: %s", candidate, exc)
    return None


def availability() -> Availability:
    """Local prerequisites only; voice entitlement is checked when recording starts."""
    if codex_executable() is None:
        return Availability(False, CODE_MISSING, "Install Codex CLI and sign in with codex login.")
    if importlib.util.find_spec("aiortc") is None:
        return Availability(False, CODE_EXTRA, "Install the Kiro Crew voice-codex extra.")
    try:
        refused = sandbox.no_backend_refuses_spawn(codex_rpc.sandbox_mode())
    except Exception as exc:
        logger.debug("codex sandbox pre-check failed: %s", exc)
        refused = True
    if refused:
        return Availability(
            False, CODE_SANDBOX, "Codex dictation needs the agent sandbox on this host."
        )
    return Availability(True)


class StreamingSession:
    """The ordinary STT session contract, backed by one disposable voice connection."""

    def __init__(self) -> None:
        self.rpc = CodexRpc()
        self.pc: Any = None
        self.track: Any = None
        self.channel: Any = None
        self.thread_id: str | None = None
        self.queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=_MAX_EVENTS)
        self.tasks: set[asyncio.Task[Any]] = set()
        self.negotiated = asyncio.Event()
        self.connected = asyncio.Event()
        self.ready = asyncio.Event()
        self.finalize_done = asyncio.Event()
        self.failure: CodexError | None = None
        self.stopping = False
        self.finalizing = False
        self.confirmed = False
        self.finished = False
        self.pending_text = ""
        self.text_chars = 0
        self.seen: set[str] = set()

    def _emit(self, event: dict[str, Any] | None) -> None:
        if self.queue.full():
            self.queue.get_nowait()
            self.failure = CodexError(CODE_FAILED, "Codex dictation output could not keep up.")
            event = {"type": "error", "message": str(self.failure), "code": self.failure.code}
        self.queue.put_nowait(event)

    def _fail(self, error: CodexError) -> None:
        if self.failure is None:
            self.failure = error
            self._emit({"type": "error", "message": str(error), "code": error.code})
        self.negotiated.set()
        self.connected.set()
        self.ready.set()
        self.finalize_done.set()

    def _spawn(self, coroutine: Any) -> None:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

        def completed(done: asyncio.Task[Any]) -> None:
            if (
                not done.cancelled()
                and done.exception() is not None
                and not self.stopping
                and not self.confirmed
            ):
                self._fail(CodexError(CODE_FAILED, "The Codex dictation connection failed."))

        task.add_done_callback(completed)

    async def start(self) -> str | None:
        try:
            problem = await asyncio.to_thread(availability)
            if not problem.ok:
                raise CodexError(problem.code, problem.detail)
            executable = await asyncio.to_thread(codex_executable)
            if executable is None:
                raise CodexError(CODE_MISSING, "Install Codex CLI and sign in with codex login.")
            self.pc, self.track = codex_media.create_peer()
            self.channel = self.pc.createDataChannel("oai-events")

            @self.channel.on("message")
            def message(raw: str | bytes) -> None:
                try:
                    self._data_event(json.loads(raw))
                except Exception:
                    self._fail(
                        CodexError(CODE_FAILED, "Codex returned an invalid transcript event.")
                    )

            @self.pc.on("connectionstatechange")
            def state_changed() -> None:
                if self.pc.connectionState == "connected":
                    self.connected.set()
                elif (
                    self.pc.connectionState in {"failed", "closed"}
                    and not self.stopping
                    and not self.confirmed
                ):
                    self._fail(CodexError(CODE_FAILED, "The Codex voice connection closed."))

            @self.pc.on("track")
            def discard_output(track: Any) -> None:
                self._spawn(self._discard_output(track))

            await self.pc.setLocalDescription(await self.pc.createOffer())
            await self.rpc.start(executable)
            assert self.rpc.directory is not None
            result = await self.rpc.call(
                "thread/start",
                {
                    "ephemeral": True,
                    "cwd": self.rpc.directory.name,
                    "sandbox": "read-only",
                    "approvalPolicy": "untrusted",
                    "baseInstructions": "Dictation transport only. Never execute tasks or use tools.",
                },
            )
            self.thread_id = result["thread"]["id"]
            self._spawn(self._rpc_events())
            await self.rpc.call(
                "thread/realtime/start",
                {
                    "threadId": self.thread_id,
                    "version": "v3",
                    "outputModality": "audio",
                    "includeStartupContext": False,
                    "clientManagedHandoffs": True,
                    "flushTranscriptTailOnSessionEnd": False,
                    "prompt": "Listen for dictation. Do not answer or perform tasks. The client handles all work.",
                    "transport": {"type": "webrtc", "sdp": self.pc.localDescription.sdp},
                },
            )
            try:
                await asyncio.wait_for(self.negotiated.wait(), _CONNECT_SECS)
                await asyncio.wait_for(self.connected.wait(), _CONNECT_SECS)
                await asyncio.wait_for(self.ready.wait(), _CONNECT_SECS)
            except TimeoutError as exc:
                if self.failure:
                    raise self.failure
                raise CodexError(CODE_FAILED, "The Codex dictation connection timed out.") from exc
            if self.failure:
                raise self.failure
            return None
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as exc:
            error = (
                exc
                if isinstance(exc, CodexError)
                else CodexError(
                    CODE_UNAVAILABLE, "This Codex build or account cannot start voice dictation."
                )
            )
            self.failure = error
            await self.close()
            return str(error)

    async def _discard_output(self, track: Any) -> None:
        while not self.stopping:
            await track.recv()

    async def _rpc_events(self) -> None:
        while True:
            message = await self.rpc.events.get()
            if self.confirmed or self.stopping:
                return
            method, params = message.get("method"), message.get("params", {})
            if method == "thread/realtime/sdp":
                await self.pc.setRemoteDescription(codex_media.remote_answer(params["sdp"]))
                self.negotiated.set()
            elif method == codex_rpc.TRANSPORT_FAILED:
                self._fail(
                    self.rpc.failure
                    or CodexError(CODE_FAILED, "The Codex dictation connection failed.")
                )
                return
            elif method == "thread/realtime/error":
                self._fail(
                    CodexError(
                        CODE_UNAVAILABLE, "Codex voice is unavailable for this build or account."
                    )
                )
                return
            elif method == "thread/realtime/closed" and not self.finalizing:
                self._fail(
                    CodexError(CODE_FAILED, "The Codex dictation session closed unexpectedly.")
                )
                return

    def _final(self) -> None:
        if self.pending_text:
            self._emit({"type": "final", "text": self.pending_text})
            self.pending_text = ""

    def _data_event(self, event: dict[str, Any]) -> None:
        if self.failure or self.finished or self.confirmed:
            return
        kind = event.get("type")
        if kind == "session.started":
            self.ready.set()
        elif kind == "session.closed":
            if not self.finalizing or event.get("reason") not in {
                "client_request",
                "close_requested",
            }:
                self._fail(CodexError(CODE_FAILED, "Codex dictation ended before finalization."))
                return
            self.confirmed = True
            self.finalize_done.set()
        elif kind == "error":
            self._fail(CodexError(CODE_FAILED, "Codex could not transcribe this recording."))
        elif kind == "input_transcript.added":
            item = event.get("item", {})
            ident, text = item.get("id"), item.get("text")
            if (
                not isinstance(ident, str)
                or not isinstance(text, str)
                or len(ident) > _MAX_ITEM_ID_CHARS
            ):
                raise ValueError("Invalid Codex input transcript")
            if ident in self.seen:
                return
            self.text_chars += len(text)
            if len(self.seen) >= _MAX_ITEMS or self.text_chars > _MAX_TEXT_CHARS:
                raise ValueError("Codex transcript exceeded its limit")
            self.seen.add(ident)
            self.pending_text += text
            self._emit({"type": "partial", "text": self.pending_text})
        elif kind == "turn.done" and event.get("turn", {}).get("role") == "user":
            self._final()
        # Assistant speech, duplicate RPC transcripts, and delegation events are
        # never exposed or executed. Only the data channel's input ASR is text.

    async def feed(self, pcm: bytes) -> bool:
        if self.track is None or self.failure or self.stopping or self.finalizing:
            return False
        try:
            self.track.append(pcm)
            return True
        except ValueError:
            self._fail(CodexError(CODE_FAILED, "Codex microphone audio exceeded its buffer limit."))
            return False

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while (event := await self.queue.get()) is not None:
            yield event
            if event.get("type") == "error":
                return

    async def finish(self) -> None:
        if self.finalizing or self.finished or self.stopping:
            return
        self.finalizing = True
        try:
            if not self.failure and self.track is not None:
                await self.track.drain()
                # Let the final RTP packet arrive before requesting a flush.
                await asyncio.sleep(_TAIL_SECS)
                if not self.failure:
                    self.channel.send(json.dumps({"type": "session.close"}))
                    await asyncio.wait_for(self.finalize_done.wait(), _FINAL_SECS)
                if self.confirmed and not self.failure:
                    self._final()
        except Exception:
            self._fail(CodexError(CODE_FAILED, "Codex could not finish this recording."))
        self.finished = True
        self._emit(None)

    async def close(self) -> None:
        self.stopping = True
        if self.thread_id is not None and self.rpc.process is not None:
            with contextlib.suppress(Exception):
                await self.rpc.call("thread/realtime/stop", {"threadId": self.thread_id})
            self.thread_id = None
        for task in list(self.tasks):
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        try:
            if self.track is not None:
                with contextlib.suppress(Exception):
                    self.track.stop()
                self.track = None
            if self.pc is not None:
                with contextlib.suppress(Exception):
                    await self.pc.close()
                self.pc = None
        finally:
            await self.rpc.close()


async def transcribe_file(audio_path: str) -> str | None:
    """Use the same voice session for a bounded recording, with no local-model load."""
    from kiro_crew.transcribe_local import _pcm_from_wav, _pcm_via_ffmpeg

    session = StreamingSession()
    collector: asyncio.Task[None] | None = None
    parts: list[str] = []
    try:
        pcm = await asyncio.to_thread(_pcm_from_wav, audio_path, MAX_AUDIO_SECS + 1)
        if pcm is None:
            pcm = await _pcm_via_ffmpeg(audio_path, _DECODE_SECS, MAX_AUDIO_SECS + 1)
        if pcm is None or len(pcm) > MAX_AUDIO_SECS * codex_media.SAMPLE_RATE:
            return None
        if not len(pcm):
            return ""
        if await session.start():
            return None

        async def collect() -> None:
            async for event in session.events():
                if event["type"] == "error":
                    raise CodexError(CODE_FAILED, "Codex could not transcribe this recording.")
                if event["type"] == "final":
                    parts.append(str(event["text"]).strip())

        collector = asyncio.create_task(collect())
        audio = (pcm * 32768).clip(-32768, 32767).astype("<i2").tobytes()
        chunk_bytes = codex_media.SAMPLE_RATE * 2 // 10
        for offset in range(0, len(audio), chunk_bytes):
            chunk = audio[offset : offset + chunk_bytes]
            if not await session.feed(chunk):
                return None
            await asyncio.sleep(len(chunk) / (codex_media.SAMPLE_RATE * 2))
        await session.finish()
        await asyncio.wait_for(collector, _CONNECT_SECS)
        return " ".join(parts)
    except Exception:
        logger.warning("Codex dictation could not transcribe the recording")
        return None
    finally:
        if collector is not None:
            collector.cancel()
            await asyncio.gather(collector, return_exceptions=True)
        await session.close()
