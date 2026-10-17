"""Shared fixtures and helpers for the sandbox argv suites.

``test_sandbox_argv.py`` and the four files split from it (``test_sandbox_argv_profile.py``,
``_launcher_plan.py``, ``_spawn.py`` and ``_resource_ceilings.py``) use these by name: the autouse ``clean_backend`` reset, ``systemd_run_resolvable``, the POSIX-only
skip marker, and the seccomp helpers the launcher tests share.
"""

from __future__ import annotations

import struct
import sys

import pytest

import kiro_crew.sandbox as sandbox_mod
from kiro_crew import sandbox_launcher_program as launcher_program
from kiro_crew.sandbox import reset_backend

# ``_build_launcher_script`` calls POSIX-only ``os.getuid``/``os.getgid`` (the
# namespace launcher is Linux-only), so any test that builds the launcher script
# raises AttributeError on Windows. Skip those on win32 -- the reduced-scope
# Windows CI lane runs them, but they pass in the full POSIX suite.
_POSIX_ONLY = pytest.mark.skipif(
    sys.platform == "win32",
    reason="_build_launcher_script uses POSIX-only os.getuid (#2041)",
)

#: The verdicts the launcher's seccomp program returns.
_SECCOMP_ALLOW = 0x7FFF0000
_SECCOMP_EPERM = 0x00050001
_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}


def _seccomp_verdict(machine: str, nr: int, arg0: int, arch: int | None = None) -> int:
    """What the launcher's seccomp program answers for one syscall, by running it.

    The program is classic BPF over ``struct seccomp_data``: the syscall number at
    offset 0, the audit arch at 4, the instruction pointer at 8 and ``args[0]`` at 16,
    as two little-endian 32-bit words -- the low one at 16, the high one at 20.
    """
    data = struct.pack(
        "<iIQQ",
        nr,
        _AUDIT_ARCH[machine] if arch is None else arch,
        0,
        arg0 & 0xFFFF_FFFF_FFFF_FFFF,
    )
    insns = launcher_program.seccomp_program(machine)
    acc = 0
    pc = 0
    while True:
        code, jt, jf, k = struct.unpack("<HBBI", insns[pc])
        if code == 0x20:  # BPF_LD | BPF_W | BPF_ABS
            (acc,) = struct.unpack_from("<I", data, k)
            pc += 1
        elif code == 0x15:  # BPF_JMP | BPF_JEQ | BPF_K
            pc += 1 + (jt if acc == k else jf)
        elif code == 0x06:  # BPF_RET | BPF_K
            return k
        else:  # pragma: no cover - an opcode the program does not use
            raise AssertionError(f"unexpected BPF opcode {code:#x}")


@pytest.fixture()
def systemd_run_resolvable(monkeypatch):
    """Make ``trusted_system_bin("systemd-run")`` resolve on a host without systemd.

    The cgroup-scope tests that request it mock ``_probe_cgroup_scope`` to "available" and
    assert the argv ``cgroup_scope_argv`` BUILDS. That argv is only built when
    the wrapper also resolves from a trusted system directory, and on macOS /
    a container without systemd it never does -- so the code degraded (no
    ceiling, loud warning), and seven tests about argv SHAPE failed for a reason
    that has nothing to do with argv shape. Only ``systemd-run`` is faked; every
    other name still goes through the real resolver, so the tests that assert
    degradation when it is ABSENT (they patch the resolver to ``None``
    themselves, inside their own ``with``) are unaffected -- an inner patch
    wins and reverts to this one.
    """
    from kiro_crew import platform_compat

    real = platform_compat.trusted_system_bin

    def _resolve(name: str) -> str | None:
        if name == "systemd-run":
            return "/usr/bin/systemd-run"
        return real(name)

    monkeypatch.setattr(sandbox_mod.platform_compat, "trusted_system_bin", _resolve)


@pytest.fixture(autouse=True)
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- the sandbox argv suites' autouse reset; none of them calls monkeypatch.undo()
def clean_backend(monkeypatch):
    """Reset cached backend between tests.

    Also neutralize the host's real kiro internal-sandbox setting: on a macOS
    dev box where ``~/.kiro/settings/amazon-internal.json`` has
    ``{"sandbox": true}``, the darwin kiro-delegation branch in ``wrap_argv``
    preempts the mocked ``detect_backend`` and these unit tests — which exercise
    Kiro Crew's OWN backend selection / fail-closed path — never reach the code
    they assert on. Point the settings path at a non-existent file so delegation
    is off by default; the dedicated delegation tests set
    ``_KIRO_INTERNAL_SETTINGS_PATH`` explicitly and are unaffected.

    Clears ``KIROCREW_SANDBOX_ACTIVE`` to prevent the "already inside sandbox"
    passthrough from short-circuiting tests on hosts (like Cloud Desktops) where
    the gateway process itself runs sandboxed. Tests that exercise the
    passthrough set the env var explicitly.

    Pins ``_ssh_supports_accept_new`` at the module seam the namespace plan's host
    facts are read from (``sandbox._live_plan_host``): the real probe runs the host's
    ``ssh -V`` from whichever of the ~30 plan-building tests happens to call it first
    (it is ``lru_cache``d), a host program none of them is about (test-hygiene class
    7). ``True`` is what a modern host answers. ``TestSshSupportsAcceptNew`` still
    exercises the real function through the name it imported, with
    ``subprocess.run`` patched.
    """
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.setattr(
        "kiro_crew.sandbox._KIRO_INTERNAL_SETTINGS_PATH",
        "/nonexistent/kirocrew-test/amazon-internal.json",
    )
    monkeypatch.setattr(sandbox_mod, "_ssh_supports_accept_new", lambda: True)
    # Reset one-shot warning flags
    if hasattr(sandbox_mod.wrap_argv, "_warned"):
        delattr(sandbox_mod.wrap_argv, "_warned")
    if hasattr(sandbox_mod._warn_mode_off_unconfined, "_warned_set"):
        delattr(sandbox_mod._warn_mode_off_unconfined, "_warned_set")
    if hasattr(sandbox_mod._warn_mode_off_unconfined, "_info_logged"):
        delattr(sandbox_mod._warn_mode_off_unconfined, "_info_logged")
    reset_backend()
    yield
    reset_backend()
