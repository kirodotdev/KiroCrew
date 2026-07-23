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
import tempfile
from pathlib import Path

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
    matching a plain ``ffmpeg`` on POSIX. The prior ``os.path.isfile(<d>/ffmpeg)``
    check missed the ``.exe`` suffix on Windows and never prepended the dir.
    """
    path_parts = os.environ.get("PATH", "").split(os.pathsep)
    for d in reversed(_FFMPEG_CANDIDATE_DIRS):
        if d in path_parts:
            continue
        if shutil.which("ffmpeg", path=d):
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
            path_parts.insert(0, d)


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


_WHISPER_SEARCH_PATHS = [
    os.path.expanduser("~/.local/bin/whisper"),
    "/usr/local/bin/whisper",
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
    # Check system python3's scripts dir (pip install target)
    py3_bin = _python3_bin_dir()
    if py3_bin:
        p = os.path.join(py3_bin, "whisper")
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    for p in _WHISPER_SEARCH_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


_MLX_WHISPER_SEARCH_PATHS = [
    os.path.expanduser("~/.local/bin/mlx_whisper"),
    "/opt/homebrew/bin/mlx_whisper",
    "/usr/local/bin/mlx_whisper",
]


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
    py3_bin = _python3_bin_dir()
    if py3_bin:
        p = os.path.join(py3_bin, "mlx_whisper")
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
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
    ensure_ffmpeg_in_path()
    return _find_whisper(stt_config.whisper_path) is not None


async def transcribe_audio(audio_path: str, stt_config=None) -> str | None:  # type: ignore[no-untyped-def]
    """Transcribe audio file. Returns text or None."""
    if stt_config is None:
        from kiro_crew.config.loader import KiroCrewConfig

        stt_config = KiroCrewConfig.load().stt

    if not stt_config.enabled:
        logger.debug("STT disabled in config")
        return None

    from kiro_crew.security import is_sensitive_path

    if is_sensitive_path(audio_path):
        logger.error("Refusing to read sensitive path: %s", audio_path)
        return None

    provider = stt_config.provider
    if provider == "transcribe":
        result = await _transcribe_aws(audio_path, stt_config)
    elif provider == "mlx":
        ensure_ffmpeg_in_path()
        result = await _transcribe_mlx(audio_path, stt_config)
    else:
        ensure_ffmpeg_in_path()
        result = await _transcribe_native(audio_path, stt_config)

    if result:
        from kiro_crew.security import redact_credentials, redact_exfiltration_urls

        result, _ = redact_exfiltration_urls(result)
        result, _ = redact_credentials(result)
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
        from amazon_transcribe.client import TranscribeStreamingClient
        from amazon_transcribe.handlers import TranscriptResultStreamHandler
        from amazon_transcribe.model import TranscriptEvent
    except ImportError:
        logger.error("AWS Transcribe not available: install 'kirocrew[voice]'")
        return None

    region = stt_config.transcribe_region
    tmp_ogg = None
    actual_path = audio_path
    if ext in (".webm",):
        ensure_ffmpeg_in_path()
        if not shutil.which("ffmpeg"):
            logger.error("ffmpeg required to remux webm to ogg for Transcribe")
            return None
        fd, tmp_ogg = tempfile.mkstemp(suffix=".ogg")
        os.close(fd)
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
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
            if tmp_ogg and os.path.exists(tmp_ogg):
                os.unlink(tmp_ogg)
            return None
        actual_path = tmp_ogg

    transcript_parts: list[str] = []

    class Handler(TranscriptResultStreamHandler):
        async def handle_transcript_event(self, transcript_event: TranscriptEvent) -> None:
            for result in transcript_event.transcript.results:
                if not result.is_partial and result.alternatives:
                    transcript_parts.append(result.alternatives[0].transcript)

    stream = None
    try:
        file_size = os.path.getsize(actual_path)
        if file_size > _TRANSCRIBE_MAX_BYTES:
            logger.error("Audio file too large for Transcribe: %d bytes", file_size)
            return None

        profile = stt_config.transcribe_profile or None
        credential_resolver = _ProfileCredentialResolver(profile) if profile else None

        client = TranscribeStreamingClient(
            region=region,
            credential_resolver=credential_resolver,
        )
        stream = await client.start_stream_transcription(
            language_code=stt_config.language_code,
            media_sample_rate_hz=TRANSCRIBE_SAMPLE_RATE_HZ,
            media_encoding="ogg-opus",
        )

        async def write_chunks():
            audio_bytes = Path(actual_path).read_bytes()
            chunk_size = 8192
            for i in range(0, len(audio_bytes), chunk_size):
                await stream.input_stream.send_audio_event(
                    audio_chunk=audio_bytes[i : i + chunk_size]
                )
            await stream.input_stream.end_stream()

        handler = Handler(stream.output_stream)
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
        if tmp_ogg and os.path.exists(tmp_ogg):
            os.unlink(tmp_ogg)


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
    out_dir = tempfile.mkdtemp()
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
        return _collect_whisper_output(proc.returncode, stderr, out_dir, label=label)
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
        shutil.rmtree(out_dir, ignore_errors=True)


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
    mlx_bin = _find_mlx_whisper()
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


async def _transcribe_native(audio_path: str, stt_config) -> str | None:  # type: ignore[no-untyped-def]
    """Transcribe using the native openai-whisper binary."""
    whisper_bin = _find_whisper(stt_config.whisper_path)
    if not whisper_bin:
        logger.error("whisper not found — install: pip install openai-whisper")
        return None

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
            "--fp16",
            "False",
        ],
        stt_config.timeout_secs,
        label="whisper",
    )
