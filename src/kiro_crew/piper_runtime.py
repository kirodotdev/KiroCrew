"""Dashboard-owned resident Piper worker, always behind the subprocess sandbox."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import os
import struct
import sys
import uuid
from functools import lru_cache

from kiro_crew.piper_worker import (
    MAX_AUDIO_BYTES,
    MAX_FRAME_BYTES,
    MAX_HEADER_BYTES,
    MAX_REQUEST_BYTES,
)
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    cgroup_scope_argv,
    create_subprocess_limited,
    scrub_env,
    wrap_argv,
    wrap_argv_async,
)

IDLE_SECONDS = 120.0
REQUEST_TIMEOUT_SECONDS = 180.0
REAP_TIMEOUT_SECONDS = 5.0


@lru_cache(maxsize=1)
def python_piper_available() -> bool:
    """Probe the optional API off-loop; no model is loaded in the gateway."""
    # A frozen app executable is not a general Python interpreter. Its bundled
    # imports do not establish that it can run our worker via ``-m``.
    if getattr(sys, "frozen", False):
        return False
    try:
        from piper import PiperVoice, SynthesisConfig
        from piper.voice import AudioChunk

        return (
            "config_path" in inspect.signature(PiperVoice.load).parameters
            and callable(PiperVoice.synthesize)
            and "length_scale" in inspect.signature(SynthesisConfig).parameters
            and hasattr(AudioChunk, "audio_int16_bytes")
        )
    except Exception:
        return False


def _model_key(model: str, config: str) -> tuple:
    """A changed model/config must never reuse a stale loaded voice."""
    return tuple(
        (os.path.realpath(path), os.stat(path).st_size, os.stat(path).st_mtime_ns)
        for path in (model, config)
    )


class PiperRuntime:
    """One model per dashboard; serialize requests with deterministic completion."""

    def __init__(self, *, idle_seconds: float = IDLE_SECONDS):
        self._lock = asyncio.Lock()
        self._proc: asyncio.subprocess.Process | None = None
        self._key: tuple | None = None
        self._cleanup: str | None = None
        self._stderr: asyncio.Task | None = None
        self._reaping: asyncio.Task | None = None
        self._idle: asyncio.Task | None = None
        self._active: asyncio.Task | None = None
        self._idle_seconds = idle_seconds
        self._closed = False

    async def _frame(self) -> tuple[dict, bytes]:
        from kiro_crew.voice_reply import VoiceSynthesisError

        assert self._proc is not None and self._proc.stdout is not None
        try:
            header_size, pcm_size = struct.unpack("!II", await self._proc.stdout.readexactly(8))
            if not 0 < header_size <= MAX_HEADER_BYTES or pcm_size > MAX_FRAME_BYTES:
                raise ValueError("oversize frame")
            meta = json.loads(await self._proc.stdout.readexactly(header_size))
            if not isinstance(meta, dict):
                raise ValueError("invalid frame")
            pcm = await self._proc.stdout.readexactly(pcm_size)
            return meta, pcm
        except (asyncio.IncompleteReadError, ValueError) as exc:
            raise VoiceSynthesisError(
                "voice_invalid_audio", "Piper worker returned an invalid or incomplete frame."
            ) from exc

    async def _start(self, model: str, config: str, sample_rate: int, key: tuple) -> None:
        from kiro_crew.voice_reply import VoiceSynthesisError

        await self._stop()
        cmd = [
            sys.executable,
            "-E",  # Ignore PYTHONPATH but retain legitimate --user installations.
            "-P",  # Do not import a shadow package from a chat/repository cwd.
            "-u",
            *(["-s"] if sys.flags.no_user_site else []),
            "-m",
            "kiro_crew.piper_worker",
            "--model",
            model,
            "--config",
            config,
        ]
        # Model paths remain configurable: this is NOT a first-party fixed argv
        # exception. Preserve standard sandbox refusal and resource ceilings.
        cmd, self._cleanup = await wrap_argv_async(cmd, mode="standard", _prepare=wrap_argv)
        cmd = await asyncio.to_thread(cgroup_scope_argv, cmd)
        self._proc = await create_subprocess_limited(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=scrub_env({**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}),
        )
        assert self._proc.stderr is not None
        errors = self._proc.stderr

        async def drain_errors() -> None:
            # No unbounded communicate buffer and no native text in UI/logs.
            while await errors.read(4096):
                pass

        self._stderr = asyncio.create_task(drain_errors())
        meta, pcm = await self._frame()
        if meta.get("type") != "ready" or meta.get("sample_rate") != sample_rate or pcm:
            raise VoiceSynthesisError("voice_synthesis_failed", "Piper could not load its voice.")
        self._key = key

    async def _stop(self) -> None:
        if self._reaping is not None:
            await asyncio.shield(self._reaping)
            self._reaping = None
        proc, errors, cleanup = self._proc, self._stderr, self._cleanup
        self._proc = self._stderr = self._cleanup = self._key = None
        if proc is None and not cleanup:
            return

        async def reap() -> None:
            try:
                if proc is not None:
                    if proc.returncode is None:
                        with contextlib.suppress(OSError):
                            proc.kill()
                    if errors is not None:
                        errors.cancel()
                        await asyncio.gather(errors, return_exceptions=True)
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(proc.communicate(), timeout=REAP_TIMEOUT_SECONDS)
            finally:
                if cleanup:
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(os.unlink, cleanup)

        # Repeated cancellation must not lose a killed child's pipes/profile.
        # Retain this task until close/next start joins it; shielding lets the
        # bounded reap finish even if another cancel interrupts its caller.
        self._reaping = asyncio.create_task(reap())
        await asyncio.shield(self._reaping)
        self._reaping = None

    async def _cancel_idle(self) -> None:
        if self._idle is not None:
            self._idle.cancel()
            await asyncio.gather(self._idle, return_exceptions=True)
            self._idle = None

    async def _expire(self) -> None:
        await asyncio.sleep(self._idle_seconds)
        async with self._lock:
            await self._stop()

    async def close(self) -> None:
        self._closed = True
        if self._active is not None and self._active is not asyncio.current_task():
            self._active.cancel()
            await asyncio.gather(self._active, return_exceptions=True)
        await self._cancel_idle()
        async with self._lock:
            await self._stop()

    async def stream(
        self,
        phrases: list[str],
        *,
        model: str,
        config: str,
        sample_rate: int,
        length_scale: float,
        request_id: str = "",
    ):
        from kiro_crew.voice_reply import VoiceSynthesisError

        request_id = request_id or uuid.uuid4().hex
        payload = (
            json.dumps(
                {"id": request_id, "phrases": phrases, "length_scale": length_scale},
                ensure_ascii=False,
            )
            + "\n"
        ).encode("utf-8")
        if len(payload) > MAX_REQUEST_BYTES:
            raise VoiceSynthesisError("voice_invalid_request", "Piper request exceeds the limit.")
        try:
            async with asyncio.timeout(REQUEST_TIMEOUT_SECONDS):
                # A cancelled waiter must not terminate another request's worker.
                async with self._lock:
                    if self._closed:
                        raise VoiceSynthesisError("voice_cancelled", "Piper runtime is closed.")
                    self._active = asyncio.current_task()
                    try:
                        await self._cancel_idle()
                        key = await asyncio.to_thread(_model_key, model, config)
                        if (
                            self._proc is None
                            or self._proc.returncode is not None
                            or key != self._key
                        ):
                            await self._start(model, config, sample_rate, key)
                        assert self._proc is not None and self._proc.stdin is not None
                        self._proc.stdin.write(payload)
                        await self._proc.stdin.drain()
                        total = index = 0
                        while True:
                            meta, pcm = await self._frame()
                            if meta.get("id") != request_id:
                                raise VoiceSynthesisError(
                                    "voice_invalid_audio", "Piper response identity did not match."
                                )
                            if meta.get("type") == "done" and not pcm and total:
                                break
                            if (
                                meta.get("type") != "pcm"
                                or meta.get("sample_rate") != sample_rate
                                or not pcm
                                or len(pcm) % 2
                            ):
                                raise VoiceSynthesisError(
                                    "voice_synthesis_failed",
                                    "Piper returned invalid or empty audio.",
                                )
                            total += len(pcm)
                            if total > MAX_AUDIO_BYTES:
                                raise VoiceSynthesisError(
                                    "voice_audio_limit", "Synthesized audio exceeds the limit."
                                )
                            yield index, sample_rate, pcm
                            index += 1
                    except BaseException:
                        # Includes timeout, cancellation and early generator close.
                        # Inference cannot reliably be interrupted inside ONNX.
                        await self._stop()
                        raise
                    finally:
                        self._active = None
                        if self._proc is not None and not self._closed:
                            self._idle = asyncio.create_task(self._expire())
        except TimeoutError as exc:
            raise VoiceSynthesisError("voice_timeout", "Piper synthesis timed out.") from exc
        except SandboxUnavailableError as exc:
            raise VoiceSynthesisError("voice_sandbox_unavailable", str(exc)) from exc
