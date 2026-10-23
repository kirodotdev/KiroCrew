"""AWS Transcribe, the paid provider adapted onto :func:`kiro_crew.transcribe.transcribe_audio`.

Whether its client libraries are importable, the profile credential resolver, the
streaming transcription of one file, and the custom-vocabulary listing the settings
panel offers. The optional 'voice' extra is imported here, and this module stays
importable without it.

:mod:`kiro_crew.transcribe` stays the import path and the patch surface; see
:mod:`kiro_crew.transcribe_ffmpeg`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from dataclasses import dataclass
from typing import Any

from kiro_crew import aws_consent, stt
from kiro_crew.extras import install_hint
from kiro_crew.transcribe_ffmpeg import (
    _close_ffmpeg_for_execution,
    _create_ffmpeg_subprocess,
    _describe_ffmpeg_exit,
    _forced_demuxer_args,
    _input_suffix,
    _resolve_ffmpeg_for_execution,
    _unlink_if_exists,
)

# Logs under the facade's name, which operators and tests filter on.
logger = logging.getLogger("kiro_crew.transcribe")


# Transcribe-path deps are the OPTIONAL 'voice' extra (amazon-transcribe + boto3).
# The module MUST stay importable when they're absent (default install, partial
# install, pip mid-install) so that `cli_doctor` — which imports this module —
# can surface the missing-deps diagnostic. Methods that actually use boto3 or
# the Credentials class are only invoked when stt.provider == "transcribe" and
# a profile is configured, so absence here is harmless for non-STT use. The
# local recogniser (default STT provider) needs neither.
try:
    import boto3
    from amazon_transcribe.auth import CredentialResolver, Credentials
except ImportError:  # pragma: no cover — covered by cli_doctor tests
    boto3 = None  # type: ignore[assignment,misc]
    CredentialResolver = object  # type: ignore[assignment,misc]
    Credentials = None  # type: ignore[assignment,misc]


#: What to do about a missing AWS Transcribe client. Named once because doctor,
#: the settings panel and the failure log all report it, and a divergent copy
#: sends a user to install the wrong thing.
_VOICE_EXTRA_HINT = f"AWS Transcribe needs its cloud dependencies: {install_hint('voice-aws')}"


def _aws_availability() -> stt.Availability:
    """Whether the AWS Transcribe client libraries are importable.

    Consent is deliberately NOT consulted here. ``aws_consent.refuse_and_log``
    records an audit entry, and this predicate is polled (once per inbound Slack
    message, on every settings read), so asking it here would fill the audit log
    with refusals nobody requested. The paid-service gate stays at the point
    where audio would actually leave the host.
    """
    if boto3 is None:
        return stt.Availability(False, stt.CODE_EXTRA_MISSING, _VOICE_EXTRA_HINT)
    try:
        import amazon_transcribe  # noqa: F401
    except ImportError:
        return stt.Availability(False, stt.CODE_EXTRA_MISSING, _VOICE_EXTRA_HINT)
    return stt.Availability(True)


class _ProfileCredentialResolver(CredentialResolver):
    """Async credential resolver that delegates to a boto3 Session profile.

    Built per stream, and resolves once: the SDK asks again for every audio event,
    and each event's signature chains from the opening request's, so a key the
    session refreshed mid-stream would have Transcribe refuse the next event.
    """

    def __init__(self, profile: str) -> None:
        if boto3 is None:  # pragma: no cover (the optional 'voice' extra is absent)
            raise RuntimeError(
                "AWS Transcribe support is not available: install the optional "
                f"dependencies ({install_hint('voice-aws')})."
            )
        self._session = boto3.Session(profile_name=profile)
        self._credentials: Credentials | None = None

    async def get_credentials(self) -> Credentials | None:
        if self._credentials is not None:
            return self._credentials
        loop = asyncio.get_running_loop()
        creds = await loop.run_in_executor(None, lambda: self._session.get_credentials())
        if creds is None:
            # Profile name in error is safe — only logged server-side via
            # logger.exception in _transcribe_aws, never exposed in HTTP responses.
            raise RuntimeError(
                f"No AWS credentials found for profile '{self._session.profile_name}'"
            )
        frozen = await loop.run_in_executor(None, creds.get_frozen_credentials)
        self._credentials = Credentials(frozen.access_key, frozen.secret_key, frozen.token)
        return self._credentials


#: Sample rate declared to AWS Transcribe for the ogg-opus stream. Chrome's
#: MediaRecorder with the opus codec defaults to 48 kHz; a different rate here
#: makes Transcribe reject or garble the stream. Unrelated to the recogniser's
#: 16 kHz (``stt.SAMPLE_RATE_HZ``): this one describes bytes already encoded by a
#: browser, that one describes samples we hand to a decoder.
_TRANSCRIBE_SAMPLE_RATE_HZ = 48000


_TRANSCRIBE_MAX_BYTES = 25 * 1024 * 1024  # 25 MB Transcribe API limit


def _load_aws_transcribe_components() -> tuple[Any, Any]:
    """Import optional AWS Transcribe components outside the event loop."""
    from amazon_transcribe.client import TranscribeStreamingClient
    from amazon_transcribe.handlers import TranscriptResultStreamHandler
    from amazon_transcribe.model import TranscriptEvent

    class TranscriptCollector(TranscriptResultStreamHandler):
        def __init__(self, output_stream: Any, transcript_parts: list[str]) -> None:
            super().__init__(output_stream)
            self._transcript_parts = transcript_parts

        async def handle_transcript_event(self, transcript_event: TranscriptEvent) -> None:
            for result in transcript_event.transcript.results:
                if not result.is_partial and result.alternatives:
                    self._transcript_parts.append(result.alternatives[0].transcript)

    return TranscribeStreamingClient, TranscriptCollector


def _make_temp_ogg() -> str:
    """Create and close a temporary OGG file without leaking its descriptor."""
    fd, path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    return path


def _read_audio_bytes(audio_path: str) -> bytes:
    """Read at most one byte beyond AWS Transcribe's upload limit."""
    with open(audio_path, "rb") as audio_file:
        return audio_file.read(_TRANSCRIBE_MAX_BYTES + 1)


async def _transcribe_aws(audio_path: str, stt_config) -> str | None:  # type: ignore[no-untyped-def]
    """Transcribe using AWS Transcribe Streaming API (ogg-opus)."""
    ext = _input_suffix(audio_path)
    if ext not in (".ogg", ".webm"):
        logger.error("Unsupported format '%s' for Transcribe (expected .ogg or .webm)", ext)
        return None

    # Transcribe is a PAID AWS service, so no audio leaves the host without a
    # recorded operator consent for this exact profile+region. Checked before
    # the optional-dependency probe and before any remux work, so a refusal
    # costs nothing and no temp file is created. Returning None is this
    # function's established failure contract.
    if not await aws_consent.refuse_and_log(
        aws_consent.SERVICE_TRANSCRIBE,
        profile=stt_config.transcribe_profile,
        region=stt_config.transcribe_region,
    ):
        return None

    # amazon-transcribe + boto3 are the optional 'voice' extra. Absent on a
    # vanilla install → report not available rather than raising ImportError.
    if boto3 is None:
        logger.error("AWS Transcribe not available: %s", install_hint("voice-aws"))
        return None
    try:
        TranscribeStreamingClient, TranscriptCollector = await asyncio.to_thread(
            _load_aws_transcribe_components
        )
    except ImportError:
        logger.error("AWS Transcribe not available: %s", install_hint("voice-aws"))
        return None

    region = stt_config.transcribe_region
    tmp_ogg = None
    actual_path = audio_path
    if ext in (".webm",):
        try:
            # BEFORE the decoder handle is resolved: a refusal here must not
            # leak the authenticated FFmpeg descriptor the seam would own.
            demux_args = _forced_demuxer_args(audio_path)
        except OSError:
            logger.exception("Could not resolve a demuxer to remux %s", audio_path)
            return None
        ffmpeg_bin = await _resolve_ffmpeg_for_execution()
        if not ffmpeg_bin:
            logger.error("ffmpeg required to remux webm to ogg for Transcribe")
            return None
        try:
            tmp_ogg = await asyncio.to_thread(_make_temp_ogg)
        except BaseException:
            await _close_ffmpeg_for_execution(ffmpeg_bin, preserve_active_exception=True)
            raise
        proc = None
        try:
            try:
                proc = await _create_ffmpeg_subprocess(
                    ffmpeg_bin,
                    "-y",
                    *demux_args,
                    "-i",
                    audio_path,
                    # Audio only: a Slack video/webm clip also carries a video
                    # track, and ogg's default video encoder (theora) is not in
                    # the audio-only decoder, so mapping it fails the remux.
                    "-vn",
                    "-c:a",
                    "copy",
                    tmp_ogg,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.communicate()
                    raise
                if proc.returncode != 0:
                    tail = stderr.decode(errors="replace").strip()[-500:] if stderr else ""
                    raise RuntimeError(f"ffmpeg {_describe_ffmpeg_exit(proc.returncode, tail)}")
            except Exception:
                logger.exception("ffmpeg remux failed for %s", audio_path)
                if tmp_ogg:
                    await asyncio.to_thread(_unlink_if_exists, tmp_ogg)
                return None
            except BaseException:
                # ``CancelledError`` derives from ``BaseException``, so the
                # ``Exception`` guard above never sees it: a cancellation landing
                # mid-``communicate`` would leave the ffmpeg child running and the
                # owned temp on disk. Mirror ``_to_native_audio``'s cleanup:
                # stop AND reap the child BEFORE the unlink — Windows keeps
                # the output file locked until the child fully exits, and on POSIX a
                # live child can race the removal. Every step is best-effort, and
                # the unlink stays synchronous (one file): a
                # repeat cancellation could eat an off-loop hop before it runs. The
                # exception in flight is the one that must surface.
                if proc is not None:
                    try:
                        proc.kill()
                    except (OSError, ProcessLookupError):
                        logger.debug(
                            "ffmpeg kill during cancellation cleanup failed",
                            exc_info=True,
                        )
                    else:
                        try:
                            await proc.communicate()
                        except BaseException:
                            # A repeat cancellation can land on this await; swallow
                            # it so the unlink below still runs and the ORIGINAL
                            # exception is the one that propagates.
                            pass
                if tmp_ogg:
                    try:
                        _unlink_if_exists(tmp_ogg)
                    except OSError:
                        # A not-yet-exited child can still hold the file (Windows
                        # lock); letting that escape would REPLACE the in-flight
                        # cancellation with a PermissionError.
                        pass
                raise
        finally:
            # The authenticated handle must outlive the spawn: every
            # branch above has already reaped the child (``communicate`` on
            # success and on a nonzero exit, kill-and-reap on timeout and on
            # cancellation), so the staged image can be released now. The
            # ``finally`` makes the close unconditional — the staged 0700
            # directory must never leak. ``preserve_active_exception`` is set
            # only while an exception is genuinely in flight, so a cleanup
            # failure never masks the original error and a cancellation landing
            # on the close await of a success path still propagates.
            try:
                await _close_ffmpeg_for_execution(
                    ffmpeg_bin,
                    preserve_active_exception=sys.exc_info()[1] is not None,
                )
            except BaseException:
                # This await is the only suspension point between the remux
                # child exiting and ``actual_path`` taking ownership of the
                # temp. A cancellation landing exactly here (it can only raise
                # on the no-exception-in-flight path) would otherwise propagate
                # with ``tmp_ogg`` still on disk; the failure branches already
                # unlinked, and ``_unlink_if_exists`` tolerates that.
                if tmp_ogg:
                    try:
                        _unlink_if_exists(tmp_ogg)
                    except OSError:
                        pass
                raise
        actual_path = tmp_ogg

    transcript_parts: list[str] = []
    stream = None
    try:
        audio_bytes = await asyncio.to_thread(_read_audio_bytes, actual_path)
        if len(audio_bytes) > _TRANSCRIBE_MAX_BYTES:
            logger.error(
                "Audio file too large for Transcribe: >%d bytes",
                _TRANSCRIBE_MAX_BYTES,
            )
            return None

        profile = stt_config.transcribe_profile or None
        credential_resolver = (
            await asyncio.to_thread(_ProfileCredentialResolver, profile) if profile else None
        )

        client = await asyncio.to_thread(
            TranscribeStreamingClient,
            region=region,
            credential_resolver=credential_resolver,
        )
        stream = await client.start_stream_transcription(
            language_code=stt_config.effective_language_code,
            media_sample_rate_hz=_TRANSCRIBE_SAMPLE_RATE_HZ,
            media_encoding="ogg-opus",
            # Same vocabulary as live dictation, so a voice memo hears the same
            # names. A refusal lands in the ``except`` below like any other.
            vocabulary_name=stt_config.transcribe_vocabulary or None,
        )

        async def write_chunks():
            chunk_size = 8192
            for i in range(0, len(audio_bytes), chunk_size):
                await stream.input_stream.send_audio_event(
                    audio_chunk=audio_bytes[i : i + chunk_size]
                )
            await stream.input_stream.end_stream()

        handler = TranscriptCollector(stream.output_stream, transcript_parts)
        await asyncio.wait_for(
            asyncio.gather(write_chunks(), handler.handle_events()),
            timeout=stt_config.timeout_secs,
        )

        # A stream that completed with no final results is a recording Transcribe
        # heard nothing in, not a failure: ``""`` here, None only from the
        # ``except`` below. See :func:`transcribe_audio` for why the two differ.
        return " ".join(transcript_parts).strip()
    except Exception:
        logger.exception("AWS Transcribe streaming STT failed")
        return None
    finally:
        # Nested ``finally`` so the unlink is unconditional: the ``end_stream``
        # await can itself raise on a REPEAT cancellation (``CancelledError`` is
        # a ``BaseException``, so its ``Exception`` guard misses it), and that
        # escape would otherwise skip the temp removal below.
        try:
            if stream is not None:
                try:
                    await stream.input_stream.end_stream()
                except Exception:
                    pass
        finally:
            if tmp_ogg:
                try:
                    await asyncio.to_thread(_unlink_if_exists, tmp_ogg)
                except BaseException:
                    # A repeat cancellation can land on this await before the
                    # off-loop hop runs; unlink synchronously (one file) and
                    # let the cancellation propagate. The
                    # OSError guard keeps a locked/contended file from
                    # REPLACING the exception already in flight.
                    try:
                        _unlink_if_exists(tmp_ogg)
                    except OSError:
                        pass
                    raise


# ---------------------------------------------------------------------------
# Custom vocabularies (Amazon Transcribe)
# ---------------------------------------------------------------------------

#: Why :func:`list_custom_vocabularies` could not answer, as served by
#: ``GET /api/stt/vocabularies``. Two codes because the fixes differ: a refused
#: ``transcribe:ListVocabularies`` needs an IAM change, anything else (no
#: credentials, no network, a timeout) needs the credentials or the connection.
VOCABULARIES_ACCESS_DENIED = "stt_vocabularies_access_denied"


VOCABULARIES_LIST_FAILED = "stt_vocabularies_list_failed"


#: The IAM action an access denial needs granted. Served with that refusal as data,
#: so the panel can name it without carrying an AWS identifier of its own.
VOCABULARIES_LIST_PERMISSION = "transcribe:ListVocabularies"


#: ``ListVocabularies`` error codes that mean the credentials may not list.
_VOCABULARY_ACCESS_DENIED_ERRORS = frozenset({"AccessDeniedException", "AccessDenied"})


#: One page, the API's own maximum.
_VOCABULARY_PAGE_SIZE = 100


#: Pages followed before stopping. Amazon Transcribe's default quota is 100 custom
#: vocabularies per region, so one page is the normal case and this only bounds a
#: raised quota or a NextToken that never ends.
_VOCABULARY_MAX_PAGES = 10


#: Per-attempt network bounds for the listing. It runs on a worker thread while a
#: settings panel waits, so neither botocore default (60 s each) applies.
_VOCABULARY_CONNECT_TIMEOUT_SECS = 5


_VOCABULARY_READ_TIMEOUT_SECS = 10


@dataclass(frozen=True)
class CustomVocabulary:
    """One custom vocabulary as ``ListVocabularies`` reports it.

    ``state`` is the service's ``VocabularyState`` (``READY``, ``PENDING`` or
    ``FAILED``). Only a ``READY`` one can be used; the others are kept so a caller
    can tell "still processing" from "does not exist".
    """

    name: str
    language_code: str
    state: str


@dataclass(frozen=True)
class CustomVocabularyListing:
    """Custom vocabularies returned by AWS and whether more pages remain."""

    vocabularies: list[CustomVocabulary]
    truncated: bool


class VocabularyListError(Exception):
    """Listing failed; ``code`` is one of the ``VOCABULARIES_*`` codes."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# Named by the path callers import it from: a traceback or a log line renders the
# raised class as ``module.qualname``, so the owner module must not leak into it.
VocabularyListError.__module__ = "kiro_crew.transcribe"


def list_custom_vocabularies(profile: str, region: str) -> CustomVocabularyListing:
    """Custom vocabularies *profile* can see in *region*, sorted by name.

    ``truncated`` is true when the page cap stopped the listing with a next page
    still available.

    Blocking: call it from a worker thread. Resolves credentials the way the
    stream does, so the list shows what a dictation could actually use: the named
    profile, or the default chain when *profile* is empty.

    Never raises anything but :class:`VocabularyListError`. The service's own
    message stays in the log, because an access-denied message names the caller's
    ARN and the caller only needs to know which fix applies.
    """
    if boto3 is None:
        logger.warning("Custom vocabularies need the AWS packages: %s", install_hint("voice-aws"))
        raise VocabularyListError(VOCABULARIES_LIST_FAILED)
    # botocore ships with boto3, which is present by this point.
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ClientError

    found: list[CustomVocabulary] = []
    try:
        client = boto3.Session(profile_name=profile or None).client(
            "transcribe",
            region_name=region or None,
            config=Config(
                connect_timeout=_VOCABULARY_CONNECT_TIMEOUT_SECS,
                read_timeout=_VOCABULARY_READ_TIMEOUT_SECS,
                retries={"max_attempts": 2, "mode": "standard"},
            ),
        )
        token = ""
        for _ in range(_VOCABULARY_MAX_PAGES):
            request: dict[str, Any] = {"MaxResults": _VOCABULARY_PAGE_SIZE}
            if token:
                request["NextToken"] = token
            page = client.list_vocabularies(**request)
            for item in page.get("Vocabularies") or []:
                name = item.get("VocabularyName")
                if isinstance(name, str) and name:
                    found.append(
                        CustomVocabulary(
                            name=name,
                            language_code=str(item.get("LanguageCode") or ""),
                            state=str(item.get("VocabularyState") or ""),
                        )
                    )
            token = page.get("NextToken") or ""
            if not token:
                break
        else:
            logger.warning(
                "Stopped listing custom vocabularies in %s after %d pages; the rest are "
                "not offered",
                region,
                _VOCABULARY_MAX_PAGES,
            )
    except ClientError as exc:
        error_code = str(exc.response.get("Error", {}).get("Code", ""))
        logger.warning("ListVocabularies refused in %s: %s", region, exc)
        raise VocabularyListError(
            VOCABULARIES_ACCESS_DENIED
            if error_code in _VOCABULARY_ACCESS_DENIED_ERRORS
            else VOCABULARIES_LIST_FAILED
        ) from exc
    except (BotoCoreError, ValueError) as exc:
        # BotoCoreError covers a missing profile, no credentials, no region and
        # every connection failure; ValueError an unusable region name.
        logger.warning("ListVocabularies failed in %s: %s", region, exc)
        raise VocabularyListError(VOCABULARIES_LIST_FAILED) from exc
    return CustomVocabularyListing(
        vocabularies=sorted(found, key=lambda v: (v.name.casefold(), v.name)),
        truncated=bool(token),
    )
