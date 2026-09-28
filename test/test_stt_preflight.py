"""The native recogniser is checked before it is loaded, and a crash trips a fuse.

kirodotdev/KiroCrew#13179: a ``pywhispercpp`` build compiled with AVX-512 was
loaded on a Broadwell Xeon that has none. ``SIGILL`` is a process-level signal,
so the whole gateway died, the supervisor restarted it, the boot prewarm loaded
the model again five seconds later, and the gateway died again -- every 15-25 s,
indefinitely, with every chat session and channel along for the ride.

Two things are pinned here. The ISA check in :mod:`kiro_crew.stt.preflight` runs
the one native call that both exercises ``ggml_cpu_init`` and reports the build's
instruction sets in a CHILD process, so the answer costs the gateway nothing; and
the load marker the engine writes around every native load turns "the last load
took the process down" into a refusal instead of another attempt. Both reach
every surface through :func:`kiro_crew.stt.engine.probe`.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

from kiro_crew.stt import capabilities as caps_mod
from kiro_crew.stt import engine as engine_mod
from kiro_crew.stt import models, preflight

# Real captures. The first is a stock CPU wheel (see stt/capabilities.py); the
# second is what a `-march=native` build on a Skylake-SP class host reports.
_AVX2_BUILD = (
    "WHISPER : COREML = 0 | OPENVINO = 0 | CPU : SSE3 = 1 | SSSE3 = 1 | AVX = 1 | "
    "AVX2 = 1 | F16C = 1 | FMA = 1 | OPENMP = 1 | REPACK = 1 |"
)
_AVX512_BUILD = (
    "WHISPER : COREML = 0 | OPENVINO = 0 | CPU : SSE3 = 1 | SSSE3 = 1 | AVX = 1 | "
    "AVX2 = 1 | F16C = 1 | FMA = 1 | BMI2 = 1 | AVX512 = 1 | AVX512_VNNI = 1 | "
    "OPENMP = 1 | REPACK = 1 |"
)

#: The reporter's CPU, as /proc/cpuinfo prints it: Xeon E5-2686 v4, no AVX-512.
_BROADWELL_CPUINFO = (
    "processor\t: 0\n"
    "model name\t: Intel(R) Xeon(R) CPU E5-2686 v4 @ 2.30GHz\n"
    "flags\t\t: fpu vme de pse tsc msr pae mce cx8 apic sep mtrr pge mca cmov pat "
    "pse36 clflush mmx fxsr sse sse2 ht syscall nx pdpe1gb rdtscp lm constant_tsc "
    "rep_good nopl xtopology cpuid pni pclmulqdq ssse3 fma cx16 pcid sse4_1 sse4_2 "
    "x2apic movbe popcnt aes xsave avx f16c rdrand hypervisor lahf_lm abm "
    "3dnowprefetch invpcid_single pti fsgsbase bmi1 avx2 smep bmi2 erms invpcid "
    "xsaveopt\n"
)
_GRAVITON2_CPUINFO = (
    "processor\t: 0\n"
    "BogoMIPS\t: 243.75\n"
    "Features\t: fp asimd evtstrm aes pmull sha1 sha2 crc32 atomics fphp asimdhp "
    "cpuid asimdrdm lrcpc dcpop asimddp ssbs\n"
)


def _flags(tmp_path: Path, cpuinfo: str, machine: str, monkeypatch) -> frozenset[str] | None:
    """Run `host_flags` against a written cpuinfo as if on *machine* under Linux."""
    monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
    monkeypatch.setattr(preflight.platform, "machine", lambda: machine)
    path = tmp_path / "cpuinfo"
    path.write_text(cpuinfo, encoding="utf-8")
    return preflight.host_flags(path)


@pytest.fixture(autouse=True)
def _fresh_cache():
    preflight.reset_cache()
    yield
    preflight.reset_cache()


#: A stand-in extension path for staged-runner tests; the fake runner never opens it.
_EXT = "/site/_pywhispercpp.so"


def _runner(returncode, out="", err=""):
    def run(_timeout, _extension):
        return returncode, out, err

    return run


# ── Host instruction sets ──


class TestHostFlags:
    def test_reads_the_x86_flags_line_through_the_shared_reader(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """x86 goes through `embeddings._linux_x86_64_cpu_flags`, the one cpuinfo
        parser this repository keeps; its `pni` -> `sse3` alias comes along."""
        flags = _flags(tmp_path, _BROADWELL_CPUINFO, "x86_64", monkeypatch)
        assert flags is not None
        assert {"avx2", "fma", "f16c", "bmi2", "pni", "sse3"} <= flags
        assert "avx512f" not in flags

    def test_reads_the_arm_features_line(self, tmp_path: Path, monkeypatch) -> None:
        flags = _flags(tmp_path, _GRAVITON2_CPUINFO, "aarch64", monkeypatch)
        assert flags is not None
        assert {"asimd", "asimddp", "asimdhp"} <= flags
        assert "i8mm" not in flags
        assert "sve" not in flags

    def test_arm_features_are_intersected_across_cores(self, tmp_path: Path, monkeypatch) -> None:
        two = _GRAVITON2_CPUINFO + "processor\t: 1\nFeatures\t: fp asimd aes\n"
        flags = _flags(tmp_path, two, "aarch64", monkeypatch)
        assert flags == frozenset({"fp", "asimd", "aes"})

    def test_a_cpuinfo_without_either_line_is_unknown(self, tmp_path: Path, monkeypatch) -> None:
        assert (
            _flags(tmp_path, "processor : 0\nmodel name : Something\n", "aarch64", monkeypatch)
            is None
        )

    def test_off_linux_is_unknown(self, monkeypatch) -> None:
        monkeypatch.setattr(preflight.platform, "system", lambda: "Darwin")
        assert preflight.host_flags() is None

    def test_thirty_two_bit_arm_is_unknown_not_refused(self, monkeypatch, tmp_path: Path) -> None:
        """A 32-bit ARM kernel spells `neon` where AArch64 spells `asimd`; with no
        reader for it the host is unknown, and an unknown host is never refused."""
        cpuinfo = tmp_path / "cpuinfo"
        cpuinfo.write_text(
            "Features\t: half thumb fastmult vfp edsp neon vfpv3\n", encoding="utf-8"
        )
        monkeypatch.setattr(preflight.platform, "system", lambda: "Linux")
        monkeypatch.setattr(preflight.platform, "machine", lambda: "armv7l")
        assert preflight.host_flags(cpuinfo) is None
        monkeypatch.setattr(preflight.platform, "machine", lambda: "armv8l")
        assert preflight.host_flags(cpuinfo) is None


class TestMissingFeatures:
    def test_names_the_build_features_the_host_lacks(self, tmp_path: Path, monkeypatch) -> None:
        flags = _flags(tmp_path, _BROADWELL_CPUINFO, "x86_64", monkeypatch)
        build = ("SSE3", "AVX", "AVX2", "FMA", "BMI2", "AVX512", "AVX512_VNNI", "OPENMP")
        assert preflight.missing_features(build, flags) == ("AVX512", "AVX512_VNNI")

    def test_a_graviton3_build_is_refused_on_graviton2(self, tmp_path: Path, monkeypatch) -> None:
        flags = _flags(tmp_path, _GRAVITON2_CPUINFO, "aarch64", monkeypatch)
        build = ("NEON", "ARM_FMA", "FP16_VA", "DOTPROD", "MATMUL_INT8", "SVE")
        assert preflight.missing_features(build, flags) == ("MATMUL_INT8", "SVE")

    def test_an_unknown_host_refuses_nothing(self) -> None:
        assert preflight.missing_features(("AVX512",), None) == ()

    def test_a_feature_the_table_does_not_know_refuses_nothing(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A wrong table entry would refuse a working host, so an unknown name is
        never a refusal; the child probe is the evidence for those."""
        flags = _flags(tmp_path, _BROADWELL_CPUINFO, "x86_64", monkeypatch)
        assert preflight.missing_features(("REPACK", "LLAMAFILE", "FUTURE_ISA"), flags) == ()

    def test_the_table_spells_avx512_vbmi_the_way_the_kernel_does(self) -> None:
        """`avx512vbmi`, no underscore (X86_FEATURE_AVX512VBMI), unlike its
        `avx512_vbmi2` / `avx512_vnni` siblings. A native build on an Ice Lake host
        reports `AVX512_VBMI = 1`, and the wrong token refuses that host its own build."""
        assert preflight._HOST_FLAG_FOR_FEATURE["AVX512_VBMI"] == "avx512vbmi"
        assert (
            preflight.missing_features(
                ("AVX512", "AVX512_VBMI", "AVX512_VNNI"),
                frozenset({"avx512f", "avx512vbmi", "avx512_vnni"}),
            )
            == ()
        )


# ── The subprocess probe ──


class TestProbeNative:
    def test_a_child_killed_by_sigill_is_a_refusal(self) -> None:
        v = preflight.probe_native(extension=_EXT, runner=_runner(-4), read_host_flags=False)
        assert v.ok is False
        assert v.code == preflight.CODE_UNSUPPORTED_CPU
        assert "Illegal instruction" in v.detail
        assert "stt.enabled" in v.detail

    def test_the_windows_illegal_instruction_status_is_the_same_refusal(self) -> None:
        v = preflight.probe_native(
            extension=_EXT, runner=_runner(0xC000001D), read_host_flags=False
        )
        assert v.code == preflight.CODE_UNSUPPORTED_CPU

    def test_another_fatal_signal_gets_its_own_code(self) -> None:
        v = preflight.probe_native(extension=_EXT, runner=_runner(-11), read_host_flags=False)
        assert v.ok is False
        assert v.code == preflight.CODE_NATIVE_PROBE_CRASHED

    def test_an_ordinary_error_exit_is_inconclusive_not_a_refusal(self) -> None:
        """An import failure is the engine probe's to report, with the loader's
        own message; refusing here would hide that message behind this one."""
        v = preflight.probe_native(
            extension=_EXT,
            runner=_runner(1, err="ModuleNotFoundError: No module named '_pywhispercpp'"),
            read_host_flags=False,
        )
        assert v.ok is True
        assert v.code == ""
        assert "_pywhispercpp" in v.detail

    def test_a_timeout_is_inconclusive(self) -> None:
        v = preflight.probe_native(extension=_EXT, runner=_runner(None), read_host_flags=False)
        assert v.ok is True
        assert "did not finish" in v.detail

    def test_a_build_needing_what_the_host_lacks_is_refused(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        flags = _flags(tmp_path, _BROADWELL_CPUINFO, "x86_64", monkeypatch)
        v = preflight.probe_native(extension=_EXT, runner=_runner(0, _AVX512_BUILD), flags=flags)
        assert v.ok is False
        assert v.code == preflight.CODE_UNSUPPORTED_CPU
        assert "AVX512, AVX512_VNNI" in v.detail
        assert v.raw == _AVX512_BUILD

    def test_a_build_the_host_can_run_passes(self, tmp_path: Path, monkeypatch) -> None:
        flags = _flags(tmp_path, _BROADWELL_CPUINFO, "x86_64", monkeypatch)
        v = preflight.probe_native(extension=_EXT, runner=_runner(0, _AVX2_BUILD), flags=flags)
        assert v.ok is True
        assert v.code == ""

    def test_a_host_whose_flags_are_unknown_passes_on_the_child_alone(self) -> None:
        v = preflight.probe_native(
            extension=_EXT, runner=_runner(0, _AVX512_BUILD), read_host_flags=False
        )
        assert v.ok is True

    def test_the_child_is_this_interpreter_running_the_binding(self) -> None:
        """The probe has to run the SAME extension the gateway would load, so it
        runs under the same interpreter and loads the file the parent resolved."""
        assert "_pywhispercpp" in preflight._PROBE_SOURCE
        assert "whisper_print_system_info" in preflight._PROBE_SOURCE
        # By exact path (argv[1]), not by name: `-I` drops the user site from the
        # child's sys.path, so a `--user` install would import here and fail
        # there -- an inconclusive verdict on exactly the build to be judged.
        assert "spec_from_file_location" in preflight._PROBE_SOURCE
        assert "sys.argv[1]" in preflight._PROBE_SOURCE
        assert "import _pywhispercpp" not in preflight._PROBE_SOURCE
        # Nothing in the child but the probe: no site customisation of ours, no
        # numpy, no gateway import that could itself be the thing that crashes.
        assert "kiro_crew" not in preflight._PROBE_SOURCE
        # The child expects to die; it must not leave a core file behind, so the
        # rlimit prelude comes first and is a separate constant a test can keep.
        assert preflight._PROBE_SOURCE == preflight._PROBE_PRELUDE + preflight._PROBE_BODY
        assert "RLIMIT_CORE" in preflight._PROBE_PRELUDE
        assert "RLIMIT_CORE" not in preflight._PROBE_BODY

    def test_the_child_loads_the_file_the_parent_resolved(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A real spawn against a stand-in extension at a path `-I` cannot see by
        name (a user-site install is the real case): the child must load THAT
        file, because the parent's `binary_identity()` is what gets judged."""
        user_site = tmp_path / "user-site"
        user_site.mkdir()
        fake = user_site / "_pywhispercpp.py"
        fake.write_text(
            "def whisper_print_system_info():\n    return 'FROM_EXACT_PATH AVX2 = 1 | '\n",
            encoding="utf-8",
        )
        code, out, _err = preflight._run_probe_child(30.0, str(fake))
        assert code == 0
        assert "FROM_EXACT_PATH" in out

    def test_probe_native_resolves_the_extension_through_binary_identity(self, monkeypatch) -> None:
        seen: list = []

        def runner(timeout, extension):
            seen.append(extension)
            return 0, _AVX2_BUILD, ""

        ident = preflight._BinaryIdentity("/site/_pywhispercpp.so", 1, 1)
        monkeypatch.setattr(preflight, "binary_identity", lambda: ident)
        v = preflight.probe_native(runner=runner, read_host_flags=False)
        assert v.ok is True
        assert seen == ["/site/_pywhispercpp.so"]
        monkeypatch.setattr(preflight, "binary_identity", lambda: None)
        v = preflight.probe_native(runner=runner, read_host_flags=False)
        assert v.ok is True and "no speech runtime extension" in (v.detail or "")
        assert len(seen) == 1

    def test_the_child_cannot_be_shadowed_or_fed_by_the_gateway_environment(
        self, monkeypatch
    ) -> None:
        """`-I` keeps the working directory off `sys.path` and `PYTHON*` out; cwd is
        the interpreter's own prefix, not wherever the gateway was started; and the
        environment is the fixed allow-list, so no credential the gateway carries
        for its own children reaches a process that is expected to crash."""
        seen: dict = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            seen.update(kw)
            return types.SimpleNamespace(returncode=0, stdout=_AVX2_BUILD, stderr="")

        monkeypatch.setattr(preflight.subprocess, "run", fake_run)
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "nope")
        monkeypatch.setenv("KIROCREW_SESSION_KEY", "nope")
        monkeypatch.setenv("PYTHONPATH", "/tmp/planted")
        monkeypatch.setenv("PATH", os.environ.get("PATH", "/usr/bin"))
        preflight._run_probe_child(5.0, "/site/_pywhispercpp.so")
        assert seen["cmd"][:4] == [sys.executable, "-I", "-S", "-c"]
        assert seen["cmd"][5:] == ["/site/_pywhispercpp.so"]
        assert seen["cwd"] == preflight._CHILD_CWD == sys.prefix
        assert "AWS_SECRET_ACCESS_KEY" not in seen["env"]
        assert "KIROCREW_SESSION_KEY" not in seen["env"]
        assert "PYTHONPATH" not in seen["env"]
        assert "PATH" in seen["env"]
        assert set(seen["env"]) <= preflight._CHILD_ENV_KEEP

    def test_site_startup_code_does_not_run_in_the_child(self, monkeypatch, tmp_path: Path) -> None:
        """A real spawn: `-I` alone still imports `site`, which executes every
        `.pth` line in the interpreter's site-packages -- a directory the venv's
        owner writes to, so a planted `.pth` would run as the gateway inside a
        child that is unsandboxed on purpose. `-S` skips `site` altogether; the
        child never needed it, because it loads the extension from an explicit
        path."""
        seen: dict = {}
        real_run = preflight.subprocess.run

        def spy_run(cmd, **kw):
            seen["cmd"] = list(cmd)
            return real_run(cmd, **kw)

        monkeypatch.setattr(preflight.subprocess, "run", spy_run)
        preflight._run_probe_child(30.0, "/nonexistent/_pywhispercpp.so")
        flags = [a for a in seen["cmd"][1 : seen["cmd"].index("-c")]]
        assert "-S" in flags and "-I" in flags
        # Under the child's exact flags the real interpreter starts with `site`
        # never imported, so no site directory -- and no `.pth` -- is processed.
        probe = "import sys; sys.stdout.write('site' if 'site' in sys.modules else 'nosite')"
        with_flags = subprocess.run(
            [sys.executable, *flags, "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert with_flags.stdout == "nosite"
        # Control, so the assertion is not vacuous about what `.pth` processing
        # does: the same interpreter under `-I` alone imports `site`, and a `.pth`
        # in a directory `site` processes executes its `import` line.
        without_s = subprocess.run(
            [sys.executable, "-I", "-c", probe],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert without_s.stdout == "site"
        site_dir = tmp_path / "site-packages"
        site_dir.mkdir()
        sentinel = tmp_path / "ran"
        (site_dir / "planted.pth").write_text(
            f"import pathlib; pathlib.Path({str(sentinel)!r}).write_text('x')\n",
            encoding="utf-8",
        )
        subprocess.run(
            [sys.executable, "-I", "-c", f"import site; site.addsitedir({str(site_dir)!r})"],
            check=True,
            timeout=30,
        )
        assert sentinel.exists()

    def test_a_planted_module_in_the_cwd_does_not_reach_the_child(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """A real spawn: `_pywhispercpp.py` in the gateway's cwd must not be what
        the child imports. With `-I` and a pinned cwd it cannot be; the child then
        either imports the real extension or fails to import, and both are fine."""
        (tmp_path / "_pywhispercpp.py").write_text(
            "import sys; sys.stdout.write('PLANTED'); sys.exit(0)\n", encoding="utf-8"
        )
        monkeypatch.chdir(tmp_path)
        code, out, _err = preflight._run_probe_child(30.0, "/nonexistent/_pywhispercpp.so")
        assert "PLANTED" not in out
        assert code != 0  # the handed-in path does not exist; nothing else was tried

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal semantics")
    def test_a_real_child_dying_of_sigill_reaches_the_refusal(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        """Not a staged return code: a real interpreter, really killed by SIGILL,
        through the real `subprocess` call, so the sign convention this module
        reads (`-signum`) is the one the platform actually produces. The child's
        cwd is pinned to `tmp_path` (the helper's own pin, not the test's chdir,
        which the helper would override) and the RLIMIT_CORE prelude is kept, so
        a host with core dumps on and a relative `core_pattern` writes nothing,
        and nothing outside `tmp_path` if it did."""
        monkeypatch.setattr(preflight, "_CHILD_CWD", str(tmp_path))
        monkeypatch.setattr(
            preflight,
            "_PROBE_SOURCE",
            preflight._PROBE_PRELUDE + "import os, signal; os.kill(os.getpid(), signal.SIGILL)\n",
        )
        v = preflight.probe_native(
            extension="/unused/_pywhispercpp.so", timeout=30, read_host_flags=False
        )
        assert v.ok is False
        assert v.code == preflight.CODE_UNSUPPORTED_CPU
        assert not [p for p in tmp_path.iterdir() if p.name.startswith("core")]


class TestAChildThatCannotStart:
    def test_a_missing_interpreter_is_inconclusive_not_a_raise(self, monkeypatch) -> None:
        """`engine.probe` promises an `Availability`; a spawn that fails before the
        child exists must not turn that into a 500 or an aborted `doctor`."""
        monkeypatch.setattr(preflight.sys, "executable", "/nonexistent/python-that-is-gone")
        code, out, err = preflight._run_probe_child(5.0, "/site/_pywhispercpp.so")
        assert code is None
        # The loader's own wording differs per platform (`No such file or
        # directory` / `[WinError 2] The system cannot find the file specified`);
        # what matters is that the reason travelled with the inconclusive answer.
        assert err.strip()
        assert out == ""

    def test_a_refused_fork_is_inconclusive(self, monkeypatch) -> None:
        def boom(*_a, **_kw):
            raise OSError(11, "Resource temporarily unavailable")

        monkeypatch.setattr(preflight.subprocess, "run", boom)
        code, _out, err = preflight._run_probe_child(5.0, "/site/_pywhispercpp.so")
        assert code is None
        assert "Resource temporarily unavailable" in err

    def test_the_verdict_says_why_it_is_inconclusive(self) -> None:
        def runner(_timeout, _extension):
            return None, "", "[Errno 2] No such file or directory: '/gone/python'"

        v = preflight.probe_native(extension=_EXT, runner=runner, read_host_flags=False)
        assert v.ok is True
        assert "could not start" in (v.detail or "")
        v = preflight.probe_native(extension=_EXT, runner=_runner(None), read_host_flags=False)
        assert v.ok is True
        assert "did not finish" in (v.detail or "")


class TestVerdictCache:
    def test_probes_once_per_binary_and_again_after_a_reinstall(self, monkeypatch) -> None:
        calls = []
        ident = preflight._BinaryIdentity("/x/_pywhispercpp.so", 1, 1)
        monkeypatch.setattr(preflight, "binary_identity", lambda: ident)

        def fake_probe(**_kw):
            calls.append(1)
            return preflight.Verdict(True)

        monkeypatch.setattr(preflight, "probe_native", fake_probe)
        preflight.verdict()
        preflight.verdict()
        assert len(calls) == 1
        # A reinstall changes the file, so the next call probes afresh.
        monkeypatch.setattr(
            preflight,
            "binary_identity",
            lambda: preflight._BinaryIdentity("/x/_pywhispercpp.so", 2, 2),
        )
        preflight.verdict()
        assert len(calls) == 2

    def test_no_extension_installed_is_a_pass_without_a_probe(self, monkeypatch) -> None:
        monkeypatch.setattr(preflight, "binary_identity", lambda: None)
        monkeypatch.setattr(preflight, "probe_native", lambda **_kw: pytest.fail("probed"))
        assert preflight.verdict().ok is True


# ── The load marker ──


class TestLoadMarker:
    @pytest.fixture
    def ident(self, monkeypatch):
        ident = preflight._BinaryIdentity("/x/_pywhispercpp.so", 1, 1)
        monkeypatch.setattr(preflight, "binary_identity", lambda: ident)
        return ident

    def test_a_marker_from_this_process_is_a_load_in_flight_not_a_crash(
        self, tmp_path: Path, ident
    ) -> None:
        preflight.write_load_marker(tmp_path, "/m/ggml-base.bin")
        assert (tmp_path / preflight.LOAD_MARKER_NAME).is_file()
        assert preflight.previous_load_crashed(tmp_path).ok is True

    def test_a_marker_from_another_process_naming_this_binary_trips_the_fuse(
        self, tmp_path: Path, ident
    ) -> None:
        preflight.write_load_marker(tmp_path, "/m/ggml-large-v3-turbo.bin")
        marker = tmp_path / preflight.LOAD_MARKER_NAME
        data = json.loads(marker.read_text(encoding="utf-8"))
        data["token"] = "0000deadbeef0000"  # the process that died
        marker.write_text(json.dumps(data), encoding="utf-8")

        v = preflight.previous_load_crashed(tmp_path)
        assert v.ok is False
        assert v.code == preflight.CODE_LOAD_CRASHED
        assert "ggml-large-v3-turbo.bin" in v.detail
        assert str(marker) in v.detail
        # It stays tripped: the fuse is not consumed by being read.
        assert marker.is_file()
        assert preflight.previous_load_crashed(tmp_path).ok is False

    def test_a_marker_from_a_binary_that_is_gone_is_ignored_and_left_in_place(
        self, tmp_path: Path, ident, monkeypatch
    ) -> None:
        """The inspector only READS. Unlinking here would race another process
        sitting between its own arm and disarm on the same path: a stale marker it
        judged and removed could be the live fuse of a load in flight next door.
        A marker that is not this binary's is simply no fuse; the next arm
        replaces it atomically."""
        preflight.write_load_marker(tmp_path, "/m/ggml-base.bin")
        marker = tmp_path / preflight.LOAD_MARKER_NAME
        data = json.loads(marker.read_text(encoding="utf-8"))
        data["token"] = "0000deadbeef0000"
        marker.write_text(json.dumps(data), encoding="utf-8")
        monkeypatch.setattr(
            preflight,
            "binary_identity",
            lambda: preflight._BinaryIdentity("/x/_pywhispercpp.so", 9, 9),
        )
        assert preflight.previous_load_crashed(tmp_path).ok is True
        assert marker.is_file()
        # The next arm replaces it, and the replacement is this process's own.
        preflight.write_load_marker(tmp_path, "/m/ggml-base.bin")
        assert json.loads(marker.read_text(encoding="utf-8"))["token"] == preflight._PROCESS_TOKEN

    def test_the_inspector_never_unlinks(self, tmp_path: Path, ident, monkeypatch) -> None:
        """Pinned directly: no branch of `previous_load_crashed` removes the file."""
        unlinks: list[Path] = []
        real_unlink = Path.unlink

        def _spy(self, *a, **k):
            unlinks.append(self)
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(Path, "unlink", _spy)
        marker = tmp_path / preflight.LOAD_MARKER_NAME
        for content in ("not json", "[1, 2]", json.dumps({"token": "x", "binary": None})):
            marker.write_text(content, encoding="utf-8")
            assert preflight.previous_load_crashed(tmp_path).ok is True
        assert unlinks == []
        assert marker.is_file()

    def test_the_same_pid_in_a_new_process_is_still_a_crash(self, tmp_path: Path, ident) -> None:
        """The marker outlives the process on the models directory, and a replacement
        container in a fresh PID namespace gets the same low pid as a matter of
        course; identity is the per-process token, never the pid."""
        preflight.write_load_marker(tmp_path, "/m/ggml-base.bin")
        marker = tmp_path / preflight.LOAD_MARKER_NAME
        data = json.loads(marker.read_text(encoding="utf-8"))
        assert data["pid"] == os.getpid()
        data["token"] = "0000deadbeef0000"
        marker.write_text(json.dumps(data), encoding="utf-8")
        assert preflight.previous_load_crashed(tmp_path).code == preflight.CODE_LOAD_CRASHED

    def test_clearing_removes_it_and_is_idempotent(self, tmp_path: Path, ident) -> None:
        preflight.write_load_marker(tmp_path, "/m/ggml-base.bin")
        preflight.clear_load_marker(tmp_path)
        assert not (tmp_path / preflight.LOAD_MARKER_NAME).exists()
        preflight.clear_load_marker(tmp_path)

    def test_garbage_is_not_trusted_and_not_touched(self, tmp_path: Path, ident) -> None:
        marker = tmp_path / preflight.LOAD_MARKER_NAME
        marker.write_text("not json", encoding="utf-8")
        assert preflight.previous_load_crashed(tmp_path).ok is True
        assert marker.read_text(encoding="utf-8") == "not json"

    def test_an_oversized_marker_is_ignored_without_being_read(
        self, tmp_path: Path, ident, monkeypatch
    ) -> None:
        """The marker sits at a fixed name in a directory a sandboxed agent can
        write, and it is re-read on every availability check; a planted multi-GB
        file must not be pulled into the gateway. The read is bounded by
        `_MARKER_MAX_BYTES` and a larger file is judged by its size alone."""
        marker = tmp_path / preflight.LOAD_MARKER_NAME
        with marker.open("wb") as fh:
            fh.truncate(preflight._MARKER_MAX_BYTES + 1)  # sparse: costs no disk
        reads: list[int] = []
        real_read = os.read

        def _spy(fd, n):
            reads.append(n)
            return real_read(fd, n)

        monkeypatch.setattr(os, "read", _spy)
        assert preflight.previous_load_crashed(tmp_path).ok is True
        assert reads == []
        assert marker.stat().st_size == preflight._MARKER_MAX_BYTES + 1

    def test_a_marker_exactly_at_the_cap_is_still_read(self, tmp_path: Path, ident) -> None:
        marker = tmp_path / preflight.LOAD_MARKER_NAME
        body = json.dumps({"token": "0000deadbeef0000", "binary": ident.as_dict(), "model": "m"})
        padded = (
            body[:-1] + ', "pad": "' + "x" * (preflight._MARKER_MAX_BYTES - len(body) - 11) + '"}'
        )
        assert len(padded.encode()) == preflight._MARKER_MAX_BYTES
        marker.write_text(padded, encoding="utf-8")
        assert preflight.previous_load_crashed(tmp_path).code == preflight.CODE_LOAD_CRASHED

    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="no O_NOFOLLOW here")
    def test_a_symlinked_marker_is_not_followed(self, tmp_path: Path, ident) -> None:
        target = tmp_path / "elsewhere.json"
        target.write_text(
            json.dumps({"token": "0000deadbeef0000", "binary": ident.as_dict(), "model": "m"}),
            encoding="utf-8",
        )
        marker = tmp_path / preflight.LOAD_MARKER_NAME
        marker.symlink_to(target)
        # Followed, this would trip the fuse from a file the writer never published.
        assert preflight.previous_load_crashed(tmp_path).ok is True
        assert marker.is_symlink()

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
    def test_a_non_regular_marker_is_ignored_without_blocking(self, tmp_path: Path, ident) -> None:
        """A FIFO at the marker name would block a plain `read_text` forever."""
        marker = tmp_path / preflight.LOAD_MARKER_NAME
        os.mkfifo(marker)
        done: list[bool] = []

        def _run():
            done.append(preflight.previous_load_crashed(tmp_path).ok)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=5)
        assert done == [True]

    def test_a_marker_that_cannot_be_written_is_an_error_not_a_shrug(
        self, tmp_path: Path, ident, monkeypatch
    ) -> None:
        """An unarmed fuse is no fuse: a load that then kills the process leaves
        nothing behind, and the next boot repeats it. The write must raise so the
        caller does not run the native load."""

        def _refuse(*_a, **_k):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(preflight, "atomic_write", _refuse)
        with pytest.raises(OSError, match="speech load fuse"):
            preflight.write_load_marker(tmp_path, "/m/ggml-base.bin")
        assert not (tmp_path / preflight.LOAD_MARKER_NAME).exists()


# ── Through the engine ──


def _installed_recogniser(monkeypatch) -> None:
    """Make the engine's import steps succeed without the real binding."""
    monkeypatch.setattr(engine_mod.importlib.util, "find_spec", lambda name: object(), raising=True)
    pkg = types.ModuleType("pywhispercpp")
    model = types.ModuleType("pywhispercpp.model")
    pkg.model = model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pywhispercpp", pkg)
    monkeypatch.setitem(sys.modules, "pywhispercpp.model", model)


class TestProbeConsultsPreflight:
    def test_an_unsupported_cpu_is_reported_before_anything_loads(
        self, monkeypatch, tmp_path: Path
    ) -> None:
        _installed_recogniser(monkeypatch)
        monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
        monkeypatch.setattr(
            preflight,
            "verdict",
            lambda: preflight.Verdict(False, preflight.CODE_UNSUPPORTED_CPU, "no AVX-512 here"),
        )
        result = engine_mod.probe()
        assert result.ok is False
        assert result.code == preflight.CODE_UNSUPPORTED_CPU
        assert result.detail == "no AVX-512 here"

    def test_the_verdict_is_read_before_the_in_process_import(self, monkeypatch) -> None:
        """The import dlopens the extension and runs its static initialisers, which
        on an incompatible build can be the first thing that faults; a refusal that
        comes after it would never be reached. So the in-process import must not
        happen at all when the child says no."""
        monkeypatch.setattr(
            engine_mod.importlib.util, "find_spec", lambda name: object(), raising=True
        )
        import builtins

        real_import = builtins.__import__

        def _forbidden(name, *args, **kwargs):
            if name.startswith("pywhispercpp"):
                pytest.fail("imported the extension in-process on a refused host")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _forbidden)
        monkeypatch.setattr(
            preflight,
            "verdict",
            lambda: preflight.Verdict(False, preflight.CODE_UNSUPPORTED_CPU, "refused"),
        )
        assert engine_mod.probe().code == preflight.CODE_UNSUPPORTED_CPU

    def test_a_tripped_fuse_is_reported(self, monkeypatch, tmp_path: Path) -> None:
        _installed_recogniser(monkeypatch)
        monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
        monkeypatch.setattr(preflight, "verdict", lambda: preflight.Verdict(True))
        monkeypatch.setattr(
            preflight,
            "previous_load_crashed",
            lambda _dir: preflight.Verdict(False, preflight.CODE_LOAD_CRASHED, "died last time"),
        )
        result = engine_mod.probe()
        assert result.code == preflight.CODE_LOAD_CRASHED

    def test_a_passing_preflight_leaves_probe_as_it_was(self, monkeypatch, tmp_path: Path) -> None:
        _installed_recogniser(monkeypatch)
        monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
        monkeypatch.setattr(preflight, "verdict", lambda: preflight.Verdict(True))
        assert engine_mod.probe() == engine_mod.Availability(True)

    def test_the_new_codes_are_part_of_the_engine_contract(self) -> None:
        """They travel to the browser like the engine's own codes, so they are
        re-exported from the engine module and distinct from every other code."""
        codes = [
            engine_mod.CODE_EXTRA_MISSING,
            engine_mod.CODE_NO_WHEEL,
            engine_mod.CODE_IMPORT_FAILED,
            engine_mod.CODE_MODEL_MISSING,
            engine_mod.CODE_DECODE_FAILED,
            engine_mod.CODE_UNSUPPORTED_CPU,
            engine_mod.CODE_LOAD_CRASHED,
            engine_mod.CODE_NATIVE_PROBE_CRASHED,
        ]
        assert len(set(codes)) == len(codes)
        assert engine_mod.CODE_UNSUPPORTED_CPU == preflight.CODE_UNSUPPORTED_CPU

    def test_capabilities_do_not_touch_the_build_when_preflight_refuses(self, monkeypatch) -> None:
        """`GET /api/stt/status` reads the build's capabilities in-process, and
        that call runs `ggml_cpu_init` -- the very code that faults."""
        from kiro_crew.stt import capabilities as caps_mod

        monkeypatch.setattr(
            preflight,
            "verdict",
            lambda: preflight.Verdict(False, preflight.CODE_UNSUPPORTED_CPU, "refused"),
        )
        monkeypatch.setattr(
            caps_mod, "detect", lambda binding=None: pytest.fail("touched the build")
        )
        caps = engine_mod.WhisperEngine.capabilities()
        assert caps.known is False
        assert "refused" in caps.detail


class _FakeModel:
    def transcribe(self, pcm, **_kw):
        return []


@pytest.mark.asyncio
async def test_the_engine_arms_the_fuse_around_a_native_load(monkeypatch, tmp_path: Path) -> None:
    """The marker exists while `_build_model` runs and is gone once it returned."""
    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    stub = tmp_path / "ggml-base.bin"
    stub.write_bytes(b"not a real model")

    async def _ensure(_model):
        return stub

    monkeypatch.setattr(models, "store", lambda: type("S", (), {"ensure": staticmethod(_ensure)})())
    seen: list[bool] = []

    def _build(key):
        marker = tmp_path / preflight.LOAD_MARKER_NAME
        seen.append(marker.is_file())
        data = json.loads(marker.read_text(encoding="utf-8"))
        assert data["token"] == preflight._PROCESS_TOKEN
        assert data["model"] == key.model_path
        return _FakeModel()

    monkeypatch.setattr(engine_mod.WhisperEngine, "_build_model", staticmethod(_build))
    loop_thread = threading.get_ident()
    writers: list[int] = []
    real_write = preflight.write_load_marker

    def _write(marker_dir, model_path):
        writers.append(threading.get_ident())
        return real_write(marker_dir, model_path)

    monkeypatch.setattr(preflight, "write_load_marker", _write)
    engine = engine_mod.WhisperEngine(idle_evict_secs=600)

    result = await engine.ensure_loaded("base", "en")

    assert result.ok is True
    assert seen == [True]
    assert writers and all(t != loop_thread for t in writers)
    assert not (tmp_path / preflight.LOAD_MARKER_NAME).exists()


@pytest.mark.asyncio
async def test_a_load_that_raises_still_clears_the_fuse(monkeypatch, tmp_path: Path) -> None:
    """A Python exception is a load that RETURNED; only a dead process leaves the marker."""
    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    stub = tmp_path / "ggml-base.bin"
    stub.write_bytes(b"not a real model")

    async def _ensure(_model):
        return stub

    monkeypatch.setattr(models, "store", lambda: type("S", (), {"ensure": staticmethod(_ensure)})())

    def _build(key):
        raise RuntimeError("bad weights")

    monkeypatch.setattr(engine_mod.WhisperEngine, "_build_model", staticmethod(_build))
    engine = engine_mod.WhisperEngine(idle_evict_secs=600)

    result = await engine.ensure_loaded("base", "en")

    assert result.ok is False
    assert not (tmp_path / preflight.LOAD_MARKER_NAME).exists()


@pytest.mark.asyncio
async def test_a_fuse_that_cannot_be_armed_stops_the_load(monkeypatch, tmp_path: Path) -> None:
    """A models directory that cannot take the marker (read-only, full) must not
    let the native load run unguarded: the load is the one step that can kill the
    process, and without the marker the next boot would repeat it."""
    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    stub = tmp_path / "ggml-base.bin"
    stub.write_bytes(b"not a real model")

    async def _ensure(_model):
        return stub

    monkeypatch.setattr(models, "store", lambda: type("S", (), {"ensure": staticmethod(_ensure)})())

    def _refuse(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(preflight, "atomic_write", _refuse)
    built: list[object] = []

    def _build(key):
        built.append(key)
        return _FakeModel()

    monkeypatch.setattr(engine_mod.WhisperEngine, "_build_model", staticmethod(_build))
    engine = engine_mod.WhisperEngine(idle_evict_secs=600)

    result = await engine.ensure_loaded("base", "en")

    assert result.ok is False
    assert "speech load fuse" in result.detail
    assert built == []
    assert engine.loaded_key is None


@pytest.mark.asyncio
async def test_the_backend_is_read_off_the_loop_after_a_load(monkeypatch, tmp_path: Path) -> None:
    """`capabilities()` is a native read behind a preflight gate that can spawn the
    probe child when the wheel changed under a running gateway; `ensure_loaded`
    runs on the websocket handler's task, so that read must not happen on the
    loop thread."""
    monkeypatch.setattr(engine_mod, "_engine", None)
    monkeypatch.setattr(engine_mod, "probe", lambda: engine_mod.Availability(True))
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    stub = tmp_path / "ggml-base.bin"
    stub.write_bytes(b"not a real model")

    async def _ensure(_model):
        return stub

    monkeypatch.setattr(models, "store", lambda: type("S", (), {"ensure": staticmethod(_ensure)})())
    monkeypatch.setattr(
        engine_mod.WhisperEngine, "_build_model", staticmethod(lambda key: _FakeModel())
    )
    loop_thread = threading.get_ident()
    threads: list[int] = []

    def _caps():
        threads.append(threading.get_ident())
        return caps_mod.Capabilities(raw="", detail="stubbed")

    monkeypatch.setattr(engine_mod.WhisperEngine, "capabilities", staticmethod(_caps))
    engine = engine_mod.WhisperEngine(idle_evict_secs=600)

    result = await engine.ensure_loaded("base", "en")

    assert result.ok is True
    assert threads and all(t != loop_thread for t in threads)


def test_the_fuse_is_armed_and_disarmed_on_the_worker_not_the_loop(
    tmp_path: Path, monkeypatch
) -> None:
    """The marker is filesystem I/O, so the loop neither writes nor clears it: the
    worker arms it right before the native call and disarms it in a `finally`. A
    shutdown that cancels the boot prewarm mid-load closes the event loop before
    an executor future can chain a done-callback back into it, so the disarm
    holds with no loop at all."""
    monkeypatch.setattr(models, "models_dir", lambda: tmp_path)
    key = engine_mod.LoadedKey(str(tmp_path / "ggml-base.bin"), "en", 2)

    def _build(_key):
        assert (tmp_path / preflight.LOAD_MARKER_NAME).is_file()
        raise RuntimeError("the loop is gone; nobody is listening")

    monkeypatch.setattr(engine_mod.WhisperEngine, "_build_model", staticmethod(_build))
    assert not (tmp_path / preflight.LOAD_MARKER_NAME).exists()
    with pytest.raises(RuntimeError):
        engine_mod.WhisperEngine._build_model_fused(key, tmp_path)
    assert not (tmp_path / preflight.LOAD_MARKER_NAME).exists()
