"""The embedding runtime's bundled llama.cpp libraries: which directory loads, and when.

The platform's libs directory name, the in-process seam that hands the vendored
loader its directory without touching the environment, and the two gates that
refuse the bundled libraries before the native import: the Linux x86-64
CPU-feature gate, which keeps a missing instruction from killing the process, and
the Windows MSVC runtime gate, which names the missing redistributable instead of a
bare ``WinError 126``. No real library is loaded.
"""

from __future__ import annotations

import importlib
import io
import os
import sys
from pathlib import Path
from types import ModuleType

import embeddings_helpers
import pytest

import kiro_crew.embeddings as embeddings_mod
from kiro_crew.embeddings import _platform_libs_dirname

# The shared autouse fixture, bound here by name so pytest registers it for this module.
_reset_embedding_singletons = embeddings_helpers._reset_embedding_singletons


# ═══════════════════════════════════════════════════════════════════════════
# _platform_libs_dirname
# ═══════════════════════════════════════════════════════════════════════════


class TestPlatformLibsDirname:
    @pytest.mark.parametrize(
        ("platform_str", "machine", "expected"),
        [
            ("linux", "x86_64", "linux_x86_64"),
            ("linux", "amd64", "linux_x86_64"),
            ("linux", "aarch64", "linux_aarch64"),
            ("linux", "arm64", "linux_aarch64"),
            ("linux", "AARCH64", "linux_aarch64"),  # machine is lowercased
            ("darwin", "arm64", "macos_arm64"),
            ("darwin", "x86_64", "macos_x86_64"),  # Intel Mac (universal DMG x64 slice)
            ("win32", "amd64", "win_amd64"),
            ("win32", "x86_64", "win_amd64"),
        ],
    )
    def test_supported_platforms(
        self, monkeypatch, platform_str: str, machine: str, expected: str
    ) -> None:
        monkeypatch.setattr("kiro_crew.embeddings.sys.platform", platform_str)
        monkeypatch.setattr("kiro_crew.embeddings.platform.machine", lambda: machine)
        assert _platform_libs_dirname() == expected

    @pytest.mark.parametrize(
        ("platform_str", "machine"),
        [
            ("linux", "ppc64le"),
            ("darwin", "ppc"),
            ("win32", "arm64"),
            ("sunos5", "x86_64"),
            ("aix", "aarch64"),
        ],
    )
    def test_unsupported_combos_return_none(
        self, monkeypatch, platform_str: str, machine: str
    ) -> None:
        monkeypatch.setattr("kiro_crew.embeddings.sys.platform", platform_str)
        monkeypatch.setattr("kiro_crew.embeddings.platform.machine", lambda: machine)
        assert _platform_libs_dirname() is None


class TestLibPathSeam:
    """The bundled directory travels to the vendored loader in-process, never via the environment."""

    @pytest.fixture(autouse=True)
    def _no_seam_outlives_the_test(self, _floor_monkeypatch) -> None:
        _floor_monkeypatch.setitem(sys.modules, embeddings_mod._LIB_PATH_SEAM, object())
        _floor_monkeypatch.delitem(sys.modules, embeddings_mod._LIB_PATH_SEAM)

    def test_publishing_registers_the_directory_and_touches_no_environment(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv(embeddings_mod._LIB_PATH_ENV, raising=False)
        libs = tmp_path / "linux_x86_64"

        embeddings_mod._publish_lib_path_for_import(libs)

        seam = sys.modules[embeddings_mod._LIB_PATH_SEAM]
        assert seam.LIBS_DIR == str(libs)
        assert embeddings_mod._LIB_PATH_ENV not in os.environ

    def test_publishing_none_removes_the_seam(self, tmp_path: Path) -> None:
        embeddings_mod._publish_lib_path_for_import(tmp_path)
        embeddings_mod._publish_lib_path_for_import(None)
        assert embeddings_mod._LIB_PATH_SEAM not in sys.modules
        # Idempotent: removing an absent seam is not an error.
        embeddings_mod._publish_lib_path_for_import(None)

    def test_republishing_replaces_the_directory(self, tmp_path: Path) -> None:
        embeddings_mod._publish_lib_path_for_import(tmp_path / "first")
        embeddings_mod._publish_lib_path_for_import(tmp_path / "second")
        assert sys.modules[embeddings_mod._LIB_PATH_SEAM].LIBS_DIR == str(tmp_path / "second")


def _stub_bundled_linux_libs(root: Path) -> None:
    libs = root / embeddings_mod._LIBS_DIR_NAME / "linux_x86_64"
    libs.mkdir(parents=True)
    for name in embeddings_mod._REQUIRED_VENDORED_LIBS["linux_x86_64"]:
        (libs / name).write_bytes(b"\x7fELF")


_RECORDING_LLAMA_CPP = '''\
"""Stand-in for the vendored package: records both lib-path channels at import.

The real ``_vendor/llama_cpp/llama_cpp.py`` reads the host's process-local seam
first and ``LLAMA_CPP_LIB_PATH`` second (its ``kiro_crew DIVERGENCE FROM
UPSTREAM``); this records what each held while the package was being imported.
"""
import os
import subprocess
import sys

SEAM_AT_IMPORT = getattr(sys.modules.get("_kiro_crew_llama_cpp_lib_path"), "LIBS_DIR", None)
ENV_AT_IMPORT = os.environ.get("LLAMA_CPP_LIB_PATH")
# What a child spawned WHILE the import runs inherits: the environment is
# snapshotted at spawn, so a variable set only for the import still reaches it.
CHILD_ENV_AT_IMPORT = subprocess.run(
    [sys.executable, "-c", "import os; print(os.environ.get('LLAMA_CPP_LIB_PATH', ''))"],
    capture_output=True,
    encoding="utf-8",
    check=True,
).stdout.strip() or None


class Llama:
    pass
'''


def _install_recording_llama_cpp(monkeypatch, vendor: Path) -> None:
    """Put a stand-in ``llama_cpp`` package on the vendor path the loader prepends.

    The vendored loader reads the seam and ``LLAMA_CPP_LIB_PATH`` while
    ``llama_cpp`` is being imported, so a stand-in that is to observe them must
    run at import time too -- a module pre-seeded into ``sys.modules`` never
    executes. Any ``llama_cpp`` module already imported is evicted for the test,
    and the stand-in and the seam are evicted after it, so none outlives the test.
    """
    package = vendor / "llama_cpp"
    package.mkdir()
    (package / "__init__.py").write_text(_RECORDING_LLAMA_CPP, encoding="utf-8")
    for name in [n for n in sys.modules if n == "llama_cpp" or n.startswith("llama_cpp.")]:
        monkeypatch.delitem(sys.modules, name)
    # The seam the loader registers is evicted too, whether or not one is
    # already present from an earlier load in this process.
    monkeypatch.setitem(sys.modules, embeddings_mod._LIB_PATH_SEAM, object())
    monkeypatch.delitem(sys.modules, embeddings_mod._LIB_PATH_SEAM)
    # setitem then delitem: monkeypatch records a teardown action for the key
    # even when it was absent, so the stand-in the import adds is removed again.
    monkeypatch.setitem(sys.modules, "llama_cpp", object())
    monkeypatch.delitem(sys.modules, "llama_cpp")
    monkeypatch.setattr(sys, "path", list(sys.path))
    importlib.invalidate_caches()


def _bundled_libs_dir_of_another_install(root: Path, *, present: bool) -> Path:
    """A different install's bundled libs directory, shaped as a loader leaves it in the environment."""
    libs = (
        root
        / "previous-install"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "kiro_crew"
        / "_vendor"
        / "llama_cpp_libs"
        / "linux_x86_64"
    )
    if present:
        libs.mkdir(parents=True)
        for name in embeddings_mod._REQUIRED_VENDORED_LIBS["linux_x86_64"]:
            (libs / name).write_bytes(b"\x7fELF")
    return libs


def _load_bundled_linux_llama(monkeypatch, vendor: Path, cpu_probe):
    monkeypatch.setattr(embeddings_mod, "_VENDOR_DIR", vendor)
    monkeypatch.setattr(embeddings_mod, "_platform_libs_dirname", lambda: "linux_x86_64")
    monkeypatch.setattr(embeddings_mod, "_linux_x86_64_cpu_flags", cpu_probe)
    embeddings_mod._load_llama_class.cache_clear()
    try:
        result = embeddings_mod._load_llama_class()
        active_lib_path = os.environ.get(embeddings_mod._LIB_PATH_ENV)
        return result, active_lib_path
    finally:
        embeddings_mod._load_llama_class.cache_clear()


class TestBundledLinuxX86CpuGate:
    def test_cpuinfo_parser_normalizes_sse3_and_intersects_processors(self, tmp_path: Path) -> None:
        cpuinfo = tmp_path / "cpuinfo"
        cpuinfo.write_text(
            "processor: 0\nflags: pni ssse3 avx avx2 bmi2 f16c fma\n\n"
            "processor: 1\nflags: pni ssse3 avx bmi2 f16c fma\n",
            encoding="utf-8",
        )

        flags = embeddings_mod._linux_x86_64_cpu_flags(cpuinfo)

        assert flags is not None
        assert "sse3" in flags
        assert "avx" in flags
        assert "avx2" not in flags

    def test_unreadable_cpuinfo_is_unknown(self, tmp_path: Path) -> None:
        assert embeddings_mod._linux_x86_64_cpu_flags(tmp_path / "missing") is None

    def test_compatible_cpu_continues_to_native_import(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.delenv(embeddings_mod._LIB_PATH_ENV, raising=False)
        _stub_bundled_linux_libs(tmp_path)
        _install_recording_llama_cpp(monkeypatch, tmp_path)

        result, active_lib_path = _load_bundled_linux_llama(
            monkeypatch,
            tmp_path,
            lambda: embeddings_mod._LINUX_X86_64_REQUIRED_CPU_FLAGS,
        )

        stand_in = sys.modules["llama_cpp"]
        assert result is stand_in.Llama
        # The vendored loader was handed the bundled directory through the
        # process-local seam while it imported ...
        assert stand_in.SEAM_AT_IMPORT == str(
            tmp_path / embeddings_mod._LIBS_DIR_NAME / "linux_x86_64"
        )
        # ... and the environment never held it, not even during the import:
        # a child spawned in that window -- an MCP server, a session, the
        # gateway an in-app restart execs into -- snapshots os.environ, and one
        # with its own llama-cpp bindings would load this install's libs.
        assert stand_in.ENV_AT_IMPORT is None
        assert (
            stand_in.CHILD_ENV_AT_IMPORT is None
        ), "a child spawned during the import inherited it"
        assert active_lib_path is None
        assert embeddings_mod._LIB_PATH_ENV not in os.environ

    @pytest.mark.parametrize("previous_install_present", [False, True])
    def test_inherited_bundled_lib_path_is_ignored_for_this_installs_loader(
        self, tmp_path: Path, monkeypatch, caplog, previous_install_present: bool
    ) -> None:
        """A loader reached without an entry prelude uses its own bundled directory.

        The stale value remains observable to prove the loader performs no
        environment write, while the process-local seam and CPU probe prove it
        is not honoured as an operator override.
        """
        _stub_bundled_linux_libs(tmp_path)
        inherited = _bundled_libs_dir_of_another_install(tmp_path, present=previous_install_present)
        monkeypatch.setenv(embeddings_mod._LIB_PATH_ENV, str(inherited))
        _install_recording_llama_cpp(monkeypatch, tmp_path)
        cpu_probes: list[bool] = []

        def counting_cpu_probe():
            cpu_probes.append(True)
            return embeddings_mod._LINUX_X86_64_REQUIRED_CPU_FLAGS

        with caplog.at_level("INFO", logger=embeddings_mod.__name__):
            result, active_lib_path = _load_bundled_linux_llama(
                monkeypatch, tmp_path, counting_cpu_probe
            )

        stand_in = sys.modules["llama_cpp"]
        assert result is stand_in.Llama
        assert stand_in.SEAM_AT_IMPORT == str(
            tmp_path / embeddings_mod._LIBS_DIR_NAME / "linux_x86_64"
        )
        assert stand_in.ENV_AT_IMPORT == str(inherited)
        assert stand_in.CHILD_ENV_AT_IMPORT == str(inherited)
        assert cpu_probes, "the bundled CPU gate must apply to the bundled libs"
        assert active_lib_path == str(inherited), "the loader must not write the environment"
        assert str(inherited) in caplog.text
        assert "left in the environment" in caplog.text

    @pytest.mark.parametrize("why_unusable", ["unsupported-platform", "libs-dir-absent"])
    def test_inherited_bundled_lib_path_stays_untouched_when_runtime_is_unusable(
        self, tmp_path: Path, monkeypatch, caplog, why_unusable: str
    ) -> None:
        """Every early return leaves a bundled-shaped environment value unchanged."""
        inherited = _bundled_libs_dir_of_another_install(tmp_path, present=True)
        monkeypatch.setenv(embeddings_mod._LIB_PATH_ENV, str(inherited))
        monkeypatch.setattr(embeddings_mod, "_VENDOR_DIR", tmp_path / "no-libs-here")
        if why_unusable == "unsupported-platform":
            monkeypatch.setattr(embeddings_mod, "_platform_libs_dirname", lambda: None)
        else:
            monkeypatch.setattr(embeddings_mod, "_platform_libs_dirname", lambda: "linux_x86_64")
        embeddings_mod._load_llama_class.cache_clear()
        try:
            with caplog.at_level("INFO", logger=embeddings_mod.__name__):
                result = embeddings_mod._load_llama_class()
            assert result is None
            assert os.environ[embeddings_mod._LIB_PATH_ENV] == str(inherited)
            assert str(inherited) in caplog.text
            assert "left in the environment" in caplog.text
        finally:
            embeddings_mod._load_llama_class.cache_clear()

    def test_empty_lib_path_is_ignored_without_an_environment_write(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """An empty value remains empty while the bundled runtime uses the seam."""
        _stub_bundled_linux_libs(tmp_path)
        monkeypatch.setenv(embeddings_mod._LIB_PATH_ENV, "")
        _install_recording_llama_cpp(monkeypatch, tmp_path)

        with caplog.at_level("INFO", logger=embeddings_mod.__name__):
            result, active_lib_path = _load_bundled_linux_llama(
                monkeypatch, tmp_path, lambda: embeddings_mod._LINUX_X86_64_REQUIRED_CPU_FLAGS
            )

        stand_in = sys.modules["llama_cpp"]
        assert result is stand_in.Llama
        assert stand_in.SEAM_AT_IMPORT == str(
            tmp_path / embeddings_mod._LIBS_DIR_NAME / "linux_x86_64"
        )
        assert stand_in.ENV_AT_IMPORT == ""
        assert stand_in.CHILD_ENV_AT_IMPORT is None
        assert active_lib_path == ""
        assert embeddings_mod._LIB_PATH_ENV in os.environ
        assert "Ignoring LLAMA_CPP_LIB_PATH= left in the environment" in caplog.text

    def test_operator_override_is_kept_out_of_the_bundled_shape_rule(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        """A directory that merely sits inside a Kiro Crew tree is still the operator's."""
        _stub_bundled_linux_libs(tmp_path)
        override = tmp_path / "kiro_crew" / "_vendor" / "llama_cpp_libs"  # no platform leaf
        override.mkdir(parents=True)
        monkeypatch.setenv(embeddings_mod._LIB_PATH_ENV, str(override))
        _install_recording_llama_cpp(monkeypatch, tmp_path)

        def unexpected_cpu_probe():
            raise AssertionError("operator runtime must not use the bundled CPU gate")

        with caplog.at_level("INFO", logger=embeddings_mod.__name__):
            result, active_lib_path = _load_bundled_linux_llama(
                monkeypatch, tmp_path, unexpected_cpu_probe
            )

        stand_in = sys.modules["llama_cpp"]
        assert result is stand_in.Llama
        # No seam for an operator runtime: the loader reads their directory from
        # the environment, exactly as upstream does, and it stays theirs.
        assert stand_in.SEAM_AT_IMPORT is None
        assert stand_in.ENV_AT_IMPORT == str(override)
        assert active_lib_path == str(override)
        assert "inherited from another Kiro Crew process" not in caplog.text

    def test_runtime_import_hardens_locale_encoded_null_streams(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.delenv(embeddings_mod._LIB_PATH_ENV, raising=False)
        _stub_bundled_linux_libs(tmp_path)
        fake_llama_cpp = ModuleType("llama_cpp")
        expected = object()
        setattr(fake_llama_cpp, "Llama", expected)
        monkeypatch.setitem(sys.modules, "llama_cpp", fake_llama_cpp)

        fake_utils = ModuleType("llama_cpp._utils")
        stdout_sink = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        stderr_sink = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
        setattr(fake_utils, "outnull_file", stdout_sink)
        setattr(fake_utils, "errnull_file", stderr_sink)
        monkeypatch.setitem(sys.modules, "llama_cpp._utils", fake_utils)

        result, _active_lib_path = _load_bundled_linux_llama(
            monkeypatch,
            tmp_path,
            lambda: embeddings_mod._LINUX_X86_64_REQUIRED_CPU_FLAGS,
        )

        assert result is expected
        assert stdout_sink.encoding == "utf-8"
        assert stderr_sink.encoding == "utf-8"
        stdout_sink.write("👻")
        stderr_sink.write("👻")

    def test_missing_cpu_features_refuse_before_native_import(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        monkeypatch.delenv(embeddings_mod._LIB_PATH_ENV, raising=False)
        _stub_bundled_linux_libs(tmp_path)

        with caplog.at_level("WARNING", logger=embeddings_mod.__name__):
            result, active_lib_path = _load_bundled_linux_llama(
                monkeypatch, tmp_path, lambda: frozenset({"sse3", "ssse3"})
            )

        assert result is None
        assert active_lib_path is None
        assert "missing avx, avx2, bmi2, f16c, fma" in caplog.text
        assert "SIGILL" in caplog.text
        assert embeddings_mod._LIB_PATH_ENV not in os.environ

    def test_unknown_cpu_features_fail_closed(self, tmp_path: Path, monkeypatch, caplog) -> None:
        monkeypatch.delenv(embeddings_mod._LIB_PATH_ENV, raising=False)
        _stub_bundled_linux_libs(tmp_path)

        with caplog.at_level("WARNING", logger=embeddings_mod.__name__):
            result, active_lib_path = _load_bundled_linux_llama(monkeypatch, tmp_path, lambda: None)

        assert result is None
        assert active_lib_path is None
        assert "Cannot verify CPU compatibility" in caplog.text
        assert embeddings_mod._LIB_PATH_ENV not in os.environ

    def test_operator_override_bypasses_the_bundled_cpu_gate(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        _stub_bundled_linux_libs(tmp_path)
        override = tmp_path / "operator-libs"
        override.mkdir()
        monkeypatch.setenv(embeddings_mod._LIB_PATH_ENV, str(override))
        fake_llama_cpp = ModuleType("llama_cpp")
        expected = object()
        setattr(fake_llama_cpp, "Llama", expected)
        monkeypatch.setitem(sys.modules, "llama_cpp", fake_llama_cpp)

        def unexpected_cpu_probe():
            raise AssertionError("operator runtime must not use the bundled CPU gate")

        result, active_lib_path = _load_bundled_linux_llama(
            monkeypatch, tmp_path, unexpected_cpu_probe
        )

        assert result is expected
        assert active_lib_path == str(override)
        assert os.environ[embeddings_mod._LIB_PATH_ENV] == str(override)


class TestBundledWindowsMsvcRuntimeGate:
    """The vendored Windows libs import an MSVC runtime that is not shipped with them.

    All four DLLs in ``win_amd64`` import ``MSVCP140``, ``VCRUNTIME140`` and
    ``VCRUNTIME140_1``; ``ggml-base``/``ggml-cpu`` add ``VCOMP140`` (MSVC OpenMP).
    Both Linux payloads DO vendor their OpenMP equivalent, so the omission is on the
    Windows lane rather than a deliberate asymmetry — and on a host without the
    redistributable the load failed as a bare ``WinError 126`` naming the library
    being opened rather than the runtime it needs, which reads as an unsupported
    platform. CI cannot see any of this: the ``windows-latest`` runner ships the
    redistributable preinstalled.
    """

    def test_the_probe_is_a_no_op_off_windows(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.embeddings.sys.platform", "linux")
        assert embeddings_mod._missing_windows_msvc_runtime() == []

    def test_every_named_runtime_dll_is_probed(self, monkeypatch) -> None:
        """Absence is reported per DLL, so the message can name what to install."""
        monkeypatch.setattr("kiro_crew.embeddings.sys.platform", "win32")
        asked: list[str] = []

        def _fail(name):
            asked.append(name)
            raise OSError("[WinError 126] The specified module could not be found")

        monkeypatch.setattr(embeddings_mod.ctypes, "WinDLL", _fail, raising=False)
        missing = embeddings_mod._missing_windows_msvc_runtime()
        assert asked == list(embeddings_mod._WINDOWS_MSVC_RUNTIME_DLLS)
        assert missing == list(embeddings_mod._WINDOWS_MSVC_RUNTIME_DLLS)

    def test_a_present_runtime_reports_nothing_missing(self, monkeypatch) -> None:
        monkeypatch.setattr("kiro_crew.embeddings.sys.platform", "win32")
        monkeypatch.setattr(embeddings_mod.ctypes, "WinDLL", lambda name: object(), raising=False)
        assert embeddings_mod._missing_windows_msvc_runtime() == []

    def test_the_loader_refuses_and_names_the_redistributable(
        self, monkeypatch, tmp_path: Path, caplog
    ) -> None:
        """Refused BEFORE the import, so the log carries the fix rather than a WinError."""
        libs = tmp_path / embeddings_mod._LIBS_DIR_NAME / "win_amd64"
        libs.mkdir(parents=True)
        for name in embeddings_mod._REQUIRED_VENDORED_LIBS["win_amd64"]:
            (libs / name).write_bytes(b"MZ")
        monkeypatch.setattr(embeddings_mod, "_VENDOR_DIR", tmp_path)
        monkeypatch.setattr(embeddings_mod, "_platform_libs_dirname", lambda: "win_amd64")
        monkeypatch.setattr(
            embeddings_mod, "_missing_windows_msvc_runtime", lambda: ["VCOMP140.DLL"]
        )
        monkeypatch.delenv(embeddings_mod._LIB_PATH_ENV, raising=False)
        embeddings_mod._load_llama_class.cache_clear()
        try:
            with caplog.at_level("WARNING"):
                assert embeddings_mod._load_llama_class() is None
        finally:
            embeddings_mod._load_llama_class.cache_clear()
        assert "VCOMP140.DLL" in caplog.text
        assert "Visual C++" in caplog.text
        assert "keyword search" in caplog.text

    def test_a_complete_runtime_does_not_refuse_on_the_windows_branch(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Mutation guard: the refusal must be conditional on something real."""
        libs = tmp_path / embeddings_mod._LIBS_DIR_NAME / "win_amd64"
        libs.mkdir(parents=True)
        for name in embeddings_mod._REQUIRED_VENDORED_LIBS["win_amd64"]:
            (libs / name).write_bytes(b"MZ")
        monkeypatch.setattr(embeddings_mod, "_VENDOR_DIR", tmp_path)
        monkeypatch.setattr(embeddings_mod, "_platform_libs_dirname", lambda: "win_amd64")
        monkeypatch.setattr(embeddings_mod, "_missing_windows_msvc_runtime", lambda: [])
        monkeypatch.delenv(embeddings_mod._LIB_PATH_ENV, raising=False)
        _install_recording_llama_cpp(monkeypatch, tmp_path)
        embeddings_mod._load_llama_class.cache_clear()
        try:
            # Reaches the import; the point is only that the runtime gate did not
            # short-circuit it. The stand-in package records the directory the
            # seam carried into the import; the environment is never written.
            embeddings_mod._load_llama_class()
            assert sys.modules["llama_cpp"].SEAM_AT_IMPORT == str(libs)
            assert sys.modules["llama_cpp"].ENV_AT_IMPORT is None
            assert embeddings_mod._LIB_PATH_ENV not in os.environ
        finally:
            embeddings_mod._load_llama_class.cache_clear()
