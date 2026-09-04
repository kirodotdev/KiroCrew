"""Voice synthesis endpoints — TTS config and streaming synthesis.

TTS synthesis is optional and routed through ``voice_reply`` (which lazily
imports any cloud TTS backend only when invoked). The endpoints below stay
importable on a vanilla machine; synthesis simply errors gracefully if no
backend is configured.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field

from aiohttp import web

from kiro_crew import aws_consent
from kiro_crew.config.loader import config_path
from kiro_crew.dashboard.handlers._shared import read_bounded_json
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.piper_runtime import PiperRuntime
from kiro_crew.sandbox import SandboxUnavailableError
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.slack.handler import _vc
from kiro_crew.voice_reply import (
    PROVIDER_PIPER,
    PROVIDER_POLLY,
    PROVIDER_SYSTEM,
    VALID_ENGINES,
    VALID_PROVIDERS,
    SystemVoiceProbeError,
    VoiceSynthesisError,
    _validate_pitch,
    _validate_rate,
    list_system_voices,
    pcm_to_wav,
    resolve_polly_cli,
    resolve_system_tts_async,
    stitch_mp3s,
    streaming_piper_reply,
    streaming_voice_reply,
    synthesize_speech,
    validate_length_scale,
    validated_config_bool,
    validated_config_string,
)

logger = logging.getLogger(__name__)

_MAX_SYNTHESIS_TEXT_CHARS = 20000
_MAX_ACTIVE_SYNTHESES = 2
_MAX_REQUEST_ID_CHARS = 128
_CANCELLED_REQUEST_TTL = 60.0
_MAX_CANCELLED_REQUESTS = 128


@dataclass
class _VoiceRequests:
    tasks: dict[tuple[str, str], asyncio.Task] = field(default_factory=dict)
    cancelled: dict[tuple[str, str], float] = field(default_factory=dict)
    piper: PiperRuntime = field(default_factory=PiperRuntime)

    def prune(self) -> None:
        cutoff = time.monotonic() - _CANCELLED_REQUEST_TTL
        self.cancelled = {key: ts for key, ts in self.cancelled.items() if ts > cutoff}


_VOICE_REQUESTS = web.AppKey("voice_requests", _VoiceRequests)


def register_voice_lifecycle(app: web.Application) -> None:
    """Keep synthesis ownership and cancellation bounded to this dashboard."""
    app[_VOICE_REQUESTS] = _VoiceRequests()

    async def shutdown(app: web.Application) -> None:
        tasks = list(app[_VOICE_REQUESTS].tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await app[_VOICE_REQUESTS].piper.close()

    app.on_shutdown.append(shutdown)


async def api_voice_cancel(request: web.Request) -> web.Response:
    """POST /api/voice/cancel — stop one identified synthesis, including a racing POST."""
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None
    if not isinstance(body.get("slot", ""), str) or not (
        isinstance(body.get("request_id"), str)
        and 0 < len(body["request_id"]) <= _MAX_REQUEST_ID_CHARS
    ):
        return web.json_response(
            {"error": "slot and request_id required", "code": "voice_invalid_request"}, status=400
        )
    jobs = request.app[_VOICE_REQUESTS]
    key = (body.get("slot", ""), body["request_id"])
    jobs.prune()
    if len(jobs.cancelled) >= _MAX_CANCELLED_REQUESTS:
        jobs.cancelled.pop(next(iter(jobs.cancelled)))
    jobs.cancelled[key] = time.monotonic()
    task = jobs.tasks.get(key)
    if task is not None:
        task.cancel()
    return web.json_response({"ok": True, "request_id": key[1]})


# Synthesized audio is read and written WHOLE, and its size scales with the
# length of the reply being spoken — a Piper clip is uncompressed WAV, so a
# minute of speech is megabytes. AUTOSDE `no-blocking-call-on-event-loop` names
# "large synchronous file IO" as something that must not run on the gateway's
# single loop, where it stalls every other session — and the heartbeat — for the
# duration of the transfer. These two helpers hold the blocking halves; every
# caller reaches them through `asyncio.to_thread`, the same seam this module
# already uses for `resolve_polly_cli`.


def _spill_chunk(mp3_bytes: bytes) -> str:
    """Write one synthesized chunk to a temp file and return its path.

    Blocking by design — call it through ``asyncio.to_thread``. ``os.close`` on
    the ``mkstemp`` descriptor is in here for the same reason the write is: the
    same AUTOSDE rule names it, and splitting the pair would leave a bare
    descriptor owned by neither side.
    """
    fd, chunk_path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    with open(chunk_path, "wb") as f:
        f.write(mp3_bytes)
    return chunk_path


def _read_audio(path: str) -> bytes:
    """Read a synthesized clip whole. Blocking — call it through ``to_thread``."""
    with open(path, "rb") as f:
        return f.read()


#: How long one sandbox-refusal kind stays reported before it is worth saying
#: again. Auto-speak synthesises per sentence and the remedy never changes, so the
#: first notice is the informative one; a later recurrence still deserves a notice
#: rather than silence, in case an operator fixed the host and it regressed.
_SANDBOX_NOTICE_COOLDOWN_S = 1800.0


def _sandbox_refusal_response(
    state: DashboardState, identity: dict[str, str], exc: SandboxUnavailableError
) -> web.Response:
    """Report a fail-closed sandbox refusal on every surface that can carry it.

    Shared by both synthesis branches so the streaming and non-streaming paths
    cannot drift into reporting the same refusal differently.

    The sandbox layer's OWN prose is relayed rather than composed here: it picks
    the remedy per ``exc.kind``, and only "no_backend" may name the
    allow_unsandboxed_exec opt-in — "transient" is momentary resource pressure
    where advising that opt-in would be actively wrong, and "foreign_sandbox"
    points at a kiro-cli setting instead.

    The ``code`` is what the repo requires of any new ``status >= 400`` JSON body
    (``test_no_new_error_response_without_a_code``), because the dashboard renders
    ``error`` verbatim into a localized UI and an un-coded sentence is
    untranslatable by construction. It is derived from the kind mechanically,
    since the kinds are a closed lower_snake set.

    The notification is the only surface that carries the REMEDY. The dashboard
    does consume ``voice_error``, but its handler keys a generic failure off
    ``code`` and never renders ``error``, and both auto-speak call sites discard
    the rejected request — so the prose reaches a person only through the note.
    That handler also drops any event without a ``request_id``, which is why the
    broadcast carries the whole identity rather than the slot alone. It is
    throttled per KIND rather than per request: the remedy is a host-level
    property, identical for every slot, so the kind is the whole of what
    distinguishes one notice from another.
    """
    msg = f"Voice synthesis was refused by the sandbox. {exc}"
    code = f"sandbox_{exc.kind}"
    state.broadcast_ws("voice_error", {**identity, "error": msg, "code": code})
    now = time.monotonic()
    last = state._voice_sandbox_notified.get(exc.kind)
    if last is None or now - last >= _SANDBOX_NOTICE_COOLDOWN_S:
        state._voice_sandbox_notified[exc.kind] = now
        state.notify(
            "agent",
            "Voice reply skipped",
            msg,
            meta={"slot": identity.get("slot", ""), "kind": exc.kind},
        )
    return web.json_response(
        {"ok": False, "error": msg, "code": code, "request_id": identity.get("request_id", "")},
        status=502,
    )


async def api_voice_config(request: web.Request) -> web.Response:
    """GET/PUT /api/voice/config — read or update voice settings."""
    if request.method == "GET":
        return web.json_response(
            {
                "enabled": _vc.global_enabled,
                "provider": _vc.provider,
                "voice": _vc.default_voice,
                "engine": _vc.default_engine,
                "rate": _vc.default_rate,
                "pitch": _vc.default_pitch,
                "autoSpeak": _vc.auto_speak,
                "aws_profile": _vc.aws_profile,
                "region": _vc.region,
                "piper_binary": _vc.piper_binary,
                "piper_model": _vc.piper_model,
                "piper_model_config": _vc.piper_model_config,
                "piper_length_scale": _vc.piper_length_scale,
                "system_voice": _vc.system_voice,
            }
        )

    # PUT — update and persist
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success

    # Validate the WHOLE patch before touching _vc, then apply it in one pass.
    # Mutating as we walk the fields makes a rejected request a torn write: the
    # caller gets a 400 while an earlier field has already taken effect, so the
    # live provider can change on a request the API said it refused.
    pending: dict[str, object] = {}

    # ``in VALID_PROVIDERS`` would raise TypeError on an unhashable JSON value
    # (list/dict), 500ing the PUT — require a str first.
    if (
        "provider" in body
        and isinstance(body["provider"], str)
        and body["provider"] in VALID_PROVIDERS
    ):
        pending["provider"] = body["provider"]
    if "voice" in body:
        # A voice NAME, same class as system_voice: a wrong type must be refused
        # rather than stringified, because "{}" is a name no engine can satisfy.
        _voice = validated_config_string(body["voice"])
        if _voice is None:
            return web.json_response(
                {"error": "voice must be a string", "code": "field_not_string"},
                status=400,
            )
        pending["default_voice"] = _voice
    # Same as provider above: ``in VALID_ENGINES`` raises TypeError on an
    # unhashable JSON value (list/dict), 500ing the PUT — require a str first.
    if "engine" in body and isinstance(body["engine"], str) and body["engine"] in VALID_ENGINES:
        pending["default_engine"] = body["engine"]
    # rate/pitch differ from the name fields on purpose: their validators own a
    # documented degrade-to-default contract (a numeric "rate": 100 is a mundane
    # typo, not a refusable request), and they are shape-checked against a percent
    # pattern, so a wrong value can never persist as an opaque string.
    if "rate" in body:
        pending["default_rate"] = _validate_rate(body["rate"])
    if "pitch" in body:
        pending["default_pitch"] = _validate_pitch(body["pitch"])
    for _flag, _flag_attr in (("enabled", "global_enabled"), ("autoSpeak", "auto_speak")):
        if _flag in body:
            _flag_value = validated_config_bool(body[_flag])
            if _flag_value is None:
                return web.json_response(
                    {"error": f"{_flag} must be a boolean", "code": "field_not_boolean"},
                    status=400,
                )
            pending[_flag_attr] = _flag_value
    # Every string field goes through one validator rather than str(): stringifying
    # persists a dict as the literal "{}" -- a voice name or binary path that no
    # engine can satisfy, so synthesis then returns silence with nothing in the
    # config that looks wrong. Rejecting is right rather than coercing to "",
    # because "" already means something here (the OS default voice).
    for _field, _attr in (
        ("aws_profile", "aws_profile"),
        ("region", "region"),
        ("piper_binary", "piper_binary"),
        ("piper_model", "piper_model"),
        ("piper_model_config", "piper_model_config"),
        ("system_voice", "system_voice"),
    ):
        if _field in body:
            _value = validated_config_string(body[_field])
            if _value is None:
                return web.json_response(
                    {"error": f"{_field} must be a string", "code": "field_not_string"},
                    status=400,
                )
            pending[_attr] = _value
    if "piper_length_scale" in body:
        # Coerce to finite/positive via the shared validator (rejects non-numeric,
        # inf/NaN, and <=0) so a bad value can't reach synthesis or be persisted
        # as unserializable JSON that breaks the browser's config GET.
        pending["piper_length_scale"] = validate_length_scale(body["piper_length_scale"])

    for _attr, _new in pending.items():
        setattr(_vc, _attr, _new)

    # Persist to config.json. MERGE into the existing voice_reply block rather
    # than rewriting it wholesale — the loader (slack/handler.py) also reads
    # auto_speak / auto_reply_to_voice from here, and a wholesale rewrite would
    # silently drop any key not in this handler's set.
    try:
        cfg_path = config_path()
        with open(cfg_path) as f:
            cfg = json.load(f)
        vr = cfg.get("voice_reply")
        if not isinstance(vr, dict):
            vr = {}
        vr.update(
            {
                "enabled": _vc.global_enabled,
                "auto_speak": _vc.auto_speak,
                "provider": _vc.provider,
                "voice_id": _vc.default_voice,
                "engine": _vc.default_engine,
                "rate": _vc.default_rate,
                "pitch": _vc.default_pitch,
                "aws_profile": _vc.aws_profile,
                "region": _vc.region,
                "piper_binary": _vc.piper_binary,
                "piper_model": _vc.piper_model,
                "piper_model_config": _vc.piper_model_config,
                "piper_length_scale": _vc.piper_length_scale,
                "system_voice": _vc.system_voice,
            }
        )
        cfg["voice_reply"] = vr
        with open(cfg_path, "w") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        logger.exception("Failed to persist voice config")

    return web.json_response({"ok": True})


async def api_voice_synthesize(request: web.Request) -> web.Response:
    """POST /api/voice/synthesize — identified, cancellable provider streaming."""
    body, body_err = await read_bounded_json(request)
    if body_err is not None:
        return body_err
    assert body is not None  # read_bounded_json returns (dict, None) on success

    if not isinstance(body.get("text"), str):
        return web.json_response(
            {"error": "text required", "code": "voice_invalid_request"}, status=400
        )
    text = body["text"].strip()
    slot_key = body.get("slot", "")
    request_id = body.get("request_id", uuid.uuid4().hex)
    if (
        not text
        or len(text) > _MAX_SYNTHESIS_TEXT_CHARS
        or not isinstance(slot_key, str)
        or not isinstance(request_id, str)
        or not 0 < len(request_id) <= _MAX_REQUEST_ID_CHARS
        or any(
            key in body and not isinstance(body[key], str)
            for key in ("voice", "engine", "rate", "pitch")
        )
    ):
        return web.json_response(
            {"error": "invalid synthesis request", "code": "voice_invalid_request"}, status=400
        )
    jobs = request.app[_VOICE_REQUESTS]
    jobs.prune()
    key = (slot_key, request_id)
    if key in jobs.cancelled:
        return web.json_response(
            {"ok": False, "code": "voice_cancelled", "request_id": request_id}, status=409
        )
    if key in jobs.tasks or len(jobs.tasks) >= _MAX_ACTIVE_SYNTHESES:
        return web.json_response(
            {"error": "voice synthesis is busy", "code": "voice_busy", "request_id": request_id},
            status=429,
        )
    task = asyncio.current_task()
    assert task is not None
    jobs.tasks[key] = task
    try:
        return await _synthesize_request(
            request.app["state"], body, text, slot_key, request_id, jobs.piper
        )
    finally:
        jobs.tasks.pop(key, None)


async def _synthesize_request(
    state: DashboardState,
    body: dict,
    text: str,
    slot_key: str,
    request_id: str,
    piper: PiperRuntime,
) -> web.Response:
    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    identity = {"slot": slot_key, "request_id": request_id}

    # Voice config — use defaults from handler config or body overrides
    voice_id = body.get("voice", _vc.default_voice)
    engine = body.get("engine", _vc.default_engine)
    rate = body.get("rate", _vc.default_rate)
    pitch = body.get("pitch", _vc.default_pitch)

    chunk_paths: list[str] = []
    final_path: str | None = None
    try:
        if _vc.provider == PROVIDER_PIPER:
            return await _synthesize_piper_stream(state, text, identity, piper)
        # Only explicitly selected Polly may reach the paid sentence stream.
        # The built-in engine retains its provider-aware single-WAV path.
        if _vc.provider != PROVIDER_POLLY:
            return await _synthesize_nonstreaming(state, text, identity)
        async for idx, sentence, mp3_bytes in streaming_voice_reply(
            text,
            voice_id=voice_id,
            engine=engine,
            rate=rate,
            pitch=pitch,
            aws_profile=_vc.aws_profile,
            region=_vc.region,
        ):
            # Save chunk for stitching
            chunk_path = await asyncio.to_thread(_spill_chunk, mp3_bytes)
            chunk_paths.append(chunk_path)

            # Broadcast to dashboard for immediate playback
            state.broadcast_ws(
                "voice_chunk",
                {
                    **identity,
                    "index": idx,
                    "sentence": sentence,
                    "audio": base64.b64encode(mp3_bytes).decode(),
                    "audioMime": "audio/mpeg",
                },
            )

        # Stitch all chunks into single MP3
        if chunk_paths:
            final_path = await stitch_mp3s(chunk_paths)
            if final_path:
                final_bytes = await asyncio.to_thread(_read_audio, final_path)
                state.broadcast_ws(
                    "voice_complete",
                    {
                        **identity,
                        "audio": base64.b64encode(final_bytes).decode(),
                        "chunks": len(chunk_paths),
                        "audioMime": "audio/mpeg",
                    },
                )

        if not chunk_paths:
            raise VoiceSynthesisError(
                "voice_unavailable", "The selected voice provider returned no audio."
            )
        return web.json_response({"ok": True, "chunks": len(chunk_paths), "request_id": request_id})
    except SandboxUnavailableError as exc:
        # Polly reaches the sandbox through this branch, so without its own clause
        # the refusal would fall to the generic handler below -- a 500 with no
        # notification, leaving the remedy invisible on a Polly host exactly as it
        # was before. Same helper as the non-streaming branch, so both report
        # identically.
        logger.exception("Voice synthesis was refused by the sandbox")
        return _sandbox_refusal_response(state, identity, exc)
    except Exception as exc:
        logger.exception("Voice synthesis failed")
        err_msg, _ = redact_exfiltration_urls(str(exc))
        err_msg, _ = redact_credentials(err_msg)
        state.broadcast_ws(
            "voice_error",
            {**identity, "error": err_msg, "code": getattr(exc, "code", "voice_synthesis_failed")},
        )
        return web.json_response(
            {
                "ok": False,
                "error": err_msg,
                "code": getattr(exc, "code", "voice_synthesis_failed"),
                "request_id": request_id,
            },
            status=502,
        )
    finally:
        if final_path:
            with contextlib.suppress(OSError):
                os.unlink(final_path)
        for p in chunk_paths:
            with contextlib.suppress(OSError):
                os.unlink(p)


async def _synthesize_piper_stream(
    state: DashboardState, text: str, identity: dict[str, str], piper: PiperRuntime
) -> web.Response:
    """Send playable PCM chunks immediately and retain one bounded replay clip."""
    pcm = bytearray()
    chunks = 0
    sample_rate = 0
    stream = streaming_piper_reply(
        text,
        piper_binary=_vc.piper_binary,
        piper_model=_vc.piper_model,
        piper_model_config=_vc.piper_model_config,
        length_scale=_vc.piper_length_scale,
        runtime=piper,
        request_id=identity["request_id"],
    )
    # Explicit close is essential when the request is cancelled while the
    # generator is suspended at yield: its child still belongs to this request.
    async with contextlib.aclosing(stream):
        async for index, sample_rate, audio in stream:
            pcm.extend(audio)
            chunks += 1
            state.broadcast_ws(
                "voice_chunk",
                {
                    **identity,
                    "index": index,
                    "sentence": text if index == 0 else "",
                    "audio": base64.b64encode(pcm_to_wav(audio, sample_rate)).decode("ascii"),
                    "audioMime": "audio/wav",
                },
            )
    if not chunks:
        raise VoiceSynthesisError("voice_unavailable", "Piper returned no speakable audio.")

    def replay() -> str:
        return base64.b64encode(pcm_to_wav(bytes(pcm), sample_rate)).decode("ascii")

    audio_b64 = await asyncio.to_thread(replay)
    state.broadcast_ws(
        "voice_complete",
        {**identity, "audio": audio_b64, "chunks": chunks, "audioMime": "audio/wav"},
    )
    return web.json_response({"ok": True, "chunks": chunks, "request_id": identity["request_id"]})


async def _synthesize_nonstreaming(
    state: DashboardState, text: str, identity: dict[str, str]
) -> web.Response:
    """Keep the built-in engine's single-file synthesis and failure remedies.

    Its chunk, completion, and errors share the request identity used by Piper
    and Polly, so interruption and late-frame rejection apply to every provider.
    """
    audio_path: str | None = None
    provider = _vc.provider
    try:
        audio_path = await synthesize_speech(
            text,
            provider=provider,
            rate=_vc.default_rate,
            piper_binary=_vc.piper_binary,
            piper_model=_vc.piper_model,
            piper_model_config=_vc.piper_model_config,
            length_scale=_vc.piper_length_scale,
            system_voice=_vc.system_voice,
        )
        if not audio_path:
            # The two local providers fail for unrelated reasons, and a wrong
            # remedy costs the user the whole debugging session. For the system
            # provider that means PROBING rather than assuming: synthesis also
            # returns None with an engine present — a stale persisted
            # ``system_voice`` the engine rejects, a timeout, a sandbox refusal —
            # and telling that user to install espeak-ng sends them to fix
            # something that is not broken.
            if provider == PROVIDER_SYSTEM:
                resolved = await resolve_system_tts_async()
                if resolved is None:
                    msg = (
                        "System TTS unavailable — this host has no built-in speech "
                        "engine. Install espeak-ng, or pick another provider in "
                        "Voice settings."
                    )
                else:
                    msg = (
                        f"System TTS failed — the host's {resolved[0]} engine is "
                        "installed but produced no audio. Check the voice and "
                        "speed in Voice settings, or see the gateway log."
                    )
            else:
                msg = (
                    "Piper TTS unavailable — check the piper binary and model "
                    "path in Voice settings."
                )
            code = "voice_synthesis_failed"
            if provider == PROVIDER_SYSTEM and resolved is None:
                code = "voice_unavailable"
            state.broadcast_ws("voice_error", {**identity, "error": msg, "code": code})
            return web.json_response(
                {"ok": False, "error": msg, "code": code, "request_id": identity["request_id"]},
                status=502,
            )
        audio_bytes = await asyncio.to_thread(_read_audio, audio_path)
        audio_b64 = base64.b64encode(audio_bytes).decode()
        state.broadcast_ws(
            "voice_chunk",
            {
                **identity,
                "index": 0,
                "sentence": text,
                "audio": audio_b64,
                "audioMime": "audio/wav",
            },
        )
        state.broadcast_ws(
            "voice_complete",
            {**identity, "audio": audio_b64, "chunks": 1, "audioMime": "audio/wav"},
        )
        return web.json_response({"ok": True, "chunks": 1, "request_id": identity["request_id"]})
    except SandboxUnavailableError as exc:
        # The default `system` engine reaches the sandbox through here; piper has its
        # own streaming path. Reporting lives in the shared helper so this branch and
        # the streaming one cannot diverge.
        logger.exception("Voice synthesis was refused by the sandbox")
        return _sandbox_refusal_response(state, identity, exc)
    except Exception as exc:
        logger.exception("Local voice synthesis failed")
        err_msg, _ = redact_exfiltration_urls(str(exc))
        err_msg, _ = redact_credentials(err_msg)
        code = getattr(exc, "code", "voice_synthesis_failed")
        state.broadcast_ws("voice_error", {**identity, "error": err_msg, "code": code})
        return web.json_response(
            {"ok": False, "error": err_msg, "code": code, "request_id": identity["request_id"]},
            status=502,
        )
    finally:
        if audio_path:
            with contextlib.suppress(OSError):
                os.unlink(audio_path)


# ── Voices list (cached) ──

_voices_cache: list[dict] | None = None
_voices_cache_ts: float = 0.0
_VOICES_CACHE_TTL = 3600  # 1 hour

_system_voices_cache: list[dict[str, str]] | None = None
_system_voices_cache_ts: float = 0.0


async def api_voice_system_voices(request: web.Request) -> web.Response:
    """GET /api/voice/system-voices — the host engine's voices (cached 1h).

    ``available`` is what the panel needs to distinguish "this host has no
    built-in engine" from "the engine has no selectable voices": the first is a
    Linux box without espeak-ng and needs an install, the second is a working
    engine the user simply cannot pick a voice on.
    """
    global _system_voices_cache, _system_voices_cache_ts

    if await resolve_system_tts_async() is None:
        return web.json_response({"available": False, "voices": []})

    now = time.time()
    if _system_voices_cache is not None and (now - _system_voices_cache_ts) < _VOICES_CACHE_TTL:
        return web.json_response({"available": True, "voices": _system_voices_cache})
    try:
        voices = await list_system_voices()
    except SystemVoiceProbeError:
        # Named rather than swallowed by the broad catch below: this is the one
        # failure the panel can act on, and the code it returns claims exactly
        # that the probe failed.
        logger.warning("System voice enumeration failed")
        return web.json_response(
            {"error": "Failed to retrieve voices", "code": "system_voices_probe_failed"},
            status=502,
        )
    except Exception:
        logger.exception("System voice enumeration raised unexpectedly")
        return web.json_response(
            {"error": "Failed to retrieve voices", "code": "system_voices_probe_failed"},
            status=502,
        )
    # Cached even when empty: the answer is stable for the life of the install,
    # and re-probing on every panel open costs a subprocess per visit.
    _system_voices_cache = voices
    _system_voices_cache_ts = now
    return web.json_response({"available": True, "voices": voices})


async def api_voice_voices(request: web.Request) -> web.Response:
    """GET /api/voice/voices — list available Polly voices (cached 1h)."""
    global _voices_cache, _voices_cache_ts

    now = time.time()
    if _voices_cache is not None and (now - _voices_cache_ts) < _VOICES_CACHE_TTL:
        return web.json_response({"voices": _voices_cache})

    # The catalogue lives behind a paid provider, so two gates come before the
    # subprocess. Neither may be dropped: without them the only thing stopping
    # this endpoint from calling AWS is the frontend declining to fetch it while
    # Piper is selected, so any other client — or a direct request — reaches
    # `aws polly describe-voices` against whatever the ambient credential chain
    # resolves to.
    #
    # 1. Not the active provider: a Piper user has no business shipping a
    #    request to Polly at all.
    if _vc.provider != PROVIDER_POLLY:
        return web.json_response({"voices": []})
    # 2. Polly IS selected but unconfirmed. Same empty list: the operator-facing
    #    explanation is the consent card's job (it has its own GET carrying the
    #    reason), so returning a second copy here would be a response field with
    #    no reader. Routed through ``refuse_and_log`` rather than ``authorize``
    #    so the denial reaches the tamper-evident audit log like every other
    #    gated call site -- a denial that only logs is a denial an incident
    #    review cannot see.
    if not await aws_consent.refuse_and_log(
        aws_consent.SERVICE_POLLY, profile=_vc.aws_profile, region=_vc.region
    ):
        return web.json_response({"voices": []})

    aws_bin = await asyncio.to_thread(resolve_polly_cli)
    if aws_bin is None:
        # Polly voice listing needs the AWS CLI, which is optional (the
        # default Piper provider works without it). Resolution goes through
        # the deploy engine's shared well-known-dirs resolver, so a gateway
        # running under launchd with a minimal PATH still finds a Homebrew /
        # official-pkg install. When the CLI genuinely is not
        # installed, degrade to an empty list instead of a 500 + traceback.
        # Not cached, so the list recovers as soon as `aws` becomes
        # resolvable. The probe runs in a thread so a wedged network mount
        # on PATH cannot stall the event loop.
        logger.info("aws CLI not resolvable — returning empty voices list")
        return web.json_response({"voices": []})

    cmd = [aws_bin, "polly", "describe-voices", "--output", "json"]
    if _vc.aws_profile:
        cmd += ["--profile", _vc.aws_profile]
    if _vc.region:
        cmd += ["--region", _vc.region]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            err = stderr.decode().strip()
            logger.error("describe-voices failed: %s", err)
            return web.json_response({"error": "Failed to retrieve voices"}, status=502)

        data = json.loads(stdout)
        voices = [
            {
                "id": v["Id"],
                "name": v["Name"],
                "language": v["LanguageName"],
                "languageCode": v["LanguageCode"],
                "gender": v["Gender"],
                "engines": v["SupportedEngines"],
            }
            for v in data.get("Voices", [])
        ]
        voices.sort(key=lambda v: (v["languageCode"], v["name"]))
        _voices_cache = voices
        _voices_cache_ts = now
        return web.json_response({"voices": voices})
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        # Reap via communicate(), not wait(): wait_for cancelled the pipe
        # readers before the kill landed, so a child blocked writing to a
        # full stderr PIPE is never drained and wait() can hang the request
        # handler indefinitely.
        await proc.communicate()
        return web.json_response({"error": "timeout"}, status=504)
    except FileNotFoundError:
        # Defense-in-depth behind the which() guard above: exec can still
        # fail with ENOENT — the binary was removed between the check and
        # the spawn, or `aws` is a script whose interpreter is missing.
        # Same graceful degrade as the guard, with the exception logged so
        # the non-PATH causes stay diagnosable.
        logger.info(
            "aws CLI could not be executed — returning empty voices list",
            exc_info=True,
        )
        return web.json_response({"voices": []})
    except Exception:
        logger.exception("describe-voices error")
        return web.json_response({"error": "Failed to retrieve voices"}, status=500)
