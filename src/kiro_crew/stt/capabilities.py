"""What the installed whisper.cpp build can actually do, read from the build itself.

Separate module, importing neither numpy nor the rest of :mod:`kiro_crew.stt`, for
the same reason :mod:`kiro_crew.stt.limits` is: ``kirocrew doctor`` and the status
endpoint both need this answer, and neither should pull an array library to get it.

**Why a module exists for one string.** The obvious ways to ask "is this build
accelerated" all answer wrongly, in the unsafe direction:

- ``whisper_context_default_params()`` returns ``use_gpu=True`` and
  ``flash_attn=True`` on a CPU-only wheel. Measured on the packaged
  ``pywhispercpp`` for linux-aarch64: both are ``True`` while the build contains no
  GPU backend whatsoever. They are *requests*, honoured only if a backend was
  compiled in, so reading them back is reading our own wish.
- The presence of a GPU on the host says nothing either. The wheels PyPI publishes
  are described upstream as "Basic Pre-built CPU wheels"; CUDA, Vulkan, CoreML and
  OpenBLAS each need a different source build
  (https://github.com/absadiki/pywhispercpp#installation), so an NVIDIA card and a
  CPU-only wheel is the normal case, not an exotic one.
- The model name says nothing: ``large-v3-turbo`` on a CPU build and on a CUDA
  build differ by more than an order of magnitude in decode time.

``whisper_print_system_info()`` is the one value produced BY the compiled artifact,
listing the backends that were actually linked into it. It is therefore the only
honest source, and this module's whole job is to parse it and to refuse to guess
when it cannot be read.

**The section label IS the backend name.** This is the one thing about the format
that is easy to get backwards, and getting it backwards inverts the answer on two
of the three shipped platforms. Upstream builds the string by walking the ggml
backend registry and printing each registry's *name* as a section label, followed
by that registry's own feature flags (``whisper_print_system_info`` in
``src/whisper.cpp``)::

    s += "WHISPER : ";
    s += "VITISAI = "  + ... + " | ";
    s += "COREML = "   + ... + " | ";
    s += "OPENVINO = " + ... + " | ";
    for (i = 0; i < ggml_backend_reg_count(); i++) {
        s += ggml_backend_reg_name(reg);   // "CUDA" / "Vulkan" / "Metal" / "BLAS" / "CPU"
        s += " : ";
        for (; features->name; features++) { s += features->name; ... }
    }

So ``VITISAI``, ``COREML`` and ``OPENVINO`` are the ONLY genuine ``KEY = VALUE``
backend flags. Every other backend appears solely as a label, and its features are
named something else entirely -- CUDA's are ``ARCHS``, ``USE_GRAPHS``,
``FORCE_MMQ``. There is no ``CUDA = 1`` token in any build's output.

Two real captures, both from reviewers on the hosts in question rather than
synthesized here. Stock CPU wheel on Windows Server 2025 x64::

    WHISPER : COREML = 0 | OPENVINO = 0 | CPU : SSE3 = 1 | SSSE3 = 1 | AVX = 1 |
    AVX2 = 1 | F16C = 1 | FMA = 1 | OPENMP = 1 | REPACK = 1 |

The same wheel on macOS arm64 (Apple M5 Pro), which links Metal and Apple's BLAS::

    WHISPER : COREML = 0 | OPENVINO = 0 | MTL : EMBED_LIBRARY = 1 | CPU : NEON = 1 |
    ARM_FMA = 1 | FP16_VA = 1 | DOTPROD = 1 | ACCELERATE = 1 | REPACK = 1 |

Note ``MTL`` rather than ``Metal``, and note that Apple's BLAS is a CPU-registry
feature (``ACCELERATE``) rather than a ``BLAS :`` section. Both are why label
matching is a table of accepted spellings and not a title-case guess.

A caveat this module cannot fix: the string is COMPILE-TIME information. Which
backend a load actually *uses* is stated only by the loader's own
``using <name> backend`` lines, reachable through ``whisper_log_set`` during a
load. This is the honest answer to "what was linked", not to "what ran".
"""

from __future__ import annotations

import logging
import platform
from dataclasses import dataclass, field
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: ggml backend-registry names, as they appear as SECTION LABELS, mapped to the
#: canonical name this repo reports. Strongest first: a build links several
#: registries (a CUDA build still registers CPU) and the strongest one decides
#: decode cost.
#:
#: Matched case-insensitively against the label text, because upstream spells them
#: mixed-case (``Vulkan``, ``Metal``, ``ROCm``) and Apple's Metal registry
#: shortens to ``MTL``. Every spelling here came from a real build's output or
#: from the registry's own ``ggml_backend_reg_name``; none is a guess at what a
#: label might look like.
_BACKEND_SECTIONS: tuple[tuple[str, str], ...] = (
    ("CUDA", "cuda"),
    # A HIP build registers itself as ROCm, not CUDA, so a user on one would
    # otherwise be told their GPU build links no GPU.
    ("ROCM", "rocm"),
    ("MUSA", "musa"),
    ("METAL", "metal"),
    ("MTL", "metal"),
    ("VULKAN", "vulkan"),
    ("SYCL", "sycl"),
    ("CANN", "cann"),
    ("OPENCL", "opencl"),
    ("BLAS", "blas"),
)

#: Section labels that are NOT backends: the whisper-level header, and the CPU
#: registry that every build has. Listed so an unrecognised label can be reported
#: as such instead of being silently lumped in with these.
_NON_BACKEND_SECTIONS = frozenset({"WHISPER", "CPU"})

#: The three genuine ``KEY = VALUE`` backend flags. Everything else in the string
#: is either a section label (see :data:`_BACKEND_SECTIONS`) or a feature of the
#: registry whose section it sits in.
#:
#: ``ACCELERATE`` is here rather than among the CPU features because it is Apple's
#: BLAS: a macOS build prints it as a CPU-registry feature, and the loader then
#: logs ``using BLAS backend``. Reporting it as a mere instruction-set flag would
#: understate what is linked. It sits last so Metal outranks it on a Mac, which is
#: the order the loader itself picks.
_BACKEND_FLAGS: tuple[tuple[str, str], ...] = (
    ("COREML", "coreml"),
    ("OPENVINO", "openvino"),
    ("VITISAI", "vitisai"),
    ("ACCELERATE", "blas"),
)

#: Backends that accelerate the ENCODER only; the autoregressive decoder still
#: runs on CPU. Named so a report can say "partially accelerated" rather than
#: implying a build is as fast as a full GPU one. Whisper's encoder is the larger
#: share of a short utterance and the smaller share of a long one, so the
#: distinction changes what a user should expect.
_ENCODER_ONLY_BACKENDS = frozenset({"coreml", "openvino"})

#: CPU capability flags worth reporting when nothing better is linked in. These do
#: not change the backend, but they are the difference between a wheel built for
#: this machine's instruction set and a lowest-common-denominator one, which is a
#: real factor in CPU decode time and is otherwise invisible.
_CPU_FEATURE_FLAGS: tuple[str, ...] = (
    "AVX",
    "AVX2",
    "AVX512",
    "AVX512_VBMI",
    "AVX512_VNNI",
    "FMA",
    "NEON",
    "ARM_FMA",
    # Apple-side ARM features, from the macOS capture in the module docstring.
    # Dropped before, which made the diagnostic on a Mac shorter than the truth.
    "FP16_VA",
    "DOTPROD",
    "MATMUL_INT8",
    "SVE",
    "F16C",
    "SSE3",
    "SSSE3",
    "OPENMP",
    "REPACK",
    "LLAMAFILE",
)

#: What :attr:`Capabilities.backend` is when the build could not be interrogated.
#: A distinct value rather than ``"cpu"``: reporting an unreadable build as CPU
#: would be a guess that happens to be right most of the time, and the one case it
#: is wrong is a user whose GPU build we would then tell them to stop expecting.
BACKEND_UNKNOWN = "unknown"

#: What it is when the build was read and contains nothing but scalar/SIMD CPU.
BACKEND_CPU = "cpu"


@dataclass(frozen=True)
class Capabilities:
    """The acceleration the loaded native build provides, as read from the build.

    ``accelerated`` is deliberately false for :data:`BACKEND_UNKNOWN`. Everything
    in this module exists to avoid claiming an acceleration that is not there, and
    an unknown build is exactly the case where a claim would be unfounded.
    """

    #: The strongest linked backend: one of the ``BACKEND_*`` constants above
    #: other than ``auto``, or :data:`BACKEND_UNKNOWN`.
    backend: str = BACKEND_UNKNOWN
    #: Whether *anything* faster than scalar CPU was linked in. False when unknown.
    accelerated: bool = False
    #: True when the linked backend covers the encoder only, so the decoder still
    #: runs on CPU and long utterances gain proportionally less.
    encoder_only: bool = False
    #: The CPU instruction-set and threading flags the build reports, in the order
    #: of :data:`_CPU_FEATURE_FLAGS`. Advisory; for diagnostics, not for decisions.
    cpu_features: tuple[str, ...] = ()
    #: Every parsed ``KEY = VALUE`` pair, so a diagnostic can show a flag this
    #: module does not model yet without needing a release to teach it one.
    flags: Mapping[str, bool] = field(default_factory=dict)
    #: The section labels the build printed, in order -- i.e. the ggml backend
    #: registries it linked. Carried because this is where the backend name comes
    #: from, so a bug report can show the evidence rather than just the verdict.
    sections: tuple[str, ...] = ()
    #: ``whisper_print_system_info()`` verbatim, or ``""``. The audit trail for
    #: every field above, and the thing to paste into a bug report.
    raw: str = ""
    #: One sentence for a human. States what was found, or why nothing was.
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.backend != BACKEND_UNKNOWN


def _parse(info: str) -> tuple[dict[str, bool], tuple[str, ...]]:
    """Split ``A : K = 1 | K = 0 | B : K = 1`` into its flags and its section labels.

    Returns ``({"K": True, ...}, ("A", "B"))``. Both halves are load-bearing: the
    flags carry the three genuine backend flags plus every registry's features, and
    the LABELS carry the backend names, which is the half that decides the answer on
    most platforms (see the module docstring).

    A label is recognised as the text before a ``:`` in a token that also has an
    ``=``, or as a bare ``X :`` token with no flags of its own. Order is preserved
    so the caller can report which registry a build actually walked.

    Tolerant by construction: an unrecognised separator, a missing value or a
    non-numeric one drops that one token and keeps the rest, because the string is a
    debug aid upstream and its shape is not a contract. A parse that threw would
    turn a cosmetic format change into a broken voice panel.
    """
    flags: dict[str, bool] = {}
    sections: list[str] = []

    def note(label: str) -> None:
        name = label.strip().upper()
        if name and name not in sections:
            sections.append(name)

    for chunk in info.split("|"):
        token = chunk.strip()
        if not token:
            continue
        if "=" not in token:
            # A label whose registry reported no features at all still tells us the
            # registry was linked, which is the whole answer for such a build.
            note(token.rstrip(":"))
            continue
        name, _, value = token.rpartition("=")
        name = name.strip()
        if ":" in name:
            label, _, name = name.rpartition(":")
            note(label)
            name = name.strip()
        value = value.strip()
        if not name or value not in {"0", "1"}:
            continue
        flags[name.upper()] = value == "1"
    return flags, tuple(sections)


def _system_info(binding: Any | None = None) -> str:
    """``whisper_print_system_info()``, or ``""`` when it cannot be reached.

    *binding* is injectable so a test can exercise a CUDA or Metal build's string
    on a host that has neither. Passing ``None`` asks for the real extension
    module, which is imported here rather than at module scope because importing it
    dlopens a native library (measured at 209 ms cold) and the CLI must not pay
    that to print its help.
    """
    if binding is None:
        try:
            import _pywhispercpp as binding  # type: ignore[no-redef]
        except Exception as exc:  # pragma: no cover (host-specific loader failures)
            logger.debug("Cannot read whisper build capabilities: %s", exc)
            return ""
    reader = getattr(binding, "whisper_print_system_info", None)
    if reader is None:
        return ""
    try:
        info = reader()
    except Exception as exc:  # pragma: no cover (host-specific loader failures)
        logger.debug("whisper_print_system_info() failed: %s", exc)
        return ""
    # Older bindings print to stdout and return None. That is the unknown case:
    # there is nothing to parse, and inventing "cpu" from it is the guess this
    # module refuses to make.
    return info if isinstance(info, str) else ""


def detect(binding: Any | None = None) -> Capabilities:
    """Read the native build's linked backends. Cheap; safe to call per request.

    Not cached here on purpose. The call is a string format in the native library,
    the caller that polls it is a status endpoint answering a human, and a cache
    would have to be invalidated on a reinstall that replaced the wheel underneath
    a running gateway -- a real thing during development, and the one moment a
    stale answer is most misleading.
    """
    info = _system_info(binding)
    if not info:
        return Capabilities(
            detail=(
                "the speech backend could not be interrogated, so acceleration is "
                "unknown; decode speed here is whatever the installed build provides"
            )
        )
    flags, sections = _parse(info)
    if not flags:
        # No `KEY = VALUE` pair anywhere. Every real build prints at least the three
        # whisper-level flags, so zero of them means the format changed rather than
        # that the build is bare -- and concluding "cpu" from it would be the guess
        # this module exists to refuse. Keyed on flags rather than on "nothing
        # parsed at all", because a label alone is still not a format we understand:
        # `WHISPER : something entirely different` yields the label `WHISPER` and no
        # knowledge whatsoever.
        logger.debug("Unparseable whisper system info: %r", info)
        return Capabilities(
            raw=info.strip(),
            detail=(
                "the speech backend reported its capabilities in a format this "
                "version does not understand, so acceleration is unknown"
            ),
        )
    backend = BACKEND_CPU
    encoder_only = False
    # Section labels first: they name the registries this build actually linked, and
    # a full GPU backend outranks the encoder-only flags below.
    for label, name in _BACKEND_SECTIONS:
        if label in sections:
            backend = name
            break
    else:
        for flag, name in _BACKEND_FLAGS:
            if flags.get(flag):
                backend = name
                encoder_only = name in _ENCODER_ONLY_BACKENDS
                break
    features = tuple(f for f in _CPU_FEATURE_FLAGS if flags.get(f))
    # A label this version does not model. Reported rather than swallowed: it is
    # evidence of a linked registry, and staying silent about it is how the previous
    # version of this module came to report every Mac as CPU-only.
    known_labels = {label for label, _ in _BACKEND_SECTIONS} | _NON_BACKEND_SECTIONS
    unmodelled = tuple(x for x in sections if x not in known_labels)
    if backend == BACKEND_CPU:
        detail = "CPU only: this speech build links no GPU or BLAS backend" + (
            f" (CPU features: {', '.join(features)})" if features else ""
        )
    elif encoder_only:
        detail = (
            f"{backend} accelerates the encoder; the decoder still runs on CPU, "
            "so long utterances gain less than short ones"
        )
    else:
        detail = f"{backend} acceleration is linked into this speech build"
    if unmodelled:
        detail += (
            f"; this build also links {', '.join(unmodelled)}, which this version "
            "does not recognise -- worth reporting"
        )
    return Capabilities(
        backend=backend,
        accelerated=backend != BACKEND_CPU,
        encoder_only=encoder_only,
        cpu_features=features,
        flags=flags,
        sections=sections,
        raw=info.strip(),
        detail=detail,
    )


def host_summary() -> dict[str, str]:
    """Non-sensitive host facts a diagnostic pairs the capabilities with.

    Deliberately narrow: OS, machine architecture and the Python build. No
    hostname, no user, no paths -- this travels into status payloads and bug
    reports, and the brief for this work forbids putting anything identifying
    there.
    """
    return {
        "os": platform.system(),
        "arch": platform.machine(),
        "python": platform.python_version(),
    }
