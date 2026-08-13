"""Apple on-device speech-to-text (``apple`` STT provider) — macOS only.

Apple's ``SpeechAnalyzer`` / ``SpeechTranscriber`` (macOS 26+) is a **Swift-only**
framework, so the gateway cannot call it in-process. This module owns the
out-of-process seam: it compiles the bundled ``AppleTranscribe.swift`` helper on
demand, caches the binary under the data home, and runs it as a subprocess that
returns one line of JSON.

Why compile on demand rather than ship a binary: a prebuilt Mach-O would have to be
signed and notarized to survive Gatekeeper on a downloaded DMG, and would need one
slice per macOS deployment target. Compiling once against the *host's* SDK sidesteps
both and keeps the wheel architecture-neutral. The cost is a hard dependency on the
Xcode Command Line Tools, which is why :func:`availability` reports the missing
toolchain as its own distinguishable state instead of a generic "unavailable".

**Model tier is not selectable.** Apple exposes no API to request the
"Advanced Dictation" (AFM 3 Core Advanced) model, nor to query which tier served a
request — the whole ``Speech.SpeechModels`` enum is a single ``endRetention()``
function. The OS decides based on hardware. Do not add a config knob implying
otherwise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from kiro_crew import platform_compat, sandbox
from kiro_crew.config.loader import config_dir

logger = logging.getLogger(__name__)

#: Minimum macOS major version carrying the SpeechAnalyzer API.
MIN_MACOS_MAJOR = 26

#: Name of the compiled helper inside the cache dir.
_HELPER_NAME = "AppleTranscribe"

#: Container formats ``AVAudioFile`` opens directly. Anything else (notably the
#: dashboard's ``.webm``/Opus recordings) is transcoded with ffmpeg first — the
#: framework cannot read WebM, so without this the provider would reject exactly
#: the input the voice-memo path produces.
_NATIVE_AUDIO_SUFFIXES = frozenset(
    {".wav", ".aiff", ".aif", ".aifc", ".caf", ".m4a", ".mp3", ".mp4", ".flac", ".aac"}
)

#: Fixed toolchain locations, probed with a stat so the loop-safe resolver never
#: spawns. Covers Command Line Tools and a full Xcode install.
#: Fixed toolchain locations, probed with a stat so the loop-safe resolver never
#: spawns. These are CONCRETE compilers.
#:
#: `/usr/bin/swiftc` is deliberately NOT here. It shares an inode with
#: `/usr/bin/clang` (78 hard links) because it is the `xcrun` shim, not a
#: compiler: it reads `DEVELOPER_DIR` and delegates. Trusting it would verify the
#: shim while executing `$DEVELOPER_DIR/usr/bin/swiftc` unchecked — the same
#: escalation as the PATH lookup this replaced, just through an env var, and it is
#: the path that would be taken in practice. The reason a CLT binary fails on its
#: own ("unable to load standard library for target ...") is a missing SDK, which
#: `-sdk` supplies; see `_sdk_path`.
_SWIFTC_FIXED_PATHS = (
    "/Library/Developer/CommandLineTools/usr/bin/swiftc",
    "/Applications/Xcode.app/Contents/Developer/Toolchains/"
    "XcodeDefault.xctoolchain/usr/bin/swiftc",
)

#: SDK locations to stat when `xcrun` cannot be consulted, in the same fixed
#: layout as the toolchains above.
_SDK_FIXED_PATHS = (
    "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk",
    "/Applications/Xcode.app/Contents/Developer/Platforms/"
    "MacOSX.platform/Developer/SDKs/MacOSX.sdk",
)

#: Env vars that redirect the Apple toolchain to another location. Stripped from
#: every child we spawn: with these present, verifying the binary we invoke proves
#: nothing about the compiler that actually runs.
_TOOLCHAIN_ENV_OVERRIDES = ("DEVELOPER_DIR", "SDKROOT", "TOOLCHAINS", "SWIFT_EXEC")


def _build_env() -> dict[str, str]:
    """A scrubbed environment for every toolchain child process.

    `subprocess.run` without `env=` inherits `os.environ`, so an agent that can
    write a shell rc file exports `DEVELOPER_DIR` and the next shell-launched
    gateway compiles — then executes — with a user-writable toolchain. Dropping
    the overrides makes `xcrun` fall back to the `xcode-select` system link
    (`/var/db/xcode_select_link`, root:wheel in a root:wheel dir), which the agent
    cannot repoint without sudo. PATH is pinned for the same reason.
    """
    env = {k: v for k, v in os.environ.items() if k not in _TOOLCHAIN_ENV_OVERRIDES}
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    return env


def _sdk_path() -> str | None:
    """Resolve the macOS SDK. **Blocks — build path only.**

    A concrete `swiftc` invoked directly cannot find the standard library without
    this. The result is trust-checked like the compiler: a hostile SDK supplies the
    headers the helper is built against.
    """
    try:
        out = subprocess.run(
            ["/usr/bin/xcrun", "--show-sdk-path"],
            capture_output=True,
            text=True,
            timeout=10,
            env=_build_env(),
        )
        path = out.stdout.strip()
        if out.returncode == 0 and path and os.path.isdir(path) and _is_trusted_toolchain(path):
            return path
    except (OSError, subprocess.SubprocessError):
        pass
    for candidate in _SDK_FIXED_PATHS:
        if os.path.isdir(candidate) and _is_trusted_toolchain(candidate):
            return candidate
    return None


#: Prefixes a compiler may live under. Anything outside these is refused even if
#: ``xcrun`` names it, because the gateway executes what the compiler produces.
_TRUSTED_TOOLCHAIN_PREFIXES = (
    "/Applications/Xcode.app/",
    "/Applications/Xcode-beta.app/",
    "/Library/Developer/",
    "/usr/bin/",
    "/usr/libexec/",
)

#: Wall-clock ceiling for one helper run. Generous relative to observed runtimes
#: (sub-second per 10s of audio) because a first-ever run may block on asset install.
DEFAULT_TIMEOUT_SECS = 300


@dataclass(frozen=True)
class Availability:
    """Why the provider can or cannot run, in a form the UI can act on."""

    ok: bool
    reason: str = ""
    #: True when the only thing missing is the Swift toolchain — actionable by the
    #: user (`xcode-select --install`), unlike an unsupported OS.
    needs_toolchain: bool = False


def _macos_major() -> int:
    """Return the macOS major version, or 0 when not macOS / unparseable."""
    if platform.system() != "Darwin":
        return 0
    try:
        return int(platform.mac_ver()[0].split(".")[0])
    except (ValueError, IndexError):
        return 0


def _is_trusted_toolchain(path: str) -> bool:
    """True when *path* resolves inside a root-owned system toolchain.

    `_build_helper` COMPILES with whatever this returns and the gateway then
    executes the product, so a compiler the agent can write is exactly as bad as
    an agent-writable output directory: drop a `swiftc` shim into any PATH entry
    the gateway inherits (`~/.local/bin` is on PATH for a shell-launched gateway,
    and mlx-whisper already installs there) and the next build runs it.

    The test is OWNERSHIP, walked from the binary up to the trusted prefix that
    matched — not "no ancestor is writable all the way to `/`". That distinction
    is load-bearing on macOS: `/Applications` is `775 root:admin`, so an
    admin-group write is possible there and a walk to `/` would refuse a genuine
    Xcode. What actually separates a real toolchain from a planted one is that the
    real bundle is `root:wheel` while anything the agent creates is owned by the
    invoking user — so requiring root ownership with no group/other write at every
    level up to the prefix accepts a real install and refuses a fake `Xcode.app`,
    without depending on `/Applications` itself being locked down.
    """
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    prefix = next((p for p in _TRUSTED_TOOLCHAIN_PREFIXES if real.startswith(p)), None)
    if prefix is None:
        return False
    stop = prefix.rstrip("/")
    probe = real
    while True:
        try:
            st = os.lstat(probe)
        except OSError:
            return False
        if st.st_uid != 0 or (st.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            return False
        if probe == stop:
            return True
        parent = os.path.dirname(probe)
        if parent == probe:
            return True
        probe = parent


def _swiftc_fast() -> str | None:
    """Locate ``swiftc`` WITHOUT spawning anything. Safe on the event loop.

    :func:`availability` is reached synchronously from the asyncio loop (the
    ``/api/config/stt`` GET, ``api_stt_transcribe``, the Slack voice path), so it
    must not run a subprocess. The two fixed toolchain locations answer the
    question for every real install; the ``xcrun`` fallback lives in
    :func:`_swiftc`, which is only reached from the already-offloaded build path.

    A bare ``shutil.which`` is deliberately NOT used: PATH is attacker-influenced
    (see :func:`_is_trusted_toolchain`), and on macOS ``swiftc`` only ever ships
    inside Command Line Tools or Xcode — both fixed — so consulting PATH buys no
    real install and costs the trust boundary.
    """
    for candidate in _SWIFTC_FIXED_PATHS:
        if os.path.isfile(candidate) and _is_trusted_toolchain(candidate):
            return candidate
    return None


def _swiftc() -> str | None:
    """Locate ``swiftc``, falling back to ``xcrun``. **Blocks — never call on the loop.**

    Used by :func:`_build_helper`, which callers reach through
    ``asyncio.to_thread``. ``xcrun`` is consulted last because it is the only
    branch that spawns a process, and it is what finds a toolchain in a
    non-standard developer dir.
    """
    fast = _swiftc_fast()
    if fast:
        return fast
    try:
        out = subprocess.run(
            ["/usr/bin/xcrun", "--find", "swiftc"],
            capture_output=True,
            text=True,
            timeout=10,
            env=_build_env(),
        )
        path = out.stdout.strip()
        if out.returncode == 0 and path and os.path.isfile(path):
            # xcrun honors DEVELOPER_DIR / xcode-select, both of which can name a
            # user-writable toolchain — so its answer is subject to the same trust
            # check as the fixed paths. Without this the PATH hole simply moves.
            if _is_trusted_toolchain(path):
                return path
            logger.warning("Ignoring untrusted swiftc from xcrun: %s", path)
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def availability() -> Availability:
    """Report whether the provider can run on this host, and why not if it cannot.

    **Cheap and loop-safe by contract**: platform/version reads plus a PATH lookup
    and two stats. It deliberately does NOT confirm the helper is already compiled
    — that would mean building, and this is called from the event loop. The build
    happens inside :func:`transcribe` / :meth:`StreamingSession.start`, both of
    which offload it.
    """
    if platform.system() != "Darwin":
        return Availability(False, "Apple on-device speech is macOS only")
    major = _macos_major()
    if major and major < MIN_MACOS_MAJOR:
        return Availability(
            False,
            f"needs macOS {MIN_MACOS_MAJOR} or later for the SpeechAnalyzer API "
            f"(this host reports {major})",
        )
    if _swiftc_fast() is None:
        return Availability(
            False,
            "the Xcode Command Line Tools are required to build the speech helper "
            "(run: xcode-select --install)",
            needs_toolchain=True,
        )
    return Availability(True)


def _source_path(filename: str = "AppleTranscribe.swift") -> Path:
    return Path(__file__).with_name(filename)


def _cache_dir() -> Path:
    """Where the compiled helpers live.

    Under ``run/`` deliberately, NOT ``cache/``. The gateway *executes* what is
    written here, so an agent-writable location would turn "the agent can write a
    file" into "the agent can run code as the gateway" — it could replace the
    compiled helper and have the next transcription execute it. ``run`` is on
    ``security._CREW_SECRET_LEAVES``, so ``is_sensitive_path`` refuses agent reads
    and writes for the whole subtree, the same fence that protects
    ``.local_secret`` and the governance trust root.
    """
    return config_dir() / "run" / "apple-speech"


def _source_fingerprint(filename: str = "AppleTranscribe.swift") -> str:
    """Signature that changes when the helper source or toolchain changes.

    Keyed on source mtime+size and the swiftc path so an upgraded package or a
    switch from Command Line Tools to full Xcode triggers a rebuild instead of
    silently reusing a stale binary.
    """
    src = _source_path(filename)
    try:
        st = src.stat()
        stamp = f"{st.st_mtime_ns}-{st.st_size}"
    except OSError:
        stamp = "missing"
    return f"{stamp}-{_swiftc() or 'no-swiftc'}"


#: What to tell the user when the host has no OS sandbox backend. The helper is
#: NOT run unsandboxed as a fallback — that would silently undo the isolation and
#: hand back the very unsandboxed exec the sandbox was added to remove. The
#: message carries the remedy, matching `cron_script.py`'s handling.
_NO_SANDBOX_HINT = "the Apple speech helper must run in an OS sandbox and this host has none: "


def _sandboxed(argv: list[str]) -> tuple[list[str], dict[str, str], str | None]:
    """Wrap a helper invocation in the OS sandbox with a scrubbed environment.

    The helper is a binary Kiro Crew compiles on demand from Swift that ships inside
    the package, and the gateway executes it. Even though the source sits at the
    same trust level as the surrounding Python — an agent able to write there
    already has gateway-privileged execution through any ``.py`` file — routing the
    execution through the repo's own chokepoint is strictly better than arguing the
    boundary: ``mode="strict"`` denies the credential dirs outright, and
    ``scrub_env`` strips the rest.

    ``strict`` was verified to leave the helper fully functional: batch transcription
    returns the same text, and the streaming path still produces partials at the same
    cadence plus a final — the Speech framework's on-device work needs none of what
    the profile denies.

    **Blocks — never call on the event loop.** The backend probe spawns
    ``sandbox-exec`` on macOS (measured: 17.7ms cold, 0.3ms once cached), so every
    caller offloads it with ``asyncio.to_thread``. This is the same
    ``no-blocking-call-on-event-loop`` invariant the resolver split already
    protects; keeping the probe out of ``availability()`` was necessary but not
    sufficient, because these three call sites are themselves async.

    Raises:
        SandboxUnavailableError: propagated from the chokepoint when the host has no
            sandbox backend. Callers MUST catch this and surface the message, which
            carries the remedy. Falling back to an unsandboxed exec would silently
            undo the isolation this function exists to add, so the failure is
            deliberate — see the callers.
    """
    return sandbox.sandboxed_spawn_argv(argv, mode="strict", env=_build_env())


def _mkstemp_path(suffix: str) -> str:
    """Create an empty temp file and return its path, descriptor already closed.

    Kept as a helper so callers on the event loop can offload both syscalls with a
    single ``asyncio.to_thread`` hop instead of leaving either one inline.
    """
    fd, path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    return path


def _build_helper(name: str, source: str, *, build: bool = True) -> str | None:
    """Compile *source* to a cached binary named *name*; return its path or None.

    Shared by the batch and streaming helpers. Fingerprint-guarded, and the write
    goes through a temp file + ``os.replace`` so a concurrent caller never observes
    a half-written binary.
    """
    src = _source_path(source)
    if not src.is_file():
        logger.warning("apple_speech: helper source missing at %s", src)
        return None

    cache = _cache_dir()
    # 0o700, matching the other two `run/` writers (`sandbox.py::_ensure_run_dir`).
    # BEFORE the cache-hit early return on purpose: placed after it, the repair
    # would never run in the steady state — the directory holding an executable
    # the gateway runs would stay at whatever the umask gave it. `exist_ok` does
    # not re-apply the mode to an existing dir, so repair explicitly.
    try:
        cache.mkdir(parents=True, exist_ok=True)
        # Tighten the parent `run/` too: `parents=True` creates it at umask default,
        # and on a host where this builds before `sandbox._ensure_run_dir` ever runs
        # nothing else would narrow it.
        platform_compat.chmod_safe(str(cache.parent), 0o700)
        platform_compat.chmod_safe(str(cache), 0o700)
    except OSError:
        logger.warning("Cannot create %s", cache)
    # `chmod_safe` swallows its own OSError and never raises, so the guard above
    # does NOT cover it — verify instead of assuming, or the build would proceed
    # into a directory that stayed world-traversable.
    try:
        if (os.stat(cache).st_mode & 0o077) != 0:
            logger.warning("%s is not owner-only; helper stays group/other readable", cache)
    except OSError:
        pass
    binary = cache / name
    stamp_file = cache / f"{name}.stamp"
    want = _source_fingerprint(source)

    if binary.is_file() and os.access(binary, os.X_OK):
        try:
            if stamp_file.read_text(encoding="utf-8").strip() == want:
                return str(binary)
        except OSError:
            pass  # unreadable stamp: fall through and rebuild

    if not build:
        return None

    swiftc = _swiftc()
    if swiftc is None:
        return None
    sdk = _sdk_path()
    if sdk is None:
        logger.warning("apple_speech: no trusted macOS SDK found; cannot build %s", name)
        return None

    tmp_out = ""
    try:
        fd, tmp_out = tempfile.mkstemp(dir=str(cache), prefix=".build-")
        os.close(fd)
        proc = subprocess.run(
            # `-sdk` explicitly, and a scrubbed env: the compiler is a concrete
            # trust-checked binary rather than the `xcrun` shim, so nothing supplies
            # the SDK implicitly and nothing may redirect the toolchain.
            [swiftc, "-O", "-parse-as-library", "-sdk", sdk, str(src), "-o", tmp_out],
            capture_output=True,
            text=True,
            timeout=180,
            env=_build_env(),
        )
        if proc.returncode != 0:
            logger.warning(
                "apple_speech: %s build failed (%s): %s",
                name,
                proc.returncode,
                proc.stderr.strip()[-500:],
            )
            return None
        platform_compat.chmod_safe(tmp_out, 0o700)
        os.replace(tmp_out, binary)
        tmp_out = ""
        stamp_file.write_text(want, encoding="utf-8")
        logger.info("apple_speech: built %s at %s", name, binary)
        return str(binary)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("apple_speech: %s build error: %s", name, exc)
        return None
    finally:
        if tmp_out and os.path.exists(tmp_out):
            try:
                os.unlink(tmp_out)
            except OSError:
                pass


def helper_path(*, build: bool = True) -> str | None:
    """Return the compiled batch helper's path, building it once if needed."""
    return _build_helper(_HELPER_NAME, "AppleTranscribe.swift", build=build)


def is_available() -> bool:
    """True when the provider CAN transcribe — without compiling to find out.

    Deliberately not "is the helper already built": ``helper_path()`` builds, and
    every caller of this function (``transcribe.is_available`` via the config GET,
    the transcribe endpoint, the Slack voice path) runs it on the asyncio loop. A
    180s ``swiftc`` invocation there freezes chat turns and the liveness
    heartbeat. The build is deferred to the offloaded paths.
    """
    return availability().ok


async def _to_native_audio(audio_path: str) -> tuple[str, bool]:
    """Return a path ``AVAudioFile`` can open, plus whether it is a temp file.

    Transcodes to 16 kHz mono WAV with ffmpeg when the input container is not one
    the framework reads. Falls back to the original path when ffmpeg is missing, so
    the caller still gets the framework's own error rather than a silent refusal.
    """
    if Path(audio_path).suffix.lower() in _NATIVE_AUDIO_SUFFIXES:
        return audio_path, False

    from kiro_crew.transcribe import _find_ffmpeg, ensure_ffmpeg_in_path

    await asyncio.to_thread(ensure_ffmpeg_in_path)
    ffmpeg = _find_ffmpeg()
    if not ffmpeg:
        logger.warning(
            "apple_speech: %s needs transcoding but ffmpeg was not found",
            Path(audio_path).suffix or "<no suffix>",
        )
        return audio_path, False

    # Both syscalls in ONE thread hop. `os.close` alone is trivial, but
    # `tempfile.mkstemp` is the heavier half — it creates a file — and leaving it
    # on the loop while offloading only the close would be the worse split.
    out = await asyncio.to_thread(_mkstemp_path, ".wav")
    proc = await asyncio.create_subprocess_exec(
        ffmpeg,
        "-y",
        "-i",
        audio_path,
        "-ar",
        "16000",
        "-ac",
        "1",
        out,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        logger.warning(
            "apple_speech: ffmpeg transcode failed: %s", err.decode(errors="replace")[-300:]
        )
        try:
            os.unlink(out)
        except OSError:
            pass
        return audio_path, False
    return out, True


async def transcribe(
    audio_path: str,
    *,
    locale: str = "en-US",
    install: bool = True,
    timeout_secs: int = DEFAULT_TIMEOUT_SECS,
) -> tuple[str | None, dict]:
    """Transcribe *audio_path* on device. Returns ``(text_or_None, metrics)``.

    ``metrics`` always carries what the run reported (``transcribe_secs``,
    ``audio_secs``, resolved ``locale``) or an ``error`` key — callers surface it in
    diagnostics rather than re-deriving timings.
    """
    avail = availability()
    if not avail.ok:
        return None, {"error": avail.reason}

    helper = await asyncio.to_thread(helper_path)
    if not helper:
        return None, {"error": "speech helper could not be built"}

    native_path, is_temp = await _to_native_audio(audio_path)
    argv = [helper, "--locale", locale]
    if install:
        argv.append("--install")
    argv.append(native_path)

    try:
        argv, spawn_env, _sb_cleanup = await asyncio.to_thread(_sandboxed, argv)
    except sandbox.SandboxUnavailableError as exc:
        logger.warning("apple_speech: %s%s", _NO_SANDBOX_HINT, exc)
        return None, {"error": f"{_NO_SANDBOX_HINT}{exc}"}
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            env=spawn_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_secs)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.communicate()
            except (OSError, ProcessLookupError):
                pass
            return None, {"error": f"timed out after {timeout_secs}s"}
    except OSError as exc:
        return None, {"error": f"could not run speech helper: {exc}"}
    finally:
        if is_temp:
            try:
                os.unlink(native_path)
            except OSError:
                pass

    raw = stdout.decode(errors="replace").strip()
    if not raw:
        detail = stderr.decode(errors="replace").strip()[-300:]
        return None, {"error": f"speech helper produced no output: {detail}"}
    try:
        payload = json.loads(raw.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return None, {"error": f"unparseable helper output: {raw[:200]}"}

    if proc.returncode != 0 or "error" in payload:
        return None, payload if "error" in payload else {"error": "speech helper failed"}

    text = str(payload.get("text", "")).strip()
    return (text or None), payload


async def inventory() -> dict:
    """Return ``{"supported": [...], "installed": [...]}`` BCP-47 locale lists."""
    helper = await asyncio.to_thread(helper_path)
    if not helper:
        return {"error": "speech helper unavailable"}
    try:
        try:
            inv_argv, inv_env, _ = await asyncio.to_thread(_sandboxed, [helper, "--inventory"])
        except sandbox.SandboxUnavailableError as exc:
            return {"error": f"{_NO_SANDBOX_HINT}{exc}"}
        proc = await asyncio.create_subprocess_exec(
            *inv_argv,
            env=inv_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
        return dict(json.loads(stdout.decode().strip().splitlines()[-1]))
    except (OSError, asyncio.TimeoutError, json.JSONDecodeError, IndexError) as exc:
        return {"error": str(exc)}


# ─────────────────────────── live streaming ───────────────────────────────────

#: Name of the compiled streaming helper (sibling of the batch one).
_STREAM_HELPER_NAME = "StreamTranscribe"

#: PCM the dashboard's audio worklet produces: 16 kHz mono signed 16-bit LE.
STREAM_SAMPLE_RATE_HZ = 16000

#: Seconds to wait for the helper's ``ready`` line before giving up. Model warm-up
#: was measured at 50-95 ms, so this is a generous ceiling, not a typical wait.
_READY_TIMEOUT_SECS = 20.0


def stream_helper_path(*, build: bool = True) -> str | None:
    """Return the compiled *streaming* helper path, building it once if needed."""
    return _build_helper(_STREAM_HELPER_NAME, "StreamTranscribe.swift", build=build)


class StreamingSession:
    """One live dictation session: a long-lived helper process fed PCM chunks.

    A process per session rather than per utterance: the batch helper pays ~70 ms of
    process start, invisible for a whole file but dominant against a ~210 ms partial
    cadence.

    Lifecycle: :meth:`start` (waits for the helper's ``ready``), then :meth:`feed` for
    each audio chunk while consuming :meth:`events`, then :meth:`finish`. Always call
    :meth:`close` — an orphaned helper holds a speech-recognition session open.
    """

    def __init__(self, *, locale: str = "en-US", sample_rate: int = STREAM_SAMPLE_RATE_HZ):
        self.locale = locale
        self.sample_rate = sample_rate
        self._proc: asyncio.subprocess.Process | None = None
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._pump: asyncio.Task | None = None
        self._final_text = ""

    async def start(self) -> str:
        """Spawn the helper and wait for ``ready``. Returns "" on success, else why not."""
        avail = availability()
        if not avail.ok:
            return avail.reason
        helper = await asyncio.to_thread(stream_helper_path)
        if not helper:
            return "streaming speech helper could not be built"
        try:
            try:
                stream_argv, stream_env, _ = await asyncio.to_thread(
                    _sandboxed,
                    [helper, "--locale", self.locale, "--sample-rate", str(self.sample_rate)],
                )
            except sandbox.SandboxUnavailableError as exc:
                return f"{_NO_SANDBOX_HINT}{exc}"
            self._proc = await asyncio.create_subprocess_exec(
                *stream_argv,
                env=stream_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as exc:
            return f"could not start streaming helper: {exc}"

        self._pump = asyncio.create_task(self._read_events())
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=_READY_TIMEOUT_SECS)
        except asyncio.TimeoutError:
            await self.close()
            return "streaming helper did not become ready"
        if first is None:
            await self.close()
            return "streaming helper exited before becoming ready"
        if first.get("type") == "error":
            await self.close()
            return str(first.get("message", "streaming helper failed"))
        if first.get("type") != "ready":
            # Put it back: an early result is not a protocol violation worth failing on.
            self._queue.put_nowait(first)
        return ""

    async def _read_events(self) -> None:
        """Forward the helper's JSON lines onto the queue; None marks end of stream."""
        assert self._proc is not None and self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if not text:
                    continue
                try:
                    event = json.loads(text)
                except json.JSONDecodeError:
                    logger.debug("apple_speech: unparseable helper line: %s", text[:120])
                    continue
                if isinstance(event, dict):
                    await self._queue.put(event)
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            await self._queue.put(None)

    async def feed(self, pcm: bytes) -> bool:
        """Write one PCM chunk. False means the helper is gone and the caller should stop."""
        if self._proc is None or self._proc.stdin is None:
            return False
        try:
            self._proc.stdin.write(pcm)
            await self._proc.stdin.drain()
            return True
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    async def events(self):
        """Yield ``{"type": "partial"|"final"|"done"|"error", ...}`` until the stream ends."""
        while True:
            event = await self._queue.get()
            if event is None:
                return
            if event.get("type") == "final":
                self._final_text += str(event.get("text", ""))
            yield event

    async def finish(self, *, timeout: float = 10.0) -> str:
        """Close stdin (the helper's cue to finalize) and wait for exit.

        Returns the accumulated final transcript. Safe to call more than once.
        """
        if self._proc is None:
            return self._final_text
        if self._proc.stdin is not None and not self._proc.stdin.is_closing():
            try:
                self._proc.stdin.close()
            except OSError:
                pass
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("apple_speech: streaming helper did not exit; killing")
            await self.close()
        return self._final_text

    async def close(self) -> None:
        """Kill the helper and stop the reader. Idempotent."""
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except (OSError, ProcessLookupError):
                pass
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
