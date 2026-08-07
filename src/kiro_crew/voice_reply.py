"""KiroCrew voice reply — generate TTS audio and upload to Slack thread.

Post-response hook: strips markdown, generates audio via the configured
TTS provider (Amazon Polly or local Piper), uploads to the same Slack thread.
Fire-and-forget — never blocks the text response.

Supported providers:
- ``polly``: OPTIONAL Amazon Polly via the ``aws polly synthesize-speech`` CLI.
  Produces MP3. Requires the ``aws`` CLI on PATH plus valid AWS credentials +
  network. This module never imports ``boto3``/``botocore`` — Polly is driven
  entirely through the CLI subprocess, so the module imports cleanly on a
  vanilla machine without any AWS SDK installed. When the CLI or credentials
  are absent, the Polly path degrades gracefully (returns ``None``).
- ``piper`` (recommended offline default): Local neural TTS via the ``piper``
  CLI (https://github.com/rhasspy/piper). Produces WAV. Requires the ``piper``
  binary and a voice model (.onnx + .onnx.json) on disk. Fully offline.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import shutil
import tempfile
from typing import TYPE_CHECKING

from kiro_crew.sandbox import (
    SandboxUnavailableError,
    cgroup_scope_argv,
    create_subprocess_limited,
    wrap_argv,
)
from kiro_crew.security import redact_credentials, redact_exfiltration_urls

if TYPE_CHECKING:
    from kiro_crew.slack.client import SlackClientOps

logger = logging.getLogger(__name__)

# ── Provider constants ──
PROVIDER_POLLY = "polly"
PROVIDER_PIPER = "piper"
VALID_PROVIDERS = frozenset({PROVIDER_POLLY, PROVIDER_PIPER})
# Local offline TTS (Piper) is the documented recommended default — it needs no
# AWS credentials or network. Polly remains fully supported when explicitly
# selected (``provider="polly"``) with the ``aws`` CLI + credentials present.
DEFAULT_PROVIDER = PROVIDER_PIPER


def is_available(
    provider: str = DEFAULT_PROVIDER,
    piper_binary: str = "",
    piper_model: str = "",
) -> bool:
    """Return True if TTS for the given provider can be produced.

    - ``polly``: checks the ``aws`` CLI is on PATH. Credential validity is
      NOT verified here — a missing/expired profile still fails gracefully
      inside ``synthesize_speech`` (caught, logged, returns None).
    - ``piper``: checks the piper binary is invocable AND the model file
      exists. Model-less piper is unusable, so we verify both up front.

    Callers use this to decide whether to post a fallback ephemeral message
    when voice output was requested but cannot be produced.
    """
    if provider == PROVIDER_POLLY:
        return shutil.which("aws") is not None
    if provider == PROVIDER_PIPER:
        bin_path = _resolve_piper_binary(piper_binary)
        if not bin_path:
            return False
        # Piper is unusable without a model — require a non-empty path that
        # points at an existing file. Returning True when ``piper_model`` is
        # unset would cause ``_synthesize_piper`` to fail silently downstream.
        if not piper_model:
            return False
        if not os.path.isfile(os.path.expanduser(piper_model)):
            return False
        return True
    logger.warning("is_available: unknown provider %r", provider)
    return False


def _resolve_piper_binary(configured: str = "") -> str | None:
    """Return piper binary path or None if not found.

    Resolution order: explicit ``configured`` path → ``piper`` on PATH →
    common pip-install location at ``~/piper-venv/bin/piper``.

    Windows: not yet supported — upstream rhasspy/piper ships no Windows binary,
    so this returns None and Piper TTS is unavailable (Polly works). Tracked as
    follow-on work.
    """
    if configured:
        p = os.path.expanduser(configured)
        return p if os.path.isfile(p) and os.access(p, os.X_OK) else None
    found = shutil.which("piper")
    if found:
        return found
    fallback = os.path.expanduser("~/piper-venv/bin/piper")
    if os.path.isfile(fallback) and os.access(fallback, os.X_OK):
        return fallback
    return None


# ── Config defaults ──
DEFAULT_VOICE = "Ruth"
DEFAULT_ENGINE = "generative"
DEFAULT_RATE = "100%"
DEFAULT_PITCH = "+0%"
DEFAULT_LENGTH_SCALE = 1.0  # Piper speed: <1 faster, >1 slower
OUTPUT_FORMAT = "mp3"
MAX_CHARS = 2900  # Polly SSML limit ~3000 chars, leave margin

VALID_ENGINES = frozenset({"neural", "generative", "long-form", "standard"})
_RATE_RE = re.compile(r"^\d{1,3}%$")
_PITCH_RE = re.compile(r"^[+-]\d{1,2}%$")


def _validate_rate(rate: str) -> str:
    """Return *rate* if it looks like ``'95%'``, else the default."""
    return rate if _RATE_RE.match(rate) else DEFAULT_RATE


def _validate_pitch(pitch: str) -> str:
    """Return *pitch* if it looks like ``'+10%'``, else the default."""
    return pitch if _PITCH_RE.match(pitch) else DEFAULT_PITCH


def validate_length_scale(value: object) -> float:
    """Coerce *value* to a finite, positive Piper length-scale.

    Returns :data:`DEFAULT_LENGTH_SCALE` for anything non-numeric, non-finite
    (``inf``/``nan``), zero, or negative. ``float()`` of a very large int can
    raise ``OverflowError``, so that is caught too. Shared by the config loader
    and the dashboard PUT handler so a bad value can never reach synthesis or be
    persisted as unserializable JSON (``Infinity``/``NaN`` would break the
    browser's ``JSON.parse`` of the config GET).
    """
    try:
        scale = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, OverflowError):
        return DEFAULT_LENGTH_SCALE
    if not math.isfinite(scale) or scale <= 0:
        return DEFAULT_LENGTH_SCALE
    return scale


def strip_markdown(text: str) -> str:
    """Strip Slack mrkdwn / markdown to plain speakable text."""
    t = text
    # Replace fenced code blocks with spoken placeholder

    def _code_block(m):
        content = m.group(0)
        if content.startswith("```diff"):
            return " (diff block) "
        return " (code block) "

    t = re.sub(r"```[\s\S]*?```", _code_block, t)
    # Remove HTML/XML tags and their content for block-level elements
    t = re.sub(r"<mcwidget[^>]*>[\s\S]*?</mcwidget>", " (widget) ", t)
    # Replace markdown tables with spoken placeholder

    def _table(m):
        rows = (
            len([ln for ln in m.group(0).splitlines() if ln.strip().startswith("|")]) - 1
        )  # exclude header separator
        return f" (table with {rows} rows) "

    t = re.sub(r"(?:^\|.+\|$\n?){2,}", _table, t, flags=re.MULTILINE)
    # Remove inline code: keep short non-path text, strip long or path-like

    def _inline_code(m):
        inner = m.group(1)
        if len(inner) > 30 or "/" in inner:
            return " (file path) "
        return inner

    t = re.sub(r"`([^`]+)`", _inline_code, t)
    # Slack links: <url|label> → label, bare <url> → ""
    t = re.sub(r"<([^|>]+)\|([^>]+)>", r"\2", t)
    t = re.sub(r"<https?://[^>]+>", " (link) ", t)
    # Remove remaining HTML/XML tags (after Slack links are processed)
    t = re.sub(r"</?[a-zA-Z][^>]*>", "", t)
    # Markdown links: [label](url) → label
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)
    # Bold / italic / strikethrough markers
    t = re.sub(r"[*_~]+", "", t)
    # Emoji shortcodes
    t = re.sub(r":[a-z0-9_+-]+:", "", t)
    # Unicode emoji
    t = re.sub(
        r"[\U0001f300-\U0001faff\U00002702-\U000027b0\U0000fe00-\U0000fe0f\U0000200d]+",
        "",
        t,
    )
    # Bare URLs
    t = re.sub(r"https?://\S+", " (link) ", t)
    # OPTIONS buttons line
    t = re.sub(r"\[OPTIONS:.*?\]", "", t)
    # Diff blocks: lines starting with +/- only inside fenced blocks are
    # already removed above; catch stray unified-diff hunks.
    t = re.sub(r"^@@[^@]+@@.*$", "", t, flags=re.MULTILINE)
    # Collapse whitespace
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"  +", " ", t)
    return t.strip()


def text_to_ssml(
    text: str,
    rate: str = DEFAULT_RATE,
    pitch: str = DEFAULT_PITCH,
    engine: str = DEFAULT_ENGINE,
) -> str:
    """Wrap plain text in SSML with natural pauses and prosody controls."""
    from kiro_crew.security import redact_credentials, redact_exfiltration_urls

    text, _ = redact_exfiltration_urls(text)
    text, _ = redact_credentials(text)
    clean = strip_markdown(text)
    if not clean:
        return ""
    if len(clean) > MAX_CHARS:
        # Truncate at last sentence boundary before limit
        trunc = clean[:MAX_CHARS].rsplit(".", 1)[0]
        clean = (trunc or clean[:MAX_CHARS]) + "."
    # Escape XML entities
    clean = clean.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # Natural pauses
    clean = re.sub(r"\n\n+", '<break time="600ms"/>\n', clean)
    clean = re.sub(r"\n", '<break time="300ms"/>\n', clean)
    # Neural engines don't support <prosody> tags (InvalidSsmlException);
    # keep <break> for natural pauses but skip the prosody wrapper.
    if engine == "neural":
        return f"<speak>{clean}</speak>"
    rate = _validate_rate(rate)
    pitch = _validate_pitch(pitch)
    # generative / long-form engines don't support pitch
    if engine in ("generative", "long-form"):
        prosody = f'<prosody rate="{rate}">{clean}</prosody>'
    else:
        prosody = f'<prosody rate="{rate}" pitch="{pitch}">{clean}</prosody>'
    return f"<speak>{prosody}</speak>"


async def _synthesize_piper(
    text: str,
    piper_binary: str = "",
    piper_model: str = "",
    piper_model_config: str = "",
    length_scale: float = 1.0,
) -> str | None:
    """Call local piper TTS to generate WAV. Returns temp file path or None.

    Piper doesn't understand SSML; it takes plain text on stdin. ``length_scale``
    controls speed (<1 faster, >1 slower).
    """
    bin_path = _resolve_piper_binary(piper_binary)
    if not bin_path:
        logger.error("piper binary not found (configured=%r)", piper_binary)
        return None
    model = os.path.expanduser(piper_model) if piper_model else ""
    if not model or not os.path.isfile(model):
        logger.error("piper model not found: %r", piper_model)
        return None
    # Model config is optional — piper auto-detects `<model>.onnx.json` adjacent
    # to the .onnx file when -c is not supplied.
    cfg = os.path.expanduser(piper_model_config) if piper_model_config else ""

    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    sandbox_cleanup: str | None = None
    try:
        try:
            cmd: list[str] = [bin_path, "-m", model, "-f", path]
            if cfg:
                cmd += ["-c", cfg]
            if length_scale != 1.0:
                cmd += ["--length-scale", str(length_scale)]
            # LLM-generated text is piped to piper on stdin. Apply the
            # standard OS-level sandbox (namespace on Linux / seatbelt on
            # macOS) so a compromised model or binary can't reach private
            # filesystem areas. ``wrap_argv`` is a no-op on platforms
            # without a backend and returns a cleanup path that we must
            # unlink after the child exits (Linux launcher script /
            # macOS seatbelt profile).
            cmd, sandbox_cleanup = wrap_argv(cmd, mode="standard")
            cmd = cgroup_scope_argv(cmd)  # cgroup DoS ceiling
            proc = await create_subprocess_limited(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _stdout, stderr = await asyncio.wait_for(
                    proc.communicate(text.encode("utf-8")), timeout=60,
                )
            except asyncio.TimeoutError:
                # asyncio.wait_for cancels communicate() on timeout but does NOT
                # terminate the child process — kill it explicitly to avoid a
                # zombie piper consuming CPU after we return.
                logger.error("piper timed out after 60s; killing subprocess")
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    logger.debug("piper wait after kill failed", exc_info=True)
                try:
                    os.unlink(path)
                except OSError:
                    pass
                return None
            if proc.returncode != 0:
                logger.error("piper failed (rc=%d): %s", proc.returncode, stderr.decode(errors="replace")[:500])
                os.unlink(path)
                return None
            if os.path.getsize(path) < 100:
                logger.error("piper output too small")
                os.unlink(path)
                return None
            return path
        except SandboxUnavailableError as exc:
            # Same fail-closed sandbox refusal as the Polly path — relay the
            # sandbox layer's own kind-specific remedy prose (see that handler
            # for why it is never hardcoded) instead of logging a stack trace
            # that reads as a piper binary/model fault.
            logger.error(
                "voice_reply: Piper TTS refused by the sandbox (%s): %s",
                exc.kind,
                exc,
            )
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
        except Exception:
            logger.exception("piper synthesis error")
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
    finally:
        # Clean up the sandbox launcher script / seatbelt profile spawned
        # by wrap_argv (None on platforms without a sandbox backend).
        if sandbox_cleanup:
            try:
                os.unlink(sandbox_cleanup)
            except OSError:
                pass


async def synthesize_speech(
    text: str,
    provider: str = DEFAULT_PROVIDER,
    # Polly-specific:
    voice_id: str = DEFAULT_VOICE,
    engine: str = DEFAULT_ENGINE,
    rate: str = DEFAULT_RATE,
    pitch: str = DEFAULT_PITCH,
    aws_profile: str = "",
    region: str = "",
    # Piper-specific:
    piper_binary: str = "",
    piper_model: str = "",
    piper_model_config: str = "",
    length_scale: float = 1.0,
) -> str | None:
    """Generate audio from *text* using the configured *provider*.

    Returns the path to a temp audio file (``.mp3`` for Polly, ``.wav`` for
    Piper), or None on failure. Caller is responsible for deleting the file.

    LLM output is redacted for credentials and exfiltration URLs before
    synthesis — audio files uploaded to Slack bypass the usual text-path
    redaction, so we apply both filters here to prevent secrets or
    suspicious URLs from being spoken and persisted in Slack.
    """
    # ── Redact LLM output before it crosses an external surface (audio) ──
    text, cred_warns = redact_credentials(text)
    text, url_warns = redact_exfiltration_urls(text)
    if cred_warns:
        logger.warning("voice_reply: redacted %d credential pattern(s) before TTS", len(cred_warns))
    if url_warns:
        logger.warning("voice_reply: redacted %d suspicious URL(s) before TTS", len(url_warns))

    if provider == PROVIDER_POLLY:
        ssml = text_to_ssml(text, rate=rate, pitch=pitch, engine=engine)
        if not ssml:
            return None
        return await _synthesize_polly(
            ssml,
            voice_id=voice_id,
            engine=engine,
            aws_profile=aws_profile,
            region=region,
        )
    if provider == PROVIDER_PIPER:
        plain = strip_markdown(text).strip()
        if not plain:
            return None
        return await _synthesize_piper(
            plain,
            piper_binary=piper_binary,
            piper_model=piper_model,
            piper_model_config=piper_model_config,
            length_scale=length_scale,
        )
    logger.error("synthesize_speech: unknown provider %r", provider)
    return None


async def _synthesize_polly(
    ssml: str,
    voice_id: str = DEFAULT_VOICE,
    engine: str = DEFAULT_ENGINE,
    aws_profile: str = "",
    region: str = "",
) -> str | None:
    """Call Amazon Polly to generate MP3.  Returns temp file path or ``None``.

    ``ssml`` may be SSML (starting with ``<speak``) or plain text;
    text-type is auto-detected from the leading ``<speak`` tag.
    """
    # Polly is OPTIONAL and driven via the ``aws`` CLI (no boto3 dependency).
    # On a vanilla machine without the CLI installed, degrade gracefully here
    # instead of raising FileNotFoundError from create_subprocess_exec.
    if shutil.which("aws") is None:
        logger.info("voice_reply: Polly unavailable (aws CLI not on PATH); skipping TTS")
        return None
    if engine not in VALID_ENGINES:
        logger.error("Invalid Polly engine %r, falling back to neural", engine)
        engine = DEFAULT_ENGINE
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    sandbox_cleanup: str | None = None
    try:
        try:
            cmd: list[str] = ["aws", "polly", "synthesize-speech"]
            if aws_profile:
                cmd += ["--profile", aws_profile]
            if region:
                cmd += ["--region", region]
            cmd += [
                "--engine",
                engine,
                "--voice-id",
                voice_id,
                "--output-format",
                OUTPUT_FORMAT,
                "--text-type",
                "ssml" if ssml.startswith("<speak") else "text",
                "--text",
                ssml,
                path,
            ]
            # LLM-derived SSML is passed on the command line to the AWS CLI.
            # Apply an OS-level sandbox (namespace on Linux / seatbelt on
            # macOS) so a compromised CLI or model can't reach private
            # filesystem areas. ``wrap_argv`` is a no-op on platforms without
            # a backend and returns a cleanup path that we must unlink after
            # the child exits.
            cmd, sandbox_cleanup = wrap_argv(cmd, mode="standard")
            cmd = cgroup_scope_argv(cmd)  # cgroup DoS ceiling
            proc = await create_subprocess_limited(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                # ``asyncio.wait_for`` cancels ``proc.communicate()`` on
                # timeout but does NOT terminate the child — kill it
                # explicitly to avoid a hung ``aws polly`` process consuming
                # resources after we return.
                logger.error("Polly timed out after 30s; killing subprocess")
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except Exception:
                    logger.debug("polly wait after kill failed", exc_info=True)
                try:
                    os.unlink(path)
                except OSError:
                    pass
                return None
            if proc.returncode != 0:
                logger.error("Polly failed: %s", stderr.decode())
                os.unlink(path)
                return None
            if os.path.getsize(path) < 100:
                logger.error("Polly output too small")
                os.unlink(path)
                return None
            return path
        except SandboxUnavailableError as exc:
            # A host with no OS sandbox backend (every Windows host, and Linux
            # without user namespaces) fail-closes here rather than spawning the
            # AWS CLI unconfined. Report it as its own diagnosis: the generic
            # handler below logs a stack trace under "Polly synthesis error",
            # which reads as a Polly/credentials fault and sends the operator
            # looking in the wrong place.
            #
            # The remedy prose comes from ``str(exc)``, never hardcoded here.
            # The sandbox layer picks it per ``kind``, and only "no_backend"
            # names the allow_unsandboxed_exec opt-in: "transient" means
            # momentary resource pressure where callers must NOT advise
            # disabling the sandbox, and "foreign_sandbox" means this host's
            # sandbox is fine and the fix is a kiro-cli setting.
            logger.error(
                "voice_reply: Polly TTS refused by the sandbox (%s): %s",
                exc.kind,
                exc,
            )
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
        except Exception:
            logger.exception("Polly synthesis error")
            try:
                os.unlink(path)
            except OSError:
                pass
            return None
    finally:
        # Clean up the sandbox launcher script / seatbelt profile spawned
        # by wrap_argv (None on platforms without a sandbox backend).
        if sandbox_cleanup:
            try:
                os.unlink(sandbox_cleanup)
            except OSError:
                pass


async def upload_voice_to_slack(
    slack_client: SlackClientOps,
    channel: str,
    thread_ts: str,
    audio_path: str,
) -> bool:
    """Upload an audio file to a Slack thread as a voice clip.

    The file extension (.mp3 / .wav / .ogg etc.) is preserved so Slack's
    player renders it correctly.
    """
    ext = os.path.splitext(audio_path)[1].lstrip(".") or "mp3"
    try:
        await slack_client.upload_file(
            channel=channel,
            thread_ts=thread_ts,
            file=audio_path,
            filename=f"voice-reply.{ext}",
            title="\U0001f50a Voice Reply",
        )
        return True
    except Exception:
        logger.exception("Slack file upload failed")
        return False


def split_sentences(text: str) -> list[str]:
    """Split text into sentences at . ! ? boundaries."""
    clean = strip_markdown(text)
    if not clean:
        return []
    parts = re.split(r"(?<=[.!?])\s+", clean)
    return [s.strip() for s in parts if s.strip()]


async def stitch_mp3s(paths: list[str], output: str | None = None) -> str | None:
    """Concatenate MP3 files into a single file using ffmpeg.

    Windows: ffmpeg is not guaranteed on PATH (the dashboard-streaming stitch
    path then fails); should be made optional + warn. The Slack thread-upload
    path is unaffected. Tracked as follow-on work.
    """
    if not paths:
        return None
    if len(paths) == 1:
        if output:
            import shutil

            shutil.copy2(paths[0], output)
            return output
        return paths[0]
    if output is None:
        fd, output = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
    concat = "|".join(paths)
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            f"concat:{concat}",
            "-c",
            "copy",
            output,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await asyncio.wait_for(proc.communicate(), timeout=30)
        if proc.returncode != 0 or not os.path.exists(output):
            return None
        return output
    except Exception:
        logger.exception("ffmpeg stitch failed")
        return None


async def streaming_voice_reply(
    response_text: str,
    voice_id: str = DEFAULT_VOICE,
    engine: str = DEFAULT_ENGINE,
    rate: str = DEFAULT_RATE,
    pitch: str = DEFAULT_PITCH,
    aws_profile: str = "",
    region: str = "",
):
    """Async generator: yields (sentence_index, sentence_text, mp3_bytes) per sentence.

    Use this for dashboard streaming — play each chunk as it arrives,
    then call ``stitch_mp3s`` on the collected paths for a single replay file.

    LLM output is redacted for credentials and exfiltration URLs before
    synthesis (same rationale as ``synthesize_speech``) — streaming audio
    to the dashboard bypasses the usual text-path redaction.
    """
    # ── Redact LLM output before it crosses an external surface (audio) ──
    response_text, cred_warns = redact_credentials(response_text)
    response_text, url_warns = redact_exfiltration_urls(response_text)
    if cred_warns:
        logger.warning("stream_voice_chunks: redacted %d credential pattern(s) before TTS", len(cred_warns))
    if url_warns:
        logger.warning("stream_voice_chunks: redacted %d suspicious URL(s) before TTS", len(url_warns))

    sentences = split_sentences(response_text)
    for i, sentence in enumerate(sentences):
        ssml = text_to_ssml(sentence, rate=rate, pitch=pitch, engine=engine)
        if not ssml:
            continue
        # Dashboard streaming is Polly-only today (sentence-by-sentence MP3
        # chunks). Calls the Polly-specific internal to avoid double-wrapping
        # text→SSML through the public dispatcher.
        mp3_path = await _synthesize_polly(
            ssml,
            voice_id=voice_id,
            engine=engine,
            aws_profile=aws_profile,
            region=region,
        )
        if not mp3_path:
            continue
        try:
            with open(mp3_path, "rb") as f:
                mp3_bytes = f.read()
            yield i, sentence, mp3_bytes
        finally:
            try:
                os.unlink(mp3_path)
            except OSError:
                pass


async def voice_reply(
    slack_client: SlackClientOps,
    channel: str,
    thread_ts: str,
    response_text: str,
    provider: str = DEFAULT_PROVIDER,
    # Polly:
    voice_id: str = DEFAULT_VOICE,
    engine: str = DEFAULT_ENGINE,
    rate: str = DEFAULT_RATE,
    pitch: str = DEFAULT_PITCH,
    aws_profile: str = "",
    region: str = "",
    # Piper:
    piper_binary: str = "",
    piper_model: str = "",
    piper_model_config: str = "",
    length_scale: float = 1.0,
) -> bool:
    """Full pipeline: text → provider synthesis → Slack upload.

    Provider selection is controlled by the ``provider`` argument; Polly-
    specific args are ignored when ``provider="piper"`` and vice versa.
    """
    audio_path = await synthesize_speech(
        response_text,
        provider=provider,
        voice_id=voice_id,
        engine=engine,
        rate=rate,
        pitch=pitch,
        aws_profile=aws_profile,
        region=region,
        piper_binary=piper_binary,
        piper_model=piper_model,
        piper_model_config=piper_model_config,
        length_scale=length_scale,
    )
    if not audio_path:
        return False

    try:
        return await upload_voice_to_slack(slack_client, channel, thread_ts, audio_path)
    finally:
        try:
            os.unlink(audio_path)
        except OSError:
            pass
