"""Kiro Crew voice reply — generate TTS audio and deliver it to the requesting surface.

Post-response hook: strips markdown, generates audio via the configured
TTS provider (Amazon Polly or local Piper), then delivers it back to the
requesting surface (Slack thread upload, dashboard playback).
Fire-and-forget — never blocks the text response.

Supported providers:
- ``system`` (default): the host's own speech engine, which needs nothing
  installed and no account — ``say`` on macOS, System.Speech through
  Windows PowerShell 5.1 on Windows, ``espeak-ng`` on Linux when present.
  Produces WAV. Linux is the one platform where it can be absent.
- ``polly``: OPTIONAL Amazon Polly via the ``aws polly synthesize-speech`` CLI.
  Produces MP3. Requires the ``aws`` CLI on PATH plus valid AWS credentials +
  network. This module never imports ``boto3``/``botocore`` — Polly is driven
  entirely through the CLI subprocess, so the module imports cleanly on a
  vanilla machine without any AWS SDK installed. When the CLI or credentials
  are absent, the Polly path degrades gracefully (returns ``None``).
- ``piper``: Local neural TTS via the ``piper`` CLI
  (https://github.com/OHF-Voice/piper1-gpl, published as the ``piper-tts``
  wheel). Produces WAV. Requires the ``piper`` binary and a voice model
  (.onnx + .onnx.json) on disk. Fully offline, and the best offline quality.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import math
import os
import re
import shutil
import struct
import tempfile
from typing import TYPE_CHECKING, Any

from kiro_crew import aws_consent, piper_runtime
from kiro_crew.constants import strip_control_comments
from kiro_crew.deploy.engine import resolve_aws_bin
from kiro_crew.piper_runtime import REQUEST_TIMEOUT_SECONDS as _PIPER_STREAM_TIMEOUT_SECONDS
from kiro_crew.piper_worker import MAX_AUDIO_BYTES as _PIPER_MAX_AUDIO_BYTES
from kiro_crew.platform.context import redact_log_via_context
from kiro_crew.platform_compat import IS_MACOS, IS_WINDOWS, trusted_system_bin
from kiro_crew.sandbox import (
    SandboxUnavailableError,
    cgroup_scope_argv,
    create_subprocess_limited,
    sandboxed_spawn_argv_async,
    scrub_env,
    wrap_argv,
    wrap_argv_async,
)
from kiro_crew.security import (
    PathResolutionStalled,
    is_sensitive_path,
    redact_credentials,
    redact_exfiltration_urls,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from kiro_crew.piper_runtime import PiperRuntime
    from kiro_crew.slack.client import SlackClientOps

logger = logging.getLogger(__name__)


def resolve_polly_cli() -> str | None:
    """Resolve the ``aws`` CLI for Polly spawn sites; ``None`` when not invocable.

    Routes through the deploy engine's shared well-known-dirs resolver so a
    GUI-launched gateway's minimal PATH still finds the CLI instead of silently
    skipping TTS / degrading to an empty voice list (#4770). The trailing
    ``shutil.which`` turns the resolver's bare-name fallback into the ``None``
    these probe sites already treat as "unavailable", and confirms an absolute
    hit is still actually executable.
    """
    aws_bin = resolve_aws_bin()
    return aws_bin if shutil.which(aws_bin) else None


# ── Provider constants ──
PROVIDER_POLLY = "polly"
PROVIDER_PIPER = "piper"
PROVIDER_SYSTEM = "system"
VALID_PROVIDERS = frozenset({PROVIDER_POLLY, PROVIDER_PIPER, PROVIDER_SYSTEM})
# The host's built-in speech engine is the default because it is the only
# provider that needs nothing installed and no account: macOS and Windows both
# ship one, so auto-speak works on a fresh machine. Piper is the offline
# quality upgrade (a binary plus a voice model on disk) and Polly the paid
# cloud option; both are used only when explicitly selected.
DEFAULT_PROVIDER = PROVIDER_SYSTEM

# Built-in engine per platform. The value is what ``_synthesize_system``
# switches on, so it names the engine rather than the OS: a host can lack the
# engine its OS normally ships (a container without ``say``), and then there is
# no built-in provider at all rather than a differently-named one.
SYSTEM_ENGINE_SAY = "say"  # macOS /usr/bin/say
SYSTEM_ENGINE_SAPI = "sapi"  # Windows System.Speech via powershell.exe
SYSTEM_ENGINE_ESPEAK = "espeak-ng"  # Linux/BSD, when installed

# SAPI's Rate is an integer -10..10 on a roughly logarithmic scale, while
# ``say`` and ``espeak-ng`` take words per minute. Both are derived from the
# same ``rate`` percentage the Polly provider already validates, so selecting a
# speed never depends on which platform the user is on.
_SYSTEM_BASE_WPM = 175  # `say` and `espeak-ng` default speaking rate
_SAPI_RATE_MIN = -10
_SAPI_RATE_MAX = 10
_SYSTEM_WPM_MIN = 80
_SYSTEM_WPM_MAX = 500


def is_available(
    provider: str = DEFAULT_PROVIDER,
    piper_binary: str = "",
    piper_model: str = "",
) -> bool:
    """Return True if TTS for the given provider can be produced.

    - ``system``: checks the host's built-in engine is invocable. macOS and
      Windows always satisfy this; a Linux host without ``espeak-ng`` does not,
      and there is no fallback to fabricate — the caller reports it as
      unavailable so the user can install it or pick another provider.
    - ``polly``: checks the ``aws`` CLI is on PATH. Credential validity is
      NOT verified here — a missing/expired profile still fails gracefully
      inside ``synthesize_speech`` (caught, logged, returns None).
    - ``piper``: checks the piper binary is invocable AND the model file
      exists. Model-less piper is unusable, so we verify both up front.

    Callers use this to decide whether to post a fallback ephemeral message
    when voice output was requested but cannot be produced.
    """
    if provider == PROVIDER_SYSTEM:
        return resolve_system_tts() is not None
    if provider == PROVIDER_POLLY:
        return resolve_polly_cli() is not None
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


class SystemVoiceProbeError(RuntimeError):
    """The host's built-in engine exists but its voice list could not be read.

    Distinct from "no engine at all", which is an ordinary state the panel
    reports neutrally. This one is a failure with a retry, so it must not be
    flattened into an empty voice list.
    """


def resolve_system_tts() -> tuple[str, str] | None:
    """Return ``(engine, binary)`` for the host's built-in TTS, else ``None``.

    Resolution goes through ``trusted_system_bin`` rather than ``PATH``: the
    gateway's ``PATH`` can lead with directories the agent itself can write, and
    a planted ``say``/``espeak-ng`` shim would then be handed LLM text with the
    gateway's environment.

    Linux is the one platform with no guaranteed engine — a stock Ubuntu Desktop
    carries the espeak-ng *library* and data but not the CLI, and Server carries
    neither — so ``None`` there is the normal answer, not a broken host.
    """
    if IS_MACOS:
        found = trusted_system_bin("say")
        return (SYSTEM_ENGINE_SAY, found) if found else None
    if IS_WINDOWS:
        # Windows PowerShell 5.1 specifically, which is what ships in
        # System32\WindowsPowerShell\v1.0. System.Speech is .NET-Framework-only
        # and Speak() throws in PowerShell 7, so resolving a `pwsh` on PATH
        # instead would produce a provider that reports available and then
        # fails on every call.
        found = trusted_system_bin("powershell")
        return (SYSTEM_ENGINE_SAPI, found) if found else None
    for name in ("espeak-ng", "espeak"):
        found = trusted_system_bin(name)
        if found:
            return (SYSTEM_ENGINE_ESPEAK, found)
    return None


async def resolve_system_tts_async() -> tuple[str, str] | None:
    """``resolve_system_tts`` off the event loop.

    Resolution stats a handful of fixed directories. Each stat is cheap on a
    healthy host but is not bounded: a fixed directory can sit on a stalled
    network or fuse mount, and the gateway runs every session and its heartbeats
    on one loop, so one blocked stat freezes all of them. Every async caller
    goes through this rather than the sync form.
    """
    return await asyncio.to_thread(resolve_system_tts)


def _system_wpm(rate: object) -> int:
    """Map a validated ``rate`` percentage onto words per minute."""
    pct = int(_validate_rate(rate).rstrip("%"))
    wpm = round(_SYSTEM_BASE_WPM * pct / 100)
    return max(_SYSTEM_WPM_MIN, min(_SYSTEM_WPM_MAX, wpm))


def _sapi_rate(rate: object) -> int:
    """Map a validated ``rate`` percentage onto SAPI's -10..10 integer scale.

    SAPI's scale is roughly logarithmic with each step near 1.4x, so a linear
    percentage cannot land exactly; 10% per step keeps 100% at 0 (the engine
    default) and reaches the endpoints at the extremes the UI offers.
    """
    pct = int(_validate_rate(rate).rstrip("%"))
    step = round((pct - 100) / 10)
    return max(_SAPI_RATE_MIN, min(_SAPI_RATE_MAX, step))


def _resolve_piper_binary(configured: str = "") -> str | None:
    """Return piper binary path or None if not found.

    Resolution order: explicit ``configured`` path → ``piper`` on PATH →
    the console script inside a conventional ``~/piper-venv``. The venv's
    script directory differs by platform (``Scripts`` on Windows, ``bin``
    elsewhere), so the fallback is built from the platform rather than assumed.
    """
    if configured:
        p = os.path.expanduser(configured)
        return p if _is_executable_file(p) else None
    found = shutil.which("piper")
    if found:
        return found
    scripts = "Scripts" if IS_WINDOWS else "bin"
    exe = "piper.exe" if IS_WINDOWS else "piper"
    fallback = os.path.expanduser(os.path.join("~", "piper-venv", scripts, exe))
    return fallback if _is_executable_file(fallback) else None


def _is_executable_file(path: str) -> bool:
    """Return True when *path* is a file this platform can launch.

    ``os.access(X_OK)`` is not a usable test on Windows — it reports any
    readable file as executable — so there a file that HAS an extension must
    carry a launchable one. Without that, a configured
    ``piper_model_notes.txt`` passes ``is_available``, the caller skips its
    "voice unavailable" notice, and the spawn then fails with no explanation.
    ``PATHEXT`` is read rather than hardcoded because an operator can extend it.
    """
    if not os.path.isfile(path):
        return False
    if IS_WINDOWS:
        suffix = os.path.splitext(path)[1].lower()
        if not suffix:
            # PATHEXT governs how a bare NAME resolves against PATH, not what
            # CreateProcess will launch from an explicit path, and a PE image
            # needs no extension — so an extensionless configured path stands.
            return True
        # PATHEXT is always ';'-separated, which is not `os.pathsep` when this
        # branch is exercised from a POSIX host's tests.
        exts = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
        return suffix in {e.strip().lower() for e in exts.split(";") if e.strip()}
    return os.access(path, os.X_OK)


# ── Config defaults ──
DEFAULT_VOICE = "Ruth"
DEFAULT_ENGINE = "generative"
DEFAULT_RATE = "100%"
DEFAULT_PITCH = "+0%"
DEFAULT_LENGTH_SCALE = 1.0  # Piper speed: <1 faster, >1 slower
_PIPER_CHUNK_SECONDS = 0.2
_PIPER_MAX_PHRASE_CHARS = 240
_PIPER_REAP_TIMEOUT_SECONDS = 5
_PIPER_MAX_CONFIG_BYTES = 64 * 1024
_PIPER_MAX_STDERR_BYTES = 4096
OUTPUT_FORMAT = "mp3"
MAX_CHARS = 2900  # Polly SSML limit ~3000 chars, leave margin

VALID_ENGINES = frozenset({"neural", "generative", "long-form", "standard"})
_RATE_RE = re.compile(r"^\d{1,3}%$")
_PITCH_RE = re.compile(r"^[+-]\d{1,2}%$")


def _validate_rate(rate: object) -> str:
    """Return *rate* if it looks like ``'95%'``, else the default.

    The parameter is ``object`` because the value reaches here straight from
    ``config.json``: a hand-edited ``"rate": 100`` arrives as an int, and
    handing that to ``_RATE_RE.match`` raises TypeError inside synthesis. A bare
    number is a mundane typo rather than an extreme condition, so it degrades to
    the default rate instead of dropping the audio. Coercing here rather than at
    each caller is what makes every reader of ``rate`` safe at once.
    """
    return rate if isinstance(rate, str) and _RATE_RE.match(rate) else DEFAULT_RATE


def _validate_pitch(pitch: object) -> str:
    """Return *pitch* if it looks like ``'+10%'``, else the default.

    Non-string input degrades to the default for the same reason as
    :func:`_validate_rate`.
    """
    return pitch if isinstance(pitch, str) and _PITCH_RE.match(pitch) else DEFAULT_PITCH


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


def validated_config_string(value: object) -> str | None:
    """Return *value* stripped when it is a string, else ``None``.

    ``None`` means "reject", not "empty": for these fields an empty string is
    itself a meaningful value (``system_voice=""`` selects the OS default voice),
    so silently coercing a wrong TYPE to ``""`` would rewrite the caller's intent
    from "set this voice" into "use the default" without telling them.

    Shared by the config loader and the dashboard PUT handler, like
    :func:`validate_length_scale`. Splitting them is what let the write path keep
    persisting ``str({})`` -> ``"{}"`` as a voice name or a binary path while the
    loader was already defending against exactly that value on read.
    """
    if not isinstance(value, str):
        return None
    return value.strip()


def validated_config_bool(value: object) -> bool | None:
    """Return *value* when it is a real bool, else ``None`` meaning "reject".

    ``bool()`` is the trap this exists to avoid. It never raises, which makes it
    look like a safe total function, but it is not TRUTH-PRESERVING for the values
    a JSON client actually sends: ``bool("false")`` is ``True``, so a caller
    passing the string ``"false"`` persists the exact opposite of what it asked
    for, silently. Total is not the same property as correct.

    JSON has real booleans, so a client that means false can send ``false``; a
    quoted ``"false"`` is a bug worth reporting rather than guessing at.
    """
    if not isinstance(value, bool):
        return None
    return value


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
    # Strip RECOGNIZED control-tag comments (keep-visible #7948, deliver
    # routing, plan_task_id anchors) — never all comments, and never inside
    # inline code, which renders literally and must survive to speech. The
    # generic tag regex below deliberately excludes "<!". Shared
    # implementation + grammar spec: constants.strip_control_comments.
    t = strip_control_comments(t)
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
    # Re-run redaction on the STRIPPED text. Every strip above can make two
    # halves of a secret contiguous (a control comment, `**` emphasis, or an
    # HTML tag interposed inside a key id), so a credential scan that ran on
    # the raw text has not necessarily seen the string TTS will speak.
    # Idempotent on clean text; placeholders survive re-scanning. (#7960)
    t, _ = redact_exfiltration_urls(t)
    t, _ = redact_credentials(t)
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


async def _kill_and_reap(
    proc: "asyncio.subprocess.Process", label: str, *, timed_out: bool
) -> None:
    """Terminate and drain a child ``asyncio.wait_for`` already gave up on.

    ``wait_for`` cancels ``communicate()`` but never terminates the child, and a
    caller cancellation (client disconnect) or an interpreter-exit signal arrives
    the same way, so without this a zombie keeps consuming CPU after we return.

    The reap goes through ``communicate()`` rather than ``wait()``: the pipe
    readers are already cancelled, so a killed child blocked on a full PIPE would
    never be drained and ``wait()`` would hang. On the non-timeout path a repeat
    cancellation landing on the reap is swallowed so the ORIGINAL exception is
    the one that propagates; on the timeout path it is a genuinely new
    cancellation and is let out.
    """
    try:
        proc.kill()
    except OSError:
        pass
    try:
        await proc.communicate()
    except Exception:
        logger.debug("%s wait after kill failed", label, exc_info=True)
    except BaseException:
        if timed_out:
            raise


MIN_AUDIO_BYTES = 100


def _produced_audio(out_path: str) -> bool:
    """Return whether *out_path* holds a plausible audio file.

    Both stats are done here, in one call, so a caller can offload the pair with
    a single thread hop. An engine that exits 0 having written nothing (or a
    truncated header) is a real outcome, so size is checked, not just existence.
    """
    return os.path.isfile(out_path) and os.path.getsize(out_path) >= MIN_AUDIO_BYTES


async def _run_tts_subprocess(
    cmd: list[str],
    out_path: str,
    *,
    label: str,
    display: str,
    stdin_text: str = "",
    first_party_fixed_argv: bool = False,
    timeout: int = 60,
) -> bool:
    """Run one TTS command that writes *out_path*; return whether it produced audio.

    Shared by every local engine so the subtle parts — killing and reaping a
    child that timed out or was cancelled, and never leaving a zombie holding a
    full pipe — have one implementation rather than one per provider.
    The caller owns *out_path* and discards it when this returns False.

    EVERY engine goes through the sandbox: each one parses text it did not
    author, so confinement is the default and there is no per-provider skip.
    :func:`sandboxed_spawn_argv_async` supplies BOTH layers — the OS-level
    wrap and a credential-scrubbed environment — because on a backend-less
    host the env scrub is the only one of the two that still applies, and a
    TTS child has no business reading the gateway's credentials either way.
    ``first_party_fixed_argv`` is the sanctioned carve-out for the one spawn
    whose argv this package derives entirely — it changes nothing where a
    backend exists, and on a backend-less host (Windows) it runs env-scrubbed,
    loudly warned and SEL-audited under any governance floor, instead of
    fail-closing and leaving that platform with no built-in voice.
    """
    sandbox_cleanup: str | None = None
    try:
        # Both preparation steps stat the filesystem — the sandbox probe walks
        # PATH, and the cgroup wrap ensures the parent slice's limits — so
        # neither may run inline on the event loop.
        cmd, child_env, sandbox_cleanup = await sandboxed_spawn_argv_async(
            cmd,
            mode="standard",
            first_party_fixed_argv=first_party_fixed_argv,
        )
        cmd = await asyncio.to_thread(cgroup_scope_argv, cmd)  # cgroup DoS ceiling
        proc = await create_subprocess_limited(
            *cmd,
            env=child_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(
                proc.communicate(stdin_text.encode("utf-8")),
                timeout=timeout,
            )
        except BaseException as exc:
            timed_out = isinstance(exc, asyncio.TimeoutError)
            if timed_out:
                logger.error("%s timed out after %ds; killing subprocess", label, timeout)
            await _kill_and_reap(proc, label, timed_out=timed_out)
            if not timed_out:
                raise  # cancellation/interrupt must propagate to the caller
            return False
        if proc.returncode != 0:
            logger.error(
                "%s failed (rc=%d): %s",
                label,
                proc.returncode,
                stderr.decode(errors="replace")[:500],
            )
            return False
        # Both stats go off-loop in ONE hop: the output lives under TMPDIR, which
        # an operator can point at a network or FUSE mount, and a stalled stat
        # there would freeze every session and heartbeat this loop serves.
        if not await asyncio.to_thread(_produced_audio, out_path):
            logger.error("%s output too small", label)
            return False
        return True
    except SandboxUnavailableError as exc:
        # Same fail-closed sandbox refusal as the Polly path — relay the sandbox
        # layer's own kind-specific remedy prose (see that handler for why it is
        # never hardcoded) instead of logging a stack trace that reads as an
        # engine fault.
        logger.error(
            "voice_reply: %s refused by the sandbox (%s): %s",
            display,
            exc.kind,
            exc,
        )
        return False
    except Exception:
        logger.exception("%s synthesis error", label)
        return False
    finally:
        # Clean up the sandbox launcher script / seatbelt profile spawned by
        # wrap_argv (None on platforms without a sandbox backend).
        if sandbox_cleanup:
            try:
                os.unlink(sandbox_cleanup)
            except OSError:
                pass


def _new_wav_path() -> str:
    """Allocate an empty temp ``.wav`` and return its path."""
    fd, path = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    return path


def _spill_text(text: str) -> str:
    """Write *text* to a temp UTF-8 file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    return path


async def _synthesize_system(
    text: str,
    voice: object = "",
    rate: object = DEFAULT_RATE,
) -> str | None:
    """Speak *text* with the host's built-in engine. Returns a WAV path or None.

    Every engine here is handed the text out of band — on stdin, or in a temp
    file for SAPI — never in argv, so nothing in a model's output is parsed as a
    command-line option or a shell word.

    Both temp-file allocations run on a worker thread: a reply's whole text is
    spilled to disk on the SAPI path, and the gateway runs every session on one
    loop, so a synchronous write here stalls every other chat turn.
    """
    resolved = await resolve_system_tts_async()
    if not resolved:
        logger.error("no built-in system TTS engine on this host")
        return None
    engine, bin_path = resolved

    # Coerced at the one consumer rather than at each config reader: the value
    # arrives raw from config.json, where a hand-edited `"system_voice": []` is
    # possible, and every engine here would break on it differently — SAPI's
    # `_ps_quote` raises AttributeError, and the two argv engines raise TypeError
    # inside the spawn. An unusable name degrades to the engine's own default.
    voice = voice if isinstance(voice, str) else ""

    path = await asyncio.to_thread(_new_wav_path)
    text_file = ""
    voice_file = ""
    succeeded = False
    try:
        cmd: list[str]
        stdin_text = text
        if engine == SYSTEM_ENGINE_SAY:
            cmd = [
                bin_path,
                "-o",
                path,
                "--file-format=WAVE",
                # `say -o` defaults to AIFF-C; the dashboard plays PCM WAV, and
                # the sample rate matches what the piper voices produce so the
                # two providers sound consistently pitched.
                "--data-format=LEI16@22050",
                "-r",
                str(_system_wpm(rate)),
            ]
            if voice:
                cmd += ["-v", voice]
        elif engine == SYSTEM_ENGINE_ESPEAK:
            cmd = [bin_path, "-w", path, "-s", str(_system_wpm(rate))]
            if voice:
                cmd += ["-v", voice]
        else:
            text_file = await asyncio.to_thread(_spill_text, text)
            # The voice name goes to a file for the same reason the text does:
            # it comes from config.json, and keeping it off argv is what makes
            # this spawn's argv derived entirely inside this package.
            if voice:
                voice_file = await asyncio.to_thread(_spill_text, voice)
            cmd = [
                bin_path,
                "-NoProfile",
                "-NonInteractive",
                "-NoLogo",
                "-EncodedCommand",
                _sapi_encoded_command(path, text_file, voice_file, _sapi_rate(rate)),
            ]
            stdin_text = ""
        succeeded = await _run_tts_subprocess(
            cmd,
            path,
            label=engine,
            display="System TTS",
            stdin_text=stdin_text,
            # SAPI only: after the two spills above, that argv is a System32
            # binary plus module constants and internally-derived temp paths, so
            # it satisfies the first-party carve-out. `say`/`espeak-ng` carry the
            # configured voice on argv and cannot claim it — which costs nothing,
            # because the carve-out is inert wherever a backend exists.
            first_party_fixed_argv=engine == SYSTEM_ENGINE_SAPI,
        )
        return path if succeeded else None
    finally:
        # Invariant, not per-exit cleanup: EVERY unsuccessful exit — including
        # CancelledError, which ``except Exception`` does not catch — must
        # discard the owned temp file, or a new exit path re-opens the leak.
        if not succeeded:
            try:
                os.unlink(path)
            except OSError:
                pass
        for scratch in (text_file, voice_file):
            if scratch:
                try:
                    os.unlink(scratch)
                except OSError:
                    pass


def _sapi_encoded_command(out_path: str, text_path: str, voice_path: str, rate: int) -> str:
    """Build the base64 ``-EncodedCommand`` payload for the SAPI synthesizer.

    Encoding sidesteps command-line quoting entirely, which matters because the
    script embeds filesystem paths. ONLY internally-derived temp paths and a
    validated integer are interpolated: both the spoken text and the voice name
    are read from files at runtime, so nothing a user or a model supplied ever
    reaches argv. That is what lets the Windows spawn claim
    ``first_party_fixed_argv`` at the sandbox chokepoint. Each path is escaped
    for a PowerShell single-quoted literal so a quote in a temp path cannot
    terminate the string.
    """
    select_voice = (
        f"$v=[IO.File]::ReadAllText({_ps_quote(voice_path)},[Text.Encoding]::UTF8);"
        "$s.SelectVoice($v);"
        if voice_path
        else ""
    )
    script = (
        "$ErrorActionPreference='Stop';"
        "Add-Type -AssemblyName System.Speech;"
        f"$t=[IO.File]::ReadAllText({_ps_quote(text_path)},[Text.Encoding]::UTF8);"
        "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "try{"
        f"{select_voice}"
        f"$s.Rate={rate};"
        # Pin the container format: SetOutputToWaveFile(path) alone leaves it to
        # the selected voice, and the dashboard decodes one shape.
        "$f=New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo("
        "22050,"
        "[System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen,"
        "[System.Speech.AudioFormat.AudioChannel]::Mono);"
        f"$s.SetOutputToWaveFile({_ps_quote(out_path)},$f);"
        "$s.Speak($t);"
        "}finally{$s.Dispose()}"
    )
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


def _ps_quote(value: str) -> str:
    """Return *value* as a PowerShell single-quoted literal."""
    return "'" + value.replace("'", "''") + "'"


# macOS `say -v '?'` pads the voice name with at least two spaces before the
# locale, and names themselves contain spaces and parentheses ("Grandma
# (Deutsch)"), so the run of spaces is the only reliable field separator.
# The locale tail excludes the separators it is delimited by (`[A-Za-z0-9]`, not
# `\w`, which contains `_`): with `\w` there are many ways to partition a run
# like `0_0_0_`, and the resulting backtracking is exponential in its length.
_SAY_VOICE_RE = re.compile(r"^(?P<name>\S.*?)\s{2,}(?P<locale>[A-Za-z]{2}(?:[-_][A-Za-z0-9]+)*)\s")

_SAPI_LIST_VOICES = (
    "$ErrorActionPreference='Stop';"
    "Add-Type -AssemblyName System.Speech;"
    "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "try{$s.GetInstalledVoices()|"
    "Where-Object{$_.Enabled}|"
    "ForEach-Object{$_.VoiceInfo.Name+'|'+$_.VoiceInfo.Culture.Name}}"
    "finally{$s.Dispose()}"
)


async def list_system_voices() -> list[dict[str, str]]:
    """Enumerate the built-in engine's voices as ``{id, name, language}`` dicts.

    Empty when the host has no built-in engine. A probe that FAILS raises
    :class:`SystemVoiceProbeError` instead of returning empty: the caller cannot
    tell those apart from the value alone, and an empty list is rendered as a
    picker offering only the OS default, which reads as "this host has one
    voice" rather than as a failure the user can retry.

    The engine's default voice is deliberately not synthesized into an entry: it
    speaks whatever language the OS is set to, which is the right answer often
    enough that naming a specific voice must stay optional.

    A voice's ``id`` is what the synthesis path passes back to the engine, so it
    is the engine's own selector — a name for ``say`` and SAPI, a language code
    for ``espeak-ng``, which has one voice per language.
    """
    resolved = await resolve_system_tts_async()
    if not resolved:
        return []
    engine, bin_path = resolved
    if engine == SYSTEM_ENGINE_SAY:
        cmd = [bin_path, "-v", "?"]
    elif engine == SYSTEM_ENGINE_ESPEAK:
        cmd = [bin_path, "--voices"]
    else:
        cmd = [
            bin_path,
            "-NoProfile",
            "-NonInteractive",
            "-NoLogo",
            "-EncodedCommand",
            base64.b64encode(_SAPI_LIST_VOICES.encode("utf-16-le")).decode("ascii"),
        ]
    try:
        # The cgroup wrap ensures the parent slice's limits, which touches the
        # cgroup filesystem, so it cannot run inline on the event loop. The
        # probe's argv is fixed, but its environment is still the gateway's, and
        # a voice listing has no use for the gateway's credentials.
        scoped, probe_env = await asyncio.to_thread(
            lambda: (cgroup_scope_argv(cmd), scrub_env()),
        )
        proc = await create_subprocess_limited(
            *scoped,
            env=probe_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except Exception as exc:
        logger.exception("system voice enumeration failed to spawn")
        raise SystemVoiceProbeError("voice enumeration failed to spawn") from exc
    try:
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=15)
    except BaseException as exc:
        timed_out = isinstance(exc, asyncio.TimeoutError)
        await _kill_and_reap(proc, "system voice probe", timed_out=timed_out)
        if not timed_out:
            raise
        logger.error("system voice enumeration timed out")
        raise SystemVoiceProbeError("voice enumeration timed out") from exc
    if proc.returncode != 0:
        logger.warning("system voice enumeration failed (rc=%s)", proc.returncode)
        raise SystemVoiceProbeError(f"voice enumeration exited {proc.returncode}")
    return _parse_system_voices(engine, stdout.decode(errors="replace"))


def _parse_system_voices(engine: str, out: str) -> list[dict[str, str]]:
    """Turn one engine's voice listing into sorted ``{id, name, language}`` rows."""
    voices: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in out.splitlines():
        line = line.rstrip()
        if not line.strip():
            continue
        if engine == SYSTEM_ENGINE_SAY:
            match = _SAY_VOICE_RE.match(line)
            if not match:
                continue
            name = match.group("name").strip()
            entry = {"id": name, "name": name, "language": match.group("locale").replace("_", "-")}
        elif engine == SYSTEM_ENGINE_ESPEAK:
            fields = line.split()
            # The listing starts with a header row whose first column is the
            # literal "Pty"; every voice row starts with a numeric priority.
            if len(fields) < 4 or not fields[0].isdigit():
                continue
            entry = {"id": fields[1], "name": fields[3], "language": fields[1]}
        else:
            name, _, culture = line.partition("|")
            name = name.strip()
            if not name:
                continue
            entry = {"id": name, "name": name, "language": culture.strip()}
        if entry["id"] in seen:
            continue
        seen.add(entry["id"])
        voices.append(entry)
    voices.sort(key=lambda v: (v["language"], v["name"]))
    return voices


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

    path = await asyncio.to_thread(_new_wav_path)
    succeeded = False
    try:
        cmd: list[str] = [bin_path, "-m", model, "-f", path]
        if cfg:
            cmd += ["-c", cfg]
        if length_scale != 1.0:
            cmd += ["--length-scale", str(length_scale)]
        succeeded = await _run_tts_subprocess(
            cmd,
            path,
            label="piper",
            display="Piper TTS",
            stdin_text=text,
        )
        return path if succeeded else None
    finally:
        # Invariant, not per-exit cleanup: EVERY unsuccessful exit — including
        # CancelledError, which ``except Exception`` does not catch — must
        # discard the owned temp file, or a new exit path re-opens the leak.
        if not succeeded:
            try:
                os.unlink(path)
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
    # System-specific:
    system_voice: str = "",
) -> str | None:
    """Generate audio from *text* using the configured *provider*.

    Returns the path to a temp audio file (``.mp3`` for Polly, ``.wav`` for
    Piper and the built-in system engine), or None on failure. Caller is
    responsible for deleting the file.

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
    if provider == PROVIDER_SYSTEM:
        plain = strip_markdown(text).strip()
        if not plain:
            return None
        return await _synthesize_system(plain, voice=system_voice, rate=rate)
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
    # Polly is a PAID AWS service and this is the request that spends money, so
    # it does not happen without a recorded operator consent for this exact
    # profile+region. The check is local (no AWS call of its own) because this
    # path runs unattended — a Slack thread reply, an auto-reply to a voice
    # memo, a scheduled job — so there is nobody here to prompt. Refusing
    # returns None, which is this function's established "no audio" contract:
    # callers already fall back to a text-only reply.
    if not await aws_consent.refuse_and_log(
        aws_consent.SERVICE_POLLY, profile=aws_profile, region=region
    ):
        return None
    # Polly is OPTIONAL and driven via the ``aws`` CLI (no boto3 dependency).
    # On a vanilla machine without the CLI installed, degrade gracefully here
    # instead of raising FileNotFoundError from create_subprocess_exec. Resolved
    # absolutely (shared deploy-engine resolver) so a GUI-launched gateway's
    # minimal PATH does not silently skip TTS (#4770); resolution probes the
    # filesystem, so it runs in a thread rather than on the event loop.
    aws_bin = await asyncio.to_thread(resolve_polly_cli)
    if aws_bin is None:
        logger.info("voice_reply: Polly unavailable (aws CLI not resolvable); skipping TTS")
        return None
    if not aws_profile:
        # The reporter's core case: with no profile the argv below carries no
        # ``--profile``, so the CLI's own chain decides which account is
        # billed. The consent record above pinned that choice, but say so in
        # the log too, because "no profile" reads as "no account" and is not.
        logger.info(
            "voice_reply: Polly is using the %s",
            aws_consent.credential_source(aws_profile),
        )
    if engine not in VALID_ENGINES:
        logger.error("Invalid Polly engine %r, falling back to neural", engine)
        engine = DEFAULT_ENGINE
    fd, path = tempfile.mkstemp(suffix=".mp3")
    os.close(fd)
    sandbox_cleanup: str | None = None
    succeeded = False
    try:
        try:
            cmd: list[str] = [aws_bin, "polly", "synthesize-speech"]
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
            cmd, sandbox_cleanup = await wrap_argv_async(cmd, mode="standard", _prepare=wrap_argv)
            cmd = cgroup_scope_argv(cmd)  # cgroup DoS ceiling
            proc = await create_subprocess_limited(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            except BaseException as exc:
                # ``asyncio.wait_for`` cancels ``proc.communicate()`` on
                # timeout — and a caller cancellation (client disconnect)
                # arrives here as CancelledError, as do interpreter-exit
                # signals such as KeyboardInterrupt — but none of them
                # terminate the child: kill it explicitly to avoid a hung
                # ``aws polly`` process consuming resources after we exit.
                # Temp-file discard is owned by the ``finally`` invariant
                # below.
                timed_out = isinstance(exc, asyncio.TimeoutError)
                if timed_out:
                    logger.error("Polly timed out after 30s; killing subprocess")
                try:
                    proc.kill()
                except OSError:
                    pass
                # Reap via communicate(), not wait(): wait_for already
                # cancelled the pipe readers, so a killed child blocked on a
                # full PIPE would never be drained and wait() would hang.
                try:
                    await proc.communicate()
                except Exception:
                    logger.debug("polly wait after kill failed", exc_info=True)
                except BaseException:
                    # A repeat cancellation can land on the reap await. When we
                    # are already propagating (non-timeout path) swallow it so
                    # the ORIGINAL exception is the one that propagates; on the
                    # timeout path it is a genuinely new cancellation, so let
                    # it out.
                    if timed_out:
                        raise
                if not timed_out:
                    raise  # cancellation/interrupt must propagate to the caller
                return None
            if proc.returncode != 0:
                logger.error("Polly failed: %s", stderr.decode())
                return None
            if os.path.getsize(path) < 100:
                logger.error("Polly output too small")
                return None
            succeeded = True
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
            return None
        except Exception:
            logger.exception("Polly synthesis error")
            return None
    finally:
        # Invariant, not per-exit cleanup: EVERY unsuccessful exit — including
        # CancelledError, which ``except Exception`` does not catch — must
        # discard the owned temp file, or a new exit path re-opens the leak.
        if not succeeded:
            try:
                os.unlink(path)
            except OSError:
                pass
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
    """Split speakable text at Latin/CJK sentence and paragraph boundaries."""
    clean = strip_markdown(text)
    if not clean:
        return []
    parts = re.split(r"(?<=[.!?])\s+|(?<=[。！？；])\s*|\n+", clean)
    return [s.strip() for s in parts if s.strip()]


class VoiceSynthesisError(RuntimeError):
    """A provider failure with a stable code for dashboard localization."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """Frame mono signed little-endian 16-bit PCM as an independently playable WAV."""
    if len(pcm) % 2:
        raise ValueError("PCM contains an incomplete sample")
    return (
        struct.pack(
            "<4sI4s4sIHHIIHH4sI",
            b"RIFF",
            len(pcm) + 36,
            b"WAVE",
            b"fmt ",
            16,
            1,
            1,
            sample_rate,
            sample_rate * 2,
            2,
            16,
            b"data",
            len(pcm),
        )
        + pcm
    )


def _piper_stream_settings(binary: str, model: str, config: str) -> tuple[str, str, str, int]:
    """Resolve the CLI and read its declared PCM format off the event loop."""
    resolved = _resolve_piper_binary(binary)
    if not resolved:
        raise VoiceSynthesisError(
            "voice_unavailable",
            "Piper is unavailable; check its binary and model in Voice settings.",
        )
    model, config, sample_rate = _piper_model_settings(model, config)
    return resolved, model, config, sample_rate


def _piper_model_settings(model: str, config: str) -> tuple[str, str, int]:
    model = os.path.abspath(os.path.expanduser(model)) if model else ""
    if not model:
        raise VoiceSynthesisError("voice_unavailable", "Piper voice model is unavailable.")
    config = os.path.abspath(os.path.expanduser(config)) if config else model + ".json"
    try:
        protected = is_sensitive_path(model) or is_sensitive_path(config)
    except PathResolutionStalled:
        protected = True
    if protected:
        raise VoiceSynthesisError(
            "voice_model_path_forbidden", "Piper model paths are protected or cannot be verified."
        )
    if not os.path.isfile(model):
        raise VoiceSynthesisError("voice_unavailable", "Piper voice model is unavailable.")
    try:
        with open(config, encoding="utf-8") as handle:
            data = handle.read(_PIPER_MAX_CONFIG_BYTES + 1)
        if len(data) > _PIPER_MAX_CONFIG_BYTES:
            raise ValueError("model configuration is too large")
        sample_rate = json.loads(data)["audio"]["sample_rate"]
        if type(sample_rate) is not int or not 8000 <= sample_rate <= 192000:
            raise ValueError("invalid sample rate")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise VoiceSynthesisError(
            "voice_model_config_invalid",
            "Piper model configuration has no valid audio sample rate.",
        ) from exc
    return model, config, sample_rate


def _piper_phrases(text: str) -> list[str]:
    """Bound each inference without dropping an unpunctuated or CJK tail."""
    phrases: list[str] = []
    for sentence in split_sentences(text):
        while len(sentence) > _PIPER_MAX_PHRASE_CHARS:
            prefix = sentence[:_PIPER_MAX_PHRASE_CHARS]
            boundaries = list(re.finditer(r"[,，、;；:]|\s+", prefix))
            cut = boundaries[-1].end() if boundaries else len(prefix)
            phrases.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if sentence:
            phrases.append(sentence)
    return phrases


async def streaming_piper_reply(
    text: str,
    *,
    piper_binary: str = "",
    piper_model: str = "",
    piper_model_config: str = "",
    length_scale: float = DEFAULT_LENGTH_SCALE,
    runtime: PiperRuntime | None = None,
    request_id: str = "",
):
    """Yield (index, sample_rate, PCM) from a sandboxed Piper provider.

    A dashboard runtime reuses a compatible Python API worker unless a custom
    CLI was explicitly selected. A failed resident attempt can fall back only
    before any audio was delivered. Both attempts share one synthesis deadline.
    """
    stream = _stream_piper_attempts(
        text,
        piper_binary=piper_binary,
        piper_model=piper_model,
        piper_model_config=piper_model_config,
        length_scale=length_scale,
        runtime=runtime,
        request_id=request_id,
    )
    try:
        async with asyncio.timeout(_PIPER_STREAM_TIMEOUT_SECONDS):
            async with contextlib.aclosing(stream):
                async for frame in stream:
                    yield frame
    except TimeoutError as exc:
        raise VoiceSynthesisError("voice_timeout", "Piper synthesis timed out.") from exc


async def _stream_piper_attempts(
    text: str,
    *,
    piper_binary: str,
    piper_model: str,
    piper_model_config: str,
    length_scale: float,
    runtime: PiperRuntime | None,
    request_id: str,
):
    """Try the resident API, then its existing CLI compatibility path if safe."""
    text, _ = redact_credentials(text)
    text, _ = redact_exfiltration_urls(text)
    phrases = _piper_phrases(text)
    if not phrases:
        return
    if runtime is not None and not piper_binary:
        if await asyncio.to_thread(piper_runtime.python_piper_available):
            model, config, sample_rate = await asyncio.to_thread(
                _piper_model_settings, piper_model, piper_model_config
            )
            stream = runtime.stream(
                phrases,
                model=model,
                config=config,
                sample_rate=sample_rate,
                length_scale=validate_length_scale(length_scale),
                request_id=request_id,
            )
            emitted = False
            try:
                async with contextlib.aclosing(stream):
                    async for frame in stream:
                        emitted = True
                        yield frame
            except (VoiceSynthesisError, OSError) as exc:
                # A successful gateway import does not establish that the
                # isolated -E/-P child can import or run the same package. Its
                # generator has retired/reaped the failed worker before this
                # branch can start another process. Never replay partial audio,
                # retry policy refusal, or turn a cancellation into new work.
                if emitted or isinstance(exc, PermissionError):
                    raise
                if isinstance(exc, VoiceSynthesisError) and exc.code not in (
                    "voice_invalid_audio",
                    "voice_synthesis_failed",
                ):
                    raise
                fallback_binary = await asyncio.to_thread(_resolve_piper_binary, "")
                if not fallback_binary:
                    raise
                logger.warning("Resident Piper failed before audio; retrying with the CLI")
                piper_binary = fallback_binary
            else:
                return
    binary, model, config, sample_rate = await asyncio.to_thread(
        _piper_stream_settings, piper_binary, piper_model, piper_model_config
    )
    cmd = [binary, "-m", model, "-c", config, "--output-raw"]
    scale = validate_length_scale(length_scale)
    if scale != DEFAULT_LENGTH_SCALE:
        cmd += ["--length-scale", str(scale)]
    cleanup: str | None = None
    proc: asyncio.subprocess.Process | None = None
    pipe_tasks: list[asyncio.Task] = []
    try:
        cmd, cleanup = await wrap_argv_async(cmd, mode="standard", _prepare=wrap_argv)
        cmd = await asyncio.to_thread(cgroup_scope_argv, cmd)
        proc = await create_subprocess_limited(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=scrub_env({**os.environ, "PYTHONIOENCODING": "utf-8"}),
        )
        assert proc is not None
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

        async def feed() -> None:
            assert proc is not None and proc.stdin is not None
            try:
                for phrase in phrases:
                    proc.stdin.write((phrase + "\n").encode("utf-8"))
                    await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                # The exit status and stderr reader own the provider failure.
                pass
            finally:
                proc.stdin.close()

        async def drain_errors() -> bytes:
            assert proc is not None and proc.stderr is not None
            tail = b""
            while data := await proc.stderr.read(_PIPER_MAX_STDERR_BYTES):
                tail = (tail + data)[-_PIPER_MAX_STDERR_BYTES:]
            return tail

        writer = asyncio.create_task(feed())
        errors = asyncio.create_task(drain_errors())
        pipe_tasks.extend((writer, errors))
        chunk_bytes = int(sample_rate * _PIPER_CHUNK_SECONDS) * 2
        pending = b""
        total = index = 0
        while data := await proc.stdout.read(chunk_bytes):
            total += len(data)
            if total > _PIPER_MAX_AUDIO_BYTES:
                raise VoiceSynthesisError(
                    "voice_audio_limit", "Synthesized audio exceeds the limit."
                )
            pending += data
            # Pipes may split an Int16 sample at any byte. Preserve that
            # byte for the next read instead of corrupting every sample.
            aligned = len(pending) - len(pending) % 2
            if aligned:
                yield index, sample_rate, pending[:aligned]
                index += 1
                pending = pending[aligned:]
        await writer
        await proc.wait()
        stderr = await errors
        if proc.returncode != 0:
            diagnostic = redact_log_via_context(stderr.decode("utf-8", errors="replace"))
            logger.error("Piper stream failed (rc=%s): %s", proc.returncode, diagnostic)
            raise VoiceSynthesisError(
                "voice_synthesis_failed", "Piper could not synthesize speech."
            )
        if pending or not total:
            raise VoiceSynthesisError(
                "voice_invalid_audio", "Piper returned incomplete or empty audio."
            )
    except SandboxUnavailableError as exc:
        logger.error("voice_reply: Piper TTS refused by the sandbox (%s): %s", exc.kind, exc)
        raise VoiceSynthesisError("voice_sandbox_unavailable", str(exc)) from exc
    finally:

        async def reap() -> None:
            try:
                if proc is not None:
                    if proc.returncode is None:
                        with contextlib.suppress(OSError):
                            proc.kill()
                    for task in pipe_tasks:
                        task.cancel()
                    await asyncio.gather(*pipe_tasks, return_exceptions=True)
                    # Cancel the stderr reader before communicate takes over.
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(
                            proc.communicate(), timeout=_PIPER_REAP_TIMEOUT_SECONDS
                        )
            finally:
                if cleanup:
                    with contextlib.suppress(OSError):
                        await asyncio.to_thread(os.unlink, cleanup)

        # Keep cleanup owned by the request until it settles. A repeated stop
        # or shutdown cancellation must not abandon its pipes or sandbox file.
        reaping = asyncio.create_task(reap())
        cancelled = False
        while not reaping.done():
            try:
                await asyncio.shield(reaping)
            except asyncio.CancelledError:
                cancelled = True
        reaping.result()
        if cancelled:
            raise asyncio.CancelledError


async def stitch_mp3s(paths: list[str], output: str | None = None) -> str | None:
    """Concatenate MP3 files into a single file using ffmpeg.

    Returns ``None`` on failure (spawn error, timeout, non-zero exit, or an
    empty output file). An output this call allocated itself (no ``output``
    argument) is removed on any unsuccessful exit; a caller-supplied
    ``output`` path is left untouched.

    Windows: ffmpeg is not guaranteed on PATH, so the dashboard-streaming stitch
    path fails there; making it optional with a warning is a known gap. The
    Slack thread-upload path is unaffected.
    """
    if not paths:
        return None
    if len(paths) == 1:
        if output:
            shutil.copy2(paths[0], output)
            return output
        return paths[0]
    owned = output is None
    if output is None:
        fd, output = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)

    def _discard_owned_output() -> None:
        # On failure this function returns None, so no caller ever receives an
        # internally allocated (mkstemp) path — remove it or it leaks with no
        # surviving owner. A caller-supplied ``output`` is never ours to delete.
        if owned:
            try:
                os.unlink(output)
            except OSError:
                pass

    concat = "|".join(paths)
    succeeded = False
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
        try:
            await asyncio.wait_for(proc.communicate(), timeout=30)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            # asyncio.wait_for cancels communicate() but does NOT terminate
            # the child — kill it explicitly (mirroring the piper/polly
            # paths) so it stops consuming CPU and, on Windows, releases the
            # output handle that would otherwise make the unlink in the
            # ``finally`` below fail and re-leak the file.
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.communicate()
            except Exception:
                logger.debug("ffmpeg wait after kill failed", exc_info=True)
            raise
        if (
            proc.returncode != 0
            or not os.path.exists(output)
            # The mkstemp allocation always exists, so "ffmpeg produced no
            # output" manifests as an empty file, not an absent one.
            or os.path.getsize(output) == 0
        ):
            return None
        succeeded = True
        return output
    except Exception:
        logger.exception("ffmpeg stitch failed")
        return None
    finally:
        # Invariant, not per-exit cleanup: EVERY unsuccessful exit — including
        # CancelledError, which ``except Exception`` does not catch — must
        # discard the owned output, or a new exit path re-opens the leak.
        if not succeeded:
            _discard_owned_output()


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
        logger.warning(
            "stream_voice_chunks: redacted %d credential pattern(s) before TTS", len(cred_warns)
        )
    if url_warns:
        logger.warning(
            "stream_voice_chunks: redacted %d suspicious URL(s) before TTS", len(url_warns)
        )

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


#: Keys read out of the ``voice_reply`` config section, with their defaults. One
#: table so a channel cannot end up honouring a different set from another
#: channel; :func:`synthesis_settings` maps it onto the kwargs
#: :func:`synthesize_and_deliver` takes.
_SYNTHESIS_KEYS: "tuple[tuple[str, str, Any], ...]" = (
    ("voice_id", "voice_id", DEFAULT_VOICE),
    ("engine", "engine", DEFAULT_ENGINE),
    ("rate", "rate", DEFAULT_RATE),
    ("pitch", "pitch", DEFAULT_PITCH),
    ("aws_profile", "aws_profile", ""),
    ("region", "region", ""),
    ("piper_binary", "piper_binary", ""),
    ("piper_model", "piper_model", ""),
    ("piper_model_config", "piper_model_config", ""),
    ("system_voice", "system_voice", ""),
)


def resolve_configured_provider(section: dict | None) -> str:
    """Resolve the provider from a raw ``voice_reply`` config section.

    One implementation for every reader, so the rules below cannot drift between
    the Slack loader, the Telegram settings path, and the dashboard.

    Validated here rather than at synthesis time: a typo (``"ploly"``) would
    otherwise pass through and only fail after the user has already spoken and is
    waiting for a spoken answer. Both the absent-key default and the
    invalid-value fallback stay LOCAL — reaching a paid AWS service because a key
    was missing is not a decision an operator made, and a wrong local provider
    costs nothing and degrades to a "TTS isn't configured" notice.

    An unnamed provider on a config that already carries ``piper_model`` keeps
    Piper. That config is a working Piper install from before the built-in engine
    became the default, and resolving it to the default would silently downgrade
    it to a lower-quality voice on upgrade — a change the operator never asked
    for and would have no reason to look for.
    """
    section = section or {}
    provider = section.get("provider")
    if not provider:
        # The migration gate asks "did an operator configure Piper?", so it has to
        # read a real string. str() would answer yes to a malformed value -- "{}"
        # is truthy -- and hand an upgrader the unavailable provider, producing the
        # exact silence this default exists to prevent.
        if validated_config_string(section.get("piper_model")):
            return PROVIDER_PIPER
        return DEFAULT_PROVIDER
    # ``in VALID_PROVIDERS`` raises TypeError on an unhashable JSON value
    # (config.json can hold a list or dict where a string belongs), so the type
    # is checked before the membership test.
    if not isinstance(provider, str) or provider not in VALID_PROVIDERS:
        logger.warning(
            "voice_reply.provider %r not in %s, defaulting to %r",
            provider,
            sorted(VALID_PROVIDERS),
            DEFAULT_PROVIDER,
        )
        return DEFAULT_PROVIDER
    return provider


def synthesis_settings(raw: dict | None) -> dict:
    """The ``voice_reply`` section as :func:`synthesize_and_deliver` kwargs.

    *raw* is the section itself (``cfg.raw.get("voice_reply")``), so a caller with
    a validated config and a caller reading ``config.json`` directly resolve the
    same way.
    """
    section = raw or {}
    out: dict = {"provider": resolve_configured_provider(section)}
    for key, kwarg, default in _SYNTHESIS_KEYS:
        out[kwarg] = section.get(key, default)
    # Coerce to finite/positive — config.json accepts inf/NaN, which would reach
    # synthesis and be re-serialized as non-RFC JSON, breaking the config GET.
    out["length_scale"] = validate_length_scale(section.get("piper_length_scale", 1.0))
    return out


async def synthesize_and_deliver(
    deliver: "Callable[[str], Awaitable[bool]]",
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
    # System:
    system_voice: str = "",
) -> bool:
    """Synthesize *response_text* and hand the audio file to *deliver*.

    The channel-neutral half of the pipeline. ``synthesize_speech`` was already
    surface-agnostic; the only Slack-shaped step was the upload, so it becomes a
    callback taking the temp file's path and returning whether it landed.

    The temp file is unlinked in a ``finally`` regardless of what *deliver* does,
    including raising — a synthesizer that keeps its output on a delivery failure
    leaks a decoded copy of the answer into the temp dir, which is the one place a
    restricted session's text must not persist.

    Returns False when synthesis produced nothing, so a caller can post its
    unavailable notice rather than silently sending only text.
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
        system_voice=system_voice,
    )
    if not audio_path:
        return False
    try:
        return await deliver(audio_path)
    finally:
        try:
            os.unlink(audio_path)
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
    # System:
    system_voice: str = "",
) -> bool:
    """Full pipeline: text → provider synthesis → Slack upload.

    Provider selection is controlled by the ``provider`` argument; arguments
    belonging to the other providers are ignored.

    Kept as its own entry point rather than folded into
    :func:`synthesize_and_deliver`: Slack's callers pass a channel and a thread
    rather than a delivery callback, and preserving the signature keeps every one
    of them — and their tests — unchanged.
    """
    return await synthesize_and_deliver(
        lambda path: upload_voice_to_slack(slack_client, channel, thread_ts, path),
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
        system_voice=system_voice,
    )
