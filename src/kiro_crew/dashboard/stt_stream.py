"""Streaming STT WebSocket endpoint.

Forwards 16 kHz Int16 PCM audio chunks from the browser to AWS Transcribe
Streaming and relays partial + final transcripts back to the client.
Requires ``stt.provider == 'transcribe'`` and ``stt.streaming == true``.
Whisper users fall back to the existing batch path at ``/api/stt/transcribe``.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import WSMsgType, web

# Streaming-path dep. Declared in Config + setup.cfg, but keep the module
# importable without it so a stale-env gateway still starts and cli_doctor
# can diagnose. api_ws_stt() re-checks and returns a friendly WS error.
try:
    from amazon_transcribe.client import TranscribeStreamingClient
except ImportError:  # pragma: no cover — exercised by test_import_error_*
    TranscribeStreamingClient = None  # type: ignore[assignment,misc]

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.origin import check_origin
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.transcribe import _ProfileCredentialResolver

logger = logging.getLogger(__name__)

# Browser AudioWorklet downsamples to 16 kHz Int16 PCM mono.
STREAM_SAMPLE_RATE_HZ = 16000
# Providers with a live streaming implementation behind this endpoint. `transcribe`
# streams from AWS; `apple` streams on-device via SpeechAnalyzer (macOS 26+). The
# other providers (`whisper`, `mlx`) are whole-file CLIs with no partial-result
# channel, so they stay on the batch POST /api/stt/transcribe path.
_STREAMING_PROVIDERS = ("transcribe", "apple")
# Cap per-WebSocket-frame size (128 KiB) — small enough to reject obvious
# abuse, large enough for any reasonable 16 kHz PCM chunk cadence.
_MAX_WS_MSG_SIZE = 128 * 1024
# Cap total session duration (seconds). Transcribe bills per audio-second
# ($0.024/min), so an abandoned or malicious connection left open could
# rack up unbounded cost. 5 min covers realistic dictation; longer
# sessions require explicit reconnect.
_MAX_STREAM_DURATION_SECS = 300
# Cap text-frame size — the only valid text frame is `{"type":"stop"}`
# (15 bytes). Reject obvious abuse without the 128 KiB binary cap.
_MAX_TEXT_FRAME_BYTES = 256
# Cap concurrent Transcribe streaming sessions per-process. Each open
# WebSocket spawns a billable Transcribe session; without a cap, a single
# user opening many tabs or an attacker past the origin check can
# multiply cost and exhaust the account's concurrent-stream quota.
# Safe as a plain int on the single-threaded asyncio loop.
_MAX_CONCURRENT_SESSIONS = 3
_active_sessions = 0

# ── Semantic endpointing (stt.endpointing, default off) ──
# On each stable Transcribe `final`, a fast background model judges whether the
# user has finished a complete request; a COMPLETE verdict emits an `endpoint`
# frame so the frontend can auto-submit. "auto" inherits the session's governed
# default (run_bg_oneliner skips the override for auto) — a hardcoded model id
# 400s on accounts/partitions that do not serve it.
# Debounced so mid-utterance finals don't each fire a model call, and
# single-flight so at most one bg call runs at once.
_ENDPOINT_MODEL = "auto"
_ENDPOINT_DEBOUNCE_SECS = 0.35
_ENDPOINT_TIMEOUT_SECS = 5.0
_ENDPOINT_PROMPT = (
    "You decide whether a person has FINISHED speaking a complete request or "
    "thought, from a live speech-to-text transcript that may cut off mid-word.\n"
    "Reply with EXACTLY one word and nothing else:\n"
    "COMPLETE — the utterance is a finished, actionable request or statement.\n"
    "INCOMPLETE — the speaker is mid-sentence, trailing off, or clearly about "
    "to continue.\n\nTranscript:\n{transcript}"
)


def _emit_end_audit(caller: str, *, outcome: str) -> None:
    """Log ``stt_stream_end`` defensively.

    All three exit paths (ImportError, Transcribe start-failure, normal
    ``finally``) must emit this event or the audit trail shows
    unmatched ``stt_stream_start`` entries. Never raise: a failing SEL
    call must not short-circuit the caller's ``return ws``.
    """
    try:
        sel().log_api_access(
            caller=caller,
            operation="stt_stream_end",
            outcome=outcome,
            resources="/api/ws/stt",
        )
    except Exception:
        logger.exception("Failed to emit stt_stream_end SEL audit")


async def _close_and_end_audit(ws: web.WebSocketResponse, caller: str, *, outcome: str) -> None:
    """Emit ``stt_stream_end``, then close *ws*, on an early-return path.

    Order matters, and it is audit-first on purpose.
    ``WebSocketResponse.close()`` awaits the peer's close acknowledgement under
    its own timeout (10s by default), so a client that has already gone away —
    an abrupt disconnect, a closed tab, or a test client that read the error
    frame and left — parks the handler inside ``close()``. With the audit after
    the close, ``stt_stream_end`` is withheld for as long as that takes, leaving
    a start with no end in the trail for up to the full timeout. Emitting first
    makes the audit independent of the peer, which is the property the balanced
    trail actually needs; the close still runs (and is still awaited) right
    after, so nothing leaks.

    ``_emit_end_audit`` never raises, so the close is always reached.
    """

    _emit_end_audit(caller, outcome=outcome)
    try:
        await ws.close()
    except Exception:
        # A broken transport must not turn an already-audited early return into
        # a 500 — the balanced trail is the invariant, the close is best-effort.
        logger.exception("Failed to close STT WebSocket on early return")


def _emit_guard_audit(caller: str, *, outcome: str) -> None:
    """Log ``stt_stream_rejected`` defensively on guard-path rejections.

    If ``sel()`` or ``log_api_access`` raises (e.g. SEL not initialized),
    the exception must not propagate — otherwise the intended
    ``HTTPForbidden``/``HTTPServiceUnavailable`` is replaced by a 500.
    """
    try:
        sel().log_api_access(
            caller=caller,
            operation="stt_stream_rejected",
            outcome=outcome,
            resources="/api/ws/stt",
        )
    except Exception:
        logger.exception("Failed to emit stt_stream_rejected SEL audit")


class _Endpointer:
    """Debounced, single-flight semantic end-of-utterance detector.

    On each Transcribe ``final`` the accumulated transcript is scheduled for a
    Haiku-class COMPLETE/INCOMPLETE judgment. A monotonic generation counter
    gives the debounce (a scheduled task aborts if a newer ``final`` arrived
    during its wait) and staleness guard (a verdict is discarded if the user
    kept speaking while it was classifying). An ``_inflight`` flag caps cost at
    one background model call at a time. A COMPLETE verdict emits
    ``{"type":"endpoint","complete":true}`` so the frontend can auto-submit.

    ``sessions`` is duck-typed (anything exposing ``get_bg_session()`` — the
    ``SessionManager``) so this stays free of a dashboard->session import cycle.
    Best-effort throughout: any failure logs at debug and never disrupts the
    live transcript stream.
    """

    def __init__(
        self,
        ws: web.WebSocketResponse,
        sessions: object,
        *,
        model: str = _ENDPOINT_MODEL,
        debounce: float = _ENDPOINT_DEBOUNCE_SECS,
        timeout: float = _ENDPOINT_TIMEOUT_SECS,
    ) -> None:
        self._ws = ws
        self._sessions = sessions
        self._model = model
        self._debounce = debounce
        self._timeout = timeout
        self._finals: list[str] = []
        self._gen = 0
        self._inflight = False
        # Latched (gen, transcript) for the LATEST final that arrived while a
        # classification was already running — re-run once it finishes rather
        # than silently dropped (that drop stranded the terminal final so
        # auto-submit never fired).
        self._pending: "tuple[int, str] | None" = None
        self._tasks: "set[asyncio.Task]" = set()  # type: ignore[type-arg]

    def note_partial(self, text: str) -> None:
        """A live partial means the user is STILL speaking, so invalidate any
        pending/in-flight verdict — it was computed for an earlier, now-
        superseded transcript — WITHOUT scheduling a new classification (only
        stable finals are worth a model call). Advancing the generation makes
        both the debounce wait and the post-classify staleness check discard the
        stale verdict, so a COMPLETE for "deploy the service" can't auto-submit
        after the user has gone on to say "to production"."""
        if text:
            self._gen += 1

    def note_final(self, text: str) -> None:
        """Record a stable transcript segment and schedule a debounced judgment.

        An empty final is ignored ENTIRELY (before touching ``_gen``): it adds
        nothing to classify, and bumping the generation would invalidate a good
        pending verdict while scheduling no successor — stranding auto-submit."""
        if not text:
            return
        self._finals.append(text)
        self._gen += 1
        gen = self._gen
        transcript = " ".join(self._finals).strip()
        if not transcript:
            return
        self._schedule(gen, transcript)

    def _schedule(self, gen: int, transcript: str) -> None:
        task = asyncio.create_task(self._classify(gen, transcript))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _classify(self, gen: int, transcript: str) -> None:
        try:
            await asyncio.sleep(self._debounce)
        except asyncio.CancelledError:
            return
        if gen != self._gen:
            return  # a newer partial/final superseded this one (debounce coalesce)
        if self._inflight:
            # Another classification is in flight. Latch this (the current) gen
            # so it re-runs after that one completes instead of being dropped —
            # otherwise the last final of a continuous utterance is lost and
            # auto-submit never fires.
            self._pending = (gen, transcript)
            return
        self._inflight = True
        verdict = ""
        try:
            verdict = await run_bg_oneliner(
                self._sessions,
                _ENDPOINT_PROMPT.format(transcript=transcript),
                model=self._model,
                sel_source="stt_endpointing",
                timeout=self._timeout,
            )
        except Exception:
            logger.debug("stt endpointing classification failed", exc_info=True)
        finally:
            self._inflight = False
        # Re-run a superseded final that collided with this in-flight call, if it
        # is still the current generation and the socket is open.
        pending = self._pending
        self._pending = None
        if pending is not None and pending[0] == self._gen and not self._ws.closed:
            self._schedule(pending[0], pending[1])
        if gen != self._gen:
            return  # user kept speaking while classifying — verdict is stale
        if verdict.strip().upper().startswith("COMPLETE") and not self._ws.closed:
            try:
                await self._ws.send_json({"type": "endpoint", "complete": True})
            except Exception:
                logger.debug("stt endpoint frame send failed", exc_info=True)

    async def aclose(self) -> None:
        """Cancel any in-flight judgment tasks on stream teardown.

        ``gather(return_exceptions=True)`` collects each task's own
        ``CancelledError``/exception WITHOUT re-raising, so a cancellation of the
        enclosing ``api_ws_stt`` task (e.g. gateway shutdown) awaiting here is
        NOT swallowed — it propagates as it should."""
        tasks = list(self._tasks)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _make_handler(ws: web.WebSocketResponse, endpointer: "_Endpointer | None" = None):  # type: ignore[no-untyped-def]
    """Build a TranscriptResultStreamHandler that forwards events to ``ws``.

    Both partials and finals pass through ``redact_credentials`` and
    ``redact_exfiltration_urls`` before they leave the process — a
    partial flashed in the browser counts as an external surface even
    though it is replaced by the next partial and never persisted.
    """
    from amazon_transcribe.handlers import TranscriptResultStreamHandler
    from amazon_transcribe.model import TranscriptEvent

    class Handler(TranscriptResultStreamHandler):
        async def handle_transcript_event(self, event: TranscriptEvent) -> None:
            if ws.closed:
                return
            for result in event.transcript.results:
                if not result.alternatives:
                    continue
                text = result.alternatives[0].transcript
                redacted, _ = redact_exfiltration_urls(text)
                redacted, _ = redact_credentials(redacted)
                try:
                    if result.is_partial:
                        # Invalidate any pending end-of-utterance verdict BEFORE
                        # the awaited send: a live partial means the user is
                        # still speaking, and doing it after `await send_json`
                        # leaves a window where that await yields and a stale
                        # COMPLETE emits, auto-submitting a truncated request.
                        if endpointer is not None:
                            endpointer.note_partial(redacted)
                        await ws.send_json({"type": "partial", "text": redacted})
                    else:
                        # Feed the stable segment to the endpointer BEFORE the
                        # awaited send (same yield-window reason as above). Uses
                        # the SAME redacted text sent to the client, so the model
                        # never sees an unredacted credential the wire didn't.
                        if endpointer is not None:
                            endpointer.note_final(redacted)
                        await ws.send_json({"type": "final", "text": redacted})
                except Exception:
                    # Client disconnected mid-send. Stop processing rather
                    # than flooding the log with per-event tracebacks.
                    return

    return Handler


async def _run_apple_session(
    ws: web.WebSocketResponse,
    cfg: "KiroCrewConfig",
    request: web.Request,
    caller: str,
) -> None:
    """Drive a live on-device dictation session over an already-prepared WebSocket.

    Emits exactly the same event shapes as the AWS path — ``ready`` / ``partial`` /
    ``final`` — so the frontend needs no branch. Redaction is applied to partials as
    well as finals: a partial is flashed into the browser DOM, which makes it an
    external surface even though the next partial replaces it.

    No billing deadline here (nothing is metered) but the duration cap still applies:
    an abandoned session holds a helper process and a recognition session open.
    """
    from kiro_crew import apple_speech

    endpointer: "_Endpointer | None" = None
    if cfg.stt.endpointing:
        _state = request.app.get("state")
        _sessions = getattr(_state, "sessions", None)
        if _sessions is not None:
            endpointer = _Endpointer(ws, _sessions)

    session = apple_speech.StreamingSession(
        locale=cfg.stt.language_code or "en-US",
        sample_rate=STREAM_SAMPLE_RATE_HZ,
    )
    problem = await session.start()
    if problem:
        try:
            await ws.send_json({"type": "error", "message": problem})
        except Exception:
            pass
        await _close_and_end_audit(ws, caller, outcome="error")
        if endpointer is not None:
            await endpointer.aclose()
        return

    async def relay() -> None:
        """Forward helper events to the client until the helper's stream ends."""
        async for event in session.events():
            kind = event.get("type")
            if kind == "error":
                # A fatal helper error must NOT be swallowed: the helper stops
                # producing after one, so dropping it leaves the client watching a
                # live socket that will never transcribe again, with no signal and
                # no way to tell that from a quiet microphone.
                #
                # Claim the cause BEFORE any awaiting work — the same discipline as
                # the deadline task below — so the `finally` audits this as `error`
                # instead of a clean stop, and so a deadline firing concurrently
                # cannot overwrite the true first cause. Closing is what ends the
                # read loop; the close in `_close_and_end_audit` is idempotent, so
                # the single audit still comes from the one owner, the `finally`.
                msg_text = str(event.get("message", "speech helper failed"))
                _claim_fatal("error")
                if not ws.closed:
                    try:
                        await ws.send_json({"type": "error", "message": msg_text})
                    except Exception:
                        pass
                    try:
                        await ws.close()
                    except Exception:
                        pass
                return
            if kind not in ("partial", "final"):
                continue
            # Both redactions, in the same order as the AWS handler: a partial is
            # flashed into the browser DOM, so it is an external surface even though
            # the next partial replaces it and nothing is persisted.
            redacted, _ = redact_exfiltration_urls(str(event.get("text", "")))
            redacted, _ = redact_credentials(redacted)
            # Strip edge whitespace: Apple's finals carry a leading space (" Then
            # tell me..."), and the frontend re-joins accumulated finals with a
            # space of its own, so passing it through yields double spaces.
            redacted = redacted.strip()
            if not redacted:
                continue
            if endpointer is not None:
                # Note BEFORE the awaited send, matching the AWS handler: a live
                # partial means the user is still speaking, and doing this after
                # `await send_json` would let a slow send delay invalidation.
                if kind == "partial":
                    endpointer.note_partial(redacted)
                else:
                    endpointer.note_final(redacted)
            try:
                await ws.send_json({"type": kind, "text": redacted})
            except Exception:
                return

    relay_task = asyncio.create_task(relay())

    # Enforce the duration cap with a dedicated task, NOT an in-loop check. The
    # AWS path below documents the same reason: `async for msg in ws` only yields
    # on client data, and aiohttp answers heartbeat ping/pong internally — so a
    # client that stops sending audio while the socket stays alive (a throttled
    # background tab, a muted input, a client bug) would never evaluate a
    # message-driven deadline. It would hold the StreamTranscribe helper, an OS
    # speech-recognition session, and one of `_MAX_CONCURRENT_SESSIONS` slots
    # indefinitely; three such sockets make dictation 503 until a gateway restart.
    # The FIRST fatal cause wins. Both teardown paths (the duration cap and a fatal
    # helper error) can fire, and each ends the read loop by closing the socket — so
    # without a single claim the second one to run would relabel the first one's
    # outcome in the audit trail. Claimed before any awaiting work, so it cannot be
    # missed; `None` means the session ended normally.
    fatal_outcome: str | None = None

    def _claim_fatal(kind: str) -> None:
        nonlocal fatal_outcome
        if fatal_outcome is None:
            fatal_outcome = kind

    async def _enforce_deadline() -> None:
        await asyncio.sleep(_MAX_STREAM_DURATION_SECS)
        # Only the first claimant sends: otherwise the cap and a concurrent helper
        # error each emit a frame in the window before the other's close lands, and
        # the client sees two contradictory errors for one failure.
        if fatal_outcome is not None:
            return
        _claim_fatal("timeout")
        if not ws.closed:
            try:
                await ws.send_json({"type": "error", "message": "max stream duration exceeded"})
            except Exception:
                pass
            # Same defensive shape as the AWS path: ws.close() can raise on a
            # broken transport, and an unhandled exception here would surface as
            # "Task exception was never retrieved".
            try:
                await ws.close()
            except Exception:
                pass

    deadline_task = asyncio.create_task(_enforce_deadline())
    outcome = "ok"
    try:
        await ws.send_json({"type": "ready"})
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                if not await session.feed(msg.data):
                    # The helper died mid-dictation. Breaking alone would audit this
                    # as a clean stop and leave the client believing it is still
                    # recording, with everything it says from here silently dropped —
                    # the same failure mode as swallowing an `error` event, reached
                    # through the write side instead of the read side.
                    logger.warning("apple streaming helper stopped accepting audio")
                    _claim_fatal("error")
                    if not ws.closed:
                        try:
                            await ws.send_json(
                                {"type": "error", "message": "speech helper stopped"}
                            )
                        except Exception:
                            pass
                    break
            elif msg.type == WSMsgType.TEXT:
                if len(msg.data) > _MAX_TEXT_FRAME_BYTES:
                    logger.warning(
                        "Oversized text frame (%d bytes) on /api/ws/stt — closing",
                        len(msg.data),
                    )
                    break
                try:
                    ctrl = json.loads(msg.data)
                except ValueError:
                    continue
                if isinstance(ctrl, dict) and ctrl.get("type") == "stop":
                    break
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                break
    except Exception:
        logger.exception("apple streaming STT session failed")
        outcome = "error"
    finally:
        # An EXPLICIT claim, not `deadline_task.done()`. Inferring from task state
        # is racy here: the cap's own `ws.close()` is what ends the read loop, so
        # the `finally` runs while that task is still awaiting the close and
        # `done()` is still False — the teardown would be audited as a clean stop.
        # The claim is made before any awaiting work, so it cannot be missed.
        timed_out = fatal_outcome == "timeout"
        # Cancel first among the cleanup steps: it is the one thing that can still
        # touch the socket, and leaving it live past cleanup would close a socket
        # the next request may already own.
        deadline_task.cancel()
        if timed_out:
            logger.info("apple streaming STT session hit the %ds cap", _MAX_STREAM_DURATION_SECS)
        # Closing stdin is the helper's cue to finalize, so the trailing finals
        # arrive AFTER this — hence finish() before waiting on the relay.
        try:
            await session.finish()
        except Exception:
            logger.warning("apple streaming helper did not finish cleanly", exc_info=True)
        try:
            await asyncio.wait_for(asyncio.shield(relay_task), timeout=3)
        except (asyncio.TimeoutError, Exception):
            relay_task.cancel()
        await session.close()
        if endpointer is not None:
            await endpointer.aclose()
        # A claimed fatal cause outranks the local `outcome`: the read loop can exit
        # cleanly (the cap/relay closed the socket under it) and would otherwise be
        # recorded as "ok" for a session that in fact died.
        await _close_and_end_audit(ws, caller, outcome=fatal_outcome or outcome)


async def api_ws_stt(request: web.Request) -> web.WebSocketResponse:
    """GET /api/ws/stt — streaming speech-to-text.

    Client sends binary PCM frames and text control messages
    (``{"type":"stop"}``). Server emits JSON events
    ``{"type":"partial"|"final"|"error"|"ready", ...}``.
    """
    if not check_origin(request, require=True):
        _emit_guard_audit(request.remote or "unknown", outcome="forbidden")
        raise web.HTTPForbidden(text="WebSocket origin not allowed")

    cfg = KiroCrewConfig.load()
    if not cfg.stt.enabled or cfg.stt.provider not in _STREAMING_PROVIDERS or not cfg.stt.streaming:
        _emit_guard_audit(request.remote or "unknown", outcome="unavailable")
        raise web.HTTPServiceUnavailable(text="streaming STT not enabled")

    global _active_sessions
    if _active_sessions >= _MAX_CONCURRENT_SESSIONS:
        _emit_guard_audit(request.remote or "unknown", outcome="unavailable")
        raise web.HTTPServiceUnavailable(text="too many concurrent STT sessions")
    _active_sessions += 1
    try:
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=_MAX_WS_MSG_SIZE)
        await ws.prepare(request)

        caller = request.remote or "dashboard"
        try:
            sel().log_api_access(
                caller=caller,
                operation="stt_stream_start",
                outcome="ok",
                resources="/api/ws/stt",
            )
        except Exception:
            # Mirror the ImportError / client-construction paths: if the
            # mandatory start audit fails, close the already-prepare()d WS
            # and emit the matching end audit so the trail stays balanced.
            logger.exception("Failed to emit stt_stream_start SEL audit")
            try:
                await ws.send_json({"type": "error", "message": "audit subsystem unavailable"})
            except Exception:
                pass
            await _close_and_end_audit(ws, caller, outcome="error")
            return ws

        if cfg.stt.provider == "apple":
            # On-device path: same client protocol (binary PCM in, partial/final JSON
            # out) and the same endpointer, so the frontend cannot tell them apart.
            # Kept as its own function rather than threaded through the Transcribe
            # setup below, which is entirely AWS-specific (credentials, region,
            # billing deadline).
            await _run_apple_session(ws, cfg, request, caller)
            return ws

        if TranscribeStreamingClient is None:
            # amazon-transcribe not installed at gateway startup. Module-top
            # import fell back to None so the gateway could boot; surface a
            # friendly error here and keep the audit trail balanced.
            await ws.send_json({"type": "error", "message": "amazon-transcribe not installed"})
            await _close_and_end_audit(ws, caller, outcome="error")
            return ws

        try:
            profile = cfg.stt.transcribe_profile or None
            resolver = _ProfileCredentialResolver(profile) if profile else None
            client = TranscribeStreamingClient(
                region=cfg.stt.transcribe_region,
                credential_resolver=resolver,
            )
        except Exception:
            # Invalid profile, bad region, or constructor error. Without
            # this guard the already-prepare()d WS never closes and no
            # stt_stream_end audit emits — audit trail then shows an
            # unmatched stt_stream_start. Mirrors the start_stream path.
            logger.exception("Failed to create Transcribe client")
            await ws.send_json(
                {"type": "error", "message": "failed to create transcription client"}
            )
            await _close_and_end_audit(ws, caller, outcome="error")
            return ws

        stream = None
        try:
            stream = await client.start_stream_transcription(
                language_code=cfg.stt.language_code,
                media_sample_rate_hz=STREAM_SAMPLE_RATE_HZ,
                media_encoding="pcm",
                # Stabilization=high tells Transcribe to commit each word
                # sooner, at the cost of slightly more downstream corrections.
                # For interactive dictation this trades accuracy on the last
                # 1-2 words for noticeably faster per-word rendering — the
                # right tradeoff for the dashboard input box.
                enable_partial_results_stabilization=True,
                partial_results_stability="high",
            )
        except Exception:
            logger.exception("Failed to start Transcribe stream")
            await ws.send_json({"type": "error", "message": "failed to start transcription"})
            await _close_and_end_audit(ws, caller, outcome="error")
            return ws

        # Enforce the bill-cap with a dedicated task, not an in-loop check.
        # `async for msg in ws` only yields on client data; aiohttp handles
        # heartbeat ping/pong internally, so an idle-but-alive client would
        # never trip a message-driven deadline.
        async def _enforce_deadline() -> None:
            await asyncio.sleep(_MAX_STREAM_DURATION_SECS)
            if not ws.closed:
                try:
                    await ws.send_json({"type": "error", "message": "max stream duration exceeded"})
                except Exception:
                    pass
                # Must match defensive style of send_json above: ws.close()
                # can raise on a broken transport. An unhandled exception
                # here would flag the task as done-with-error, spuriously
                # marking `timed_out=True` in the cleanup below and
                # emitting a "Task exception was never retrieved" warning.
                try:
                    await ws.close()
                except Exception:
                    pass

        # Wrap `send_json(ready)` + task creation in the cleanup `try`.
        # If any of these lines raises (most plausibly a client disconnect
        # during Transcribe cold-start), the finally must still run
        # `end_stream()` to release the Transcribe session — otherwise it
        # silently bills and counts against the concurrent-stream quota.
        handler_task = None
        deadline_task = None
        # Build the endpointer once, before the try, so it is always bound in the
        # finally (a raise before assignment would otherwise NameError there).
        # Gated on stt.endpointing + a reachable SessionManager (get_bg_session).
        endpointer: "_Endpointer | None" = None
        if cfg.stt.endpointing:
            _state = request.app.get("state")
            _sessions = getattr(_state, "sessions", None)
            if _sessions is not None:
                endpointer = _Endpointer(ws, _sessions)
        try:
            await ws.send_json({"type": "ready"})

            handler = _make_handler(ws, endpointer)(stream.output_stream)
            handler_task = asyncio.create_task(handler.handle_events())
            deadline_task = asyncio.create_task(_enforce_deadline())

            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    try:
                        await stream.input_stream.send_audio_event(audio_chunk=msg.data)
                    except Exception:
                        logger.exception("Transcribe send_audio_event failed")
                        break
                elif msg.type == WSMsgType.TEXT:
                    if len(msg.data) > _MAX_TEXT_FRAME_BYTES:
                        logger.warning(
                            "Oversized text frame (%d bytes) on /api/ws/stt — closing",
                            len(msg.data),
                        )
                        break
                    try:
                        ctrl = json.loads(msg.data)
                    except ValueError:
                        continue  # ignore non-JSON text frames
                    if isinstance(ctrl, dict) and ctrl.get("type") == "stop":
                        break
                    # Unknown control frames are ignored (forward-compat).
                elif msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
        finally:
            # Deadline fires exactly once and never cancels itself, so done() ⇒ timeout.
            if deadline_task is not None:
                timed_out = deadline_task.done() and not deadline_task.cancelled()
                deadline_task.cancel()
            else:
                timed_out = False
            try:
                await stream.input_stream.end_stream()
            except Exception:
                # Log at WARNING — a leaked Transcribe session counts against
                # the account concurrent-stream limit and silently bills.
                logger.warning("Failed to end Transcribe stream cleanly", exc_info=True)
            if handler_task is not None:
                # 3s is generous for Transcribe's trailing finals after
                # end_stream() — the library normally flushes within
                # ~150ms. A longer wait trades user-visible UI lag for
                # tail-event completeness; given lastPartial-fallback on
                # the frontend, we err on the side of snappy close.
                try:
                    await asyncio.wait_for(asyncio.shield(handler_task), timeout=3)
                except asyncio.TimeoutError:
                    handler_task.cancel()
                    # Swallow the post-cancel InvalidStateError the
                    # amazon-transcribe awscrt pump raises when its
                    # response Future is cancelled mid-write. The session
                    # is already closing; surfacing it adds noise.
                    try:
                        await handler_task
                    except (asyncio.CancelledError, Exception):
                        pass
                except Exception:
                    # Surface mid-stream Transcribe errors (connection drop, credential
                    # expiry) so operators can see why transcription stopped instead of
                    # silently cancelling the task.
                    logger.exception("Transcribe handler task failed")
            # Cancel pending endpointing judgments AFTER the handler drain: a
            # trailing Transcribe final delivered during the drain calls
            # note_final(), which can schedule a fresh task — cancelling before
            # the drain would let that task escape teardown (leak a background
            # session / try to send on a closing ws). The handler task is
            # done/cancelled by here, so no further note_final can fire.
            if endpointer is not None:
                await endpointer.aclose()
            # Audit BEFORE the close, for the reason documented on
            # _close_and_end_audit: ws.close() awaits the peer's close ack under
            # its own timeout, so a client that already went away would otherwise
            # hold stt_stream_end back for up to that long. The close is still
            # awaited right after, and still tolerates a broken transport.
            _emit_end_audit(caller, outcome="timeout" if timed_out else "ok")
            if not ws.closed:
                try:
                    await ws.close()
                except Exception:
                    logger.exception("Failed to close STT WebSocket during cleanup")

        return ws
    finally:
        _active_sessions -= 1
