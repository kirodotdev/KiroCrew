"""Speech-to-text for whole audio files: one local recogniser, two adapted ones.

The default provider is ``local``: the whisper.cpp recogniser that
:mod:`kiro_crew.stt` holds loaded in this process. Keeping the model resident is
what makes it usable, because the cost that made local speech-to-text feel broken
was never the decode. A warm decode of 4.2 s of audio measures 30-48 ms (real-time
factor 0.007-0.011) and a 0.9 s push-to-talk utterance 27 ms, against seconds per
utterance for anything that loads a model per recording. It needs no external
binary and it works on every OS Kiro Crew supports.

Two further providers are *adapted* onto the same seam rather than being
first-class, and neither may add a step to the local path:

- ``apple``: Apple's on-device SpeechAnalyzer (macOS 26+), which downloads no
  model because the OS ships the assets. Owned by :mod:`kiro_crew.apple_speech`.
- ``transcribe``: AWS Transcribe Streaming, a paid service, gated on the recorded
  operator consent in :mod:`kiro_crew.aws_consent`.

Compressed input still needs ffmpeg: a Slack voice memo arrives as ogg/Opus and
the dashboard records webm. Desktop releases carry a pinned audio-only decoder
executable (:mod:`kiro_crew.stt.decoder`), so desktop users never install a system binary separately --
except on macOS Intel, whose release ships no decoder and resolves exactly like a
source install. Source installs use a system FFmpeg from fixed platform paths, or
the digest-verified store :mod:`kiro_crew.stt.decoder` fetches the same pinned
bytes into. A 16 kHz mono WAV and live PCM skip the executable entirely.

Two guards here are deliberately provider-independent, because a per-branch copy
is a copy that will be missing from the next branch someone adds:
:func:`_is_sensitive_audio_path` refuses before any provider is dispatched, and
:func:`_redact_transcript` runs on every provider's output.
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import logging
import os
import re
import shutil
import sys
import sys as _sys
import wave
from typing import TYPE_CHECKING, Any

from kiro_crew import _facade_mirror, aws_consent, pinned_fs, platform_compat, stt  # noqa: F401
from kiro_crew.extras import install_hint  # noqa: F401

# The pinned-artifact table and the digest-verified decoder store live here. It
# imports no numpy and no recogniser binding, so this stays cheap on the gateway
# boot path; `stt.decoder` in turn imports THIS module only inside a function,
# which is what keeps the pair acyclic.
from kiro_crew.stt import decoder

# Re-exported: the hallucination filter lives in kiro_crew.stt.hallucinations so
# the live session's final transcript and this batch path apply the SAME rules.
# The name stays importable from here because that is where callers have always
# found it, and one filter with two import paths cannot drift.
from kiro_crew.stt.hallucinations import filter_hallucinations  # noqa: F401
from kiro_crew.transcribe_aws import _aws_availability, _transcribe_aws
from kiro_crew.transcribe_ffmpeg import (
    _FFMPEG_CANDIDATE_DIRS,
    _bundled_ffmpeg,
    _close_ffmpeg_for_execution,
    _create_ffmpeg_subprocess,
    _describe_ffmpeg_exit,
    _dev_fd_number,
    _find_system_ffmpeg,
    _forced_demuxer_args,
    _kill_and_reap,
    _resolve_ffmpeg_for_execution,
    _store_ffmpeg,
    _unlink_if_exists,
)
from kiro_crew.transcribe_local import _MAX_AUDIO_SECS, _transcribe_apple, _transcribe_local

logger = logging.getLogger(__name__)


def ensure_ffmpeg_in_path() -> None:
    """Add known ffmpeg directories to PATH if they contain an ffmpeg binary.

    Probes each candidate dir with ``shutil.which("ffmpeg", path=d)`` — that
    honours ``PATHEXT`` on Windows (so ``ffmpeg.exe`` resolves) while still
    matching a plain ``ffmpeg`` on POSIX.
    """
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    for d in reversed(_FFMPEG_CANDIDATE_DIRS):
        if d in path_parts:
            continue
        if shutil.which("ffmpeg", path=d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            path_parts.insert(0, d)


def _find_ffmpeg() -> str | None:
    """Report the authenticated bundle path, a fixed-path system FFmpeg, or the store.

    Deliberately NOT ``shutil.which("ffmpeg")``. A gateway's PATH can legitimately lead
    with agent-writable directories (a worktree venv's ``bin``, ``~/.local/bin``), which
    is exactly the threat `platform_compat.trusted_system_bin` documents: the result
    here describes what the execution resolver would use. Execution itself calls
    :func:`_open_ffmpeg_for_execution`, which keeps bundled bytes bound through spawn.

    The trusted system directories are tried first, then the fixed candidate list,
    which is itself ordered most-trusted first. ffmpeg is not an OS tool -- on macOS it
    lives under a Homebrew prefix and on Windows under a package-manager directory --
    so the system set alone would find it almost nowhere.

    Last comes the digest-verified store (:func:`_store_ffmpeg`), which is a
    filename-and-digest match rather than a directory the search trusts; see
    :func:`_open_store_ffmpeg_resource` for why that is not the same widening.

    Reached through `trusted_system_path` rather than `trusted_system_bin` because that
    helper warns once per name when a tool is on PATH but outside the system set, and
    that message states the caller "degrades instead of running a PATH-chosen binary".
    Here it does not: the candidate list below finds the packaged ffmpeg and uses it, so
    borrowing the helper would log a degradation that never happens on every macOS host
    with Homebrew. ``None`` from it means Windows, where the search must NOT fall back
    to `which`'s default (the ambient PATH) and the candidate list already carries the
    package-manager directories.
    """
    if decoder.bundle_carries_decoder():
        return _bundled_ffmpeg()
    system = _find_system_ffmpeg()
    if system is not None:
        return system
    return _store_ffmpeg()


#: Where the decoder the transcode path would run comes from, as reported by
#: :func:`ffmpeg_source` and served on ``GET /api/stt/status``. Codes rather than
#: prose because the dashboard renders localised text, and each one leads
#: somewhere different: a bundled payload is repaired by reinstalling the app, a
#: system one by the host's package manager, and the store one by
#: ``stt.decoder``'s own fetch.
FFMPEG_SOURCE_BUNDLED = "bundled"
FFMPEG_SOURCE_SYSTEM = "system"
FFMPEG_SOURCE_STORE = "store"


def ffmpeg_source() -> str | None:
    """Which of the three decoder sources answers on this host, or ``None``.

    Resolved in the same order :func:`_open_ffmpeg_for_execution` uses, so the
    settings panel names the decoder that would actually run rather than the first
    one that happens to exist.
    """
    if decoder.bundle_carries_decoder():
        return FFMPEG_SOURCE_BUNDLED if _bundled_ffmpeg() is not None else None
    if _find_system_ffmpeg() is not None:
        return FFMPEG_SOURCE_SYSTEM
    if _store_ffmpeg() is not None:
        return FFMPEG_SOURCE_STORE
    return None


# Homebrew installs its ``brew`` shim at a fixed prefix per platform, and none of
# those prefixes are on the PATH a GUI-launched gateway inherits: the desktop app
# (Dock / Finder / launchd) starts with ``/usr/bin:/bin:/usr/sbin:/sbin``, so
# ``shutil.which("brew")`` reports Homebrew MISSING on a machine that has it.
# Probing the prefixes directly is what keeps the STT prereq list and the install
# script from telling a Homebrew user to install Homebrew.
_BREW_CANDIDATE_PATHS = [
    "/opt/homebrew/bin/brew",  # Apple Silicon macOS
    "/usr/local/bin/brew",  # Intel macOS
    "/home/linuxbrew/.linuxbrew/bin/brew",  # Linuxbrew, system install
    os.path.expanduser("~/.linuxbrew/bin/brew"),  # Linuxbrew, per-user install
]


def find_brew() -> str | None:
    """Return the ``brew`` binary path, or None when Homebrew is not installed.

    Falls back to the well-known install prefixes when ``brew`` is not on PATH
    (see ``_BREW_CANDIDATE_PATHS``) so a GUI-launched gateway agrees with what
    the user sees in their terminal.
    """
    found = shutil.which("brew")
    if found:
        return found
    for p in _BREW_CANDIDATE_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

# The adapted providers answer in :class:`kiro_crew.stt.Availability`, the same
# shape and the same machine-readable vocabulary the local recogniser uses, so a
# caller renders one set of reasons whichever provider is configured. The codes
# below are the ones only this module can report; the rest come from
# :mod:`kiro_crew.stt.engine`. They travel to the browser in JSON, so renaming
# one silently drops the UI back to a generic string.

#: Speech-to-text is switched off in configuration. Not a fault: it is the
#: distinction between "you turned this off" and "this cannot run here".
CODE_DISABLED = "stt_disabled"

#: The ``stt.provider`` value that selects no recogniser. Spelled here rather
#: than imported, for the same reason ``dashboard.stt_stream`` spells
#: ``PROVIDER_LOCAL``: this module keeps ``kiro_crew.config`` off its import path
#: (every config read here is inside a function, off the event loop).
#: ``test_stt_provider_off`` pins it equal to ``config.sections.STT_PROVIDER_OFF``.
STT_PROVIDER_OFF = "off"
#: ``config.sections.STT_PROVIDER_LOCAL``, spelled here for the same reason.
_STT_PROVIDER_LOCAL = "local"


def _registered_provider(provider: object) -> Any:
    """The edition-registered STT provider named *provider* (``PlatformContext.speech``).

    ``None`` for every built-in name, so the built-in branches below keep running
    exactly as before; a registered name is looked up only when it is not one.
    """
    from kiro_crew import speech_providers

    return speech_providers.stt_provider(provider)


#: ``stt.enabled`` is on but ``stt.provider`` is ``off``. Its own code, distinct
#: from :data:`CODE_DISABLED`, because the two name different controls: the
#: dashboard renders "switch it on above" for the first, and that is the wrong
#: remedy for a user whose Enabled toggle is already on -- including everyone
#: whose unknown stored provider the loader degraded onto ``off``.
CODE_PROVIDER_OFF = "stt_provider_off"

#: The selected provider is one an edition registered, and its ``check()`` says
#: it cannot run now. Not :data:`CODE_PROVIDER_OFF`: the user chose a provider,
#: and the dashboard must not say they chose Off. A code the dashboard has no
#: sentence for, so it shows ``detail`` instead.
CODE_PROVIDER_UNAVAILABLE = "stt_provider_unavailable"

#: This host cannot run Apple's on-device speech at all (not macOS, or too old a
#: macOS for the SpeechAnalyzer API). No install fixes it.
CODE_APPLE_UNSUPPORTED = "stt_apple_unsupported"

#: Apple's on-device speech could run here once the Swift toolchain is present.
#: Separated from :data:`CODE_APPLE_UNSUPPORTED` because this one has a one-line
#: fix and that one does not.
CODE_APPLE_NEEDS_TOOLCHAIN = "stt_apple_needs_toolchain"


def _apple_availability() -> stt.Availability:
    """Whether Apple's on-device speech can run, translated into one shape."""
    from kiro_crew import apple_speech

    # Stats only, never a build: this runs on the event loop (the settings read,
    # the transcribe endpoint, the Slack voice path), and compiling the Swift
    # helper there would freeze the gateway for as long as swiftc takes. The
    # build happens inside the offloaded transcribe path.
    result = apple_speech.availability()
    if result.ok:
        return stt.Availability(True)
    code = CODE_APPLE_NEEDS_TOOLCHAIN if result.needs_toolchain else CODE_APPLE_UNSUPPORTED
    return stt.Availability(False, code, result.reason)


def availability_detail(stt_config=None) -> stt.Availability:  # type: ignore[no-untyped-def]
    """Whether speech-to-text can run, and when it cannot, precisely why.

    One shape for all three providers so a caller renders one set of reasons.
    Distinguishing them is the point: "install an extra", "your platform has no
    prebuilt wheel" and "this needs a newer macOS" lead to completely different
    actions, and collapsing them into a boolean is what makes a feature feel
    broken rather than unconfigured.

    Whether the configured MODEL is on disk is deliberately not part of the
    answer. A missing model resolves itself on first use, so reporting it as
    unavailable would hide a working install behind a condition that fixes itself.
    """
    if stt_config is None:
        from kiro_crew.config.loader import KiroCrewConfig

        stt_config = KiroCrewConfig.load().stt
    if not stt_config.enabled:
        return stt.Availability(False, CODE_DISABLED, "speech-to-text is turned off")
    if stt_config.provider == STT_PROVIDER_OFF:
        # The same "nothing runs" state as above, under a different control. `off`
        # is also where the loader puts an unknown stored provider, so this is
        # the sentence that tells such a user which setting to look at.
        return stt.Availability(
            False, CODE_PROVIDER_OFF, "the speech provider is set to off; choose a provider"
        )
    provider = stt_config.provider
    if provider == "transcribe":
        return _aws_availability()
    if provider == "apple":
        return _apple_availability()
    registered = _registered_provider(provider)
    if registered is not None:
        # An edition-registered provider: its own check() answers. BLOCKING, like
        # every caller of this function already is (each runs it off the loop).
        from kiro_crew import speech_providers

        status = speech_providers.check_status_sync(registered, stt_config)
        if status.available:
            return stt.Availability(True)
        return stt.Availability(
            False,
            CODE_PROVIDER_UNAVAILABLE,
            status.reason or f"the speech provider {provider!r} cannot run now",
        )
    # ``local``: the loader admits only selectable providers, and ``off`` and a
    # registered name were answered above, so this is the resident recogniser.
    # The first call links the recogniser's native extension, then ``sys.modules``
    # makes it a dictionary lookup. A FAILED import is not cached, so a gateway
    # that booted without the extra picks up a later install with no restart.
    return stt.availability()


def is_available(stt_config=None) -> bool:  # type: ignore[no-untyped-def]
    """Whether speech-to-text is enabled and the configured provider can run.

    The boolean view of :func:`availability_detail`, derived from it rather than
    implemented beside it: two implementations of one question drift, and the pair
    that disagrees hands a caller a 503 for a provider the settings panel is
    showing as ready.
    """
    return availability_detail(stt_config).ok


def _load_stt_config() -> Any:
    """Load STT configuration without importing or reading config on the loop."""
    from kiro_crew.config.loader import KiroCrewConfig

    return KiroCrewConfig.load().stt


def load_stt_config() -> Any:
    """One STT configuration snapshot, for callers that must not re-read it.

    Every function here that takes an ``stt_config`` parameter re-loads the
    configuration when handed None. That is right for a single call, and wrong
    for a SEQUENCE whose answers must agree: a readiness check, a duration-cap
    answer, and the transcription itself each re-reading the file can straddle an
    operator changing the provider in Settings, so the gate evaluates one
    provider's rules and the decode runs under another's. A caller doing several
    of those calls loads ONE snapshot here and passes it to each. BLOCKING
    (reads config); call off the event loop.
    """
    return _load_stt_config()


def _under_voice_runtime_root(real: str) -> bool:
    """Whether *real* lies under the gateway's own voice-runtime staging root.

    The crew ``run/`` directory is a read+write-sensitive leaf (it holds spawn
    trust roots), so ``is_sensitive_path`` refuses everything beneath it —
    including the import snapshots this gateway itself stages under
    ``run/voice-runtime`` precisely BECAUSE agents cannot reach that root.
    Judged against the kernel-resolved name of an
    already-pinned descriptor, membership here means "a file this process
    staged", not "a caller-supplied path": the route's own vet gate has
    already refused sensitive ORIGINAL paths before any snapshot exists.
    """
    from kiro_crew.sandbox import prime_voice_runtime_sandbox_paths

    root = os.path.realpath(prime_voice_runtime_sandbox_paths())
    try:
        return os.path.commonpath((os.path.realpath(real), root)) == root
    except ValueError:  # different drives on Windows
        return False


def _is_sensitive_audio_path(audio_path: str) -> bool:
    """Run the filesystem-resolving sensitive-path guard off the event loop.

    A descriptor path is judged by the kernel's name for the open descriptor,
    and an unresolvable one is refused outright — the guard must never answer
    "not sensitive" for a file it cannot identify. The one exemption is the
    gateway's own voice-runtime snapshot staging (see
    :func:`_under_voice_runtime_root`), on BOTH branches: POSIX consumers hold
    a ``/dev/fd`` path, while Windows consumers hold the snapshot NAME (the
    open handle blocks rename/delete there). The membership test resolves the
    real path first, so a link planted under the root resolves outside it and
    is judged as whatever it points at.
    """
    from kiro_crew.security import is_sensitive_path

    fd = _dev_fd_number(audio_path)
    if fd is not None:
        real = pinned_fs.fd_real_path(fd)
        if real is None:
            return True
    else:
        real = audio_path
    if _under_voice_runtime_root(real):
        return False
    return is_sensitive_path(real)


def _redact_transcript(transcript: str) -> str:
    """Apply transcript redaction without consuming event-loop time."""
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    transcript, _ = redact_exfiltration_urls(transcript)
    transcript, _ = redact_credentials(transcript)
    return transcript


async def transcribe_audio(audio_path: str, stt_config=None) -> str | None:  # type: ignore[no-untyped-def]
    """Transcribe an audio file. Returns the text, ``""``, or None.

    None on every failure, and never an exception: eight channel adapters call
    this and turn None into a visible "transcription failed" note for the user,
    whereas an exception becomes a log line nobody reads and a turn that never
    starts.

    ``""`` is a SUCCESS with no transcript: the provider ran and heard nothing
    it could write down (silence, or a recording the hallucination filter
    emptied because it was entirely caption boilerplate). Every provider branch
    below keeps that distinct from None, because the transcribe endpoint answers
    ``""`` with a 200 and None with a 500 — collapsing a quiet recording onto
    the failure sentinel would report every silent memo as a broken backend.
    The channel adapters test truthiness, so ``""`` and None read the same
    there; the distinction is for callers that can tell the user which it was.
    """
    if stt_config is None:
        stt_config = await asyncio.to_thread(_load_stt_config)

    if not stt_config.enabled or stt_config.provider == STT_PROVIDER_OFF:
        logger.debug("STT disabled in config")
        return None

    # Before dispatch, for every provider. Refusing here rather than inside each
    # branch is what makes it impossible to add a provider that skips the check.
    if await asyncio.to_thread(_is_sensitive_audio_path, audio_path):
        logger.error("Refusing to read sensitive path: %s", audio_path)
        return None

    provider = stt_config.provider
    if provider == "transcribe":
        result = await _transcribe_aws(audio_path, stt_config)
    elif provider == "apple":
        result = await _transcribe_apple(audio_path, stt_config)
    elif (registered := _registered_provider(provider)) is not None:
        # An edition-registered provider. Same contract as every branch here:
        # text, "" for silence, or None -- never an exception. Called on this
        # loop; the provider contract allows no loop-bound state across calls.
        try:
            result = await registered.transcribe_file(audio_path, stt_config)
        except Exception:
            logger.warning("STT provider %r failed to transcribe", provider, exc_info=True)
            return None
        if result is not None and not isinstance(result, str):
            logger.warning("STT provider %r returned a non-text result", provider)
            return None
    else:
        # ``local``. The config loader admits only the selectable providers: a
        # retired name has already become ``local``, an unknown one ``off``
        # (refused above), and a registered one was dispatched just above.
        result = await _transcribe_local(audio_path, stt_config)

    if result:
        # Unconditional, on every provider's output, in one off-loop hop.
        result = await asyncio.to_thread(_redact_transcript, result)
    return result


def batch_duration_cap_secs(stt_config=None) -> int | None:  # type: ignore[no-untyped-def]
    """The longest recording the ACTIVE provider transcribes whole, or None.

    The local recogniser truncates: both of its decode paths stop at
    ``_MAX_AUDIO_SECS`` (the WAV reader caps ``readframes``, the ffmpeg transcode
    passes ``-t``), and neither reports that it did. The Apple lane's
    to-native conversion is bounded the same way (``-t`` on the remux — an
    unbounded conversion of a large low-bitrate input could exhaust the temp
    volume), so it shares the ceiling. AWS Transcribe refuses
    an oversized payload outright (a loud ``None``), so for it there is no
    silent ceiling to guard. Callers that must not dispatch a truncated
    transcript — the meetings import route — ask here which ceiling applies
    and refuse longer input BEFORE transcribing. BLOCKING when *stt_config*
    is None (reads config).
    """
    if stt_config is None:
        stt_config = _load_stt_config()
    # AWS fails loudly, and a provider an edition registered handles its own
    # length, so neither gets the local decoder's cap (or its duration probe).
    # Any other name, an unrecognised one included, lands on a built-in decoder
    # and keeps the ceiling.
    provider = stt_config.provider
    if provider == "transcribe" or _registered_provider(provider) is not None:
        return None
    return _MAX_AUDIO_SECS


def provider_splits_oversized(stt_config) -> bool:  # type: ignore[no-untyped-def]
    """Whether an over-cap recording should be SPLIT rather than refused.

    Only the local recogniser both HAS a ceiling and TRUNCATES SILENTLY past it
    (both decode paths stop at ``_MAX_AUDIO_SECS`` and say nothing), so it is the
    one provider where auto-splitting turns silent data loss into a transparent
    success. The Apple lane also has the ceiling but FAILS LOUDLY at it
    (``_to_native_audio`` raises ``RecordingTooLongError``), so it needs no split
    and must not get one: its Swift helper runs in the ``mode="strict"`` sandbox,
    which masks the ``run/voice-runtime`` leaf the import stages its segments
    under (``sandbox_tiers.py``), so a segment WAV handed to the helper by name is
    unreadable and every over-cap Apple import would 502. AWS Transcribe has no
    silent ceiling at all (``batch_duration_cap_secs`` is None for it). So the
    split is local-only, by design: segmentation
    only ever triggers where the ceiling exists (AWS/Apple providers fail loudly
    and need no split). Takes the caller's config snapshot as a REQUIRED
    argument (the one production caller always has it in hand, from the same
    snapshot the readiness/cap/transcribe calls share); it never reads config
    itself, so there is no window for the three answers to describe different
    providers. An unrecognised name lands on the local decoder, so it splits
    too; a provider an edition registered handles its own length and does not.
    """
    provider = stt_config.provider
    if provider in ("transcribe", "apple"):
        return False
    return _registered_provider(provider) is None


def _wav_duration_secs(audio_path: str) -> float | None:
    """Exact duration from a WAV header, or None when it is not a readable WAV.

    Header math only — no sample data is read — so this works for any rate or
    width, including files :func:`_pcm_from_wav` would hand to ffmpeg. BLOCKING.
    """
    try:
        with wave.open(audio_path, "rb") as wav:
            rate = wav.getframerate()
            if rate <= 0:
                return None
            return wav.getnframes() / rate
    except (OSError, EOFError, wave.Error):
        return None


_PROGRESS_OUT_TIME_RE = re.compile(rb"^out_time_us=(\d+)", re.MULTILINE)


async def audio_exceeds_secs(
    audio_path: str, max_secs: int, *, timeout_secs: int = 300
) -> bool | None:
    """Whether the recording at *audio_path* is longer than *max_secs*.

    True/False when the answer is known, None when it cannot be determined.

    WAV files are answered exactly from the header. Everything else is answered
    by the same decoder that will transcribe it: a null decode bounded at
    ``max_secs`` plus one second (``-t``), reading the decoded timestamp from
    ffmpeg's ``-progress`` stream. Decoding — not metadata — is deliberate: the
    dashboard's own recordings are MediaRecorder webm, whose header carries no
    duration at all, so a metadata probe would answer None for exactly the files
    users are most likely to import. The ``-t`` bound keeps the probe's cost
    proportional to the cap, not to the file.

    A None is honest, not fail-open in disguise, ONLY while the caller gives the
    probe at least the timeout the transcode itself will get (callers with an
    ``stt_config`` in hand pass ``stt_config.timeout_secs``; the default matches
    the config default). The probe decodes at most ``max_secs + 1`` seconds — a
    strict subset of the transcode's work — so under an aligned budget every
    None cause leads to a loud downstream failure: an undecodable file fails the
    transcode the same way, and a host slow enough to time the probe out times
    the strictly-larger transcode out too. A SHORTER probe budget would reopen
    the gap where the probe gives up but the transcode "succeeds" truncated —
    silent data loss on exactly the over-cap files this guard exists to catch.
    """
    duration = await asyncio.to_thread(_wav_duration_secs, audio_path)
    if duration is not None:
        return duration > max_secs
    try:
        # BEFORE the decoder handle is resolved: a refusal here must not leak
        # the authenticated FFmpeg descriptor the seam would otherwise own.
        demux_args = _forced_demuxer_args(audio_path)
    except OSError:
        logger.exception("Could not resolve a demuxer to probe %s", audio_path)
        return None
    ffmpeg_bin = await _resolve_ffmpeg_for_execution()
    if not ffmpeg_bin:
        return None
    try:
        try:
            proc = await _create_ffmpeg_subprocess(
                ffmpeg_bin,
                "-v",
                "error",
                "-nostdin",
                "-progress",
                "pipe:1",
                *demux_args,
                "-i",
                audio_path,
                "-vn",
                # One second PAST the cap: the probe only needs to know whether the
                # recording crosses it, so decoding further would be pure waste.
                "-t",
                str(max_secs + 1),
                "-f",
                "null",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            logger.exception("Could not run ffmpeg (%s) to probe %s", ffmpeg_bin, audio_path)
            return None
        try:
            stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_secs)
        except asyncio.TimeoutError:
            await _kill_and_reap(proc)
            logger.error(
                "ffmpeg duration probe of %s timed out after %ds", audio_path, timeout_secs
            )
            return None
        except BaseException:
            # CancelledError is a BaseException; stop AND reap the child so an
            # abandoned request does not leak a decoder process.
            await _kill_and_reap(proc)
            raise
        if proc.returncode != 0:
            return None
        matches = _PROGRESS_OUT_TIME_RE.findall(stdout or b"")
        if not matches:
            return None
        return int(matches[-1]) / 1_000_000 > max_secs
    finally:
        # The authenticated handle must outlive the spawn: every path
        # reaching this ``finally`` has already reaped the child (``communicate``
        # on success and nonzero exit, kill-and-reap on timeout and
        # cancellation) or never spawned one, so the staged image can be
        # released now — off the loop, like every sibling spawn site, instead
        # of by ``__del__`` running the blocking close on the gateway loop.
        await _close_ffmpeg_for_execution(
            ffmpeg_bin,
            preserve_active_exception=sys.exc_info()[1] is not None,
        )


# ---------------------------------------------------------------------------
# Splitting an over-cap recording into transcribable segments
# ---------------------------------------------------------------------------

#: How far back from a target cut a silence is allowed to sit and still be
#: used. A recording is cut at the last silence BEFORE each cap multiple, but
#: only if that silence is within this window — otherwise the segment would be
#: far shorter than the cap and the split would need many more passes than the
#: recording's length warrants. 30s at a 3600s cap means a segment is at worst
#: 0.8% shorter than the cap; a recording whose only silence sits further back
#: than this is cut hard at the cap instead (see ``_choose_segment_cuts``).
_SILENCE_SEARCH_WINDOW_SECS = 30

#: ``silencedetect`` threshold. -30 dBFS for at least 0.3s is quiet enough to be
#: a real pause between utterances rather than a breath, and short enough to
#: catch the gaps a meeting recording actually has. These are the boundary rule,
#: not a tuning knob a caller passes, so they live here as constants.
_SILENCE_NOISE_DBFS = "-30dB"
_SILENCE_MIN_SECS = 0.3

_SILENCE_END_RE = re.compile(rb"silence_end:\s*([0-9]+(?:\.[0-9]+)?)")

#: Hard ceiling on how many silence points the scan retains. A cut is chosen per
#: cap-sized window, so even a many-hour recording needs only a handful; a few
#: thousand covers any recording a human would import with margin to spare. The
#: ceiling exists to bound MEMORY, not to shape the split: ffmpeg's stderr is read
#: incrementally (not buffered whole by ``communicate``) and only the parsed
#: ``silence_end`` floats are kept, so a pathological low-bitrate, pause-dense file
#: -- ~149h at ~8 kbps would emit ~1.5M events -- cannot buffer hundreds of MiB and
#: OOM/hard-exit the gateway. Crossing the ceiling
#: means the input is pathological, so the scan refuses (returns None) and the
#: import is refused rather than risking the crash.
_MAX_SILENCE_POINTS = 100_000

#: Hard ceiling on how many segments one recording may be split into. A real
#: recording under the import size cap cannot exceed a knowable duration, so a
#: decoded duration implying more than this many cap-sized segments is not a real
#: recording -- it is a crafted container whose terminal PTS (ffmpeg
#: ``out_time_us``) was authored far into the future. ``_choose_segment_cuts``
#: appends one cut per cap-sized window in a single synchronous loop, so an
#: unbounded duration would allocate billions of cuts and freeze/OOM the gateway
#: before any caller check runs. The caller refuses
#: (returns None) rather than build the cuts when the duration crosses this
#: ceiling. 512 covers ~21 days at the 1-hour cap -- far past any real meeting,
#: and far past what the 512 MiB import size cap can hold at any real bitrate.
_MAX_SEGMENTS = 512


async def _drain_progress_and_silence(
    proc: Any, *, timeout_secs: int
) -> "tuple[list[float], float] | None":
    """Read ffmpeg's stdout (-progress) and stderr (silencedetect) INCREMENTALLY.

    ``communicate()`` buffers both whole streams in memory; a pathological
    pause-dense recording could make stderr hundreds of MiB and OOM the gateway.
    This reads both streams line by line and keeps
    only the parsed data -- the latest ``out_time_us`` and up to
    :data:`_MAX_SILENCE_POINTS` ``silence_end`` floats -- so memory is bounded by
    the ceiling, not by the input. Returns ``(silence_ends, duration)``, or None
    when the duration is unreadable or the silence ceiling is crossed (a
    pathological input the caller must refuse rather than risk crashing on).
    Reaps nothing itself: the caller owns kill/close on timeout and cancellation.
    """
    silence_ends: list[float] = []
    last_out_time: int | None = None
    overflowed = False

    #: Bytes kept across chunk reads so a token split by a chunk boundary is not
    #: missed. Longer than any ``out_time_us=<digits>`` or ``silence_end: <float>``
    #: token ffmpeg emits. Reading with ``read(n)`` instead of ``readline()`` is the
    #: point: ``StreamReader.readline`` raises ``ValueError`` on a line past its
    #: 64 KiB limit (a >64 KiB no-newline stderr line
    #: would otherwise escape as an unhandled 500), while ``read`` has no
    #: line-length limit. A partial trailing fragment longer than this carry cannot
    #: contain a whole token that also straddles the boundary, so trimming to it is
    #: safe and keeps memory bounded against a pathological no-newline line.
    _CARRY = 256

    async def _read_stream(reader: Any, on_match: Any, pattern: Any) -> None:
        if reader is None:
            return
        pending = b""  # the trailing partial line carried to the next chunk
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            data = pending + chunk
            # Scan complete lines; keep only the trailing partial for next time so a
            # token split across a chunk boundary is still matched whole. Each line
            # is scanned exactly once, so an appending consumer never double-counts.
            newline = data.rfind(b"\n")
            if newline == -1:
                # No line terminator yet: keep only a bounded tail. A fragment
                # longer than the carry cannot hold a token that will still be
                # completed by later bytes, so discarding its head is lossless and
                # caps memory against a giant no-newline line.
                pending = data[-_CARRY:] if len(data) > _CARRY else data
                continue
            for m in pattern.finditer(data[: newline + 1]):
                on_match(m)
            pending = data[newline + 1 :]
            if len(pending) > _CARRY:
                pending = pending[-_CARRY:]
        if pending:
            for m in pattern.finditer(pending):
                on_match(m)

    def _on_silence(m: Any) -> None:
        nonlocal overflowed
        if len(silence_ends) >= _MAX_SILENCE_POINTS:
            overflowed = True
            return
        silence_ends.append(float(m.group(1)))

    def _on_progress(m: Any) -> None:
        nonlocal last_out_time
        last_out_time = int(m.group(1))

    await asyncio.wait_for(
        asyncio.gather(
            _read_stream(proc.stdout, _on_progress, _PROGRESS_OUT_TIME_RE),
            _read_stream(proc.stderr, _on_silence, _SILENCE_END_RE),
        ),
        timeout=timeout_secs,
    )
    await proc.wait()
    if proc.returncode != 0:
        return None
    if last_out_time is None:
        return None
    if overflowed:
        logger.error(
            "ffmpeg silence scan produced more than %d points; refusing", _MAX_SILENCE_POINTS
        )
        return None
    return silence_ends, last_out_time / 1_000_000


#: Bytes of ffmpeg stderr retained for an error message. Only a short tail is
#: logged, so the reader keeps at most this much and discards the rest -- ffmpeg
#: can emit a per-frame warning line, so reading the whole stream with
#: ``communicate()`` would buffer unbounded memory on a long/pathological input
#: (same class as the silence scan).
_STDERR_TAIL_BYTES = 8192


async def _drain_stderr_tail(proc: Any, *, timeout_secs: int) -> bytes:
    """Drain ffmpeg's stderr INCREMENTALLY, retaining only the last
    :data:`_STDERR_TAIL_BYTES`, then reap the child.

    ``communicate()`` buffers all of stderr; a segment extract of a long input
    could make that hundreds of MiB and OOM the gateway. This keeps a bounded
    tail (all the error log needs) and discards the rest as it arrives, so the
    pipe never blocks the child and memory stays bounded. Returns the tail;
    the caller reads ``proc.returncode`` after this returns.
    """
    tail = bytearray()
    if proc.stderr is not None:

        async def _read() -> None:
            while True:
                chunk = await proc.stderr.read(65536)
                if not chunk:
                    break
                tail.extend(chunk)
                if len(tail) > _STDERR_TAIL_BYTES:
                    del tail[: len(tail) - _STDERR_TAIL_BYTES]

        await asyncio.wait_for(_read(), timeout=timeout_secs)
    await proc.wait()
    return bytes(tail)


def _choose_segment_cuts(
    duration_secs: float, cap_secs: int, silence_ends: "list[float]"
) -> "list[float]":
    """Where to cut a *duration_secs* recording into segments each <= *cap_secs*.

    Returns the INTERIOR cut points in seconds, ascending — the segment
    boundaries between 0 and *duration_secs*, excluding both ends. A recording
    already within the cap returns ``[]`` (one segment, no split).

    Each cut is placed at the last ``silence_end`` at or before the running
    target (``previous_cut + cap_secs``) and no earlier than the target minus
    :data:`_SILENCE_SEARCH_WINDOW_SECS`, so a word straddling the target is not
    split — the cut lands in the pause after the previous utterance. When the
    window holds no silence (continuous speech across the whole window), the cut
    falls HARD on the target: a rare, documented seam that may split one word,
    which is still strictly better than refusing the whole import. The target
    always advances by a real amount, so the loop always terminates and every
    segment is <= *cap_secs*.

    Pure function, no IO: the boundary rules are the interesting part and are
    unit-tested directly, the way ``audio.split_transcript`` is.
    """
    cuts: list[float] = []
    ordered = sorted(silence_ends)
    start = 0.0
    while duration_secs - start > cap_secs:
        target = start + cap_secs
        floor = target - _SILENCE_SEARCH_WINDOW_SECS
        # The last silence in ``(floor, target]``. A silence exactly at the
        # target counts; one at or before ``start`` never does (it would make a
        # zero-length or backwards segment).
        candidates = [s for s in ordered if floor < s <= target and s > start]
        cut = candidates[-1] if candidates else target
        cuts.append(cut)
        start = cut
    return cuts


async def _detect_silence_ends(
    audio_path: str, *, timeout_secs: int
) -> "tuple[list[float], float] | None":
    """Silence-end timestamps and the decoded duration, or None when unknown.

    One decode pass through ffmpeg's ``silencedetect`` filter, reading
    ``silence_end`` events from stderr and the final decoded timestamp from the
    ``-progress`` stream. Both streams are drained INCREMENTALLY
    (:func:`_drain_progress_and_silence`), never buffered whole, so a
    pause-dense recording cannot make stderr hundreds of MiB and OOM the gateway;
    only the parsed floats are kept, capped at
    :data:`_MAX_SILENCE_POINTS`. None on any failure the caller must treat as
    "cannot split safely" -- an undecodable file, a decoder that is unavailable,
    a timeout, a nonzero exit, or a pathological input past the silence ceiling
    -- so the caller refuses the import rather than proceeding to a truncating
    decode. Mirrors the spawn shape of :func:`audio_exceeds_secs`: forced
    demuxer, local-protocol pin (inherited from :func:`_create_ffmpeg_subprocess`),
    authenticated-handle lifetime, kill-and-reap on timeout and cancellation.
    """
    try:
        demux_args = _forced_demuxer_args(audio_path)
    except OSError:
        logger.exception("Could not resolve a demuxer to scan %s for silence", audio_path)
        return None
    ffmpeg_bin = await _resolve_ffmpeg_for_execution()
    if not ffmpeg_bin:
        return None
    try:
        try:
            proc = await _create_ffmpeg_subprocess(
                ffmpeg_bin,
                "-nostdin",
                # -hide_banner drops ffmpeg's input-metadata dump from stderr, so a
                # crafted container's oversized tag value cannot appear there;
                # silencedetect logs at info level, which is kept. Defence in depth
                # for the bounded reader below, not the primary guard.
                "-hide_banner",
                "-progress",
                "pipe:1",
                *demux_args,
                "-i",
                audio_path,
                "-vn",
                "-af",
                f"silencedetect=noise={_SILENCE_NOISE_DBFS}:d={_SILENCE_MIN_SECS}",
                "-f",
                "null",
                "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            logger.exception("Could not run ffmpeg (%s) to scan %s", ffmpeg_bin, audio_path)
            return None
        try:
            result = await _drain_progress_and_silence(proc, timeout_secs=timeout_secs)
        except asyncio.TimeoutError:
            await _kill_and_reap(proc)
            logger.error("ffmpeg silence scan of %s timed out after %ds", audio_path, timeout_secs)
            return None
        except BaseException:
            await _kill_and_reap(proc)
            raise
        return result
    finally:
        await _close_ffmpeg_for_execution(
            ffmpeg_bin,
            preserve_active_exception=sys.exc_info()[1] is not None,
        )


async def _extract_segment(
    audio_path: str,
    start_secs: float,
    end_secs: "float | None",
    out_path: str,
    *,
    timeout_secs: int,
) -> bool:
    """Decode ``[start_secs, end_secs)`` of *audio_path* to a 16 kHz mono WAV.

    ``end_secs`` of None means "to the end of the recording" (the last segment).
    Returns True on a clean extraction, False on any failure — the caller turns
    a False into a whole-import refusal, never a partial transcript.

    ``-ss`` before ``-i`` is an INPUT seek (ffmpeg skips to the start without
    decoding the skipped span), so extracting the k-th segment costs one
    segment's worth of decode, not k. The output targets the recogniser's own
    format (16 kHz mono ``pcm_s16le``) exactly as :func:`_pcm_via_ffmpeg` does,
    so each segment file is a plain WAV the transcriber's fast path reads with no
    second transcode.
    """
    try:
        demux_args = _forced_demuxer_args(audio_path)
    except OSError:
        logger.exception("Could not resolve a demuxer to extract a segment of %s", audio_path)
        return False
    ffmpeg_bin = await _resolve_ffmpeg_for_execution()
    if not ffmpeg_bin:
        return False
    # ``-ss`` is an INPUT option (before ``-i``) so the skip is not decoded;
    # ``-t`` bounds the segment length. A None end means the final segment,
    # which runs to end-of-file with no ``-t``.
    seek_args = ("-ss", f"{start_secs:.3f}")
    length_args = () if end_secs is None else ("-t", f"{end_secs - start_secs:.3f}")
    try:
        try:
            proc = await _create_ffmpeg_subprocess(
                ffmpeg_bin,
                "-y",
                "-nostdin",
                *seek_args,
                *demux_args,
                "-i",
                audio_path,
                *length_args,
                "-ar",
                str(stt.SAMPLE_RATE_HZ),
                "-ac",
                "1",
                "-c:a",
                "pcm_s16le",
                out_path,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError:
            logger.exception(
                "Could not run ffmpeg (%s) to extract a segment of %s", ffmpeg_bin, audio_path
            )
            return False
        try:
            stderr_tail = await _drain_stderr_tail(proc, timeout_secs=timeout_secs)
        except asyncio.TimeoutError:
            await _kill_and_reap(proc)
            logger.error(
                "ffmpeg segment extract of %s timed out after %ds", audio_path, timeout_secs
            )
            return False
        except BaseException:
            await _kill_and_reap(proc)
            raise
        if proc.returncode != 0:
            tail = stderr_tail.decode(errors="replace").strip()[-500:] if stderr_tail else ""
            logger.error(
                "ffmpeg %s extracting a segment of %s",
                _describe_ffmpeg_exit(proc.returncode, tail),
                audio_path,
            )
            return False
        return True
    finally:
        await _close_ffmpeg_for_execution(
            ffmpeg_bin,
            preserve_active_exception=sys.exc_info()[1] is not None,
        )


async def transcribe_oversized_in_segments(
    audio_path: str,
    cap_secs: int,
    segment_dir: str,
    stt_config,  # type: ignore[no-untyped-def]
) -> "str | None":
    """Transcribe a recording longer than *cap_secs* by splitting it first.

    Returns the stitched transcript, or None when the recording cannot be split,
    any segment fails to decode or transcribe, or any segment decodes to ``""``
    (nothing heard). Unlike :func:`transcribe_audio`, which reserves None for a
    failure and answers ``""`` for silence, this helper folds a silent segment
    into None too, so the caller reports one clean refusal rather than a partial
    import.

    The split is on the AUDIO, not the transcript: one ffmpeg pass locates the
    pauses (:func:`_detect_silence_ends`), a pure function picks a cut at the
    last pause before each cap multiple (:func:`_choose_segment_cuts`), and each
    segment is decoded to its own WAV (:func:`_extract_segment`) and transcribed
    through the ordinary :func:`transcribe_audio` — so every segment gets the same
    provider dispatch, sensitive-path guard and redaction a whole recording does.
    Segments are NON-OVERLAPPING (cut in the silence between utterances), so the
    stitch is a plain space join with nothing to de-duplicate; the joined text is
    one paragraph, so the meetings import route's ``split_transcript`` sentence-
    splits it into utterances exactly as it does for a whole recording, and the
    seams are invisible.

    *segment_dir* is a caller-owned directory the segment WAVs are written into;
    the caller removes it. It must be a trusted directory the transcriber is
    allowed to read (the meetings route stages it under the voice-runtime root,
    which ``transcribe_audio``'s sensitive-path guard exempts).

    This does NOT re-check the stitched result against any total-size ceiling:
    that stays the caller's job (the import route's ``split_transcript`` still
    enforces ``MAX_IMPORT_LINES``/``MAX_TRANSCRIPT_CHARS``), so splitting is never
    a way around the total-size refusal.
    """
    scanned = await _detect_silence_ends(audio_path, timeout_secs=stt_config.timeout_secs)
    if scanned is None:
        logger.error("Could not scan %s for split boundaries", audio_path)
        return None
    silence_ends, duration = scanned
    # Bound the segment count BEFORE building cuts. ``duration`` is ffmpeg's
    # decoded terminal PTS, which a crafted container can author far into the
    # future; ``_choose_segment_cuts`` appends one cut per cap-sized window in a
    # single synchronous loop, so an unbounded duration would allocate billions of
    # cuts and freeze/OOM the gateway before any later check runs. A real
    # recording under the import size cap cannot span more
    # than ``_MAX_SEGMENTS`` cap-sized windows, so a duration past that ceiling is
    # not a real recording -- refuse rather than build the cuts.
    if duration <= 0 or duration > _MAX_SEGMENTS * cap_secs:
        logger.error(
            "%s reports an implausible duration (%.1fs > %d segments x %ds); refusing",
            audio_path,
            duration,
            _MAX_SEGMENTS,
            cap_secs,
        )
        return None
    cuts = _choose_segment_cuts(duration, cap_secs, silence_ends)
    # Boundaries as [start, end) pairs, end=None on the last (runs to EOF).
    bounds: list[tuple[float, float | None]] = []
    prev = 0.0
    for cut in cuts:
        bounds.append((prev, cut))
        prev = cut
    bounds.append((prev, None))

    transcripts: list[str] = []
    for index, (seg_start, seg_end) in enumerate(bounds):
        seg_path = os.path.join(segment_dir, f"segment-{index:03d}.wav")
        ok = await _extract_segment(
            audio_path, seg_start, seg_end, seg_path, timeout_secs=stt_config.timeout_secs
        )
        if not ok:
            logger.error(
                "Could not extract segment %d of %s; refusing the whole import", index, audio_path
            )
            return None
        try:
            text = await transcribe_audio(seg_path, stt_config)
        finally:
            await asyncio.to_thread(_unlink_if_exists, seg_path)
        if not text:
            # A falsy result refuses the WHOLE import. transcribe_audio returns
            # None for a genuine recogniser failure -- a per-segment decode
            # error, a timeout, the shared recogniser singleton being swapped
            # mid-import -- and ``""`` for a segment it decoded and heard
            # nothing in. _extract_segment returning True only proves ffmpeg
            # carved the WAV, not that the recogniser succeeded on it. Skipping a
            # None segment would silently drop a real spoken span and still
            # answer 200 -- the exact silent-partial data loss this feature
            # exists to prevent, one level down. A ``""`` segment is refused
            # as well. That includes the LAST segment, which is the remainder
            # after the final cut and can be short: a recording whose tail
            # after its last word is silent refuses the whole import. Reading
            # ``""`` as a silent span to skip is a behaviour decision for the
            # import route, outside the sentinel contract.
            logger.error(
                "Segment %d of %s produced no transcript; refusing the whole import",
                index,
                audio_path,
            )
            return None
        transcripts.append(text)
    # Join with a SPACE, not a newline: a segment transcript from the
    # local/Apple recognisers is one whitespace-joined paragraph with no internal
    # newlines (``stt/engine.py`` joins whisper segments with " "). A newline join
    # would hand ``split_transcript`` N>1 lines, which makes it treat each whole
    # segment as ONE line (tier 1) and only hard-wrap it at ``max_chars`` -- so a
    # segment would dispatch as 4000-char chunks instead of utterances, worse line
    # structure than the un-split path produces. A space join keeps the stitched
    # text as one paragraph, so ``split_transcript`` sentence-splits it into
    # utterances (tier 2) exactly as it does for a whole recording; any internal
    # newline a segment DOES carry (a future recogniser with its own line
    # structure) is preserved across the join and still read as tier-1 structure.
    return " ".join(transcripts)


# The owner modules, lowest layer first: the first one that binds a name answers a read
# of it. ``kiro_crew.transcribe.<name>`` stays the import path and the patch surface for
# every name they define: a patch through this module reaches every owner that binds the
# name (see :mod:`kiro_crew._facade_mirror`).
_OWNERS = (
    "kiro_crew.transcribe_ffmpeg",
    "kiro_crew.transcribe_aws",
    "kiro_crew.transcribe_local",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name this module serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.transcribe_aws import (  # noqa: F401
        _TRANSCRIBE_MAX_BYTES,
        _TRANSCRIBE_SAMPLE_RATE_HZ,
        _VOCABULARY_ACCESS_DENIED_ERRORS,
        _VOCABULARY_CONNECT_TIMEOUT_SECS,
        _VOCABULARY_MAX_PAGES,
        _VOCABULARY_PAGE_SIZE,
        _VOCABULARY_READ_TIMEOUT_SECS,
        _VOICE_EXTRA_HINT,
        VOCABULARIES_ACCESS_DENIED,
        VOCABULARIES_LIST_FAILED,
        VOCABULARIES_LIST_PERMISSION,
        CredentialResolver,
        Credentials,
        CustomVocabulary,
        CustomVocabularyListing,
        VocabularyListError,
        _load_aws_transcribe_components,
        _make_temp_ogg,
        _ProfileCredentialResolver,
        _read_audio_bytes,
        boto3,
        list_custom_vocabularies,
    )
    from kiro_crew.transcribe_ffmpeg import (  # noqa: F401
        _DECODER_DETAIL_MAX_CHARS,
        _DEMUXER_BY_SUFFIX,
        _DEV_FD_RE,
        _FFMPEG_SNAPSHOT_NAME_RE,
        _FFMPEG_SNAPSHOT_PREFIX,
        _MACOS_FFMPEG_REQUIREMENT,
        _MACOS_SIGNING_TEAM_ID,
        _MAX_SIGNED_FFMPEG_BYTES,
        _PACKAGED_FFMPEG_ARTIFACTS,
        _SHIPPED_FFMPEG_PLATFORMS,
        _SIGNER_REWRITTEN_FFMPEG_ARTIFACTS,
        _WINDOWS_LOADER_STATUS,
        DECODER_NOT_EXECUTABLE,
        DECODER_OK,
        DECODER_UNAUTHENTIC,
        PackagedDecoderProbe,
        _authenticated_ffmpeg,
        _AuthenticatedFfmpeg,
        _cleanup_stale_ffmpeg_snapshots,
        _close_abandoned_ffmpeg_resolution,
        _decoder_exit_detail,
        _ffmpeg_candidate_dirs,
        _ffmpeg_payload_chunks,
        _ffmpeg_snapshot_cleanup_lock,
        _ffmpeg_snapshot_root,
        _ffmpeg_snapshot_roots_cleaned,
        _input_suffix,
        _macos_developer_id_authentic,
        _new_executable_snapshot,
        _open_authenticated_in,
        _open_ffmpeg_for_execution,
        _open_packaged_ffmpeg_resource,
        _open_store_ffmpeg_resource,
        _open_windows_read_locked,
        _packaged_ffmpeg_resource,
        _packaged_ffmpeg_version_probe,
        _remove_named_snapshot,
        _resolve_once,
        _seal_linux_memfd,
        _trusted_site_package_roots,
        _write_all,
    )
    from kiro_crew.transcribe_local import (  # noqa: F401
        _LANGUAGE_RE,
        _WAV_SUFFIXES,
        _make_temp_wav,
        _pcm_from_wav,
        _pcm_via_ffmpeg,
        _whisper_language,
    )
