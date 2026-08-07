"""Local speech-to-text via openai-whisper (opt-in, config-driven).

Default STT provider is the local ``whisper`` binary (``pip install openai-whisper``).
AWS Transcribe is supported as an optional extra (``pip install 'kirocrew[voice]'``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from kiro_crew import platform_compat

# Transcribe-path deps are an OPTIONAL 'aws' extra (amazon-transcribe + boto3).
# The module MUST stay importable when they're absent (default install, partial
# install, pip mid-install) so that `cli_doctor` — which imports this module —
# can surface the missing-deps diagnostic. Methods that actually use boto3 or
# the Credentials class are only invoked when stt.provider == "transcribe" and
# a profile is configured, so absence here is harmless for non-STT use. The
# local whisper path (default STT provider) needs neither.
try:
    import boto3
    from amazon_transcribe.auth import CredentialResolver, Credentials
except ImportError:  # pragma: no cover — covered by cli_doctor tests
    boto3 = None  # type: ignore[assignment,misc]
    CredentialResolver = object  # type: ignore[assignment,misc]
    Credentials = None  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)


def _ffmpeg_candidate_dirs() -> list[str]:
    """Build the ordered directory list to probe for an ffmpeg install.

    POSIX-standard install prefixes come first (Homebrew, /usr/local, and the
    per-user ~/ffmpeg / ~/.local/bin extraction dirs). On Windows we append
    the two idiomatic install locations: the winget/Chocolatey machine-wide
    ``%ProgramFiles%\\ffmpeg\\bin`` and the winget/scoop user-scope
    ``%LOCALAPPDATA%\\Programs\\ffmpeg\\bin``. Expanded once at import time.
    """
    dirs = [
        os.path.expanduser("~/ffmpeg"),
        os.path.expanduser("~/.local/bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    if platform_compat.IS_WINDOWS:
        program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        local_appdata = os.environ.get(
            "LOCALAPPDATA",
            os.path.join(os.path.expanduser("~"), "AppData", "Local"),
        )
        dirs.extend(
            [
                os.path.join(program_files, "ffmpeg", "bin"),
                os.path.join(local_appdata, "Programs", "ffmpeg", "bin"),
            ]
        )
    return dirs


_FFMPEG_CANDIDATE_DIRS = _ffmpeg_candidate_dirs()


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


def _own_scripts_dir() -> str:
    """Scripts dir of the interpreter THIS process is running under.

    Where ``pip install openai-whisper`` (or ``mlx-whisper``) lands its console
    script when the app is installed in a virtualenv — which is how the gateway
    normally runs. Nothing else in the search order looks there:

    * ``shutil.which`` only sees ``PATH``, and a venv is on ``PATH`` only after
      ``activate``. The gateway is launched as ``<venv>/bin/kirocrew``, which does
      not modify ``PATH``, so the venv's own ``bin/`` is invisible to it.
    * :func:`_python3_bin_dir` deliberately asks the SYSTEM python3 (via
      ``find_python_interpreter``), so it reports the system scripts dir even when
      we are running inside a venv.

    The result was that installing Whisper into the app's own environment — the
    obvious thing to do — left ``is_available()`` reporting False, with the only
    workarounds being to set ``stt.whisper_path`` by hand or install it a second
    time somewhere else.

    Uses ``sys.prefix`` rather than ``sysconfig.get_path('scripts')`` because the
    latter can be redirected by an active ``--user`` scheme or a posix_prefix
    override, while the console script always sits beside the running interpreter.
    """
    return os.path.dirname(os.path.abspath(sys.executable))


def _python3_bin_dir() -> str:
    """Return the bin dir of the system python3 (where pip installs scripts)."""
    try:
        # platform_compat.find_python_interpreter prefers a real CPython >= 3.10
        # and — on Windows — rejects the Microsoft Store alias stub, which would
        # otherwise be spawned and print "Python was not found" on every call.
        py = platform_compat.find_python_interpreter()
        if not py:
            return ""
        out = subprocess.check_output(
            [py, "-c", "import sysconfig; print(sysconfig.get_path('scripts'))"],
            timeout=5,
            text=True,
        ).strip()
        return out
    except Exception:
        return ""


def _find_script_in_dir(bin_dir: str, name: str) -> str | None:
    """Return an executable ``name`` inside ``bin_dir``, trying Windows suffixes.

    A pip console script is materialized as ``<name>.exe`` in ``Scripts\\`` on
    Windows, so the extensionless POSIX name never exists there. Sweep the
    known launcher suffixes (the idiom in dev_fleet ``_trusted_bin``).
    """
    suffixes = ("", ".exe", ".cmd", ".bat") if platform_compat.IS_WINDOWS else ("",)
    for suffix in suffixes:
        p = os.path.join(bin_dir, name + suffix)
        # On Windows there is no execute bit, so gate on isfile only there.
        if os.path.isfile(p) and (platform_compat.IS_WINDOWS or os.access(p, os.X_OK)):
            return p
    return None


_WHISPER_SEARCH_PATHS = [
    os.path.expanduser("~/.local/bin/whisper"),
    # Homebrew prefixes: a GUI-launched gateway inherits a minimal PATH
    # (/usr/bin:/bin:/usr/sbin:/sbin) with no Homebrew, so shutil.which("whisper")
    # misses a `brew install`ed binary. Probing the prefixes directly — the same
    # reason _MLX_WHISPER_SEARCH_PATHS and _BREW_CANDIDATE_PATHS list them — is
    # what keeps STT available without depending on the ensure_ffmpeg_in_path()
    # PATH side effect happening to run first.
    "/opt/homebrew/bin/whisper",  # Apple Silicon macOS
    "/usr/local/bin/whisper",  # Intel macOS / Linuxbrew-less installs
    "/usr/bin/whisper",
]


def _find_whisper(configured_path: str = "") -> str | None:
    """Return whisper binary path or None if not found."""
    if configured_path:
        p = os.path.expanduser(configured_path)
        return p if os.path.isfile(p) and os.access(p, os.X_OK) else None
    found = shutil.which("whisper")
    if found:
        return found
    # This interpreter's own scripts dir FIRST of the directory probes: when the
    # app runs from a venv, that is where `pip install openai-whisper` put the
    # console script, and it is the only candidate guaranteed to match the
    # environment the caller actually installed into.
    own_bin = _own_scripts_dir()
    if own_bin:
        found_own = _find_script_in_dir(own_bin, "whisper")
        if found_own:
            return found_own
    # Check system python3's scripts dir (pip install target)
    py3_bin = _python3_bin_dir()
    if py3_bin:
        found_script = _find_script_in_dir(py3_bin, "whisper")
        if found_script:
            return found_script
    for p in _WHISPER_SEARCH_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


_MLX_WHISPER_SEARCH_PATHS = [
    os.path.expanduser("~/.local/bin/mlx_whisper"),
    "/opt/homebrew/bin/mlx_whisper",
    "/usr/local/bin/mlx_whisper",
]

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

#: Directories the STT install script prepends to ``PATH`` before probing for
#: ``brew`` / ``pipx`` / the binaries it installs. Same reasoning as
#: ``_BREW_CANDIDATE_PATHS``, expressed for the shell side: ``bash -c`` is
#: neither a login nor an interactive shell, so the ``brew shellenv`` line in the
#: user's ``~/.zprofile`` never runs and the inherited PATH is all the script gets.
#: Expanded here rather than left as ``$HOME/...`` so the script can quote each
#: entry without a shell-expansion escape hatch. ``~/.local/bin`` is where
#: ``pipx`` puts ``mlx_whisper``, so the post-install verification needs it too.
BREW_PATH_DIRS = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/home/linuxbrew/.linuxbrew/bin",
    os.path.expanduser("~/.linuxbrew/bin"),
    os.path.expanduser("~/.local/bin"),
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


def _find_mlx_whisper() -> str | None:
    """Return the mlx_whisper binary path or None if not found.

    mlx_whisper is the Apple-Silicon (Metal GPU) Whisper runtime. It is
    installed out-of-band (e.g. ``pipx install mlx-whisper``) rather than as
    a package dependency, because the ``mlx`` wheel only exists for arm64 and
    would break builds/installs on every other architecture. We therefore
    locate and invoke the CLI as a subprocess, mirroring ``_find_whisper``.
    """
    found = shutil.which("mlx_whisper")
    if found:
        return found
    # Same venv gap as `_find_whisper` — see `_own_scripts_dir`.
    own_bin = _own_scripts_dir()
    if own_bin:
        found_own = _find_script_in_dir(own_bin, "mlx_whisper")
        if found_own:
            return found_own
    py3_bin = _python3_bin_dir()
    if py3_bin:
        found_script = _find_script_in_dir(py3_bin, "mlx_whisper")
        if found_script:
            return found_script
    for p in _MLX_WHISPER_SEARCH_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def is_available(stt_config=None) -> bool:  # type: ignore[no-untyped-def]
    """Check if STT is enabled in config and a provider is usable."""
    if stt_config is None:
        from kiro_crew.config.loader import KiroCrewConfig

        stt_config = KiroCrewConfig.load().stt
    if not stt_config.enabled:
        return False
    provider = stt_config.provider
    if provider == "transcribe":
        # AWS Transcribe is an optional extra; both amazon-transcribe and boto3
        # must be present. On a vanilla install they're absent → not available.
        if boto3 is None:
            return False
        try:
            import amazon_transcribe  # noqa: F401
        except ImportError:
            return False
        ensure_ffmpeg_in_path()
        if not shutil.which("ffmpeg"):
            logger.warning("ffmpeg not found; .webm transcription will be unavailable")
        return True
    if provider == "mlx":
        ensure_ffmpeg_in_path()
        return _find_mlx_whisper() is not None
    if provider == "apple":
        from kiro_crew import apple_speech

        # NOT a build check: this function runs on the event loop (config GET,
        # the transcribe endpoint, Slack voice), and compiling the Swift helper
        # there would freeze the gateway for up to 180s. `availability()` is
        # stats-only; the build happens inside the offloaded transcribe path.
        return apple_speech.availability().ok
    ensure_ffmpeg_in_path()
    return _find_whisper(stt_config.whisper_path) is not None


def _load_stt_config() -> Any:
    """Load STT configuration without importing or reading config on the loop."""
    from kiro_crew.config.loader import KiroCrewConfig

    return KiroCrewConfig.load().stt


def _is_sensitive_audio_path(audio_path: str) -> bool:
    """Run the filesystem-resolving sensitive-path guard off the event loop."""
    from kiro_crew.security import is_sensitive_path

    return is_sensitive_path(audio_path)


def _redact_transcript(transcript: str) -> str:
    """Apply transcript redaction without consuming event-loop time."""
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    transcript, _ = redact_exfiltration_urls(transcript)
    transcript, _ = redact_credentials(transcript)
    return transcript


async def transcribe_audio(audio_path: str, stt_config=None) -> str | None:  # type: ignore[no-untyped-def]
    """Transcribe audio file. Returns text or None."""
    if stt_config is None:
        stt_config = await asyncio.to_thread(_load_stt_config)

    if not stt_config.enabled:
        logger.debug("STT disabled in config")
        return None

    if await asyncio.to_thread(_is_sensitive_audio_path, audio_path):
        logger.error("Refusing to read sensitive path: %s", audio_path)
        return None

    provider = stt_config.provider
    if provider == "transcribe":
        result = await _transcribe_aws(audio_path, stt_config)
    elif provider == "mlx":
        await asyncio.to_thread(ensure_ffmpeg_in_path)
        result = await _transcribe_mlx(audio_path, stt_config)
    elif provider == "apple":
        result = await _transcribe_apple(audio_path, stt_config)
    else:
        await asyncio.to_thread(ensure_ffmpeg_in_path)
        result = await _transcribe_native(audio_path, stt_config)

    if result:
        result = await asyncio.to_thread(_redact_transcript, result)
    return result


class _ProfileCredentialResolver(CredentialResolver):
    """Async credential resolver that delegates to a boto3 Session profile."""

    def __init__(self, profile: str) -> None:
        if boto3 is None:  # pragma: no cover — optional 'aws' extra not installed
            raise RuntimeError(
                "AWS Transcribe support is not available: install the optional "
                "dependencies (pip install 'kirocrew[voice]')."
            )
        self._session = boto3.Session(profile_name=profile)

    async def get_credentials(self) -> Credentials | None:
        loop = asyncio.get_running_loop()
        creds = await loop.run_in_executor(None, lambda: self._session.get_credentials())
        if creds is None:
            # Profile name in error is safe — only logged server-side via
            # logger.exception in _transcribe_aws, never exposed in HTTP responses.
            raise RuntimeError(
                f"No AWS credentials found for profile '{self._session.profile_name}'"
            )
        frozen = await loop.run_in_executor(None, creds.get_frozen_credentials)
        return Credentials(frozen.access_key, frozen.secret_key, frozen.token)


# Chrome MediaRecorder with opus codec defaults to 48 kHz.  If a different
# browser/config uses another rate, Transcribe may reject or garble the stream.
TRANSCRIBE_SAMPLE_RATE_HZ = 48000

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

        async def handle_transcript_event(
            self, transcript_event: TranscriptEvent
        ) -> None:
            for result in transcript_event.transcript.results:
                if not result.is_partial and result.alternatives:
                    self._transcript_parts.append(result.alternatives[0].transcript)

    return TranscribeStreamingClient, TranscriptCollector


def _find_ffmpeg() -> str | None:
    """Return an ffmpeg binary after probing known install locations."""
    ensure_ffmpeg_in_path()
    return shutil.which("ffmpeg")


def _make_temp_ogg() -> str:
    """Create and close a temporary OGG file without leaking its descriptor."""
    fd, path = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    return path


def _unlink_if_exists(path: str) -> None:
    """Remove *path*, tolerating another cleanup path winning the race."""
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _read_audio_bytes(audio_path: str) -> bytes:
    """Read at most one byte beyond AWS Transcribe's upload limit."""
    with open(audio_path, "rb") as audio_file:
        return audio_file.read(_TRANSCRIBE_MAX_BYTES + 1)


async def _transcribe_aws(audio_path: str, stt_config) -> str | None:  # type: ignore[no-untyped-def]
    """Transcribe using AWS Transcribe Streaming API (ogg-opus)."""
    ext = os.path.splitext(audio_path)[1].lower()
    if ext not in (".ogg", ".webm"):
        logger.error("Unsupported format '%s' for Transcribe (expected .ogg or .webm)", ext)
        return None

    # amazon-transcribe + boto3 are an optional 'aws' extra. Absent on a vanilla
    # install → report not available rather than raising an uncaught ImportError.
    if boto3 is None:
        logger.error("AWS Transcribe not available: install 'kirocrew[voice]'")
        return None
    try:
        TranscribeStreamingClient, TranscriptCollector = await asyncio.to_thread(
            _load_aws_transcribe_components
        )
    except ImportError:
        logger.error("AWS Transcribe not available: install 'kirocrew[voice]'")
        return None

    region = stt_config.transcribe_region
    tmp_ogg = None
    actual_path = audio_path
    if ext in (".webm",):
        ffmpeg_bin = await asyncio.to_thread(_find_ffmpeg)
        if not ffmpeg_bin:
            logger.error("ffmpeg required to remux webm to ogg for Transcribe")
            return None
        tmp_ogg = await asyncio.to_thread(_make_temp_ogg)
        try:
            proc = await asyncio.create_subprocess_exec(
                ffmpeg_bin,
                "-y",
                "-i",
                audio_path,
                "-c:a",
                "copy",
                tmp_ogg,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                await asyncio.wait_for(proc.communicate(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise
            if proc.returncode != 0:
                raise RuntimeError(f"ffmpeg exited with {proc.returncode}")
        except Exception:
            logger.exception("ffmpeg remux failed for %s", audio_path)
            if tmp_ogg:
                await asyncio.to_thread(_unlink_if_exists, tmp_ogg)
            return None
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
            await asyncio.to_thread(_ProfileCredentialResolver, profile)
            if profile
            else None
        )

        client = await asyncio.to_thread(
            TranscribeStreamingClient,
            region=region,
            credential_resolver=credential_resolver,
        )
        stream = await client.start_stream_transcription(
            language_code=stt_config.language_code,
            media_sample_rate_hz=TRANSCRIBE_SAMPLE_RATE_HZ,
            media_encoding="ogg-opus",
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

        transcript = " ".join(transcript_parts).strip() or None
        return transcript
    except Exception:
        logger.exception("AWS Transcribe streaming STT failed")
        return None
    finally:
        if stream is not None:
            try:
                await stream.input_stream.end_stream()
            except Exception:
                pass
        if tmp_ogg:
            await asyncio.to_thread(_unlink_if_exists, tmp_ogg)


def _collect_whisper_output(
    returncode: int | None,
    stderr: bytes | None,
    out_dir: str,
    label: str = "whisper",
) -> str | None:
    """Check whisper exit status and read the transcript from *out_dir*."""
    if returncode != 0:
        tail = stderr.decode(errors="replace").strip()[-500:] if stderr else ""
        logger.error("%s failed (rc=%d): %s", label, returncode, tail)
        return None
    txt_files = list(Path(out_dir).glob("*.txt"))
    if not txt_files:
        tail = stderr.decode(errors="replace").strip()[-500:] if stderr else ""
        logger.error("No %s output in %s stderr=%s", label, out_dir, tail or "(empty)")
        return None
    return txt_files[0].read_text().strip() or None


async def _run_whisper_cli(
    binary: str,
    build_args,  # Callable[[str], list[str]]: out_dir -> CLI args (excluding binary)
    timeout_secs: int,
    label: str,
) -> str | None:  # type: ignore[no-untyped-def]
    """Run a Whisper-style CLI in an isolated subprocess and read its transcript.

    Shared by ``_transcribe_native`` (openai-whisper) and ``_transcribe_mlx``
    (mlx_whisper). Both CLIs are installed out-of-band and run under their own
    Python interpreter, so we strip ``PYTHONPATH``/``PYTHONHOME`` to stop
    KiroCrew's bundled packages (numpy, torch) from leaking into their runtime.
    Each writes a ``.txt`` transcript into a temp ``out_dir`` we own and clean
    up. ``build_args`` lets callers express their differing flags (the two CLIs
    use hyphenated vs underscored option names).
    """
    out_dir = await asyncio.to_thread(tempfile.mkdtemp)
    proc = None
    try:
        clean_env = os.environ.copy()
        clean_env.pop("PYTHONPATH", None)
        clean_env.pop("PYTHONHOME", None)
        proc = await asyncio.create_subprocess_exec(
            binary,
            *build_args(out_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=clean_env,
        )
        _stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_secs)
        return await asyncio.to_thread(
            _collect_whisper_output,
            proc.returncode,
            stderr,
            out_dir,
            label,
        )
    except asyncio.TimeoutError:
        if proc is not None:
            try:
                proc.kill()
            except OSError:
                pass
            await proc.wait()
        logger.error("%s transcription timed out after %ds", label, timeout_secs)
        return None
    finally:
        await asyncio.to_thread(shutil.rmtree, out_dir, ignore_errors=True)


# Defense in depth: ``mlx_model`` is read from config.json. The dashboard PUT
# API validates it against an allowlist, but a hand- or tool-edited config could
# inject an arbitrary value that is then passed to the mlx_whisper subprocess.
# Constrain it to a HuggingFace ``owner/repo`` id (single slash, no path
# traversal — the owner segment forbids dots) before use.
_MLX_MODEL_RE = re.compile(r"^[A-Za-z0-9_-]+/[A-Za-z0-9._-]+$")


async def _transcribe_mlx(audio_path: str, stt_config) -> str | None:  # type: ignore[no-untyped-def]
    """Transcribe using the mlx_whisper CLI (Apple Silicon, Metal GPU).

    mlx_whisper is installed out-of-band (the ``mlx`` wheel is arm64-only). Note
    the hyphenated flags (``--output-dir``/``--output-format``), which differ
    from the underscore flags used by the openai-whisper CLI.
    """
    mlx_bin = await asyncio.to_thread(_find_mlx_whisper)
    if not mlx_bin:
        logger.error("mlx_whisper not found — install: pipx install mlx-whisper")
        return None

    model = stt_config.mlx_model
    if not _MLX_MODEL_RE.match(model or ""):
        logger.error(
            "Refusing to run mlx_whisper: invalid mlx_model %r "
            "(expected a HuggingFace 'owner/repo' id)",
            model,
        )
        return None

    return await _run_whisper_cli(
        mlx_bin,
        lambda out_dir: [
            audio_path,
            "--model",
            model,
            "--output-dir",
            out_dir,
            "--output-format",
            "txt",
        ],
        stt_config.timeout_secs,
        label="mlx_whisper",
    )


async def _transcribe_apple(audio_path: str, stt_config) -> str | None:  # type: ignore[no-untyped-def]
    """Transcribe with Apple's on-device SpeechAnalyzer (macOS 26+).

    Delegates to :mod:`kiro_crew.apple_speech`, which owns the Swift-helper seam.
    The framework needs a language *locale* rather than whisper's bare language
    code, so ``stt_config.language_code`` (already BCP-47, e.g. ``en-US``) is passed
    straight through; the helper falls back to another installed dialect of the same
    language before it refuses.

    Unlike whisper/mlx this needs no model download on a supported host — the OS
    ships the assets — so a failure here is a real error, not a missing-model state.
    """
    from kiro_crew import apple_speech

    text, metrics = await apple_speech.transcribe(
        audio_path,
        locale=stt_config.language_code or "en-US",
        timeout_secs=stt_config.timeout_secs or apple_speech.DEFAULT_TIMEOUT_SECS,
    )
    if text is None:
        logger.error("Apple speech transcription failed: %s", metrics.get("error", "unknown"))
        return None
    logger.debug(
        "Apple speech: %.2fs for %.1fs of audio (locale=%s)",
        metrics.get("transcribe_secs", 0.0),
        metrics.get("audio_secs", 0.0),
        metrics.get("locale", "?"),
    )
    return text


def _is_openai_whisper(whisper_bin: str) -> bool:
    """True when *whisper_bin* is the reference openai-whisper CLI.

    ``--fp16`` is an openai-whisper-only flag (it silences the "FP16 is not
    supported on CPU" warning). Drop-in replacements advertised as
    openai-whisper-compatible — e.g. ``whisper-ctranslate2`` — do not implement
    it and exit ``rc=2`` (``unrecognized arguments: --fp16``), which surfaces to
    the user as a silent empty transcript. openai-whisper's console script is
    always named ``whisper`` (``whisper`` / ``whisper.exe``), so gating on the
    resolved binary's stem lets a compatible engine work through the existing
    ``stt.whisper_path`` setting with no extra config. Getting this wrong for a
    genuine openai-whisper install only restores a harmless CPU warning; wrongly
    passing the flag to an engine that rejects it breaks transcription outright,
    so the check errs toward omitting the flag when unsure.
    """
    return Path(whisper_bin).stem.lower() == "whisper"


async def _transcribe_native(audio_path: str, stt_config) -> str | None:  # type: ignore[no-untyped-def]
    """Transcribe using the native openai-whisper (or a compatible) binary."""
    whisper_bin = await asyncio.to_thread(_find_whisper, stt_config.whisper_path)
    if not whisper_bin:
        logger.error("whisper not found — install: pip install openai-whisper")
        return None

    add_fp16 = _is_openai_whisper(whisper_bin)

    return await _run_whisper_cli(
        whisper_bin,
        lambda out_dir: [
            audio_path,
            "--model",
            stt_config.model,
            "--device",
            stt_config.device,
            "--output_dir",
            out_dir,
            "--output_format",
            "txt",
            # ``--fp16`` is openai-whisper-only; omit it for compatible engines
            # (e.g. whisper-ctranslate2) that would reject it with rc=2.
            *(["--fp16", "False"] if add_fp16 else []),
        ],
        stt_config.timeout_secs,
        label="whisper",
    )
