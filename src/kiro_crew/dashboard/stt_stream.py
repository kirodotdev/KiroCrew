"""Streaming STT WebSocket endpoint.

Forwards 16 kHz Int16 PCM audio chunks from the browser to a recogniser and
relays partial + final transcripts back to the client. Three providers stream
behind this one socket and the client cannot tell them apart: ``local`` (a
resident whisper.cpp recogniser in this process, the default), ``apple``
(on-device SpeechAnalyzer, macOS 26+) and ``transcribe`` (paid AWS). Requires
``stt.streaming == true``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Awaitable, Callable, Iterator

from aiohttp import WSMsgType, web

# Streaming-path dep. Declared in Config + setup.cfg, but keep the module
# importable without it so a stale-env gateway still starts and cli_doctor
# can diagnose. api_ws_stt() re-checks and returns a friendly WS error.
try:
    from amazon_transcribe.client import TranscribeStreamingClient
except ImportError:  # pragma: no cover — exercised by test_import_error_*
    TranscribeStreamingClient = None  # type: ignore[assignment,misc]

from kiro_crew import aws_consent, stt
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.origin import check_origin, mark_audit_claimed
from kiro_crew.llm_helpers import run_bg_oneliner
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.transcribe import _ProfileCredentialResolver, _whisper_language, availability_detail

logger = logging.getLogger(__name__)

# Browser AudioWorklet downsamples to 16 kHz Int16 PCM mono.
STREAM_SAMPLE_RATE_HZ = 16000
#: The on-device whisper.cpp provider, and the default. Named because more than one
#: module has to ask "is this the resident-model path", and the ones that decide
#: whether to download 148 MB of weights must not do it on a bare string literal.
PROVIDER_LOCAL = "local"
# Providers with a live streaming implementation behind this endpoint. `local`
# re-decodes a rolling window on a resident whisper.cpp model in this process,
# `apple` streams on-device via SpeechAnalyzer (macOS 26+), and `transcribe`
# streams from AWS. The default is first.
_STREAMING_PROVIDERS = (PROVIDER_LOCAL, "apple", "transcribe")
# Cap per-WebSocket-frame size (128 KiB) — small enough to reject obvious
# abuse, large enough for any reasonable 16 kHz PCM chunk cadence.
_MAX_WS_MSG_SIZE = 128 * 1024
# Cap total session duration (seconds). What an abandoned or malicious socket
# left open costs differs by provider and is unbounded in every case: on
# `transcribe` it bills per audio-second ($0.024/min), on `apple` it holds a
# helper process and an OS recognition session, on `local` it accumulates
# buffered audio and keeps queueing decodes onto the one shared model. 5 min
# covers realistic dictation; longer sessions require explicit reconnect.
_MAX_STREAM_DURATION_SECS = 300
# Cap text-frame size — the only valid text frame is `{"type":"stop"}`
# (15 bytes). Reject obvious abuse without the 128 KiB binary cap.
_MAX_TEXT_FRAME_BYTES = 256
# Cap concurrent streaming sessions per-process, for all three providers. Only
# `transcribe` carries a cost reason (each open socket is a billable session and
# counts against the account's concurrent-stream quota); the free on-device
# providers are capped for capacity, not money — every `local` session buffers
# its whole utterance and serialises its decodes onto the single resident model,
# so past a few simultaneous speakers partials fall behind the talker instead of
# multiplying threads. Not widened for the free providers: the number that would
# justify a higher cap is a measured one, and nothing here has measured it.
# Safe as a plain int on the single-threaded asyncio loop.
_MAX_CONCURRENT_SESSIONS = 3
_active_sessions = 0

# Ceiling on the one-time model fetch that precedes a first-ever `local` session.
# Separate from the session cap because it is a transfer of up to 1.6 GB rather than
# a dictation. This bounds the WHOLE transfer, which the downloader's own
# `_NETWORK_STALL_TIMEOUT_SECS` does not: that one bounds each socket read, so a
# mirror trickling one byte per timeout window makes progress forever without ever
# stalling. Generous enough for the largest model on a slow link, since the
# alternative to waiting is a first run that cannot succeed.
_MAX_MODEL_PREPARE_SECS = 1800
# How often a download in progress republishes its byte count. This is NOT
# cosmetic: `useMeetingTranscription` arms a 20s stall watchdog on the last frame
# it received and RECONNECTS when it fires, so a single status frame at the start
# of a 148MB fetch would have the client tear the socket down and restart the
# transfer in a loop. Anything comfortably under that watchdog works; this also
# happens to make the progress bar move smoothly.
_MODEL_PROGRESS_INTERVAL_SECS = 2.0

# Machine-readable reasons on every `error` frame this endpoint emits, on all
# three providers. The browser renders localised text, so the English `message` is
# advisory and the code is the contract: an uncoded frame is untranslatable
# English in a 12-language UI. Codes the stt package already owns
# (`stt_extra_missing`, `stt_model_missing`, …) and the availability codes
# `transcribe.availability_detail` returns travel through unchanged rather than
# being remapped, so one vocabulary covers the settings panel and the socket.
_CODE_MAX_DURATION = "stt_max_duration_exceeded"
_CODE_SESSION_FAILED = "stt_session_failed"
# The one condition no existing code names: streaming Transcribe bills per audio
# second, so the socket refuses without a recorded operator grant for this exact
# profile+region. Distinct from `_CODE_SESSION_FAILED` because the fix is an
# operator action in Settings rather than a retry.
_CODE_CONSENT_REQUIRED = "stt_consent_required"

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


def _redacted(text: str) -> str:
    """Apply both transcript redactions, in the one order every provider uses.

    Partials go through this as well as finals: a partial flashed into the browser
    DOM is an external surface even though the next partial replaces it and
    nothing is persisted. Both redactors return ``(text, warnings)``, so the
    unpacking is what this exists to keep identical across the three branches —
    indexing the tuple instead takes the first CHARACTER.
    """
    out, _ = redact_exfiltration_urls(text)
    out, _ = redact_credentials(out)
    return out


def _drop_task_result(task: "asyncio.Task[Any]") -> None:
    """Consume an abandoned task's outcome so asyncio does not warn about it.

    For a task deliberately left running past the handler that started it: with
    nobody awaiting it, an exception it raises is reported at collection time as
    "Task exception was never retrieved".
    """
    if task.cancelled():
        return
    task.exception()


async def _send_error(ws: web.WebSocketResponse, message: str, code: str) -> None:
    """Emit one ``error`` frame, tolerating a peer that has already gone away.

    Best-effort by design, and that is what makes it safe on an early-return path:
    every such path sends this frame and then emits ``stt_stream_end``, so a raise
    from the send would skip the audit and leave an unmatched ``stt_stream_start``
    in the trail for a client that merely closed its tab.

    ``code`` is not optional. The dashboard renders ``message`` verbatim into a
    12-language UI, so a frame without one can only ever be shown in English.
    """
    if ws.closed:
        return
    try:
        await ws.send_json({"type": "error", "message": message, "code": code})
    except Exception:
        logger.debug("STT error frame could not be delivered", exc_info=True)


def _emit_end_audit(caller: str, *, outcome: str) -> None:
    """Log ``stt_stream_end`` defensively.

    EVERY exit path emits this exactly once: a setup refusal, a provider that could
    not be constructed, a model fetch that outran its ceiling, a cap, a normal
    ``finally``. One that skips it leaves an unmatched ``stt_stream_start``, which in
    the trail is indistinguishable from a voice session still in progress; one that
    emits it twice invents a session that never happened. Never raise: a failing SEL
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


@contextlib.contextmanager
def _audited_setup(caller: str) -> Iterator[None]:
    """Keep the audit trail balanced when a provider session cannot be constructed.

    ``stt_stream_start`` has already emitted by the time a provider branch runs,
    and the teardown that emits the matching end does not exist until the session
    object does. So a raise while building it leaves an unmatched start, and this is
    reachable rather than theoretical: constructing the default ``local`` session is
    what first imports the recogniser package, so a broken optional dependency
    surfaces exactly here. Nothing has been created at that point, which is why the
    end audit is the whole of the cleanup.
    """
    try:
        yield
    except BaseException:
        _emit_end_audit(caller, outcome="error")
        raise


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

    Both partials and finals pass through :func:`_redacted` before they leave the
    process — a partial flashed in the browser counts as an external surface even
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
                redacted = _redacted(result.alternatives[0].transcript)
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


def _build_endpointer(
    ws: web.WebSocketResponse, cfg: "KiroCrewConfig", request: web.Request
) -> "_Endpointer | None":
    """The semantic end-of-utterance judge, or None when it is not available.

    Gated on ``stt.endpointing`` plus a reachable ``SessionManager`` (the judge is
    a background model call). ``None`` means no ``endpoint`` frame is ever emitted,
    which is the default: that frame means "the request looks complete, you may
    auto-submit", so it is a separate decision from a recogniser deciding an
    utterance is over.
    """
    if not cfg.stt.endpointing:
        return None
    state = request.app.get("state")
    sessions = getattr(state, "sessions", None)
    if sessions is None:
        return None
    return _Endpointer(ws, sessions)


async def _run_local_session(
    ws: web.WebSocketResponse,
    cfg: "KiroCrewConfig",
    request: web.Request,
    caller: str,
) -> None:
    """Drive the resident whisper.cpp recogniser over an already-prepared WebSocket.

    Emits the same ``ready`` / ``partial`` / ``final`` frames as the other two
    providers, plus a ``status`` frame around the one-time model download so a
    first-ever session is not indistinguishable from a hang. Every frame this
    branch produces on failure also carries a machine-readable ``code``.

    The recogniser's own endpointing FINALISES an utterance: when the detector
    reports the speaker stopped, the final is sent and the session listens for the
    next utterance. It does NOT close, because a session spans many utterances here
    exactly as it does on the other two providers, and both clients accumulate
    finals. That is also deliberately not the ``endpoint`` frame, which authorises
    the frontend to auto-submit and stays governed by ``stt.endpointing``.

    Nothing here is metered, but the duration cap still applies: an abandoned
    session accumulates buffered audio and holds one of
    ``_MAX_CONCURRENT_SESSIONS`` slots.
    """
    with _audited_setup(caller):
        endpointer = _build_endpointer(ws, cfg, request)
        session = stt.LocalSession(
            model_name=cfg.stt.model,
            language=_whisper_language(cfg.stt.language_code),
            silence_ms=cfg.stt.silence_ms,
            partial_interval_ms=cfg.stt.partial_interval_ms,
            idle_evict_secs=cfg.stt.idle_evict_secs,
        )

    # The FIRST fatal cause wins. Both the duration cap and a failed send can end
    # the session, and the cap ends the read loop by closing the socket — so
    # without a single claim the second one to run would relabel the first one's
    # audit outcome. Claimed before any awaiting work. `None` means a normal end.
    fatal_outcome: str | None = None
    # Set once the client stops accepting frames, so teardown skips a full-buffer
    # decode whose transcript has nowhere to go.
    client_gone = False

    def _claim_fatal(kind: str) -> None:
        nonlocal fatal_outcome
        if fatal_outcome is None:
            fatal_outcome = kind

    async def _send(frame: dict[str, object]) -> bool:
        """Send one JSON frame. False once the client stopped accepting them."""
        nonlocal client_gone
        if client_gone or ws.closed:
            return False
        try:
            await ws.send_json(frame)
            return True
        except Exception:
            # Client disconnected mid-send. Stop relaying rather than logging a
            # traceback per event for the rest of the session.
            client_gone = True
            return False

    async def _relay(events: list["stt.SttEvent"]) -> bool:
        """Forward session events to the client. False means stop the session."""
        for event in events:
            if event.kind == stt.KIND_ERROR:
                _claim_fatal("error")
                await _send({"type": "error", "message": event.text, "code": event.code})
                return False
            if event.kind == stt.KIND_STATUS:
                if not await _send(
                    {
                        "type": "status",
                        "stage": event.stage,
                        "downloaded_bytes": event.downloaded_bytes,
                        "total_bytes": event.total_bytes,
                        "code": event.code,
                    }
                ):
                    return False
                continue
            text = _redacted(event.text).strip()
            if not text:
                continue
            if endpointer is not None:
                # Note BEFORE the awaited send, matching the other two branches: a
                # live partial means the user is still speaking, and doing it after
                # `await send_json` leaves a window where that await yields and a
                # stale COMPLETE emits, auto-submitting a truncated request.
                if event.kind == stt.KIND_PARTIAL:
                    endpointer.note_partial(text)
                else:
                    endpointer.note_final(text)
            if not await _send({"type": event.kind, "text": text}):
                return False
        return True

    async def _give_up(outcome: str) -> None:
        """Abandon the session before the read loop, keeping the audit balanced."""
        session.cancel()
        await _close_and_end_audit(ws, caller, outcome=outcome)
        if endpointer is not None:
            await endpointer.aclose()

    # Asked BEFORE prepare(), which can only answer once the transfer it waits on
    # has finished. A silent 148 MB fetch is indistinguishable from a hang, so the
    # notice has to go out first; live byte progress is served by
    # GET /api/stt/status, which the panel polls once it has seen this.
    # A first run has to fetch the model, and that takes longer than a browser will
    # hold a hot microphone: the client caps its pre-`ready` buffer at a few seconds
    # and releases the mic when `ready` does not arrive, so anything the user said
    # while waiting was captured and then thrown away.
    #
    # So refuse the session instead of accepting speech that cannot survive it. The
    # transfer is still STARTED here, in the background, because the point is that
    # the next attempt works: the panel shows byte progress from
    # GET /api/stt/status, and the download also begins on the mic prewarm that
    # fires when the user first reaches for the button. Streaming stays available on
    # the local provider, which is the whole feature; what is refused is the one
    # first-run window where it could only lose words.
    pending = session.pending_download()
    if pending is not None:
        await _send(
            {
                "type": "status",
                "stage": stt.STAGE_DOWNLOADING,
                "downloaded_bytes": 0,
                "total_bytes": pending.size_bytes,
                "code": stt.CODE_MODEL_MISSING,
            }
        )
        _spawn_model_fetch(session)
        _claim_fatal("error")
        await _send(
            {
                "type": "error",
                "message": "speech model is still downloading",
                "code": stt.CODE_MODEL_MISSING,
            }
        )
        await _give_up("error")
        return

    prepare_task = asyncio.create_task(session.prepare())
    try:
        events = await asyncio.wait_for(
            asyncio.shield(_relay_download_progress(prepare_task, _send)),
            timeout=_MAX_MODEL_PREPARE_SECS,
        )
    except asyncio.TimeoutError:
        # Shielded, so the transfer is LEFT RUNNING rather than cancelled:
        # cancelling it releases the model store's transfer lock while its worker
        # thread is still writing the staging file, and the next session would
        # then start a second write to that same path. Only this socket gives up;
        # the bytes land on disk for the next attempt.
        prepare_task.add_done_callback(_drop_task_result)
        _claim_fatal("error")
        logger.warning("Local speech model was not ready within %ds", _MAX_MODEL_PREPARE_SECS)
        await _send(
            {
                "type": "error",
                "message": "speech model is still downloading",
                "code": stt.CODE_MODEL_MISSING,
            }
        )
        await _give_up("error")
        return
    except BaseException:
        # A cancelled prepare has no owner on this side yet: the teardown below
        # only exists once the read loop has been entered. The end audit keeps the
        # trail balanced, since the raise bypasses every later emitter.
        session.cancel()
        if endpointer is not None:
            await endpointer.aclose()
        _emit_end_audit(caller, outcome="error")
        raise

    if not await _relay(events):
        await _give_up(fatal_outcome or "error")
        return
    if pending is not None:
        # Only meaningful when a transfer actually ran: it tells the panel to stop
        # polling for byte progress and drop the download notice.
        if not await _send({"type": "status", "stage": stt.STAGE_READY}):
            await _give_up(fatal_outcome or "error")
            return

    # Enforce the duration cap with a dedicated task, NOT an in-loop check, for the
    # reason the other two branches document: `async for msg in ws` only yields on
    # client data and aiohttp answers heartbeat ping/pong internally, so a client
    # that stops sending audio while the socket stays alive (a throttled background
    # tab, a muted input, a client bug) would never evaluate a message-driven
    # deadline and would hold one of `_MAX_CONCURRENT_SESSIONS` slots indefinitely.
    async def _enforce_deadline() -> None:
        await asyncio.sleep(_MAX_STREAM_DURATION_SECS)
        # Only the first claimant sends: otherwise the cap and a concurrent
        # failure each emit a frame in the window before the other's close lands,
        # and the client sees two contradictory errors for one failure.
        if fatal_outcome is not None:
            return
        _claim_fatal("timeout")
        await _send(
            {
                "type": "error",
                "message": "max stream duration exceeded",
                "code": _CODE_MAX_DURATION,
            }
        )
        # ws.close() can raise on a broken transport, and an unhandled exception
        # here would surface as "Task exception was never retrieved".
        try:
            await ws.close()
        except Exception:
            pass

    deadline_task = asyncio.create_task(_enforce_deadline())
    outcome = "ok"
    try:
        await _send({"type": "ready"})
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                # A final here means the detector finalised ONE utterance; the
                # session continues. Closing on it left the Meetings app reporting
                # "disconnected" and tearing down its microphone on the speaker's
                # first pause, with nothing to restart it.
                if not await _relay(await session.feed(msg.data)):
                    break
                if session.ended:
                    # A resource ceiling (the session audio cap) rather than the
                    # detector: feed() finished the session itself, so stop reading.
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
        logger.exception("local streaming STT session failed")
        outcome = "error"
        # Claimed before the frame goes out, so a duration cap firing in the same
        # window stays silent instead of contradicting this one.
        _claim_fatal("error")
        await _send(
            {
                "type": "error",
                "message": "transcription failed",
                "code": _CODE_SESSION_FAILED,
            }
        )
    finally:
        # An EXPLICIT claim, not `deadline_task.done()`: the cap's own ws.close()
        # is what ends the read loop, so this `finally` runs while that task is
        # still awaiting the close and `done()` is still False.
        timed_out = fatal_outcome == "timeout"
        # Cancel first among the cleanup steps: it is the one thing that can still
        # touch the socket, and leaving it live past cleanup would close a socket
        # the next request may already own.
        deadline_task.cancel()
        if timed_out:
            logger.info("local streaming STT session hit the %ds cap", _MAX_STREAM_DURATION_SECS)
        if client_gone or ws.closed or not session.has_pending_audio:
            # Either every utterance has already been finalised, or the transcript
            # has nowhere to go. Abandoning the audio matters rather than being tidy:
            # finish() decodes the whole tail, which is real work on the one shared
            # model that a live session behind this one queues behind.
            #
            # Gated on PENDING AUDIO, not on "a final was sent": over a
            # multi-utterance session both are true at once, and reading the latter
            # discarded whatever the speaker said after the last detected pause.
            session.cancel()
        else:
            # A `stop` control frame, or a read loop that ended without a verdict:
            # the transcript the user keeps is one decode of everything heard, so it
            # is produced here rather than assembled from the partials.
            try:
                await _relay([await session.finish()])
            except Exception:
                logger.warning("Local final transcript decode failed", exc_info=True)
        # After the final, for the reason the AWS path documents: the final is what
        # the endpointer needs to see, and cancelling its tasks first would drop
        # the judgment on the one segment that matters.
        if endpointer is not None:
            await endpointer.aclose()
        # A claimed fatal cause outranks the local `outcome`: the read loop can
        # exit cleanly (the cap closed the socket under it) and would otherwise be
        # recorded as "ok" for a session that in fact died.
        await _close_and_end_audit(ws, caller, outcome=fatal_outcome or outcome)


def _apple_start_failure_code(cfg: "KiroCrewConfig") -> str:
    """Machine-readable reason for an apple session that would not start.

    ``StreamingSession.start`` answers in prose, and the two conditions worth
    telling apart are exactly the ones the availability probe already names: a host
    that cannot run SpeechAnalyzer at all, versus one that could once the Swift
    toolchain is installed (the second has a one-line fix, and the dashboard
    already carries localised text for both). Re-asking the probe rather than
    parsing the prose keeps that mapping in one place, and is cheap by that
    function's own contract: platform reads and two stats, no subprocess.

    A capable host that still failed to start is a session failure, not an
    availability one, so it falls back to the generic code.
    """
    return availability_detail(cfg.stt).code or _CODE_SESSION_FAILED


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
    with _audited_setup(caller):
        from kiro_crew import apple_speech

        endpointer = _build_endpointer(ws, cfg, request)
        session = apple_speech.StreamingSession(
            locale=cfg.stt.language_code or "en-US",
            sample_rate=STREAM_SAMPLE_RATE_HZ,
        )
    try:
        problem = await session.start()
    except BaseException:
        # A cancelled start() has no owner on this side yet: the teardown
        # `finally` below only exists once start() has returned. close() is
        # idempotent, so running it on top of start()'s own cancellation
        # cleanup is safe, and the endpointer holds no tasks this early so
        # its aclose() is a free symmetry with the failure branch below. The
        # end audit keeps the trail balanced — the stt_stream_start already
        # emitted would otherwise have no matching end, since the raise
        # bypasses every later emitter.
        await session.close()
        if endpointer is not None:
            await endpointer.aclose()
        _emit_end_audit(caller, outcome="error")
        raise
    if problem:
        await _send_error(ws, problem, _apple_start_failure_code(cfg))
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
                    await _send_error(ws, msg_text, _CODE_SESSION_FAILED)
                    try:
                        await ws.close()
                    except Exception:
                        pass
                return
            if kind not in ("partial", "final"):
                continue
            redacted = _redacted(str(event.get("text", "")))
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
            await _send_error(ws, "max stream duration exceeded", _CODE_MAX_DURATION)
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
                    await _send_error(ws, "speech helper stopped", _CODE_SESSION_FAILED)
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


#: Detached model fetches, held so the event loop cannot collect one mid-transfer.
#: A set rather than a single task because two sockets can open before either
#: finishes; the store itself serialises the actual download behind one lock.
_MODEL_FETCH_TASKS: set["asyncio.Task[list[stt.SttEvent]]"] = set()


def _spawn_model_fetch(session: "stt.LocalSession") -> None:
    """Start the model transfer in the background and stop caring about it.

    Deliberately detached from this socket: the socket is about to close, and the
    transfer must outlive it so the user's next attempt finds the weights on disk.
    Exceptions are swallowed by design, because the store records its own failure
    in the status the panel polls, and nothing here is left to report it to.
    """
    task = asyncio.create_task(session.prepare())
    _MODEL_FETCH_TASKS.add(task)
    task.add_done_callback(_MODEL_FETCH_TASKS.discard)
    task.add_done_callback(_drop_task_result)


def _status_int(value: object) -> int:
    """Read a byte count out of the model store's untyped status dict.

    The dict is served straight to the browser as JSON, so it is deliberately
    ``dict[str, object]`` rather than a typed record. A non-numeric value can only
    mean the store has not populated that field yet, which reads as zero.
    """
    return value if isinstance(value, int) else 0


async def _relay_download_progress(
    prepare_task: "asyncio.Task[list[stt.SttEvent]]",
    send: Callable[[dict], Awaitable[bool]],
) -> list[stt.SttEvent]:
    """Await *prepare_task*, republishing the model store's byte count while it runs.

    A client watchdog treats a quiet socket as a stall and reconnects, which during a
    transfer would abandon it and restart from zero, forever — so a `prepare` that
    ends up downloading has to keep talking.

    That is NOT the first-run path any more: `pending_download()` is asked before
    this, and a session whose model is absent is refused outright rather than made to
    wait (a browser releases the microphone long before a 148 MB fetch finishes, so
    waiting captured speech it then discarded). What reaches here is the narrow race
    where the weights were present at that check and gone by the time `prepare` looked
    — an operator clearing the model directory, or a concurrent eviction — plus the
    `_MAX_MODEL_PREPARE_SECS` ceiling this wait is what applies. Kept for that, not
    for the case its progress frames were originally written for.

    Sending is best-effort on purpose: a failed send means the peer is gone, and
    the transfer must still be allowed to finish so the bytes are on disk for the
    next attempt. So a send failure stops the reporting, never the download.
    """
    while True:
        done, _ = await asyncio.wait({prepare_task}, timeout=_MODEL_PROGRESS_INTERVAL_SECS)
        if done:
            return await prepare_task
        status = stt.model_store().status
        if status.get("step") != stt.STAGE_DOWNLOADING:
            continue
        delivered = await send(
            {
                "type": "status",
                "stage": stt.STAGE_DOWNLOADING,
                "downloaded_bytes": _status_int(status.get("downloaded_bytes")),
                "total_bytes": _status_int(status.get("total_bytes")),
                "code": stt.CODE_MODEL_MISSING,
            }
        )
        if not delivered:
            return await prepare_task


async def api_ws_stt(request: web.Request) -> web.WebSocketResponse:
    """GET /api/ws/stt — streaming speech-to-text.

    Client sends binary PCM frames and text control messages
    (``{"type":"stop"}``). Server emits JSON events
    ``{"type":"ready"|"partial"|"final"|"endpoint"|"error"|"status", ...}``.
    ``status`` reports the ``local`` provider's one-time model download and is the
    one frame a client may not see at all on the other two providers.
    """
    if not check_origin(request, require=True):
        _emit_guard_audit(request.remote or "unknown", outcome="forbidden")
        # That record is the specific one; claim the request so the deny-audit
        # boundary does not add a second, generic entry for the same refusal.
        mark_audit_claimed(request)
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
            await _send_error(ws, "audit subsystem unavailable", _CODE_SESSION_FAILED)
            await _close_and_end_audit(ws, caller, outcome="error")
            return ws

        if cfg.stt.provider == PROVIDER_LOCAL:
            # The default. Same client protocol (binary PCM in, partial/final JSON
            # out) as the other two, so the frontend cannot tell them apart, and
            # kept as its own function for the same reason the apple branch is: the
            # Transcribe setup below is entirely AWS-specific.
            await _run_local_session(ws, cfg, request, caller)
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
            # friendly error here and keep the audit trail balanced. The code is
            # the one the settings panel already renders for a missing extra, so
            # the socket and the panel agree on what is wrong.
            await _send_error(ws, "amazon-transcribe not installed", stt.CODE_EXTRA_MISSING)
            await _close_and_end_audit(ws, caller, outcome="error")
            return ws

        # Streaming Transcribe bills per second of audio, so the socket does not
        # start without a recorded operator consent for this exact
        # profile+region. Refused before the client is constructed and before
        # any audio is read, and reported over the same error channel as the
        # other setup failures so the audit trail stays balanced.
        granted, reason = await aws_consent.authorize(
            aws_consent.SERVICE_TRANSCRIBE,
            profile=cfg.stt.transcribe_profile,
            region=cfg.stt.transcribe_region,
        )
        if not granted:
            logger.warning("AWS request refused: %s", reason)
            await _send_error(ws, reason, _CODE_CONSENT_REQUIRED)
            await _close_and_end_audit(ws, caller, outcome="refused")
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
            await _send_error(ws, "failed to create transcription client", _CODE_SESSION_FAILED)
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
            await _send_error(ws, "failed to start transcription", _CODE_SESSION_FAILED)
            await _close_and_end_audit(ws, caller, outcome="error")
            return ws

        # An EXPLICIT claim by the cap itself, the same discipline as the other two
        # branches, and NOT `deadline_task.done()`. Inferring from task state is racy
        # here: the cap's own `ws.close()` is what ends the read loop, so the
        # `finally` runs while that task is still awaiting the peer's close
        # acknowledgement and `done()` is still False. A capped session would then be
        # audited as a clean stop, which on the one metered provider is the
        # distinction an operator most needs. Claimed before any awaiting work, so it
        # cannot be missed.
        capped = False

        # Enforce the bill-cap with a dedicated task, not an in-loop check.
        # `async for msg in ws` only yields on client data; aiohttp handles
        # heartbeat ping/pong internally, so an idle-but-alive client would
        # never trip a message-driven deadline.
        async def _enforce_deadline() -> None:
            nonlocal capped
            await asyncio.sleep(_MAX_STREAM_DURATION_SECS)
            if not ws.closed:
                capped = True
                await _send_error(ws, "max stream duration exceeded", _CODE_MAX_DURATION)
                # Tolerated for the same reason `_send_error` tolerates a failed
                # send: ws.close() can raise on a broken transport, and an
                # unhandled exception here would surface as "Task exception was
                # never retrieved".
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
        endpointer = _build_endpointer(ws, cfg, request)
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
            # Cancel first among the cleanup steps: it is the one thing that can still
            # touch the socket, and leaving it live past cleanup would close a socket
            # the next request may already own. Whether it FIRED is `capped`, claimed
            # by the task itself.
            if deadline_task is not None:
                deadline_task.cancel()
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
            _emit_end_audit(caller, outcome="timeout" if capped else "ok")
            if not ws.closed:
                try:
                    await ws.close()
                except Exception:
                    logger.exception("Failed to close STT WebSocket during cleanup")

        return ws
    finally:
        _active_sessions -= 1
