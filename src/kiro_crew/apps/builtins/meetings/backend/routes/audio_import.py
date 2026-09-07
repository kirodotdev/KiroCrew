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
  request-private snapshot produced by :func:`_snapshot_recording`, staged beneath
  the agent-denied voice-runtime root (:func:`_new_snapshot_dir`) so a same-uid
  agent cannot rewrite the pinned bytes in place.
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
import stat
import tempfile
from pathlib import Path
from typing import Any

from aiohttp import web

from kiro_crew import platform_compat
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
from kiro_crew.pinned_fs import (
    PinnedPathRefusal,
    copy_file_pinned,
    is_reparse_point,
    pin_parent,
    supports_pinned_walk,
)
from kiro_crew.sandbox import prime_voice_runtime_sandbox_paths

logger = logging.getLogger("kirocrew.app.meetings")

#: Meetings with an import currently running. One import per meeting at a time:
#: two concurrent imports dispatch line-by-line into the same transcript, so their
#: recordings would interleave — a garbled record neither upload asked for. Only
#: touched from the event loop with no await between test and add, so the
#: check-and-set needs no lock.
_imports_in_flight: set[str] = set()


def _meeting_id(request: web.Request) -> str:
    return store.safe_meeting_id(request.match_info.get("meeting_id", ""))


def _vet_audio_file(raw_path: str) -> "tuple[str, tuple[int, int] | None, str]":
    """Return ``(canonical, (st_dev, st_ino), "")`` or ``("", None, reason)``. BLOCKING.

    One helper for the whole check because all three steps touch the filesystem and
    they belong in the same thread hop: the gate canonicalizes (a ``realpath``), and
    the existence and suffix tests must apply to the CANONICAL path rather than to
    what the client sent — otherwise a symlink with an ``.mp3`` name could point at
    something else entirely.

    The returned identity is the inode THIS vet judged. The snapshot copy
    refuses a source descriptor that does not fstat to exactly it
    (``copy_file_pinned(expected_src_ident=...)``), so a file swapped in at the
    name after this check — a hardlink to something the sensitivity gate never
    saw included — is never copied (GPT review r29).
    """
    try:
        canonical = validate_file_path(raw_path)
    except (ValueError, OSError):
        # A path the OS itself refuses to work with — an embedded NUL byte
        # (``realpath`` raises ValueError), an over-long name — is denied like
        # any other unreadable path rather than crashing the request with a 500.
        return "", None, "denied"
    if canonical is None:
        return "", None, "denied"
    path = Path(canonical)
    if not path.is_file():
        return "", None, "not_a_file"
    if path.suffix.lower() not in k.IMPORT_AUDIO_EXTENSIONS:
        return "", None, "unsupported_format"
    try:
        st = path.stat()
    except OSError:
        # Raced away between the is_file() above and here — same answer as if it
        # had never existed.
        return "", None, "not_a_file"
    if st.st_size > k.MAX_IMPORT_AUDIO_BYTES:
        # BEFORE the decoder ever sees the file: decoding materializes PCM for
        # the whole recording, so the size gate is the only ceiling that runs
        # while the cost is still zero.
        return "", None, "file_too_large"
    return canonical, (st.st_dev, st.st_ino), ""


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


def _new_snapshot_dir() -> str:
    """Create this request's private snapshot directory under the agent-denied root.

    A ``mkdtemp`` under the system temp directory is ``0700``, but ``/tmp`` itself
    is world-listable and this module already treats a same-uid agent as a live
    racing adversary: the identity pin (``_open_snapshot_pinned``) verifies the
    INODE, so an actor who can reach the file can rewrite its bytes in place and
    still pass the pin (GPT review r28). The one place a same-uid agent cannot
    reach is the voice-runtime root — every agent sandbox mode denies read, write
    and hardlink access to that fixed path, which is exactly why the transcriber
    stages its decoder images there (``transcribe._ffmpeg_snapshot_root``). The
    same validation is applied here: the root must be a real directory (not a
    link) and is re-tightened to ``0700`` before the request's own directory is
    created beneath it.
    """
    root = prime_voice_runtime_sandbox_paths()
    root_stat = os.lstat(root)
    if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
        raise OSError("voice runtime root is not a real directory")
    os.chmod(root, 0o700)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- 0o700 intentionally keeps the snapshot staging root gateway-only while retaining directory traversal; Semgrep's suggested 0o644 would grant world-read and remove traversal.  # noqa: E501  # fmt: skip
    return tempfile.mkdtemp("-import", "kc-meetings-", root)


def _snapshot_recording(
    canonical: str, snapshot_dir: str, expected_src_ident: "tuple[int, int]"
) -> "tuple[str, tuple[int, int]] | None":
    """Copy the vetted recording into *snapshot_dir* from a pinned descriptor.

    ``expected_src_ident`` is the ``(st_dev, st_ino)`` that ``_vet_audio_file``
    judged. The copy runs with ``copy_file_pinned(expected_src_ident=...)``, so
    a source descriptor that fstats to any OTHER inode — a hardlink to a
    sensitive file swapped in at the name after the vet — is refused rather
    than copied: the descriptor pin alone proves the copied inode is the opened
    inode, not that it is the inode the vet saw (GPT review r29).

    Returns ``(snapshot_path, (st_dev, st_ino))`` — the path AND the copy-time
    identity of the inode the copy published (``copy_file_pinned``'s
    ``on_created`` witness) — or ``None`` when the source is refused. The
    identity is what lets ``_open_snapshot_pinned`` prove its reopen reached
    THIS copy rather than a replacement swapped in at the same name after the
    copy finished (GPT review r24).

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
    #: The publish-time ``fstat`` of the copy's destination descriptor — the
    #: identity witness the reopen below is verified against.
    created: list[os.stat_result] = []

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
                    expected_src_ident=expected_src_ident,
                    on_skip=_report,
                    on_created=created.append,
                )
            finally:
                os.close(dir_fd)
        else:
            # No pinned traversal (``dir_fd`` + ``O_NOFOLLOW`` — Windows): REFUSE.
            # Three successive narrowings of this branch (name sweep r9, fd
            # witness r18, vet identity r29) each conceded a residual window,
            # because every variant still performed a by-name ``os.open`` of a
            # user-influenced path — and on Windows that open FOLLOWS an
            # ancestor junction planted after any check, reaching a UNC target
            # and firing outbound SMB authentication as a side effect of the
            # open itself (GPT review r31, adjudicator-upheld). No after-open
            # check can undo that. ``snapshot.py``'s notification copy records
            # the repo ruling for this exact shape: on a platform without the
            # race-free primitive, decline the hand-rolled ctypes walk and
            # refuse loudly. The route already answers 501 before any
            # filesystem step (``handle_import_audio``); this branch is the
            # backstop for any future caller that skips that gate.
            logger.error(
                "meetings: import snapshot refused: this platform has no pinned "
                "traversal (dir_fd + O_NOFOLLOW), so a user-supplied path cannot "
                "be opened race-free; source %r was NOT read",
                canonical,
            )
            return None
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
    if not created:
        # copied=True guarantees the witness fired; this is pure fail-closed
        # paranoia so an identity-less snapshot can never be pinned.
        return None
    return dst, (created[-1].st_dev, created[-1].st_ino)


def _open_snapshot_pinned(snapshot: str, expected_ident: "tuple[int, int]") -> int | None:
    """Open the finished snapshot ONCE; every consumer reads this descriptor.

    The copy pins the SOURCE, but the snapshot itself was still consumed by
    name — opened once by the duration probe and again by the transcoder. The
    snapshot dir is 0700, yet this module's threat model already treats a
    same-uid agent as a live racing adversary, and such a racer could swap the
    file between those two opens: the probe passes the cap on the real
    recording, the transcoder decodes the replacement, and a silently wrong
    transcript returns 200 (GPT review r21). Opening once and handing BOTH
    consumers this descriptor makes that window unreachable.

    *expected_ident* closes the remaining pre-pin window (GPT review r24): a
    swap landing between the copy's completion and THIS open would otherwise
    be pinned and consistently transcribed. The open is accepted only when
    the descriptor's ``(st_dev, st_ino)`` equals the identity the copy
    published (``_snapshot_recording``'s witness), so the pinned inode is
    provably the one the pinned-source copy created — no name-trusting step
    remains anywhere between validation and consumption.

    Returns the descriptor — ownership stays with the caller — or ``None``
    when the entry is not the regular, link-free file this request's copy
    created.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(snapshot, flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
    except OSError:
        os.close(fd)
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or (st.st_dev, st.st_ino) != expected_ident:
        os.close(fd)
        return None
    return fd


def _pinned_consumer_path(snapshot: str, snap_fd: int) -> str:
    """The input path the probe and the transcriber consume.

    POSIX: a ``/dev/fd`` path, so every open — in-process readers and FFmpeg
    children alike (the spawn seam inherits the descriptor) — resolves the
    pinned inode, never the mutable name. Windows has no ``/dev/fd``; there
    the NAME stays safe to reuse because *snap_fd* is held open without
    delete sharing, which blocks the rename/delete a swap needs until the
    descriptor is closed.
    """
    if platform_compat.IS_WINDOWS:
        return snapshot
    return f"/dev/fd/{snap_fd}"


def _close_then_rmtree(snap_fd: "int | None", snapshot_dir: str) -> None:
    """Close the pinned descriptor, THEN remove the snapshot dir. One worker.

    Both are filesystem calls, so both stay off the event loop (the AUTOSDE
    no-blocking-call-on-event-loop rule names ``os.close`` explicitly — GPT
    review r22), and the ordering inside the single thread hop is what keeps
    Windows correct: the held handle blocks the directory removal until it is
    closed.
    """
    if snap_fd is not None:
        with contextlib.suppress(OSError):
            os.close(snap_fd)
    shutil.rmtree(snapshot_dir, ignore_errors=True)


async def _remove_snapshot_dir(
    copy_task: "asyncio.Future[Any] | None", snapshot_dir: str, snap_fd: "int | None" = None
) -> None:
    """Join the snapshot copy, THEN close the pin and remove its directory.

    The order is the point (GPT review r12): ``asyncio.to_thread`` workers are
    not cancellable, so a request cancelled mid-copy leaves the worker thread
    holding an open destination handle inside ``snapshot_dir``. Removing the
    directory concurrently loses that race on Windows -- ``rmtree`` cannot
    delete a file with an open handle, ``ignore_errors`` swallows the failure,
    and the stale recording accumulates until the temp volume fills. Awaiting
    the copy future first means the thread has exited and closed its handles
    before the first unlink; a copy that failed re-raises on that await, which
    is suppressed here because the removal must happen either way.

    ``snap_fd`` is the route's pinned snapshot descriptor (see
    ``_open_snapshot_pinned``); it rides this task so its close inherits the
    same run-as-own-task + shield protection as the removal, and happens in
    the same worker BEFORE ``rmtree`` (the held handle would otherwise block
    the directory removal on Windows).

    Run as its own task (``ensure_future``) so a REPEAT cancellation of the
    request abandons only the caller's wait -- this coroutine keeps running on
    the loop and still closes the pin and removes the directory after the join.
    """
    if copy_task is not None:
        with contextlib.suppress(BaseException):
            await asyncio.shield(copy_task)
    await asyncio.to_thread(_close_then_rmtree, snap_fd, snapshot_dir)


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
        # Platform gate BEFORE any filesystem step. Without kernel-level pinned
        # traversal (``dir_fd`` + ``O_NOFOLLOW``) there is no sequence of by-name
        # checks that opens a USER-SUPPLIED path race-free: a same-uid actor can
        # swap an ancestor for a junction between any sweep and the open, and on
        # Windows a junction to a UNC path makes ``os.open`` itself fire outbound
        # SMB authentication before any descriptor check can reject it (GPT
        # review r31). ``snapshot.py``'s notification copy records the repo-wide
        # ruling for exactly this shape: decline the hand-rolled ctypes walk and
        # refuse loudly rather than narrow the window. 501, because the request
        # is well-formed and the capability is what this platform lacks.
        if not await asyncio.to_thread(supports_pinned_walk):
            _reject("platform_unsupported")
            raise BadRequest(
                "importing a recording is not supported on this platform",
                status=501,
                code="import_unsupported_on_platform",
            )
        canonical, vetted_ident, reason = await asyncio.to_thread(_vet_audio_file, raw_path)
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
        if vetted_ident is None:
            # Cannot happen when reason is empty (the vet returns the identity on
            # every success path) — but the identity is the thing the snapshot
            # copy verifies against, so its absence fails closed like a denial
            # rather than being asserted away.
            _reject("denied")
            raise BadRequest("that path cannot be read", status=403, code="audio_path_denied")

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
        # user-writable name. The snapshot directory is this request's own 0700 dir
        # beneath the agent-denied voice-runtime root (see `_new_snapshot_dir` — a
        # system-tmp dir would leave the bytes rewritable in place by a same-uid
        # agent, defeating the inode pin), deleted on every exit path below.
        snapshot_dir = await asyncio.to_thread(_new_snapshot_dir)
        copy_task: "asyncio.Future[tuple[str, tuple[int, int]] | None] | None" = None
        snap_fd: int | None = None
        try:
            # The copy runs as its OWN future, shielded: a ``to_thread`` worker
            # cannot be cancelled anyway, so shielding just makes the bookkeeping
            # honest -- the future stays alive for the cleanup below to JOIN, and
            # a cancelled request abandons the wait rather than orphaning a
            # thread that still holds handles inside ``snapshot_dir``.
            copy_task = asyncio.ensure_future(
                asyncio.to_thread(_snapshot_recording, canonical, snapshot_dir, vetted_ident)
            )
            snapped = await asyncio.shield(copy_task)
            if snapped is None:
                # The file changed identity between validation and the copy — the
                # same answer as any other unreadable path, for the same reason.
                _reject("denied")
                raise BadRequest("that path cannot be read", status=403, code="audio_path_denied")
            snapshot, snap_ident = snapped

            # ONE open for everything below (see ``_open_snapshot_pinned``): the
            # probe and the transcoder consume this descriptor, never the name —
            # and the open is accepted only if it reaches the exact inode the
            # copy published, so nothing swapped in at the name (before OR after
            # this open) can ever be transcribed. Closed in the finally.
            snap_fd = await asyncio.to_thread(_open_snapshot_pinned, snapshot, snap_ident)
            if snap_fd is None:
                _reject("denied")
                raise BadRequest("that path cannot be read", status=403, code="audio_path_denied")
            pinned = _pinned_consumer_path(snapshot, snap_fd)

            # BEFORE transcription, because the local recogniser's decode paths stop
            # reading at their ceiling WITHOUT saying so: a recording over the cap
            # would transcribe its first hour, dispatch it, and return 200 — silent
            # data loss, the exact failure the TranscriptTooLong branch below refuses.
            # Provider-aware: the local decoder and the Apple lane (whose
            # to-native remux is ``-t``-bounded the same way) share the
            # ceiling, while AWS refuses an oversized payload loudly and so is
            # not wrongly refused here.
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
                    pinned, cap_secs, timeout_secs=stt_config.timeout_secs
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

            transcript = await transcribe_audio(pinned, stt_config)
        finally:
            # Join-then-close-then-remove, as its own task (see
            # ``_remove_snapshot_dir``): scheduled before it is awaited so a
            # repeat cancellation abandons only the wait, never the join, the
            # descriptor close, or the removal. The pinned descriptor rides
            # the task rather than being closed here — ``os.close`` is a
            # blocking filesystem call and must stay off the event loop.
            rm = asyncio.ensure_future(_remove_snapshot_dir(copy_task, snapshot_dir, snap_fd))
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
