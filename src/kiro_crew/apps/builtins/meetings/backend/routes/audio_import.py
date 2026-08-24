"""Meetings — import an existing recording into a live meeting.

``POST …/{id}/import`` takes a host path to an audio file, transcribes it with the
gateway's own batch speech-to-text, and feeds the result into the meeting exactly as
if it had been spoken.

**Why "as if it had been spoken" rather than a separate record.** Every line goes
through :func:`_common.dispatch_line`, the same admission transaction live speech and
the broadcast bar use. So an imported line is persisted to ``transcript.jsonl``
before it is fanned out — the app-wide data-integrity boundary: an accepted agent
line cannot be absent from the transcript — and then gets the SAME pipeline a
microphone gets: domain-dictionary correction, the noise gate, per-agent batching,
and the muted-agent list. There is no second code path to keep in step, and the
imported recording is readable back from the transcript panel like anything spoken.

The consequence, and it is the honest way round: an import needs a LIVE meeting. That
is not a limitation to work around — the agents are what turn transcript into minutes,
and they only exist while a meeting is running. The session is checked FIRST, before
the expensive steps, and each line is re-admitted individually, so a meeting stopped
while an hour of audio was being transcribed fails the dispatch loop promptly instead
of writing into a torn-down meeting.

Security posture:

* Only the dashboard OWNER may import: the handler refuses any other caller via
  :func:`~kiro_crew.dashboard.handlers.source_providers.is_owner_dashboard_request`
  before the body is read, with the shared denial shape — the same gate the
  aws-control routes and the app job routes apply, because the capability is the
  same class: reading an arbitrary host file by path on the caller's say-so.
* The client-supplied path goes through :func:`kiro_crew.hooks.validate_file_path`,
  the shared dashboard file gate, which canonicalizes (following symlinks) and
  enforces ``is_sensitive_path``. The predicate is never called directly here — using
  the gate is what keeps this route's answer identical to every other file read in the
  product. The transcriber never sees the client-supplied name: it consumes the
  request-private snapshot produced by :func:`_snapshot_recording`.
* Rejections are SEL-audited.
* **This module calls no redactor of its own**, deliberately: ``transcribe_audio``
  scrubs and hallucination-filters what it returns, and ``_common.dispatch_line``
  redacts at the transcript boundary exactly as it does for live speech — a third
  pass here would add no coverage. (That absence is also why this module is neither
  a registered redaction sink nor an allowlisted non-egress module in
  ``security_posture`` — there is no call site to classify.)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew.apps.builtins.meetings.backend import constants as k
from kiro_crew.apps.builtins.meetings.backend import store
from kiro_crew.apps.builtins.meetings.backend.domain import audio
from kiro_crew.apps.builtins.meetings.backend.routes._common import (
    BadRequest,
    audit,
    dispatch_admission,
    dispatch_line,
    field_str,
    json_body,
)
from kiro_crew.dashboard.handlers.source_providers import is_owner_dashboard_request
from kiro_crew.hooks import validate_file_path

logger = logging.getLogger("kirocrew.app.meetings")

#: Meetings with an import currently running. One import per meeting at a time:
#: two concurrent imports dispatch line-by-line into the same transcript, so their
#: recordings would interleave — a garbled record neither upload asked for. Only
#: touched from the event loop with no await between test and add, so the
#: check-and-set needs no lock.
_imports_in_flight: set[str] = set()


def _meeting_id(request: web.Request) -> str:
    return store.safe_meeting_id(request.match_info.get("meeting_id", ""))


def _vet_audio_file(raw_path: str) -> tuple[str, str]:
    """Return ``(canonical_path, "")`` or ``("", reason)``. BLOCKING.

    One helper for the whole check because all three steps touch the filesystem and
    they belong in the same thread hop: the gate canonicalizes (a ``realpath``), and
    the existence and suffix tests must apply to the CANONICAL path rather than to
    what the client sent — otherwise a symlink with an ``.mp3`` name could point at
    something else entirely.
    """
    try:
        canonical = validate_file_path(raw_path)
    except (ValueError, OSError):
        # A path the OS itself refuses to work with — an embedded NUL byte
        # (``realpath`` raises ValueError), an over-long name — is denied like
        # any other unreadable path rather than crashing the request with a 500.
        return "", "denied"
    if canonical is None:
        return "", "denied"
    path = Path(canonical)
    if not path.is_file():
        return "", "not_a_file"
    if path.suffix.lower() not in k.IMPORT_AUDIO_EXTENSIONS:
        return "", "unsupported_format"
    try:
        size = path.stat().st_size
    except OSError:
        # Raced away between the is_file() above and here — same answer as if it
        # had never existed.
        return "", "not_a_file"
    if size > k.MAX_IMPORT_AUDIO_BYTES:
        # BEFORE the decoder ever sees the file: decoding materializes PCM for
        # the whole recording, so the size gate is the only ceiling that runs
        # while the cost is still zero.
        return "", "file_too_large"
    return canonical, ""


def _transcription_ready(stt_config: Any) -> bool:
    """Whether batch speech-to-text is usable at all. BLOCKING.

    Answered against the caller's ONE config snapshot, never a fresh read — the
    readiness answer, the duration-cap answer, and the transcription itself must
    describe the same provider (see ``handle_import_audio``).
    """
    # Deliberately function-local (`top-level-imports` deviation, recorded): the STT
    # stack pulls optional heavy dependencies (faster-whisper is not a declared
    # extra), and a gateway that registers this app must not import a decoder at
    # startup. Pinned by TestWiring.test_transcribe_is_imported_lazily.
    from kiro_crew.transcribe import is_available

    try:
        return bool(is_available(stt_config))
    except Exception:  # pragma: no cover — a broken config must not 500 the route
        logger.warning("meetings: could not determine STT availability", exc_info=True)
        return False


def _snapshot_recording(canonical: str, snapshot_dir: str) -> str | None:
    """Copy the vetted recording into *snapshot_dir* from a pinned descriptor.

    The path was validated by :func:`_vet_audio_file`, but a path is a NAME, and
    between that check and the transcriber's own open anything running as this
    user can swap the final component for a link to something else — the
    validated path and the transcribed inode would then not be the same thing.
    :func:`kiro_crew.pinned_fs.copy_file_pinned` is the repo's one mechanism for
    exactly this: it opens with ``O_NOFOLLOW`` where the platform has it, judges
    the DESCRIPTOR with ``fstat``, and copies those bytes, so the inode that was
    validated is the inode the transcriber reads and no check-to-use window
    remains. Everything downstream (the duration probe, the decode) consumes the
    snapshot in our own fresh ``0700`` directory, never the user-writable name.

    The snapshot keeps the original suffix — the transcriber's WAV fast path and
    the AWS remux both look at it. Size is re-checked on the SNAPSHOT: the vet
    step's ceiling judged the original name, and a swap could have replaced it
    with something the memory ceiling exists to refuse. Returns the snapshot
    path, or None when the source was refused (swapped for a link, ancestor
    directory swapped for a link, no longer a regular file, vanished before the
    copy) or grew past the ceiling. BLOCKING.
    """
    from kiro_crew.pinned_fs import (
        PinnedPathRefusal,
        copy_file_pinned,
        is_reparse_point,
        pin_parent,
        supports_pinned_walk,
    )

    # Windows first: ``O_NOFOLLOW`` does not exist there, so the pinned open
    # below would follow a link. ``is_reparse_point`` is the module's own answer
    # for that platform (it also catches junctions, which ``islink`` misses). On
    # POSIX this is a cheap redundant pre-check; the descriptor judgment below
    # remains the race-free guard.
    if is_reparse_point(canonical):
        logger.warning("meetings: import source %r is a link/junction; refused", canonical)
        return None

    # The DESTINATION stays by-name: ``snapshot_dir`` is this request's own fresh
    # ``0700`` ``mkdtemp``, which is exactly the "path this process just created"
    # case ``copy_file_pinned`` documents as the appropriate by-name form.
    dst = os.path.join(snapshot_dir, "recording" + Path(canonical).suffix.lower())
    refused: list[str] = []

    def _report(reason: str, path: str) -> None:
        refused.append(reason)

    try:
        if supports_pinned_walk():
            # The SOURCE is a user-writable name, so ``O_NOFOLLOW`` on its final
            # component is not enough: an ANCESTOR directory swapped for a link
            # after validation redirects the whole traversal and the final open
            # never sees a link. ``pin_parent`` is the repo's one mechanism for
            # that — one ``openat`` per component, each ``O_NOFOLLOW`` — and
            # ``canonical`` is already resolved once by the shared file gate,
            # which is the resolution ``pin_parent`` requires its caller to have
            # done. Same shape as the app-art reader in ``apps/routes.py``.
            try:
                dir_fd = pin_parent(os.path.dirname(canonical), what="import recording parent")
            except PinnedPathRefusal as exc:
                logger.warning("meetings: import source %r refused: %s", canonical, exc)
                return None
            try:
                copied = copy_file_pinned(
                    canonical,
                    dst,
                    dir_fd=dir_fd,
                    name=os.path.basename(canonical),
                    force_mode=0o600,
                    max_bytes=k.MAX_IMPORT_AUDIO_BYTES,
                    on_skip=_report,
                )
            finally:
                os.close(dir_fd)
        else:
            # No ``dir_fd`` support (Windows): probe the target AND every
            # ancestor up to the filesystem root for reparse points before the
            # by-name copy — the final-component check above never fires when an
            # ANCESTOR directory is the junction, and a junction is exactly the
            # swap an unprivileged Windows process can plant (file symlinks need
            # elevation). Same shape as the Windows branch of the app-art reader
            # in ``apps/routes.py``: the window is narrowed against what the
            # platform actually permits rather than left open.
            src = Path(canonical)
            if any(is_reparse_point(p) for p in (src, *src.parents)):
                logger.warning(
                    "meetings: import source %r has a link/junction on its path; refused",
                    canonical,
                )
                return None
            copied = copy_file_pinned(
                canonical,
                dst,
                force_mode=0o600,
                max_bytes=k.MAX_IMPORT_AUDIO_BYTES,
                on_skip=_report,
            )
    except OSError as exc:
        # ``copy_file_pinned`` propagates ``FileNotFoundError`` BY CONTRACT so a
        # source that vanished between validation and the copy can be tolerated,
        # and every other ``OSError`` here is the same story with a different
        # errno: a source this request cannot read (``EACCES``/``EPERM``), a
        # component swapped mid-walk (``ELOOP``/``ENOTDIR``), or bytes that
        # stopped being readable. The honest answer for all of them is the same
        # refusal as any other unreadable path — the route maps None to 403
        # ``audio_path_denied`` — not a 500.
        logger.warning("meetings: import source %r cannot be snapshotted: %s", canonical, exc)
        return None
    if not copied:
        logger.warning(
            "meetings: import source %r refused at snapshot (%s)",
            canonical,
            ", ".join(refused) or "not copied",
        )
        return None
    # No after-the-fact size check: ``MAX_IMPORT_AUDIO_BYTES`` is enforced INSIDE
    # ``copy_file_pinned`` (fstat pre-check + abort after the first excess byte),
    # so a swapped-in oversize source is refused before it can fill the temp
    # volume rather than after the copy already materialized it.
    return dst


async def _remove_snapshot_dir(copy_task: "asyncio.Future[Any] | None", snapshot_dir: str) -> None:
    """Join the snapshot copy, THEN remove its directory. Best-effort.

    The order is the point (GPT review r12): ``asyncio.to_thread`` workers are
    not cancellable, so a request cancelled mid-copy leaves the worker thread
    holding an open destination handle inside ``snapshot_dir``. Removing the
    directory concurrently loses that race on Windows -- ``rmtree`` cannot
    delete a file with an open handle, ``ignore_errors`` swallows the failure,
    and the stale recording accumulates until the temp volume fills. Awaiting
    the copy future first means the thread has exited and closed its handles
    before the first unlink; a copy that failed re-raises on that await, which
    is suppressed here because the removal must happen either way.

    Run as its own task (``ensure_future``) so a REPEAT cancellation of the
    request abandons only the caller's wait -- this coroutine keeps running on
    the loop and still removes the directory after the join.
    """
    if copy_task is not None:
        with contextlib.suppress(BaseException):
            await asyncio.shield(copy_task)
    await asyncio.to_thread(shutil.rmtree, snapshot_dir, ignore_errors=True)


async def handle_import_audio(request: web.Request) -> web.Response:
    """Transcribe a recording from disk and dispatch it into the live meeting."""
    meeting_id = _meeting_id(request)

    # Owner gate FIRST, before the body is even read. This route's capability is
    # "read an arbitrary host file by path and surface its contents through the
    # transcript" — the same host-file class aws-control's ``_guarded`` and the
    # app job routes gate on ``is_owner_dashboard_request``. An authenticated
    # non-owner caller gets the shared denial shape, and the permission DECISION
    # reaches the audit trail: a refused import is exactly what an incident
    # review asks about.
    if not is_owner_dashboard_request(request):
        audit(
            "meetings.import_audio",
            f"{meeting_id} reason:non-owner",
            outcome="denied",
        )
        # Imported here, not at module top: ``_shared`` pulls in the dashboard
        # handler surface, and this branch is the only consumer (job_routes
        # resolves it the same way for the same reason).
        from kiro_crew.dashboard.handlers._shared import _owner_denial_response

        return _owner_denial_response(
            request, "dashboard owner required", "dashboard_owner_required"
        )

    # An ALLOW is a permission decision too (the app job routes' own rule):
    # auditing only denials leaves an incident review able to see who was
    # refused and not who got through — and without this record an owner
    # request that then fails JSON parsing (400) would leave no trace that the
    # owner path was entered at all.
    audit("meetings.import_audio", f"{meeting_id} owner-check", outcome="allowed")

    body = await json_body(request)
    raw_path = field_str(body, "audio_path", required=True, max_len=k.MAX_AUDIO_PATH_CHARS)

    def _reject(reason: str) -> None:
        audit("meetings.import_audio", f"{meeting_id} reason:{reason}", outcome="rejected")

    # The live session FIRST, before the expensive steps. Transcribing an hour of
    # audio and only then discovering there is nothing to dispatch into would waste
    # minutes of the user's time to reach an error we can give immediately. The
    # ADMITTED SESSION OBJECT is captured here and required per dispatched line
    # below: a meeting id is a name, not an identity, and a meeting stopped and
    # recreated with the same id while the audio was transcribing must not be
    # contaminated with the old recording's lines.
    async with dispatch_admission(request, meeting_id) as admitted:
        admitted_session = admitted.session

    # After admission (the liveness answer outranks the busy answer), before any
    # expensive step. Held for the whole import — vetting through dispatch — and
    # released in the ``finally`` below on every exit path.
    if meeting_id in _imports_in_flight:
        _reject("import_in_progress")
        raise BadRequest(
            "an import is already running for this meeting",
            status=409,
            code="import_in_progress",
        )
    _imports_in_flight.add(meeting_id)
    try:
        canonical, reason = await asyncio.to_thread(_vet_audio_file, raw_path)
        if reason == "denied":
            _reject(reason)
            raise BadRequest("that path cannot be read", status=403, code="audio_path_denied")
        if reason == "not_a_file":
            _reject(reason)
            raise BadRequest("no such audio file", status=404, code="audio_file_not_found")
        if reason == "file_too_large":
            # 413 like the transcript ceiling: the request is well-formed, the payload
            # is what cannot be accepted — and it is refused before the decoder can
            # spend gigabytes of memory finding that out.
            _reject(reason)
            raise BadRequest(
                "recording file is too large to import",
                status=413,
                code="audio_file_too_large",
            )
        if reason:
            _reject(reason)
            raise BadRequest(
                "unsupported audio format", status=400, code="audio_format_unsupported"
            )

        # Function-local for the recorded lazy-import reason (`_transcription_ready`):
        # the heavy optional STT stack must not be imported at gateway startup.
        from kiro_crew.transcribe import (
            audio_exceeds_secs,
            batch_duration_cap_secs,
            load_stt_config,
            transcribe_audio,
        )

        # ONE configuration snapshot for the whole request. The readiness check, the
        # duration-cap answer, and the transcription each accept a config and re-read
        # it when handed None — three separate reads can straddle the operator
        # switching providers in Settings, letting the cap be judged under a provider
        # without a ceiling while the decode runs under the local one that silently
        # truncates. The snapshot makes the three answers describe one provider.
        stt_config = await asyncio.to_thread(load_stt_config)

        if not await asyncio.to_thread(_transcription_ready, stt_config):
            # 503, not 400: the request is fine and will work once speech-to-text is
            # configured, which is a Settings action rather than a different request.
            raise BadRequest(
                "speech-to-text is not available",
                status=503,
                code="transcription_unavailable",
            )

        # Snapshot BEFORE the probe and the decode, so both consume bytes pinned at
        # validation time (see `_snapshot_recording`) rather than re-opening the
        # user-writable name. The snapshot directory is this request's own 0700 dir,
        # deleted on every exit path below.
        snapshot_dir = await asyncio.to_thread(tempfile.mkdtemp, "-import", "kc-meetings-")
        copy_task: asyncio.Future[str | None] | None = None
        try:
            # The copy runs as its OWN future, shielded: a ``to_thread`` worker
            # cannot be cancelled anyway, so shielding just makes the bookkeeping
            # honest -- the future stays alive for the cleanup below to JOIN, and
            # a cancelled request abandons the wait rather than orphaning a
            # thread that still holds handles inside ``snapshot_dir``.
            copy_task = asyncio.ensure_future(
                asyncio.to_thread(_snapshot_recording, canonical, snapshot_dir)
            )
            snapshot = await asyncio.shield(copy_task)
            if snapshot is None:
                # The file changed identity between validation and the copy — the
                # same answer as any other unreadable path, for the same reason.
                _reject("denied")
                raise BadRequest("that path cannot be read", status=403, code="audio_path_denied")

            # BEFORE transcription, because the local recogniser's decode paths stop
            # reading at their ceiling WITHOUT saying so: a recording over the cap
            # would transcribe its first hour, dispatch it, and return 200 — silent
            # data loss, the exact failure the TranscriptTooLong branch below refuses.
            # Provider-aware: only the local decoder has such a ceiling, so an AWS- or
            # Apple-backed gateway (which fail loudly instead) is not wrongly refused.
            # A probe that cannot answer (None) is REFUSED, loudly and retryably
            # (GPT review r14): the aligned budget (``stt_config.timeout_secs``)
            # guarantees a PERSISTENT cause also defeats the transcode, but a
            # TRANSIENT one — a load spike that clears between probe and
            # transcode — would let the decoder truncate an over-cap recording
            # and answer 200. The error names the retry and the cap, so the
            # refusal is actionable rather than a dead end.
            cap_secs = await asyncio.to_thread(batch_duration_cap_secs, stt_config)
            if cap_secs is not None:
                exceeds = await audio_exceeds_secs(
                    snapshot, cap_secs, timeout_secs=stt_config.timeout_secs
                )
                if exceeds is None:
                    _reject("duration_unverified")
                    raise BadRequest(
                        "could not verify the recording's duration just now; "
                        "retry the import, or trim the recording to under "
                        f"{cap_secs // 60} minutes",
                        status=503,
                        code="duration_unverified",
                    )
                if exceeds:
                    _reject("recording_too_long")
                    raise BadRequest(
                        "recording is too long to import", status=413, code="recording_too_long"
                    )

            transcript = await transcribe_audio(snapshot, stt_config)
        finally:
            # Join-then-remove, as its own task (see ``_remove_snapshot_dir``):
            # scheduled before it is awaited so a repeat cancellation abandons
            # only the wait, never the join or the removal.
            rm = asyncio.ensure_future(_remove_snapshot_dir(copy_task, snapshot_dir))
            await asyncio.shield(rm)
        # None covers three different endings — provider failure, a disabled provider,
        # and a transcript the hallucination filter emptied — and the client's move is
        # the same for all of them, so they share one code.
        if not transcript:
            audit("meetings.import_audio", f"{meeting_id} path:{canonical}", outcome="failed")
            raise BadRequest(
                "could not transcribe that recording", status=502, code="transcription_failed"
            )

        try:
            lines = audio.split_transcript(
                transcript, max_chars=k.MAX_TRANSCRIPT_CHARS, max_lines=k.MAX_IMPORT_LINES
            )
        except audio.TranscriptTooLong:
            # Refused WHOLE, before anything is dispatched: a partial import that
            # returns 200 while the recording's tail is missing is silent data loss.
            # 413 like the transcript ceiling — the request was well-formed, the
            # payload is what cannot be accepted.
            _reject("too_many_lines")
            raise BadRequest(
                "recording is too long to import", status=413, code="recording_too_long"
            )

        # One admission transaction PER LINE, exactly as if each had been spoken: the
        # line is persisted to the transcript and then enqueued for the per-agent
        # batchers, which work through it on their own timers. Agent work is never
        # awaited here; the request holds only for the appends. Per line rather than one
        # long lock hold, so a live microphone's dispatches interleave with the import
        # instead of queueing behind all of it — and a meeting stopped mid-import raises
        # the same 409/410 a spoken line would get, rather than writing into a
        # torn-down meeting. Every line requires the SESSION ADMITTED ABOVE — not merely
        # a live session under the same meeting id — so a meeting stopped, deleted, and
        # recreated with the same id mid-import gets a 410 instead of the old
        # recording's lines. A recording that fills the transcript's ceiling raises the
        # same 413 live speech gets; what was already dispatched stays dispatched, like
        # a meeting that hit the cap while people were talking.
        dispatched = 0
        for line in lines:
            _segment, accepted, _line = await dispatch_line(
                request,
                meeting_id,
                line,
                k.TRANSCRIPT_SOURCE_SPEECH,
                require_session=admitted_session,
            )
            if accepted:
                dispatched += 1

        audit("meetings.import_audio", f"{meeting_id} lines:{len(lines)}", outcome="ok")
        return _response(canonical, lines, dispatched)
    finally:
        _imports_in_flight.discard(meeting_id)


def _response(canonical: str, lines: list[str], dispatched: int) -> web.Response:
    """The success body.

    ``lines`` and ``dispatched`` are reported separately on purpose: the gap between
    them is the lines that reached no agent — noise-gate-filtered, or dropped
    because every agent was muted — and a recording that yields 400 lines of
    which 0 were dispatched is a real outcome the user needs to be able to see (an
    empty room, a filtered hallucination) rather than a silent success.
    """
    payload: dict[str, Any] = {
        "ok": True,
        "path": canonical,
        "lines": len(lines),
        "dispatched": dispatched,
    }
    return web.json_response(payload)
