"""Speech-to-text through an OpenAI-compatible API: live over Realtime, files over REST.

One provider, ``openai_compatible``, reaching any server that speaks the OpenAI
speech wire: OpenAI itself, or a self-hosted one (vLLM, LocalAI, Speaches, ...).

- **Live** dictation and Meetings use the Realtime transcription socket,
  ``<base>/realtime?intent=transcription``: PCM16 at 24 kHz goes up as
  ``input_audio_buffer.append``, and ``...input_audio_transcription.delta`` /
  ``.completed`` events come back. The server is told to wait for explicit
  ``input_audio_buffer.commit`` calls (``turn_detection: null``), because the
  newest Realtime models refuse server-side turn detection; this module decides
  the turn boundaries itself from a cheap energy gate (:class:`_TurnGate`).
- **Files** (a voice memo, an upload) use ``POST <base>/audio/transcriptions``.
- **Live without Realtime.** A server that only has ``/audio/transcriptions`` (the
  common self-hosted case) is detected at connect time and driven by
  :class:`_ChunkedSession`: the same turn gate cuts the speech into turns and each turn
  is uploaded as a WAV. Finals arrive one turn late and there are no partials.

**Where the audio goes is the owner's decision, not the agent's.** ``config.json``
is agent-writable, so neither the endpoint nor the key may live there: a
prompt-injected agent that could set the base URL would redirect every meeting to
a host it controls and collect the bearer token with it. Both are read from two
fixed dashboard-vault entries instead (:data:`VAULT_BASE_URL`,
:data:`VAULT_API_KEY`), which the agent cannot read or write. Only the model name
is ordinary config. A cleartext ``http`` endpoint is accepted for a literal
private-network address (loopback, RFC 1918, link-local) alone; everything else
must be ``https``.

This module imports no numpy and no network library at import time, so the config
loader can read :func:`openai_model_name` on the boot path.
"""

from __future__ import annotations

import array
import asyncio
import base64
import contextlib
import io
import ipaddress
import json
import logging
import math
import re
import sys
import wave
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, AsyncIterator
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from kiro_crew.stt.engine import Availability

logger = logging.getLogger(__name__)

PROVIDER = "openai_compatible"

#: The dashboard-vault entries this provider reads (Settings > Secrets). Fixed
#: names, never a name taken from config: a reference the agent could rewrite
#: would let it read any other vault entry through this provider.
VAULT_BASE_URL = "OPENAI_STT_BASE_URL"
VAULT_API_KEY = "OPENAI_STT_API_KEY"

DEFAULT_BASE_URL = "https://api.openai.com/v1"
#: Valid for both the Realtime socket and ``/audio/transcriptions``.
OPENAI_MODEL_DEFAULT = "gpt-4o-transcribe"
_MODEL_MAX = 128
#: Hosted ids (``gpt-4o-transcribe``) and self-hosted ones (``Systran/faster-whisper-small``).
_MODEL_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z._:/@+-]*")

CODE_NOT_CONFIGURED = "stt_openai_not_configured"
CODE_AUTH = "stt_openai_auth_failed"
CODE_FAILED = "stt_openai_failed"
#: Every code a session's ``error`` event can carry; the dashboard has text for each.
SESSION_ERROR_CODES = frozenset({CODE_NOT_CONFIGURED, CODE_AUTH, CODE_FAILED})

#: The Realtime API takes 24 kHz PCM16; the dashboard sends 16 kHz.
INPUT_RATE_HZ = 16000
WIRE_RATE_HZ = 24000
#: The Realtime API refuses a commit of under 100 ms of audio.
MIN_COMMIT_MS = 100
#: A turn is cut at this length even while the speaker keeps talking.
MAX_TURN_MS = 15000
#: Audio that never contained speech is discarded after this long, not transcribed.
IDLE_DISCARD_MS = 5000
DEFAULT_SILENCE_MS = 700

_CONNECT_SECS = 15
_FINAL_SECS = 20
_MAX_FRAME_BYTES = 4 * 1024 * 1024
_FAILED_TURNS_FATAL = 3
#: ``/audio/transcriptions`` accepts at most this much (OpenAI's documented limit).
MAX_FILE_BYTES = 25 * 1024 * 1024


def openai_model_name(value: object) -> str | None:
    """The model id *value* names, or None when unusable. Surrounding space is dropped.

    The one rule both writers apply: ``PUT /api/config/stt`` stores only a value
    this accepts and the loader keeps only what this accepts. The id travels in a
    JSON body and a form field, never in a path or a shell, so the shape check is
    about keeping junk out of config rather than about injection.
    """
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not name or len(name) > _MODEL_MAX or not _MODEL_RE.fullmatch(name):
        return None
    return name


# ── endpoint ──


@dataclass(frozen=True)
class Endpoint:
    """Where the audio goes and how to authenticate there."""

    base_url: str
    api_key: str
    #: True when the owner did not set a URL and the OpenAI default applies.
    default_url: bool


class EndpointError(Exception):
    """The vault holds no usable endpoint. ``code`` and the text are safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _is_private_literal(host: str) -> bool:
    """Whether *host* is a LITERAL loopback, RFC 1918 or link-local address.

    Literal only: a name is resolved by a hosts file or a resolver the owner does not
    control, so it is no proof the request stays on a network they own. An unusual
    numeric spelling (``127.1``, ``0x7f.1``) fails to parse and so counts as public,
    which fails toward ``https``. Plain ``http`` there is the owner's own choice for a
    server on their network; across the internet it would put the key and the audio in
    cleartext.
    """
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def validate_base_url(raw: str) -> str:
    """The normalised base URL, or raise :class:`EndpointError`.

    ``https`` anywhere; ``http`` only for a literal private-network address. No user info,
    query or fragment: user info is how a URL that reads as one host is made to
    dial another. The text of *raw* is never put in the error: it comes from the
    vault and may carry a credential pasted into the wrong field.
    """
    try:
        parts = urlsplit(raw.strip())
        parts.port  # raises on a malformed port
    except ValueError:
        raise EndpointError(
            CODE_NOT_CONFIGURED, "The stored base URL is not a valid URL."
        ) from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise EndpointError(CODE_NOT_CONFIGURED, "The stored base URL must be an http(s) URL.")
    if "@" in parts.netloc or parts.query or parts.fragment:
        raise EndpointError(
            CODE_NOT_CONFIGURED, "The stored base URL must not carry credentials or a query."
        )
    if parts.scheme == "http" and not _is_private_literal(parts.hostname):
        raise EndpointError(
            CODE_NOT_CONFIGURED,
            "A base URL that is not a private-network address must use https.",
        )
    return raw.strip().rstrip("/")


def resolve_endpoint() -> Endpoint:
    """Read the endpoint from the vault. BLOCKING (vault file I/O); call off the loop."""
    from kiro_crew.config.paths import config_dir
    from kiro_crew.secrets.vault import SecretVault

    try:
        found = SecretVault(config_dir()).get_many([VAULT_BASE_URL, VAULT_API_KEY])
    except Exception as exc:
        # The class only: a vault path or entry name does not belong in a log line.
        logger.warning("openai_compatible STT: the vault could not be read: %s", type(exc).__name__)
        raise EndpointError(CODE_NOT_CONFIGURED, "The secrets vault could not be read.") from None
    url_secret, key_secret = found.get(VAULT_BASE_URL), found.get(VAULT_API_KEY)
    raw_url = url_secret.reveal().strip() if url_secret is not None else ""
    api_key = key_secret.reveal().strip() if key_secret is not None else ""
    if "\r" in api_key or "\n" in api_key:
        raise EndpointError(CODE_NOT_CONFIGURED, "The stored API key is not a single line.")
    if not raw_url:
        if not api_key:
            raise EndpointError(
                CODE_NOT_CONFIGURED,
                f"Store {VAULT_API_KEY} (and {VAULT_BASE_URL} for a server other than "
                "OpenAI) under Settings > Secrets.",
            )
        return Endpoint(DEFAULT_BASE_URL, api_key, True)
    return Endpoint(validate_base_url(raw_url), api_key, False)


def availability() -> "Availability":
    """Whether the endpoint is configured. Local checks only: no network call. BLOCKING."""
    from kiro_crew.stt.engine import Availability

    try:
        resolve_endpoint()
    except EndpointError as exc:
        return Availability(False, exc.code, str(exc))
    try:
        import aiohttp  # noqa: F401
    except ImportError:
        return Availability(False, CODE_FAILED, "The aiohttp package is not installed.")
    return Availability(True)


def _headers(endpoint: Endpoint) -> dict[str, str]:
    return {"Authorization": f"Bearer {endpoint.api_key}"} if endpoint.api_key else {}


def request_language(language_code: str) -> str:
    """The ISO 639-1 code to send for ``stt.language_code``, or ``""`` for auto-detect."""
    code = (language_code or "").strip()
    if not code or code.lower() == "auto":
        return ""
    return code.split("-", 1)[0].lower()


# ── audio ──


class _Resampler:
    """16 kHz to 24 kHz mono PCM16 by linear interpolation, continuous across chunks.

    The position advances 2/3 of an input sample per output sample, kept in thirds
    so no float error accumulates over a long meeting. The unconsumed input tail
    carries into the next chunk, so chunk boundaries leave no click.
    """

    def __init__(self) -> None:
        self._tail: list[int] = []
        self._pos3 = 0

    def process(self, pcm: bytes) -> bytes:
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        if sys.byteorder == "big":
            samples.byteswap()
        buf = self._tail + samples.tolist()
        out = array.array("h")
        pos3 = self._pos3
        last = len(buf) - 1
        while pos3 // 3 < last:
            i, rem = divmod(pos3, 3)
            value = buf[i] if rem == 0 else (buf[i] * (3 - rem) + buf[i + 1] * rem) // 3
            out.append(value)
            pos3 += 2
        keep = pos3 // 3
        self._tail = buf[keep:]
        self._pos3 = pos3 - keep * 3
        if sys.byteorder == "big":
            out.byteswap()
        return out.tobytes()


def _rms(pcm: bytes) -> float:
    n = len(pcm) // 2
    if n == 0:
        return 0.0
    samples = array.array("h")
    samples.frombytes(pcm[: n * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    return math.sqrt(sum(s * s for s in samples) / n)


class _TurnGate:
    """Decides when to commit the audio buffered upstream.

    An energy gate rather than a model: a turn ends after ``silence_ms`` of quiet
    that followed speech, or at :data:`MAX_TURN_MS`. The floor tracks the room slowly
    and speech is a clear multiple of it, with an absolute minimum so digital
    silence is never "speech". A buffer that never held speech is discarded after
    :data:`IDLE_DISCARD_MS`, because transcribing room tone makes models invent words.
    """

    SPEECH_MIN_RMS = 120.0
    SPEECH_FLOOR_RATIO = 3.0

    def __init__(self, silence_ms: int) -> None:
        self._silence_limit_ms = max(200, silence_ms)
        self._floor = self.SPEECH_MIN_RMS / self.SPEECH_FLOOR_RATIO
        self.reset()

    def reset(self) -> None:
        self.buffered_ms = 0.0
        self.silence_ms = 0.0
        self.speech = False

    def push(self, pcm: bytes) -> str:
        """Account for *pcm*, already sent upstream. Returns ``commit``, ``discard`` or ``""``."""
        ms = len(pcm) // 2 * 1000 / INPUT_RATE_HZ
        if ms <= 0:
            return ""
        level = _rms(pcm)
        loud = level >= max(self.SPEECH_MIN_RMS, self._floor * self.SPEECH_FLOOR_RATIO)
        self.buffered_ms += ms
        if loud:
            self.speech = True
            self.silence_ms = 0.0
        else:
            self.silence_ms += ms
            # Slow, one-way-per-step drift so a steady hum is learned but speech is not.
            self._floor += (level - self._floor) * 0.05
        if self.speech and self.silence_ms >= self._silence_limit_ms:
            return "commit" if self.buffered_ms >= MIN_COMMIT_MS else ""
        if self.speech and self.buffered_ms >= MAX_TURN_MS:
            return "commit"
        if not self.speech and self.buffered_ms >= IDLE_DISCARD_MS:
            return "discard"
        return ""

    def has_speech(self) -> bool:
        return self.speech and self.buffered_ms >= MIN_COMMIT_MS


# ── live session ──


def _session_update(model: str, language: str) -> dict[str, Any]:
    transcription: dict[str, Any] = {"model": model}
    if language:
        # The newest live model takes a list; every other one takes a single code.
        if model.startswith("gpt-live"):
            transcription["languages"] = [language]
        else:
            transcription["language"] = language
    return {
        "type": "session.update",
        "session": {
            "type": "transcription",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": WIRE_RATE_HZ},
                    "transcription": transcription,
                    "turn_detection": None,
                }
            },
        },
    }


def _realtime_url(base_url: str) -> str:
    return base_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + (
        "/realtime?intent=transcription"
    )


class StreamingSession:
    """The ordinary STT session contract over one Realtime transcription socket."""

    def __init__(self, stt_config: Any) -> None:
        self._model = openai_model_name(getattr(stt_config, "openai_model", "")) or (
            OPENAI_MODEL_DEFAULT
        )
        self._language = request_language(getattr(stt_config, "language_code", ""))
        self._gate = _TurnGate(int(getattr(stt_config, "silence_ms", DEFAULT_SILENCE_MS)))
        self._resampler = _Resampler()
        self._http: Any = None
        self._ws: Any = None
        self._reader: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._ready = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._outstanding = 0
        self._order: deque[str] = deque()
        self._done: dict[str, str] = {}
        self._pending: dict[str, str] = {}
        self._failed_in_a_row = 0
        self._failure: tuple[str, str] | None = None
        self._finishing = False
        self._closed = False
        self._ended = False
        self._stt_config = stt_config
        self._chunked: _ChunkedSession | None = None

    @property
    def failure_code(self) -> str | None:
        """The code of the failure that ended or refused this session, if any."""
        if self._chunked is not None:
            return self._chunked.failure_code
        return self._failure[0] if self._failure else None

    # events

    def _emit(self, event: dict[str, Any] | None) -> None:
        self._queue.put_nowait(event)

    def _fail(self, code: str, message: str) -> None:
        if self._failure is None:
            self._failure = (code, message)
            self._emit({"type": "error", "message": message, "code": code})
        self._ready.set()
        self._idle.set()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        if self._chunked is not None:
            async for forwarded in self._chunked.events():
                yield forwarded
            return
        while (event := await self._queue.get()) is not None:
            yield event
            if event.get("type") == "error":
                return

    # lifecycle

    async def start(self) -> str | None:
        try:
            return await self._start()
        except asyncio.CancelledError:
            await self.close()
            raise
        except EndpointError as exc:
            self._failure = (exc.code, str(exc))
            await self.close()
            return str(exc)
        except Exception as exc:
            logger.warning(
                "openai_compatible STT: could not open the socket: %s", type(exc).__name__
            )
            if _lacks_realtime(exc):
                return await self._start_chunked()
            code, message = _classify_connect_error(exc)
            self._failure = (code, message)
            await self.close()
            return message

    async def _start_chunked(self) -> str | None:
        """Drive a server with no Realtime socket one uploaded turn at a time."""
        await self._release_socket()
        chunked = _ChunkedSession(self._stt_config)
        problem = await chunked.start()
        if problem:
            self._failure = (chunked.failure_code or CODE_FAILED, problem)
            await chunked.close()
            return problem
        logger.info("openai_compatible STT: no Realtime socket; transcribing turn by turn")
        self._chunked = chunked
        return None

    async def _start(self) -> str | None:
        import aiohttp

        endpoint = await asyncio.to_thread(resolve_endpoint)
        self._http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
        self._ws = await asyncio.wait_for(
            self._http.ws_connect(
                _realtime_url(endpoint.base_url),
                headers=_headers(endpoint),
                heartbeat=20,
                max_msg_size=_MAX_FRAME_BYTES,
            ),
            _CONNECT_SECS,
        )
        self._reader = asyncio.create_task(self._read())
        await self._ws.send_json(_session_update(self._model, self._language))
        try:
            await asyncio.wait_for(self._ready.wait(), _CONNECT_SECS)
        except asyncio.TimeoutError:
            raise ConnectionError("the server never opened a transcription session") from None
        if self._failure is not None:
            return self._failure[1]
        return None

    async def _read(self) -> None:
        import aiohttp

        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        event = json.loads(msg.data)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        self._on_event(event)
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("openai_compatible STT: the socket failed", exc_info=True)
        if not self._closed and not (self._finishing and self._outstanding == 0):
            self._fail(CODE_FAILED, "The speech server closed the connection.")

    def _on_event(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        item = event.get("item_id")
        if kind in ("session.created", "session.updated", "transcription_session.created"):
            self._ready.set()
        elif kind == "input_audio_buffer.committed" and isinstance(item, str):
            self._order.append(item)
        elif kind == "conversation.item.input_audio_transcription.delta":
            delta = event.get("delta")
            if isinstance(item, str) and isinstance(delta, str):
                text = self._pending.get(item, "") + delta
                self._pending[item] = text
                self._emit({"type": "partial", "text": text})
        elif kind == "conversation.item.input_audio_transcription.completed":
            transcript = event.get("transcript")
            self._failed_in_a_row = 0
            self._finish_item(item, transcript if isinstance(transcript, str) else "")
        elif kind == "conversation.item.input_audio_transcription.failed":
            self._failed_in_a_row += 1
            logger.warning("openai_compatible STT: a turn could not be transcribed")
            if self._failed_in_a_row >= _FAILED_TURNS_FATAL:
                self._fail(CODE_FAILED, "The speech server could not transcribe the audio.")
                return
            self._finish_item(item, "")
        elif kind == "error":
            err = event.get("error")
            code = err.get("code") if isinstance(err, dict) else None
            if code == "input_audio_buffer_commit_empty":
                self._turn_settled()
                return
            # The server's text is not relayed: it is third-party text bound for the DOM.
            logger.warning("openai_compatible STT: the server reported an error (%s)", code)
            self._fail(CODE_FAILED, "The speech server reported an error.")

    def _finish_item(self, item: object, text: str) -> None:
        if isinstance(item, str):
            self._pending.pop(item, None)
            if item in self._order:
                self._done[item] = text
            else:
                self._emit({"type": "final", "text": text})
        # Finals leave in commit order, so a late completion cannot overtake an earlier turn.
        while self._order and self._order[0] in self._done:
            self._emit({"type": "final", "text": self._done.pop(self._order.popleft())})
        self._turn_settled()

    def _turn_settled(self) -> None:
        self._outstanding = max(0, self._outstanding - 1)
        if self._outstanding == 0:
            self._idle.set()

    async def _send(self, event: dict[str, Any]) -> bool:
        if self._ws is None or self._ws.closed or self._failure is not None:
            return False
        try:
            await self._ws.send_json(event)
            return True
        except Exception:
            self._fail(CODE_FAILED, "The speech server stopped accepting audio.")
            return False

    async def _commit(self) -> bool:
        self._outstanding += 1
        self._idle.clear()
        self._gate.reset()
        return await self._send({"type": "input_audio_buffer.commit"})

    async def feed(self, pcm: bytes) -> bool:
        if self._chunked is not None:
            return await self._chunked.feed(pcm)
        if self._finishing or self._closed or self._failure is not None or not pcm:
            return False
        audio = base64.b64encode(self._resampler.process(pcm)).decode("ascii")
        if not await self._send({"type": "input_audio_buffer.append", "audio": audio}):
            return False
        verdict = self._gate.push(pcm)
        if verdict == "commit":
            return await self._commit()
        if verdict == "discard":
            self._gate.reset()
            return await self._send({"type": "input_audio_buffer.clear"})
        return True

    async def finish(self) -> None:
        if self._chunked is not None:
            await self._chunked.finish()
            return
        if self._finishing:
            return
        self._finishing = True
        try:
            if self._failure is None:
                if self._gate.has_speech():
                    await self._commit()
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._idle.wait(), _FINAL_SECS)
        finally:
            if not self._ended:
                self._ended = True
                self._emit(None)

    async def _release_socket(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            await asyncio.gather(self._reader, return_exceptions=True)
            self._reader = None
        for resource in (self._ws, self._http):
            if resource is not None:
                with contextlib.suppress(Exception):
                    await resource.close()
        self._ws = self._http = None

    async def close(self) -> None:
        self._closed = True
        await self._release_socket()
        if self._chunked is not None:
            await self._chunked.close()
        if not self._ended:
            self._ended = True
            self._emit(None)


def _lacks_realtime(exc: Exception) -> bool:
    """Whether a failed connect means "this server has no Realtime socket".

    A handshake the server answers with an HTTP status (404 and 405 for a missing route,
    426 for a refused upgrade, 403 from a gateway that only exposes the REST routes)
    says nothing about the key; the fallback checks the key itself against ``/models``.
    A refused TCP connection, a TLS failure or a timeout is a different problem and is
    reported as one.
    """
    return getattr(exc, "status", None) in (400, 401, 403, 404, 405, 426, 501)


def _wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(INPUT_RATE_HZ)
        w.writeframes(pcm)
    return out.getvalue()


async def _post_transcription(
    http: Any, endpoint: Endpoint, data: bytes, model: str, language: str
) -> tuple[int, str | None]:
    """Upload *data* to ``/audio/transcriptions``: the HTTP status and the text if it was 200."""
    import aiohttp

    form = aiohttp.FormData()
    form.add_field("model", model)
    form.add_field("response_format", "json")
    if language:
        form.add_field("language", language)
    form.add_field("file", data, filename="audio" + sniff_extension(data[:16]))
    async with http.post(
        endpoint.base_url + "/audio/transcriptions", data=form, headers=_headers(endpoint)
    ) as resp:
        if resp.status != 200:
            return resp.status, None
        body = await resp.json(content_type=None)
    text = body.get("text") if isinstance(body, dict) else None
    return 200, text.strip() if isinstance(text, str) else None


_CHUNK_QUEUE_MAX = 8
_PROBE_SECS = 10
_CHUNK_FINAL_SECS = 60


class _ChunkedSession:
    """The session contract for a server that only has ``/audio/transcriptions``.

    The turn gate cuts the speech exactly as it does for the Realtime socket; each turn
    is wrapped as a 16 kHz WAV and uploaded by one worker, in order, so finals leave in
    the order they were spoken. There are no partials: the server returns a turn whole.
    """

    def __init__(self, stt_config: Any) -> None:
        self._model = openai_model_name(getattr(stt_config, "openai_model", "")) or (
            OPENAI_MODEL_DEFAULT
        )
        self._language = request_language(getattr(stt_config, "language_code", ""))
        self._gate = _TurnGate(int(getattr(stt_config, "silence_ms", DEFAULT_SILENCE_MS)))
        self._turn = bytearray()
        self._endpoint: Endpoint | None = None
        self._http: Any = None
        self._jobs: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=_CHUNK_QUEUE_MAX)
        self._queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._failed_in_a_row = 0
        self._failure: tuple[str, str] | None = None
        self._finishing = False
        self._ended = False

    @property
    def failure_code(self) -> str | None:
        return self._failure[0] if self._failure else None

    def _fail(self, code: str, message: str) -> None:
        if self._failure is None:
            self._failure = (code, message)
            self._queue.put_nowait({"type": "error", "message": message, "code": code})

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while (event := await self._queue.get()) is not None:
            yield event
            if event.get("type") == "error":
                return

    async def start(self) -> str | None:
        import aiohttp

        try:
            self._endpoint = await asyncio.to_thread(resolve_endpoint)
        except EndpointError as exc:
            self._failure = (exc.code, str(exc))
            return str(exc)
        self._http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=max(30, _CHUNK_FINAL_SECS))
        )
        try:
            # The key is checked now, not on the first uploaded turn, so a wrong key is
            # reported as the setup failure it is. A server with no /models route proves
            # nothing either way and is let through.
            async with self._http.get(
                self._endpoint.base_url + "/models",
                headers=_headers(self._endpoint),
                timeout=aiohttp.ClientTimeout(total=_PROBE_SECS),
            ) as resp:
                status = resp.status
        except Exception as exc:
            logger.warning("openai_compatible STT: probe failed: %s", type(exc).__name__)
            self._failure = (CODE_FAILED, "Could not connect to the speech server.")
            return self._failure[1]
        if status in (401, 403):
            self._failure = (CODE_AUTH, "The speech server refused the API key.")
            return self._failure[1]
        self._worker = asyncio.create_task(self._work())
        return None

    async def _work(self) -> None:
        assert self._http is not None and self._endpoint is not None
        while (turn := await self._jobs.get()) is not None:
            if self._failure is not None:
                continue
            try:
                status, text = await _post_transcription(
                    self._http, self._endpoint, _wav(bytes(turn)), self._model, self._language
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("openai_compatible STT: a turn failed: %s", type(exc).__name__)
                status, text = 0, None
            if status in (401, 403):
                self._fail(CODE_AUTH, "The speech server refused the API key.")
            elif text is None:
                self._failed_in_a_row += 1
                logger.warning(
                    "openai_compatible STT: a turn could not be transcribed (%d)", status
                )
                if self._failed_in_a_row >= _FAILED_TURNS_FATAL:
                    self._fail(CODE_FAILED, "The speech server could not transcribe the audio.")
            else:
                self._failed_in_a_row = 0
                self._queue.put_nowait({"type": "final", "text": text})

    def _submit(self) -> bool:
        turn = bytes(self._turn)
        self._turn.clear()
        self._gate.reset()
        try:
            self._jobs.put_nowait(turn)
        except asyncio.QueueFull:
            self._fail(CODE_FAILED, "The speech server could not keep up with the audio.")
            return False
        return True

    async def feed(self, pcm: bytes) -> bool:
        if self._finishing or self._failure is not None or not pcm:
            return False
        self._turn += pcm
        verdict = self._gate.push(pcm)
        if verdict == "commit":
            return self._submit()
        if verdict == "discard":
            self._turn.clear()
            self._gate.reset()
        return True

    async def finish(self) -> None:
        if self._finishing:
            return
        self._finishing = True
        try:
            if self._failure is None and self._worker is not None:
                if self._gate.has_speech():
                    self._submit()
                await self._jobs.put(None)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(self._worker), _CHUNK_FINAL_SECS)
        finally:
            if not self._ended:
                self._ended = True
                self._queue.put_nowait(None)

    async def close(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        if self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.close()
            self._http = None
        if not self._ended:
            self._ended = True
            self._queue.put_nowait(None)


def _classify_connect_error(exc: Exception) -> tuple[str, str]:
    """A code and a fixed sentence for a failed connect. Never the exception text."""
    status = getattr(exc, "status", None)
    if status in (401, 403):
        return CODE_AUTH, "The speech server refused the API key."
    if status == 404:
        return CODE_FAILED, "The speech server has no Realtime transcription endpoint."
    return CODE_FAILED, "Could not connect to the speech server."


# ── files ──

_AUDIO_MAGIC: tuple[tuple[bytes, int, str], ...] = (
    (b"RIFF", 0, ".wav"),
    (b"OggS", 0, ".ogg"),
    (b"\x1aE\xdf\xa3", 0, ".webm"),
    (b"fLaC", 0, ".flac"),
    (b"ID3", 0, ".mp3"),
    (b"\xff\xfb", 0, ".mp3"),
    (b"ftyp", 4, ".m4a"),
)


def sniff_extension(head: bytes) -> str:
    """A file extension for the audio container *head* begins with, ``.webm`` if unknown.

    The server picks its decoder from the upload's name, and the path handed to the
    transcribe seam can be a descriptor (``/dev/fd/N``) with no extension to use.
    """
    for magic, offset, ext in _AUDIO_MAGIC:
        if head[offset : offset + len(magic)] == magic:
            return ext
    return ".webm"


def _read_upload(audio_path: str) -> bytes | None:
    with open(audio_path, "rb") as fh:
        data = fh.read(MAX_FILE_BYTES + 1)
    return None if len(data) > MAX_FILE_BYTES else data


async def transcribe_file(audio_path: str, stt_config: Any) -> str | None:
    """Transcribe a recorded file. Text, ``""`` for silence, None on any failure; never raises."""
    try:
        import aiohttp

        endpoint = await asyncio.to_thread(resolve_endpoint)
        data = await asyncio.to_thread(_read_upload, audio_path)
        if data is None:
            logger.warning("openai_compatible STT: the recording exceeds %d bytes", MAX_FILE_BYTES)
            return None
        if not data:
            return ""
        model = openai_model_name(getattr(stt_config, "openai_model", "")) or OPENAI_MODEL_DEFAULT
        language = request_language(getattr(stt_config, "language_code", ""))
        timeout = aiohttp.ClientTimeout(
            total=max(10, int(getattr(stt_config, "timeout_secs", 300)))
        )
        async with aiohttp.ClientSession(timeout=timeout) as http:
            status, text = await _post_transcription(http, endpoint, data, model, language)
        if status != 200:
            logger.warning("openai_compatible STT: file transcription returned %d", status)
        return text
    except asyncio.CancelledError:
        raise
    except EndpointError as exc:
        logger.warning("openai_compatible STT: %s", exc)
        return None
    except Exception as exc:
        logger.warning("openai_compatible STT: file transcription failed: %s", type(exc).__name__)
        return None


__all__ = [
    "CODE_AUTH",
    "CODE_FAILED",
    "CODE_NOT_CONFIGURED",
    "DEFAULT_BASE_URL",
    "OPENAI_MODEL_DEFAULT",
    "PROVIDER",
    "SESSION_ERROR_CODES",
    "VAULT_API_KEY",
    "VAULT_BASE_URL",
    "Endpoint",
    "EndpointError",
    "StreamingSession",
    "availability",
    "openai_model_name",
    "resolve_endpoint",
    "transcribe_file",
    "validate_base_url",
]
