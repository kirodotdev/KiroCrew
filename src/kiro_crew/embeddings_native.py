"""Loading the vendored llama.cpp runtime.

Which platform library directory applies, the CPU-feature and MSVC-runtime
checks that keep an unsupported host from crashing on import, the library-path
seam the vendored loader reads, and ``_load_llama_class``, which imports the
runtime once per process.

:mod:`kiro_crew.embeddings` stays the import path and the patch surface for every
name here; that module's comment lists the owners.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import functools
import importlib.util
import logging
import os
import platform
import sys
import types
from pathlib import Path

from kiro_crew._llama_lib_path import (  # noqa: F401
    _LIB_PATH_ENV,
    _LIBS_DIR_NAME,
    _REQUIRED_VENDORED_LIBS,
    consume_dropped_inherited_lib_path,
    operator_lib_path_override,
)

logger = logging.getLogger("kiro_crew.embeddings")


# ── Vendored runtime loading ──

_VENDOR_DIR = Path(__file__).resolve().parent / "_vendor"
# The process-local channel to the vendored loader: a module registered in
# ``sys.modules`` under this key, carrying ``LIBS_DIR`` (see
# _publish_lib_path_for_import). ``_vendor/llama_cpp/llama_cpp.py`` reads it
# before ``LLAMA_CPP_LIB_PATH`` (its ``kiro_crew DIVERGENCE FROM UPSTREAM``).
# Entry preludes remove inherited bundled or empty values before any thread or
# child exists. A process that bypasses those preludes may still carry one; the
# loader classifies and ignores it without writing the environment. The bundled
# directory travels only through ``sys.modules``, which children do not inherit.
# On Windows the upstream loader still prepends the directory to ``PATH`` for
# its dependent DLLs (unchanged here, and it did so before the seam); that is a
# search hint a successor's own prepend outranks, not an instruction to load
# from a named directory.
_LIB_PATH_SEAM = "_kiro_crew_llama_cpp_lib_path"
# The upstream Linux x86_64 CPU wheel is built with these code-generation
# switches enabled. Its startup path executes the corresponding instructions
# before llama.cpp can make a runtime dispatch decision, so loading it on a
# weaker CPU raises SIGILL and kills the whole process. Linux exposes `pni` for
# SSE3; the parser below normalizes that spelling to `sse3`.
_LINUX_X86_64_REQUIRED_CPU_FLAGS = frozenset(
    {"avx", "avx2", "bmi2", "f16c", "fma", "sse3", "ssse3"}
)
_LINUX_CPUINFO_PATH = Path("/proc/cpuinfo")

#: MSVC runtime DLLs the vendored Windows libs IMPORT but which are not shipped
#: beside them. All four ship with the Microsoft Visual C++ 2015-2022
#: Redistributable, and a clean Windows install may carry none of them.
#:
#: Read off the PE import tables of the four DLLs in ``win_amd64`` rather than
#: guessed: ``llama.dll`` and ``ggml.dll`` need the first three, and
#: ``ggml-base.dll``/``ggml-cpu.dll`` additionally pull ``VCOMP140.DLL``, the MSVC
#: OpenMP runtime. Both Linux payloads DO vendor their equivalent
#: (``libgomp-*.so.1.0.0``), which is what makes this an omission on the Windows
#: lane rather than a deliberate asymmetry. They are not vendored here because
#: redistributing Microsoft's runtime is a licensing decision, not a packaging one
#: — so the gap is reported precisely instead of being papered over.
_WINDOWS_MSVC_RUNTIME_DLLS = (
    "MSVCP140.dll",
    "VCRUNTIME140.dll",
    "VCRUNTIME140_1.dll",
    "VCOMP140.DLL",
)


def _platform_libs_dirname() -> str | None:
    """Map (sys.platform, machine) to a vendored native-libs directory name."""
    machine = platform.machine().lower()
    if sys.platform.startswith("linux"):
        if machine in ("x86_64", "amd64"):
            return "linux_x86_64"
        if machine in ("aarch64", "arm64"):
            return "linux_aarch64"
    elif sys.platform == "darwin":
        if machine == "arm64":
            return "macos_arm64"
        if machine == "x86_64":
            return "macos_x86_64"
    elif sys.platform == "win32":
        if machine in ("amd64", "x86_64"):
            return "win_amd64"
    return None


def _missing_windows_msvc_runtime() -> list[str]:
    """Which MSVC runtime DLLs the vendored Windows libs need but cannot be found.

    Probed BY NAME through the OS loader rather than by listing a directory, so the
    answer reflects the same search the vendored DLLs' own imports will perform
    (System32, the app directory, the DLL search path) instead of a guess about where
    the redistributable installed itself.

    Always empty off Windows: ``ctypes.WinDLL`` does not exist elsewhere.
    """
    if sys.platform != "win32":
        return []
    absent: list[str] = []
    for name in _WINDOWS_MSVC_RUNTIME_DLLS:
        try:
            ctypes.WinDLL(name)
        except OSError:
            absent.append(name)
    return absent


def _linux_x86_64_cpu_flags(
    cpuinfo_path: Path = _LINUX_CPUINFO_PATH,
) -> frozenset[str] | None:
    """Return features shared by every visible Linux x86_64 processor.

    ``None`` means the host did not expose a usable feature list. Callers must
    fail closed because a wrong optimistic answer can terminate the process
    with SIGILL before Python can catch anything.
    """
    try:
        cpuinfo = cpuinfo_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    per_cpu: list[frozenset[str]] = []
    for line in cpuinfo.splitlines():
        key, separator, value = line.partition(":")
        if separator and key.strip().lower() == "flags":
            flags = {flag.lower() for flag in value.split()}
            if "pni" in flags:
                flags.add("sse3")
            per_cpu.append(frozenset(flags))
    if not per_cpu:
        return None
    return frozenset.intersection(*per_cpu)


def _macos_x86_64_missing_cpu_flags() -> list[str] | None:
    """Return CPU features required by the bundled macOS x86_64 llama.cpp runtime
    that are absent on this host, or None if the feature list cannot be read.

    Uses ``sysctlbyname`` to query ``machdep.cpu.features`` and
    ``machdep.cpu.leaf7_features`` (the latter carries AVX2, BMI1/2, FMA).
    Falls back to ``None`` (fail-closed) if the sysctl call fails.
    """

    libc_name = ctypes.util.find_library("c")
    if libc_name is None:
        return None
    try:
        libc = ctypes.CDLL(libc_name)
    except OSError:
        return None

    def _sysctl_str(name: str) -> str:
        # Two-call pattern: first call with NULL buffer to get required size.
        # Avoids a fixed-size buffer that could truncate long feature strings.
        size = ctypes.c_size_t(0)
        libc.sysctlbyname(name.encode(), None, ctypes.byref(size), None, 0)
        if size.value == 0:
            return ""
        buf = ctypes.create_string_buffer(size.value)
        ret = libc.sysctlbyname(name.encode(), buf, ctypes.byref(size), None, 0)
        if ret != 0:
            return ""
        return buf.value.decode("ascii", errors="replace").lower()

    features = _sysctl_str("machdep.cpu.features")
    leaf7 = _sysctl_str("machdep.cpu.leaf7_features")
    if not features and not leaf7:
        return None

    # Normalise: macOS reports "AVX1.0" for AVX, "AVX2.0" for AVX2
    combined = (features + " " + leaf7).lower()
    combined = combined.replace("avx1.0", "avx").replace("avx2.0", "avx2")
    present = set(combined.split())

    # Map Linux flag names to what macOS sysctl reports
    _MACOS_FLAG_MAP = {
        "avx": "avx",
        "avx2": "avx2",
        "fma": "fma",
        "bmi2": "bmi2",
        "f16c": "f16c",
        "sse3": "sse3",
        "ssse3": "ssse3",
    }
    missing = sorted(
        linux_name
        for linux_name, macos_name in _MACOS_FLAG_MAP.items()
        if macos_name not in present and linux_name in _LINUX_X86_64_REQUIRED_CPU_FLAGS
    )
    return missing if missing else []


def verify_vendored_libs(root: Path | None = None) -> dict[str, list[str]]:
    """Report vendored native libs that :data:`_REQUIRED_VENDORED_LIBS` expects but are absent.

    The closure is declared in :mod:`kiro_crew._llama_lib_path` and re-exported
    here; this module is where it is CHECKED, which is why the report lives
    beside the loader that acts on it.

    Returns a mapping of platform dir -> sorted missing filenames; an empty
    mapping means the payload is complete for EVERY platform, not just the
    running one. ``root`` defaults to the installed ``_vendor`` directory, so
    the same check runs against a source tree, an unpacked sdist, or an
    installed wheel — that cross-lane reuse is the point, since each packaging
    lane (sdist rules, wheel package_data) selects these
    files by a different mechanism and can therefore drop them independently.

    A platform dir that is entirely absent is reported as missing all of its
    libs rather than skipped: "the directory vanished" is the more severe
    version of the bug this guards, not an exemption from it.
    """
    base = (root or _VENDOR_DIR) / _LIBS_DIR_NAME
    missing: dict[str, list[str]] = {}
    for plat, required in _REQUIRED_VENDORED_LIBS.items():
        absent = sorted(name for name in required if not (base / plat / name).is_file())
        if absent:
            missing[plat] = absent
    return missing


def _publish_lib_path_for_import(libs_dir: Path | None) -> None:
    """Hand *libs_dir* to the vendored loader through the process-local seam.

    Registers a module under :data:`_LIB_PATH_SEAM` in ``sys.modules`` carrying
    ``LIBS_DIR``; ``_vendor/llama_cpp/llama_cpp.py`` reads it when it is first
    imported, before it consults ``LLAMA_CPP_LIB_PATH``. ``None`` removes the
    seam, so an operator override in the environment is what the loader sees.

    Deliberately not the environment variable, not even for the duration of
    the import: the import runs on the embedder's loader thread while the
    event loop keeps spawning children, and a child spawned in that window
    snapshots ``os.environ`` -- an MCP server, a session, the gateway an in-app
    restart execs into. One carrying its own llama-cpp bindings would dlopen
    this install's native libs against them; a Kiro Crew successor would read
    the PREVIOUS install's directory as an operator override, skip the
    completeness and CPU gates, and load that install's libs -- or none, once
    an upgrade has removed it, which surfaces as the upstream loader's bare
    "Shared library with base name 'llama' not found" while vector memory
    silently falls back to keyword search. ``sys.modules`` is not inherited.
    """
    if libs_dir is None:
        sys.modules.pop(_LIB_PATH_SEAM, None)
        return
    seam = types.ModuleType(
        _LIB_PATH_SEAM,
        "Process-local seam: the bundled llama.cpp lib directory for the vendored loader.",
    )
    seam.LIBS_DIR = str(libs_dir)  # type: ignore[attr-defined]
    sys.modules[_LIB_PATH_SEAM] = seam


def _install_diskcache_stub() -> None:
    """Register a ``diskcache`` stub in ``sys.modules`` if the real one is absent.

    ``llama_cpp.llama_cache`` imports ``diskcache`` at module level, but
    Kiro Crew never constructs a disk-backed LLM state cache (we only use
    embeddings). Registering a stub avoids adding the real dependency to the
    version set while never shadowing an actually-installed diskcache.
    """
    if "diskcache" in sys.modules or importlib.util.find_spec("diskcache") is not None:
        return
    stub = types.ModuleType("diskcache")

    class Cache:  # pragma: no cover - never constructed by Kiro Crew
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError(
                "diskcache is stubbed out by kiro_crew.embeddings — disk-backed "
                "LLM state caching is not available"
            )

    stub.Cache = Cache  # type: ignore[attr-defined]
    stub.FanoutCache = Cache  # type: ignore[attr-defined]
    sys.modules["diskcache"] = stub


def _harden_llama_null_streams() -> None:
    """Make llama-cpp-python's process-global suppression streams Unicode-safe.

    The vendored suppressor temporarily assigns its import-time ``os.devnull``
    handles to ``sys.stdout`` and ``sys.stderr`` while a model loads. That load
    runs on a background thread, so unrelated gateway output can reach those
    handles. Reconfiguring the existing wrappers preserves the native ``dup2``
    suppression while removing the host locale from that process-wide window.
    """
    llama_utils = sys.modules.get("llama_cpp._utils")
    if llama_utils is None:
        return
    for name in ("outnull_file", "errnull_file"):
        stream = getattr(llama_utils, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _log_dropped_inherited_lib_path() -> None:
    """Emit the one INFO line for a value an entry prelude removed at startup.

    The preludes remove a bundled-shaped or empty inherited value before any
    thread or child exists, which is before logging is configured, so they only
    record it. This runs on the loader path -- the first point that reads the
    record with logging in place -- and
    :func:`consume_dropped_inherited_lib_path` hands the value over once per
    process, so a repeated loader call or a cleared ``lru_cache`` cannot repeat
    the line. ``dropped_inherited_lib_path()`` still names the value for
    ``kirocrew doctor`` afterwards.
    """
    removed = consume_dropped_inherited_lib_path()
    if removed is None:
        return
    if removed:
        logger.info(
            "Ignoring %s=%s inherited from another Kiro Crew process; "
            "this install resolves its own bundled llama.cpp libs",
            _LIB_PATH_ENV,
            removed,
        )
    else:
        logger.info(
            "Ignoring empty %s; this install resolves its own bundled llama.cpp libs",
            _LIB_PATH_ENV,
        )


@functools.lru_cache(maxsize=1)
def _load_llama_class():
    """Import the vendored llama-cpp-python runtime. Returns the Llama class or None.

    Hands the per-platform native libs directory to the vendored loader through
    the process-local seam (:func:`_publish_lib_path_for_import`, read by
    ``_vendor/llama_cpp/llama_cpp.py``) -- never through ``LLAMA_CPP_LIB_PATH``,
    which a child spawned during the import would inherit -- and prepends
    ``_vendor`` to ``sys.path`` so ``import llama_cpp`` resolves to the vendored
    copy. A value already in the environment is honoured as an operator override
    unless it is a bundled directory inherited from another Kiro Crew process,
    which is ignored without mutating the environment. Never raises -- unsupported
    platforms and import failures degrade to keyword-only memory search.
    """
    # Report what the prelude removed at startup, before anything about this
    # process's own resolution: the prelude could not log it, and this is the
    # first site that can.
    _log_dropped_inherited_lib_path()
    # Skipped when the operator set LLAMA_CPP_LIB_PATH: the libs then load from
    # THEIR directory, so the bundled tree's contents do not decide whether
    # the runtime works, and refusing on it would disable the documented escape
    # hatch (a GPU build, or a hand-restored lib dir) for precisely the users an
    # incomplete wheel stranded. Their directory is the loader's to validate.
    #
    # Entry preludes remove bundled-shaped and empty inherited values before any
    # thread or child exists. A loader reached without a prelude still ignores
    # such a value and publishes this install's directory through the process-local
    # seam, but never removes or otherwise writes the environment.
    override = operator_lib_path_override()
    if override is None:
        inherited = os.environ.get(_LIB_PATH_ENV)
        if inherited is not None:
            logger.info(
                "Ignoring %s=%s left in the environment; this install resolves "
                "its own bundled llama.cpp libs (entry preludes remove it at startup)",
                _LIB_PATH_ENV,
                inherited,
            )
    libs_dirname = _platform_libs_dirname()
    if libs_dirname is None:
        logger.warning(
            "In-process embeddings unsupported on %s/%s — memory falls back to keyword search",
            sys.platform,
            platform.machine(),
        )
        return None
    libs_dir = _VENDOR_DIR / _LIBS_DIR_NAME / libs_dirname
    if not libs_dir.is_dir():
        logger.warning("Vendored llama.cpp libs missing at %s", libs_dir)
        return None
    # Name the missing FILES before ctypes reports only the base name it could
    # not find. The upstream loader raises "Shared library with base name
    # 'llama' not found", which reads as a broken platform rather than an
    # incomplete install and sent the real-world diagnosis of this bug chasing
    # a Graviton/architecture problem instead of the packaging rule that
    # dropped the file on every architecture.
    if override is None:
        absent = [
            name
            for name in _REQUIRED_VENDORED_LIBS.get(libs_dirname, ())
            if not (libs_dir / name).is_file()
        ]
        if absent:
            logger.warning(
                "Vendored llama.cpp install for %s is incomplete — missing %s in %s. "
                "This is a packaging defect, not an unsupported platform; reinstall "
                "Kiro Crew from a current release, or point %s at a complete lib "
                "directory of your own (a path ending kiro_crew/_vendor/llama_cpp_libs/"
                "<platform> is read as another install's and ignored). Memory "
                "falls back to keyword search.",
                libs_dirname,
                ", ".join(absent),
                libs_dir,
                _LIB_PATH_ENV,
            )
            return None
        if libs_dirname == "win_amd64":
            # Named rather than left to surface as the loader's own
            # "[WinError 126] The specified module could not be found", which points
            # at the library being opened rather than at the runtime it imports and
            # so reads as a broken platform. Same reasoning as the missing-files
            # branch above: the fix here is one download, and an operator cannot
            # guess it from a WinError.
            missing_runtime = _missing_windows_msvc_runtime()
            if missing_runtime:
                logger.warning(
                    "The bundled Windows llama.cpp runtime needs the Microsoft Visual "
                    "C++ 2015-2022 Redistributable (x64); this host is missing %s. "
                    "Install it from https://aka.ms/vs/17/release/vc_redist.x64.exe "
                    "and restart Kiro Crew. Memory falls back to keyword search until "
                    "then. Set %s to use an operator-provided runtime instead.",
                    ", ".join(missing_runtime),
                    _LIB_PATH_ENV,
                )
                return None
        if libs_dirname == "linux_x86_64":
            cpu_flags = _linux_x86_64_cpu_flags()
            if cpu_flags is None:
                logger.warning(
                    "Cannot verify CPU compatibility for the bundled Linux x86_64 "
                    "llama.cpp runtime from %s. Refusing the native runtime because "
                    "an unsupported instruction would terminate the gateway; memory "
                    "falls back to keyword search. Set %s to use an operator-provided "
                    "runtime.",
                    _LINUX_CPUINFO_PATH,
                    _LIB_PATH_ENV,
                )
                return None
            missing_cpu_flags = sorted(_LINUX_X86_64_REQUIRED_CPU_FLAGS - cpu_flags)
            if missing_cpu_flags:
                logger.warning(
                    "Bundled Linux x86_64 llama.cpp runtime requires CPU features "
                    "%s; this host is missing %s. Refusing the native runtime because "
                    "it would terminate the gateway with SIGILL; memory falls back to "
                    "keyword search. Set %s to use a compatible operator-provided "
                    "runtime.",
                    ", ".join(sorted(_LINUX_X86_64_REQUIRED_CPU_FLAGS)),
                    ", ".join(missing_cpu_flags),
                    _LIB_PATH_ENV,
                )
                return None
        if libs_dirname == "macos_x86_64":
            _macos_flags = _macos_x86_64_missing_cpu_flags()
            if _macos_flags is None:
                logger.warning(
                    "Cannot verify CPU compatibility for the bundled macOS x86_64 "
                    "llama.cpp runtime. Refusing the native runtime because an "
                    "unsupported instruction would terminate the gateway with SIGILL; "
                    "memory falls back to keyword search. Set %s to use an "
                    "operator-provided runtime.",
                    _LIB_PATH_ENV,
                )
                return None
            if _macos_flags is not None and _macos_flags:
                logger.warning(
                    "Bundled macOS x86_64 llama.cpp runtime requires CPU features "
                    "%s; this host is missing %s. Refusing the native runtime because "
                    "it would terminate the gateway with SIGILL; memory falls back to "
                    "keyword search. Set %s to use a compatible operator-provided "
                    "runtime.",
                    ", ".join(sorted(_LINUX_X86_64_REQUIRED_CPU_FLAGS)),
                    ", ".join(_macos_flags),
                    _LIB_PATH_ENV,
                )
                return None
    # The bundled directory reaches the vendored loader through the process-local
    # seam; an operator-provided override (e.g. a GPU build) is left in the
    # environment as they set it, and the seam is absent so the loader reads it.
    _publish_lib_path_for_import(None if override is not None else libs_dir)
    _install_diskcache_stub()
    vendor_str = str(_VENDOR_DIR)
    if vendor_str not in sys.path:
        sys.path.insert(0, vendor_str)
    try:
        from llama_cpp import Llama  # noqa: F811

        _harden_llama_null_streams()
        return Llama
    except Exception:
        logger.warning("Vendored llama-cpp-python failed to import", exc_info=True)
        return None
