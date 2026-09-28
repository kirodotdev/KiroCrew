"""What the speech build reports about itself, and the refusal to guess when it cannot.

The property under test is one-directional: this module may never claim an
acceleration the build does not have. Every case here is therefore written from the
unsafe side -- an unreadable build, an unparseable string, a requested backend that
is absent -- because the safe side (a CUDA build reporting CUDA) was never the risk.

Every system-info string here is a VERBATIM CAPTURE from a real build, not a shape
assembled from an idea of the format. That distinction is the reason this file exists
in its current form: the first version synthesized ``CUDA = 1`` and ``METAL = 1``
tokens inside the ``CPU :`` section, which no whisper.cpp build emits, and so it
passed while ``detect()`` reported every Mac and every CUDA build as CPU-only. The
parser must be pinned against the format, never against an assumption about it.
"""

from __future__ import annotations

import pytest

from kiro_crew.stt import capabilities as caps

#: The verbatim reply of the packaged CPU-only wheel on linux-aarch64. Kept as a
#: literal because it is evidence: this exact string is why the quantized catalog
#: rows exist.
CPU_ONLY_INFO = (
    "WHISPER : COREML = 0 | OPENVINO = 0 | CPU : NEON = 1 | ARM_FMA = 1 | "
    "OPENMP = 1 | REPACK = 1 | "
)

#: Verbatim from the stock wheel on Windows Server 2025 x64. The second real CPU
#: capture, kept alongside the aarch64 one because the two differ in every feature
#: flag and a parser that only ever saw one of them is only half pinned.
WINDOWS_CPU_INFO = (
    "WHISPER : COREML = 0 | OPENVINO = 0 | CPU : SSE3 = 1 | SSSE3 = 1 | AVX = 1 | "
    "AVX2 = 1 | F16C = 1 | FMA = 1 | OPENMP = 1 | REPACK = 1 | "
)

#: Verbatim from the same wheel on macOS arm64 (Apple M5 Pro). The case that
#: falsified the original design: Metal appears ONLY as the section label ``MTL``,
#: and Apple's BLAS only as the CPU-registry feature ``ACCELERATE``.
MACOS_METAL_INFO = (
    "WHISPER : COREML = 0 | OPENVINO = 0 | MTL : EMBED_LIBRARY = 1 | CPU : NEON = 1 | "
    "ARM_FMA = 1 | FP16_VA = 1 | DOTPROD = 1 | ACCELERATE = 1 | REPACK = 1 | "
)

#: Upstream shape for a CUDA build: the registry name is the label, and its features
#: are ``ARCHS``/``USE_GRAPHS`` -- there is no ``CUDA = 1`` token anywhere.
CUDA_INFO = (
    "WHISPER : COREML = 0 | OPENVINO = 0 | CUDA : ARCHS = 890 | USE_GRAPHS = 1 | "
    "CPU : SSE3 = 1 | AVX = 1 | AVX2 = 1 | FMA = 1 | "
)

VULKAN_INFO = "WHISPER : COREML = 0 | OPENVINO = 0 | Vulkan : MATRIX_CORES = 1 | CPU : AVX2 = 1 | "

#: A HIP build registers as ROCm, not CUDA.
ROCM_INFO = "WHISPER : COREML = 0 | OPENVINO = 0 | ROCm : ARCHS = gfx1100 | CPU : AVX2 = 1 | "

#: CoreML is one of the three genuine flags, so it is found as a flag rather than a
#: label -- which is why it was the only accelerated case the original design got
#: right.
COREML_INFO = "WHISPER : COREML = 1 | OPENVINO = 0 | CPU : NEON = 1 | ARM_FMA = 1 | "


class _Binding:
    """Stands in for ``_pywhispercpp``.

    A class rather than a lambda because the production reader probes for the
    ATTRIBUTE (an older binding may not have it), and that probe is part of what is
    being tested.
    """

    def __init__(self, info: object, *, raises: bool = False, absent: bool = False) -> None:
        self._info = info
        self._raises = raises
        if not absent:
            self.whisper_print_system_info = self._read  # type: ignore[assignment]

    def _read(self) -> object:
        if self._raises:
            raise RuntimeError("native call failed")
        return self._info


class TestDetectReadsTheBuild:
    def test_the_packaged_cpu_wheel_reports_no_acceleration(self):
        found = caps.detect(_Binding(CPU_ONLY_INFO))
        assert found.backend == caps.BACKEND_CPU
        assert found.accelerated is False
        assert found.known is True
        # The CPU features are reported, because a wheel built for this machine's
        # instruction set and a lowest-common-denominator one differ in decode time
        # and are otherwise indistinguishable.
        assert found.cpu_features == ("NEON", "ARM_FMA", "OPENMP", "REPACK")
        assert "CPU only" in found.detail

    @pytest.mark.parametrize(
        ("info", "expected"),
        [
            (CUDA_INFO, "cuda"),
            (VULKAN_INFO, "vulkan"),
            (ROCM_INFO, "rocm"),
            (MACOS_METAL_INFO, "metal"),
            (COREML_INFO, "coreml"),
        ],
    )
    def test_a_linked_backend_is_named(self, info: str, expected: str):
        found = caps.detect(_Binding(info))
        assert found.backend == expected
        assert found.accelerated is True

    def test_an_encoder_only_backend_says_so(self):
        """CoreML accelerates the encoder; the decoder stays on CPU.

        Reported separately so a surface cannot imply a CoreML build is as fast as a
        Metal one. The distinction changes what a user should expect from a long
        utterance, where the decoder is the larger share.
        """
        assert caps.detect(_Binding(COREML_INFO)).encoder_only is True
        assert caps.detect(_Binding(CUDA_INFO)).encoder_only is False

    def test_a_cuda_build_is_not_mistaken_for_cpu_by_its_cpu_flags(self):
        """A GPU build still reports its CPU features; the GPU must win.

        The regression this pins is an ordering one: scanning CPU feature flags
        first, or taking the last match instead of the strongest, reports a CUDA
        build as CPU because ``AVX2`` is also set.
        """
        found = caps.detect(_Binding(CUDA_INFO))
        assert found.backend == "cuda"
        assert "AVX2" in found.cpu_features


class TestDetectRefusesToGuess:
    """The whole point of the module: unknown is a real answer."""

    @pytest.mark.parametrize(
        "binding",
        [
            _Binding("", absent=False),
            _Binding(None),
            _Binding("!!! not a system info string !!!"),
            _Binding(CPU_ONLY_INFO, raises=True),
            _Binding(CPU_ONLY_INFO, absent=True),
        ],
        ids=["empty", "returns-none", "unparseable", "call-raises", "attribute-absent"],
    )
    def test_an_uninterrogable_build_is_unknown_and_never_accelerated(self, binding: _Binding):
        found = caps.detect(binding)
        assert found.backend == caps.BACKEND_UNKNOWN
        assert found.known is False
        # The load-bearing assertion. Reporting an unreadable build as accelerated
        # would tell a user to stop expecting a speedup they are not getting; there
        # is no case in which a guess here is better than saying so.
        assert found.accelerated is False

    def test_an_unparseable_string_is_not_read_as_a_bare_cpu_build(self):
        """Zero parsed flags means the format changed, not that the build is bare.

        Every real whisper.cpp build reports at least its CPU feature flags, so an
        empty parse is a format change. Concluding ``cpu`` from it would be right
        most of the time and wrong in exactly the case that matters -- a GPU build
        whose reporting format moved on.
        """
        found = caps.detect(_Binding("WHISPER : something entirely different"))
        assert found.backend == caps.BACKEND_UNKNOWN
        assert found.raw  # the unparsed text is still carried, for a bug report


class TestFlagParsing:
    def test_a_token_carries_both_its_label_and_its_flag(self):
        """``CPU : NEON = 1`` is one token holding a section label and a flag.

        BOTH halves matter, which is the correction this file records: an earlier
        version kept the flag and dropped the label, and the label is where every
        backend except CoreML/OpenVINO/VITISAI is named.
        """
        found = caps.detect(_Binding(CPU_ONLY_INFO))
        assert found.flags["NEON"] is True
        assert found.flags["COREML"] is False
        assert found.sections == ("WHISPER", "CPU")

    def test_the_macos_capture_is_metal_and_not_cpu(self):
        """The regression this whole module was rewritten for.

        On macOS the ONLY mention of Metal is the ``MTL`` section label. Reported as
        CPU-only, this told every Mac user that a build measuring RTF 0.070 could not
        keep up with speech, and told anyone who set ``local_backend = metal`` to
        install the build they were already running.
        """
        found = caps.detect(_Binding(MACOS_METAL_INFO))
        assert found.backend == "metal"
        assert found.accelerated is True
        assert found.encoder_only is False
        # Apple's ARM features are reported rather than dropped from the diagnostic.
        assert "FP16_VA" in found.cpu_features
        assert "DOTPROD" in found.cpu_features

    def test_a_gpu_build_names_no_flag_for_its_own_backend(self):
        """Pins the format itself, so a future fixture cannot drift back.

        If someone re-synthesizes a ``CUDA = 1`` token, this fails and says why.
        """
        assert "CUDA = 1" not in CUDA_INFO
        assert "METAL = 1" not in MACOS_METAL_INFO
        assert caps.detect(_Binding(CUDA_INFO)).flags.get("CUDA") is None

    def test_a_registry_with_no_features_is_still_a_linked_backend(self):
        """A bare ``Vulkan :`` token, printed by a registry reporting no features."""
        found = caps.detect(_Binding("WHISPER : COREML = 0 | Vulkan : | CPU : AVX2 = 1 |"))
        assert found.backend == "vulkan"

    def test_an_unmodelled_label_is_reported_rather_than_swallowed(self):
        """The failure mode that produced the macOS bug, guarded against recurrence.

        A registry this version does not know must not read as CPU-only in silence.
        """
        found = caps.detect(
            _Binding("WHISPER : COREML = 0 | FutureNPU : UNITS = 4 | CPU : AVX2 = 1 |")
        )
        assert "FutureNPU".upper() in found.detail.upper()

    def test_a_malformed_token_drops_itself_and_not_the_rest(self):
        found = caps.detect(
            _Binding("WHISPER : COREML = 0 | GARBAGE | CUDA : ARCHS = 890 | AVX2 = whatever |")
        )
        assert found.backend == "cuda"
        assert "AVX2" not in found.flags


class TestHostSummaryCarriesNothingIdentifying:
    def test_it_reports_only_os_arch_and_python(self):
        """This travels into status payloads and pasted bug reports.

        Pinned as an exact key set rather than "does not contain a hostname",
        because the way identifying data arrives is by someone adding a field.
        """
        assert set(caps.host_summary()) == {"os", "arch", "python"}
