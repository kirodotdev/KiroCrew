"""Startup handling for the inherited llama.cpp native-library path.

This stdlib-only leaf is safe for entry preludes to import without pulling in
``kiro_crew.embeddings`` and its configuration and downloader dependencies.
Environment removal runs on the main thread before any thread or child process
exists: a concurrent environment snapshot can enumerate a key and then observe
its removal as a ``KeyError``, while insertion cannot invalidate a key already
enumerated. The embedding loader only classifies values left by processes that
bypass those preludes and never mutates the environment.

Removal is silent here and REPORTED later. A prelude runs before any logging
configuration, so a record it emits below WARNING reaches no handler, and a
stdlib-only leaf has no business configuring logging to fix that. So
``drop_inherited_lib_path`` only records, ``consume_dropped_inherited_lib_path``
hands the record once to the first reader that runs with logging in place (the
embedding loader), and ``dropped_inherited_lib_path`` keeps it for
``kirocrew doctor`` however many times it has been read.

The bundled native-library closure is declared here too, for readers that
cannot afford ``kiro_crew.embeddings``: ``_is_bundled_libs_dir`` takes the
platform directory names off it, and ``scripts/verify_vendored_payload.py``
reads it with ``ast.literal_eval`` in a build environment that installs build
tools but no runtime dependencies. ``embeddings`` re-exports it, so its own
readers are unaffected by where it is written.
"""

from __future__ import annotations

import os
import pathlib

_LIB_PATH_ENV = "LLAMA_CPP_LIB_PATH"
_LIBS_DIR_NAME = "llama_cpp_libs"
# The path tail shared by every install's bundled libs directories:
# ``.../kiro_crew/_vendor/llama_cpp_libs/<platform>``.
_BUNDLED_LIBS_PATH_TAIL = ("kiro_crew", "_vendor", _LIBS_DIR_NAME)

# The native-library closure every supported platform MUST ship, keyed by the
# `llama_cpp_libs/<dir>` name. `libllama` is the entry point ctypes opens by
# base name; the `libggml*` files are its NEEDED/@rpath dependencies, so a
# missing one fails the SAME way as a missing libllama — an unusable runtime.
#
# This is the single source of truth for "is the vendored payload complete",
# consumed by `embeddings.verify_vendored_libs()` and asserted per packaging
# lane. It is deliberately CODE rather than a build-time glob: a glob over
# whatever is on disk can only prove the files present are shippable, never that
# a file that should exist was silently dropped by a packaging rule — which is
# exactly the failure mode `MANIFEST.in`'s `global-exclude *.so` produced (it
# stripped precisely `libllama.so`, since every other Linux lib ends `.so.0` and
# the macOS/Windows libs are `.dylib`/`.dll`). `python -m build` builds the wheel
# FROM the sdist, so that one glob shipped a Linux wheel whose vendored
# llama_cpp could not load its own shared library, silently degrading vector
# memory to keyword search on every pip-installed Linux host.
#
# No BLAS entry on Linux is intentional, not an omission: upstream publishes no
# BLAS backend in its Linux CPU wheels (macOS gets `libggml-blas` only because
# it links the system Accelerate framework). The Linux `libggml-cpu` carries the
# optimized GEMM/repack kernels instead, so the CPU path is complete without it.
_REQUIRED_VENDORED_LIBS: dict[str, tuple[str, ...]] = {
    "linux_x86_64": (
        "libllama.so",
        "libggml.so.0",
        "libggml-base.so.0",
        "libggml-cpu.so.0",
        "libgomp-a34b3233.so.1.0.0",
    ),
    "linux_aarch64": (
        "libllama.so",
        "libggml.so.0",
        "libggml-base.so.0",
        "libggml-cpu.so.0",
        "libgomp-d22c30c5.so.1.0.0",
    ),
    "macos_arm64": (
        "libllama.dylib",
        "libggml.0.dylib",
        "libggml-base.0.dylib",
        "libggml-blas.0.dylib",
        "libggml-cpu.0.dylib",
        "libggml-metal.0.dylib",
    ),
    "macos_x86_64": (
        "libllama.dylib",
        "libggml.0.dylib",
        "libggml-base.0.dylib",
        "libggml-blas.0.dylib",
        "libggml-cpu.0.dylib",
        "libggml-metal.0.dylib",
    ),
    "win_amd64": (
        "llama.dll",
        "ggml.dll",
        "ggml-base.dll",
        "ggml-cpu.dll",
    ),
}

# The platforms this package ships libs for ARE the keys of that closure, so
# they are read off it rather than re-listed: a sixth platform added to the
# declaration is recognised here with no second edit, and the two can never
# disagree about what "a bundled directory" means.
_BUNDLED_LIBS_DIR_NAMES = frozenset(_REQUIRED_VENDORED_LIBS)

_dropped_inherited: str | None = None
# Whether that record still owes the one INFO line. Separate from the value
# because the value may legitimately be the empty string, and because the
# prelude that sets it cannot log.
_dropped_inherited_unreported = False


def _is_bundled_libs_dir(value: str) -> bool:
    """Whether *value* names SOME Kiro Crew install's bundled native-libs directory.

    Some install's, not only this one's: the shape is
    ``.../kiro_crew/_vendor/llama_cpp_libs/<platform>`` for a platform this
    package ships libs for. Kiro Crew never documents setting the override to a
    bundled directory, so a process that finds one in ``LLAMA_CPP_LIB_PATH``
    inherited it from a Kiro Crew process: an ancestor whose loader leaves the
    variable in the environment every child and every in-app-restart successor
    receives. It names THAT process's install, which need not be this one and
    may have been removed by an upgrade since.

    The shape decides on its own; the directory's contents cannot refine it. A
    complete bundled-shaped directory is a still-present PREVIOUS install's --
    an installer that keeps the last two versions leaves exactly that behind,
    and the leaked value names it -- as easily as an operator's extracted
    wheel, and honouring it loads a different version's native libs against
    this install's bindings. An operator's directory is one of their own, not
    a path of that shape; the incomplete-payload warning, the one remedy an
    operator might answer by extracting a wheel, says so. The CPU and MSVC
    refusals do not: a bundled directory is the very build they refused.
    """
    parts = pathlib.Path(value).parts
    return (
        len(parts) > len(_BUNDLED_LIBS_PATH_TAIL)
        and parts[-1] in _BUNDLED_LIBS_DIR_NAMES
        and parts[-1 - len(_BUNDLED_LIBS_PATH_TAIL) : -1] == _BUNDLED_LIBS_PATH_TAIL
    )


def operator_lib_path_override() -> str | None:
    """The operator's ``LLAMA_CPP_LIB_PATH``, or None when there is none.

    None when the variable is unset or empty, and when it holds a bundled
    directory (:func:`_is_bundled_libs_dir`): that is Kiro Crew's own value,
    inherited from another of its processes, not an operator asking for a
    different runtime. The loader and ``kirocrew doctor`` both read the
    override through this, so the two never disagree about which directory
    the libs came from.
    """
    value = os.environ.get(_LIB_PATH_ENV)
    if not value or _is_bundled_libs_dir(value):
        return None
    return value


def drop_inherited_lib_path() -> str | None:
    """Remove and record an inherited bundled or empty library-path value.

    Silent by design. This runs in the entry prelude, where the root logger is
    still at WARNING with no handlers and ``logging.lastResort`` drops anything
    below WARNING, so an INFO record emitted here would never reach a handler;
    configuring logging from a stdlib-only prelude is not this leaf's call to
    make either. The removal is only RECORDED, and
    :func:`consume_dropped_inherited_lib_path` hands the record once to the
    first reader that runs after logging is configured -- which is what turns
    it into the one INFO line.
    """
    global _dropped_inherited, _dropped_inherited_unreported

    if _LIB_PATH_ENV not in os.environ or operator_lib_path_override() is not None:
        return None

    removed = os.environ.pop(_LIB_PATH_ENV, None)
    if removed is None:
        return None
    _dropped_inherited = removed
    _dropped_inherited_unreported = True
    return removed


def dropped_inherited_lib_path() -> str | None:
    """Return the inherited library-path value removed during startup.

    Reading this never consumes the record, so ``kirocrew doctor`` still names
    the value after the INFO line has already been logged.
    """
    return _dropped_inherited


def consume_dropped_inherited_lib_path() -> str | None:
    """The removed value, handed to exactly one reader, for logging it once.

    Returns the recorded value to the first caller and None to every caller
    after it, so the removal is reported with ONE INFO line per process however
    many times the loader runs. ``None`` means nothing is pending, which an
    empty string -- a removed empty value, itself worth the line -- does not.
    """
    global _dropped_inherited_unreported

    if not _dropped_inherited_unreported:
        return None
    _dropped_inherited_unreported = False
    return _dropped_inherited
