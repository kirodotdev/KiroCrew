"""Decide, before the native recogniser is touched, whether touching it is safe.

**Why this exists.** ``pywhispercpp`` links whisper.cpp and ggml into one native
extension, and ggml is built with the instruction set of the machine that
compiled it. Load that extension on a CPU that lacks one of those instructions
and the process receives ``SIGILL``. A signal is not an exception: no
``try``/``except`` in :mod:`kiro_crew.stt.engine` can contain it, the thread it
lands on does not matter, and the whole gateway dies -- every chat session, every
channel, every cron -- for the sake of an optional speech feature. With the boot
prewarm loading the model a few seconds after every start, that is a crash loop
the supervisor cannot escape and no config change made from the crashed process
can end (kirodotdev/KiroCrew#13179: a Broadwell Xeon with no AVX-512, and a build
that assumed it).

Two mechanisms, both cheap, both consulted by :func:`kiro_crew.stt.engine.probe`
so that every surface asking "can voice run here" -- the boot prewarm, a live
session, ``GET /api/stt/status``, ``kirocrew doctor`` -- gets the same answer:

1. **A subprocess probe.** ``whisper_print_system_info()`` is the one call that
   both runs ``ggml_cpu_init()`` (the first native code an incompatible build
   trips over) and reports the instruction sets the build was compiled for. It is
   run in a CHILD interpreter, once per installed binary. A child that dies of
   ``SIGILL`` proves the build cannot run here without costing the gateway
   anything. A child that survives hands back the build's feature list, which is
   then compared with the host's own (``/proc/cpuinfo`` on Linux), so a build
   that demands ``AVX512`` is refused on a host without ``avx512f`` even if the
   probe itself happened to execute no such instruction.

2. **A load marker.** The probe cannot see an instruction that executes only
   deep inside a model load. So the engine writes a marker before every native
   load and clears it after, whatever the outcome; a marker left behind by a
   process other than this one means the last load never returned, and
   the next process refuses to try again with the same binary. It is a fuse, not
   a retry policy: it trips once and stays tripped until the binary changes
   (a reinstall) or the marker is removed by hand, and the refusal names the file.

Both verdicts are :class:`Verdict` values carrying one of the ``CODE_*`` strings
below, which travel to the browser exactly like the engine's own availability
codes. Neither imports numpy, the binding, or anything else heavy: ``doctor``
reads this on a host that may have none of them.

**What this is not.** It is not process isolation for the recogniser itself. A
worker process would also survive a fault mid-decode, but it means shipping PCM
frames, partial results and abort signals over IPC for every utterance and
holding the model's memory in a second process; that redesign is out of
proportion to a fault that is deterministic per (binary, CPU) pair and therefore
answerable before the first load.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import platform
import secrets
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kiro_crew.atomic_write import atomic_write
from kiro_crew.stt import capabilities

logger = logging.getLogger(__name__)

#: The native build was compiled for instructions this CPU does not have. No
#: install of the same build fixes it; a build for this CPU, or ``off``, does.
CODE_UNSUPPORTED_CPU = "stt_unsupported_cpu"
#: The previous attempt to load the model ended the process. Refused until the
#: binary changes or the marker is removed.
CODE_LOAD_CRASHED = "stt_load_crashed"
#: The subprocess probe died of something other than an illegal instruction.
#: Reported separately because the remedy is different: a reinstall may fix a
#: broken library where no reinstall fixes a CPU.
CODE_NATIVE_PROBE_CRASHED = "stt_native_probe_crashed"

#: How long the child may take to import the binding and print its features.
#: Generous: on macOS the first import runs ``ggml_metal_library_init``, measured
#: at 6.4 s on a cold library cache. A child that exceeds this is killed and the
#: verdict is inconclusive rather than a refusal -- a slow host is not a broken one.
PROBE_TIMEOUT_SECS = 45.0

#: ``python -c`` body for the child. Writes the feature string and nothing else,
#: so a parse failure is a real format change and not a stray log line.
#:
#: The child is EXPECTED to die of SIGILL on the host this exists for, and a
#: process that dies of a signal can leave a core dump wherever the host's
#: ``core_pattern`` points. Its own first act is therefore to set ``RLIMIT_CORE``
#: to zero, in the child and before the import, so the crash the probe is
#: designed to absorb writes nothing to disk. Done in the child rather than via
#: ``preexec_fn`` for the reason ``_spawn_exec_shim`` documents: ``preexec_fn``
#: forces CPython to ``fork()`` the whole gateway. ``resource`` is POSIX-only;
#: Windows has no core dumps of this shape, so the import failing there is fine.
#:
#: The extension is loaded from the exact file :func:`binary_identity` resolved
#: (``sys.argv[1]``), not by name: the child runs under ``-I``, which drops the
#: user site-packages from ``sys.path``, so a ``pip install --user`` of the voice
#: extra would otherwise import in the gateway and fail in the child -- an
#: inconclusive verdict that lets the gateway load the very build the probe
#: exists to refuse. Loading the parent's own resolved path makes the two
#: processes agree on WHICH binary is being judged.
_PROBE_PRELUDE = (
    "import sys\n"
    "try:\n"
    "    import resource; resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
    "except Exception:\n"
    "    pass\n"
)
_PROBE_BODY = (
    "import importlib.util\n"
    "spec = importlib.util.spec_from_file_location('_pywhispercpp', sys.argv[1])\n"
    "b = importlib.util.module_from_spec(spec)\n"
    "spec.loader.exec_module(b)\n"
    "sys.stdout.write(b.whisper_print_system_info() or '')\n"
)
_PROBE_SOURCE = _PROBE_PRELUDE + _PROBE_BODY

#: Where the child runs. The interpreter's own prefix: a directory the agent does
#: not write to, so a host whose ``core_pattern`` is relative cannot be steered
#: into dumping next to agent-readable files, and one that exists on every host
#: the interpreter runs on. Module-level so a test can point it at ``tmp_path``.
_CHILD_CWD = sys.prefix

#: Environment variables the child keeps. Everything else the gateway carries --
#: credentials propagated for its own children, ``PYTHON*`` switches, a
#: ``KIROCREW_*`` identity -- is withheld: the child imports one extension and
#: prints one string, and nothing it inherits can make that answer more correct.
#: ``PATH`` stays because the extension's ``DT_NEEDED`` resolution on some hosts
#: consults it for ``libgomp``; ``LD_LIBRARY_PATH`` / ``DYLD_*`` stay because a
#: source-built extension may depend on them to load at all, and the probe must
#: load what the gateway would. ``SYSTEMROOT`` is what Windows needs to start
#: the interpreter; ``TMP``/``TEMP`` are what ``tempfile`` needs on both.
_CHILD_ENV_KEEP: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SYSTEMROOT",
        "SystemRoot",
        "LD_LIBRARY_PATH",
        "DYLD_LIBRARY_PATH",
        "DYLD_FALLBACK_LIBRARY_PATH",
    }
)

#: Name of the load marker inside the whisper models directory.
LOAD_MARKER_NAME = ".load-in-progress.json"

#: What "this process" means to the marker. Not the pid: the marker lives on the
#: models directory, which outlives the process, and a replacement container in
#: a fresh PID namespace lands on the same low pid as a matter of course -- so a
#: pid match would read a dead process's marker as a load in flight here and let
#: the fatal load run again. A random token minted at import is unique to this
#: interpreter and to nothing else. The pid is still recorded, for a human.
_PROCESS_TOKEN = secrets.token_hex(8)

#: The most bytes a load marker may be before it is judged by size alone. The
#: writer produces a few hundred bytes; the cap is generous for a longer model
#: path and nothing else. The marker sits at a fixed, guessable name in a
#: directory a sandboxed agent can write to, and it is re-read on every
#: availability check, so the read must not be "whatever is there".
_MARKER_MAX_BYTES = 4096


def _read_marker_bounded(path: Path) -> bytes | None:
    """Read the marker as a small regular file, or ``None`` if it is not one.

    Opened ``O_NOFOLLOW | O_NONBLOCK`` where the platform has them, then judged on
    the descriptor: a symlink at the name is not followed, a FIFO or device does
    not block and is refused by ``fstat``, and a file over :data:`_MARKER_MAX_BYTES`
    is refused by its size before a byte of it is read. ``None`` is "not a marker
    the writer could have produced"; the caller ignores it exactly like garbage.
    Raises ``FileNotFoundError`` when there is no file, so the caller can tell
    "nothing there" from "something there that is not ours".
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    flags |= getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _MARKER_MAX_BYTES:
            return None
        chunks: list[bytes] = []
        remaining = _MARKER_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        # Grew under us past the cap: judged the same as too large.
        return None if len(data) > _MARKER_MAX_BYTES else data
    finally:
        os.close(fd)


#: Signal numbers that mean "the CPU rejected an instruction". Only SIGILL is
#: that; the others below are faults with other causes and get their own code.
_SIGILL = 4
#: Windows exit status for an illegal instruction (``STATUS_ILLEGAL_INSTRUCTION``).
#: ``subprocess`` reports NTSTATUS values as large unsigned ints there.
_WIN_STATUS_ILLEGAL_INSTRUCTION = 0xC000001D
_WIN_STATUS_ERROR_FLOOR = 0xC0000000

#: ggml feature names (as ``whisper_print_system_info`` prints them) mapped to
#: the token the kernel prints for the same capability in ``/proc/cpuinfo``.
#: Only names here can produce a refusal; a feature this table does not know is
#: reported but never refused on, because a wrong entry here would refuse a
#: working host. Every token was checked against the kernel's own flag names.
#: The x86 reader (``embeddings._linux_x86_64_cpu_flags``) adds ``sse3`` beside
#: the kernel's ``pni``, and ARM spells its features on a ``Features`` line with
#: ``asimd*`` prefixes.
_HOST_FLAG_FOR_FEATURE: dict[str, str] = {
    # x86-64. ``ggml_cpu_has_*`` names on the left.
    "SSE3": "sse3",
    "SSSE3": "ssse3",
    "AVX": "avx",
    "AVX_VNNI": "avx_vnni",
    "AVX2": "avx2",
    "F16C": "f16c",
    "FMA": "fma",
    "BMI2": "bmi2",
    "AVX512": "avx512f",
    # The kernel spells this one WITHOUT an underscore (X86_FEATURE_AVX512VBMI),
    # unlike its siblings `avx512_vbmi2` / `avx512_vnni` / `avx512_bf16`.
    "AVX512_VBMI": "avx512vbmi",
    "AVX512_VNNI": "avx512_vnni",
    "AVX512_BF16": "avx512_bf16",
    "AMX_INT8": "amx_int8",
    # AArch64. ``NEON`` is ``asimd``; ``ARM_FMA`` has no separate flag (it is
    # part of the base ISA on every AArch64 CPU) and is deliberately absent.
    "NEON": "asimd",
    "FP16_VA": "asimdhp",
    "DOTPROD": "asimddp",
    "MATMUL_INT8": "i8mm",
    "SVE": "sve",
    "SME": "sme",
}


@dataclass(frozen=True)
class Verdict:
    """Whether the native recogniser may be loaded in this process.

    ``ok`` with an empty ``code`` is a pass. ``ok`` with a non-empty ``detail`` and
    an empty ``code`` is an INCONCLUSIVE pass: the probe could not run to a
    conclusion (no interpreter, a timeout, an import failure the engine's own
    probe will report better) and the caller proceeds as before this module
    existed. A refusal has ``ok=False`` and one of the ``CODE_*`` values.
    """

    ok: bool
    code: str = ""
    detail: str = ""
    #: The child's ``whisper_print_system_info()`` output, verbatim, for a bug report.
    raw: str = ""


@dataclass(frozen=True)
class _BinaryIdentity:
    """What "the same binary" means: the extension's path, size and mtime.

    Not a digest: the file is 4-5 MB and this is read on every availability
    probe. A reinstall that leaves all three unchanged is not a reinstall.
    """

    path: str
    size: int
    mtime_ns: int

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "size": self.size, "mtime_ns": self.mtime_ns}

    @classmethod
    def from_dict(cls, data: Any) -> "_BinaryIdentity | None":
        if not isinstance(data, dict):
            return None
        try:
            return cls(str(data["path"]), int(data["size"]), int(data["mtime_ns"]))
        except (KeyError, TypeError, ValueError):
            return None


def binary_identity() -> _BinaryIdentity | None:
    """Identify the installed ``_pywhispercpp`` extension without importing it.

    ``None`` when there is no such file: the engine's own probe then reports the
    missing extra, and there is nothing here to check.
    """
    try:
        spec = importlib.util.find_spec("_pywhispercpp")
    except Exception:
        return None
    origin = getattr(spec, "origin", None)
    if spec is None or not isinstance(origin, str) or origin in {"built-in", "frozen"}:
        return None
    try:
        st = os.stat(origin)
    except OSError:
        return None
    return _BinaryIdentity(origin, st.st_size, st.st_mtime_ns)


# ── Host instruction sets ──


def host_flags(cpuinfo_path: Path | None = None) -> frozenset[str] | None:
    """The CPU feature tokens the kernel reports, lower-cased, or ``None``.

    Linux only. x86-64 goes through :func:`kiro_crew.embeddings._linux_x86_64_cpu_flags`,
    the reader the embedding runtime already uses against the same ggml/SIGILL
    threat (features shared by EVERY visible core, ``sse3`` added beside ``pni``),
    so this repository keeps one x86 cpuinfo parser. AArch64 has no reader there;
    its ``Features`` line is intersected across cores here the same way.

    ``None`` means "unknown", and an unknown host is never refused on -- the
    subprocess probe is the evidence that stands on its own on every platform;
    this comparison is the second line, for a build whose probe executed none of
    the instructions it was compiled to use. macOS is therefore covered by the
    child alone: its published wheel is arm64-only and every Apple Silicon part
    shares the baseline the wheel targets.

    *cpuinfo_path* is injectable so a test can hand it a Broadwell.
    """
    if platform.system() != "Linux":
        return None
    path = Path("/proc/cpuinfo") if cpuinfo_path is None else cpuinfo_path
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        # circular import: `embeddings` imports `config.loader`, which imports
        # `kiro_crew.stt.limits` through the `stt` package this module belongs to.
        from kiro_crew.embeddings import _linux_x86_64_cpu_flags

        return _linux_x86_64_cpu_flags(path)
    if not machine.startswith(("aarch64", "arm64")):
        # The `Features` reader below is an AArch64 reader: a 32-bit ARM kernel
        # (`armv7l`, or an `armv8l` userspace) spells `neon` where the 64-bit
        # kernel spells `asimd`, so a source-built recogniser that runs fine
        # would be refused on a spelling. Any architecture without a reader is
        # "unknown", and an unknown host is never refused on.
        return None
    try:
        cpuinfo = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    per_cpu: list[frozenset[str]] = []
    for line in cpuinfo.splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip().lower() == "features":
            per_cpu.append(frozenset(tok.lower() for tok in value.split()))
    if not per_cpu:
        return None
    return frozenset.intersection(*per_cpu)


def missing_features(
    build_features: tuple[str, ...], flags: frozenset[str] | None
) -> tuple[str, ...]:
    """Build features (ggml names) the host does not report, in build order.

    Empty when *flags* is unknown: a comparison against nothing is not evidence.
    """
    if flags is None:
        return ()
    return tuple(
        name
        for name in build_features
        if name in _HOST_FLAG_FOR_FEATURE and _HOST_FLAG_FOR_FEATURE[name] not in flags
    )


# ── The subprocess probe ──


def _run_probe_child(timeout: float, extension: str) -> tuple[int | None, str, str]:
    """Run the child; ``(returncode, stdout, stderr)``, ``None`` code on timeout.

    ``-I`` (isolated: no ``''`` on ``sys.path``, no ``PYTHON*`` env, no user site)
    so nothing in whatever directory the gateway was started from can shadow the
    extension -- the same "nothing to shadow" reasoning as ``sandbox.py``'s probe
    shim. ``-S`` on top of it, because ``-I`` still imports ``site`` at startup and
    ``site`` executes every ``import`` line of every ``.pth`` file in the
    interpreter's site-packages -- a directory written by whoever installs into
    the venv, and this child runs unsandboxed as the gateway on purpose. The
    child never needed ``site``: it loads the extension from the explicit path
    it is handed, so skipping ``site`` costs nothing and closes the one startup
    hook left. Because ``-I`` also hides a user-site install, the child is handed
    the exact file the parent resolved (*extension*, from :func:`binary_identity`)
    and loads that, so both processes judge the same binary. The working
    directory is pinned to :data:`_CHILD_CWD`, the environment is reduced to
    :data:`_CHILD_ENV_KEEP`, and the child zeroes its own ``RLIMIT_CORE`` before
    touching the extension.
    """
    cmd = [sys.executable, "-I", "-S", "-c", _PROBE_SOURCE, extension]
    env = {k: v for k, v in os.environ.items() if k in _CHILD_ENV_KEEP}
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            cwd=_CHILD_CWD,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else ""
        err = exc.stderr if isinstance(exc.stderr, str) else ""
        return None, out, err
    except (OSError, ValueError) as exc:
        # The interpreter path gone (a replaced venv under a running gateway),
        # a refused fork (ENOMEM, RLIMIT_NPROC, a restricted container), a cwd
        # that is missing: a child that could not START answers nothing
        # about the CPU. Report it like a timeout -- inconclusive -- so every
        # surface above `engine.probe` still gets an `Availability` rather than
        # a raise; the same rule `sandbox._probe_unshare_via_spawn` applies.
        logger.debug("Could not start the speech runtime probe: %s", exc)
        return None, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _classify_exit(returncode: int) -> str | None:
    """Which ``CODE_*`` a non-zero child exit is, or ``None`` for "just an error"."""
    if returncode < 0:
        return CODE_UNSUPPORTED_CPU if -returncode == _SIGILL else CODE_NATIVE_PROBE_CRASHED
    if returncode == _WIN_STATUS_ILLEGAL_INSTRUCTION:
        return CODE_UNSUPPORTED_CPU
    if returncode >= _WIN_STATUS_ERROR_FLOOR:
        return CODE_NATIVE_PROBE_CRASHED
    return None


def _build_features(info: str) -> tuple[str, ...]:
    """The ``KEY = 1`` names in a system-info string, via the capabilities parser."""
    flags, _sections = capabilities._parse(info)
    return tuple(name for name, on in flags.items() if on)


def probe_native(
    *,
    extension: str | None = None,
    timeout: float = PROBE_TIMEOUT_SECS,
    runner: Any = _run_probe_child,
    flags: frozenset[str] | None = None,
    read_host_flags: bool = True,
) -> Verdict:
    """Run the child probe once and turn its outcome into a :class:`Verdict`.

    Uncached; :func:`verdict` is the cached entry point. *extension* is the file
    the child loads; ``None`` resolves it through :func:`binary_identity`, and no
    resolvable file is inconclusive (nothing to judge). *runner*, *flags* and
    *read_host_flags* are injection points so a test can stage a SIGILL, a
    Windows status, or a Skylake build on a Broadwell host without either CPU.
    """
    if extension is None:
        ident = binary_identity()
        if ident is None:
            return Verdict(True, detail="no speech runtime extension installed")
        extension = ident.path
    returncode, out, err = runner(timeout, extension)
    if returncode is None:
        why = (
            f"could not start: {err.strip()}"
            if err.strip()
            else f"did not finish within {timeout:.0f}s"
        )
        return Verdict(True, detail=f"the speech runtime probe {why}; not refusing on it")
    if returncode != 0:
        code = _classify_exit(returncode)
        if code == CODE_UNSUPPORTED_CPU:
            return Verdict(
                False,
                CODE_UNSUPPORTED_CPU,
                "the speech runtime raised Illegal instruction on this CPU before any "
                "model was loaded: the installed build was compiled for instructions "
                f"this machine ({platform.machine()}) does not have. Turn speech-to-text "
                "off (stt.enabled), or install a build made for this CPU",
            )
        if code == CODE_NATIVE_PROBE_CRASHED:
            return Verdict(
                False,
                CODE_NATIVE_PROBE_CRASHED,
                f"the speech runtime probe crashed (exit status {returncode}) before "
                "any model was loaded; not loading it into the gateway. Turn "
                "speech-to-text off (stt.enabled), or reinstall the speech runtime",
            )
        # An ordinary error: the binding did not import, most likely. The engine's
        # own probe reports that with the loader's message, which is more use than
        # anything here, so this is inconclusive rather than a refusal.
        tail = (err or out).strip().splitlines()[-1:] or [""]
        return Verdict(True, detail=f"the speech runtime probe exited {returncode}: {tail[0]}")
    build = _build_features(out)
    if read_host_flags and flags is None:
        flags = host_flags()
    lacking = missing_features(build, flags)
    if lacking:
        return Verdict(
            False,
            CODE_UNSUPPORTED_CPU,
            "the installed speech runtime was built for "
            + ", ".join(lacking)
            + f", which this CPU ({platform.machine()}) does not have; loading it "
            "would stop the gateway with Illegal instruction. Turn speech-to-text "
            "off (stt.enabled), or install a build made for this CPU",
            raw=out.strip(),
        )
    return Verdict(True, raw=out.strip())


# ── The cache ──

_lock = threading.Lock()
_cached: tuple[_BinaryIdentity, Verdict] | None = None


def verdict() -> Verdict:
    """The cached probe verdict for the installed binary; runs the probe if needed.

    Keyed on :func:`binary_identity`, so a reinstall under a running gateway is
    probed afresh and a probe is never repeated for the same file. No binary at
    all is a pass: there is nothing to refuse, and the engine's probe reports
    the missing extra on its own.
    """
    global _cached
    ident = binary_identity()
    if ident is None:
        return Verdict(True, detail="no speech runtime extension installed")
    with _lock:
        if _cached is not None and _cached[0] == ident:
            return _cached[1]
        result = probe_native(extension=ident.path)
        if not result.ok:
            logger.warning("Refusing to load the speech runtime: %s", result.detail)
        elif result.detail:
            logger.debug("Speech runtime preflight inconclusive: %s", result.detail)
        _cached = (ident, result)
        return result


def reset_cache() -> None:
    """Forget the cached verdict. For tests, and for a reinstall a test simulates."""
    global _cached
    with _lock:
        _cached = None


# ── The load marker ──


def _marker_path(models_dir: Path) -> Path:
    return models_dir / LOAD_MARKER_NAME


def write_load_marker(models_dir: Path, model_path: str) -> None:
    """Record that THIS process is about to load *model_path* natively.

    Raises ``OSError`` when the marker cannot be written. Not best-effort: the
    marker is the only record a load that kills the process leaves behind, so a
    load run without it would be repeated on the next boot exactly as if this
    module did not exist. A models directory that cannot take a small file
    (read-only, full) therefore costs the load, not the fuse -- the caller
    reports the reason and the gateway stays up either way.
    """
    ident = binary_identity()
    payload = {
        "token": _PROCESS_TOKEN,
        "pid": os.getpid(),
        "started": time.time(),
        "model": model_path,
        "binary": ident.as_dict() if ident is not None else None,
    }
    path = _marker_path(models_dir)
    try:
        models_dir.mkdir(parents=True, exist_ok=True)
        # `atomic_write`, not a predictable `.tmp` sibling: the models directory
        # is where a sandboxed agent can plant a symlink under a name it can
        # guess, and this write runs unsandboxed in the gateway. The helper's
        # temp file is uniquely named and exclusively created, so there is no
        # name to pre-plant, and the rename is the same atomic publish.
        atomic_write(path, json.dumps(payload))
    except OSError as exc:
        raise OSError(
            f"could not arm the speech load fuse at {path} ({exc}); the speech model "
            "is not loaded without it, because a load that dies would otherwise be "
            "repeated on the next start"
        ) from exc


def clear_load_marker(models_dir: Path) -> None:
    """The load returned (however it ended); disarm the fuse."""
    try:
        _marker_path(models_dir).unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logger.debug("Could not clear the speech load marker", exc_info=True)


def previous_load_crashed(models_dir: Path) -> Verdict:
    """Whether a marker from ANOTHER process names the binary installed now.

    A marker carrying this process's token is a load in flight here and is not
    a crash. A marker naming a different binary is left over from an install that
    is gone; it is ignored, because the fuse is per binary. A marker from another
    process naming THIS binary is the fuse, tripped.

    This function only READS. It never unlinks a marker it does not trust:
    the file is one shared path, and another process (a second gateway, a pod)
    may be sitting between its own arm and disarm on it -- a stale-looking or
    momentarily unreadable marker judged here and removed could be that live
    fuse, and the load it guards would then die with nothing left behind. An
    ignored marker costs nothing sitting there; the next arm's ``atomic_write``
    replaces it whole.
    """
    path = _marker_path(models_dir)
    try:
        raw = _read_marker_bounded(path)
        data = None if raw is None else json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return Verdict(True)
    except (OSError, ValueError):
        # Unreadable is not evidence either way, and not ours to remove.
        return Verdict(True)
    if not isinstance(data, dict):
        return Verdict(True)
    if data.get("token") == _PROCESS_TOKEN:
        return Verdict(True)
    recorded = _BinaryIdentity.from_dict(data.get("binary"))
    current = binary_identity()
    if recorded is None or current is None or recorded != current:
        return Verdict(True)
    model = data.get("model", "")
    return Verdict(
        False,
        CODE_LOAD_CRASHED,
        "the last attempt to load the speech model "
        f"({Path(str(model)).name or 'unknown'}) never returned -- the gateway "
        "process died during it -- so it is not loaded again with this build. "
        f"Turn speech-to-text off (stt.enabled), reinstall the speech runtime, or "
        f"remove {path} to try once more",
    )
