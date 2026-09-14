"""Tests for the WSL2 sandbox backend (``agent.sandbox: "wsl2"``, Windows only).

Mirrors ``test_sandbox_backend_cache.py``'s patterns for the existing Linux
unshare probe: transient failures are never cached, positive results cache
for the process lifetime, the event loop is never blocked by a probe. The
WSL2 probe answers a DIFFERENT question (does this distro work, not does
this kernel support userns) and therefore has its OWN cache
(``_wsl2_backend_ok`` / ``_last_wsl2_failure``), which these tests exercise
directly rather than through the Linux ``_backend`` global.

No test here depends on a real WSL2 host or a live ``wsl.exe`` — every
subprocess boundary is mocked, matching how upstream Kiro Crew PR #6808's own
WSL2 discovery work is tested (injected-env unit tests, not a live host).
The mechanism itself (real bash execution, uid/gid/home resolution, DrvFs
``--cd`` translation, and — critically — actual credential-path hiding) was
separately proven live against a real WSL2/Ubuntu-26.04 host during
development; see the PR description for that evidence.
"""

from __future__ import annotations

import ast
import json
import pathlib
import platform
import re
import types

import pytest

import kiro_crew.sandbox as sb

_IS_WINDOWS = platform.system() == "Windows"


@pytest.fixture(autouse=True)
def clean_wsl2_state(monkeypatch):
    """Reset every WSL2-specific cache/global before and after each test."""
    sb.reset_wsl2_backend()
    sb.reset_backend()
    sb._WSL2_IDENTITY_CACHE.clear()
    sb._WSL2_DRVFS_VERIFIED.clear()
    sb._wsl2_warm_thread = None
    sb._wsl2_distro_list_cache = None
    monkeypatch.setattr(sb.time, "sleep", lambda _s: None)
    yield
    sb.reset_wsl2_backend()
    sb.reset_backend()
    sb._WSL2_IDENTITY_CACHE.clear()
    sb._WSL2_DRVFS_VERIFIED.clear()
    sb._wsl2_warm_thread = None
    sb._wsl2_distro_list_cache = None


def _win32():
    """A fake ``sys`` exposing only ``platform="win32"``, mirroring the
    existing suite's ``sb.sys`` monkeypatch trick for hermetic platform
    tests that don't depend on the real host OS."""
    return types.SimpleNamespace(platform="win32")


# ── _operator_wants_wsl2 / wsl2_selected: config reading ──


class _FakeAgent:
    def __init__(self, sandbox="auto", sandbox_wsl_distro=""):
        self.sandbox = sandbox
        self.sandbox_wsl_distro = sandbox_wsl_distro


class _FakeConfig:
    def __init__(self, agent):
        self.agent = agent


def test_operator_wants_wsl2_returns_none_when_not_selected(monkeypatch):
    fake_cfg = _FakeConfig(_FakeAgent(sandbox="auto"))
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: fake_cfg)
    )
    assert sb._operator_wants_wsl2() is None
    assert sb.wsl2_selected() is False


def test_operator_wants_wsl2_returns_distro_when_selected(monkeypatch):
    fake_cfg = _FakeConfig(_FakeAgent(sandbox="wsl2", sandbox_wsl_distro="Ubuntu-26.04"))
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: fake_cfg)
    )
    assert sb._operator_wants_wsl2() == "Ubuntu-26.04"
    assert sb.wsl2_selected() is True


def test_operator_wants_wsl2_empty_distro_means_wsl_default(monkeypatch):
    fake_cfg = _FakeConfig(_FakeAgent(sandbox="wsl2", sandbox_wsl_distro=""))
    monkeypatch.setattr(
        "kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(lambda: fake_cfg)
    )
    assert sb._operator_wants_wsl2() == ""


def test_operator_wants_wsl2_fails_closed_on_config_error(monkeypatch):
    """An unreadable config must never grant a DIFFERENT backend than what
    was actually configured -- matches _allow_no_isolation's own contract."""

    def _raise():
        raise RuntimeError("config disk read failed")

    monkeypatch.setattr("kiro_crew.config.loader.KiroCrewConfig.load", staticmethod(_raise))
    assert sb._operator_wants_wsl2() is None
    assert sb.wsl2_selected() is False


# ── detect_backend(): win32 + wsl2 cache policy ──


def test_detect_backend_wsl2_not_selected_falls_through_unchanged(monkeypatch):
    """No regression: win32 with wsl2 NOT selected behaves exactly as before
    this feature existed -- straight to "none", no wsl2 probing at all."""
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: None)
    probe_calls: list[int] = []
    monkeypatch.setattr(sb, "_probe_wsl2", lambda distro: probe_calls.append(1) or False)
    assert sb.detect_backend(config_mode="standard") == "none"
    assert probe_calls == []


def test_detect_backend_wsl2_selected_and_working(monkeypatch):
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: "Ubuntu-26.04")
    monkeypatch.setattr(sb, "_probe_wsl2", lambda distro: True)
    assert sb.detect_backend(config_mode="standard") == "wsl2"
    assert sb._backend == "wsl2"


def test_detect_backend_wsl2_positive_result_cached_across_calls(monkeypatch):
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: "Ubuntu-26.04")
    calls: list[int] = []
    monkeypatch.setattr(sb, "_probe_wsl2", lambda distro: calls.append(1) or True)
    assert sb.detect_backend(config_mode="standard") == "wsl2"
    assert sb.detect_backend(config_mode="cc") == "wsl2"
    assert len(calls) == 1, "second call should hit the cached _backend, not re-probe"


def test_detect_backend_wsl2_permanent_failure_caches_none(monkeypatch):
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: "Ubuntu-26.04")

    def fake_probe(distro):
        sb._last_wsl2_failure = (
            False,
            "WSL2 has no registered distributions",
            "REMEDY_WSL2_NO_DISTRO",
        )
        return False

    monkeypatch.setattr(sb, "_probe_wsl2", fake_probe)
    assert sb.detect_backend(config_mode="standard") == "none"
    assert sb._backend == "none"


def test_detect_backend_wsl2_transient_failure_not_cached(monkeypatch):
    """Mirrors test_off_mode_short_circuits_without_probing's sibling for the
    Linux probe: a transient WSL2 failure (distro still booting) must not
    poison the cache -- the next spawn re-probes and can self-heal."""
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: "Ubuntu-26.04")
    calls: list[int] = []

    def fake_probe(distro):
        calls.append(1)
        sb._last_wsl2_failure = (True, "wsl.exe did not answer `-l -v`", "")
        return False

    monkeypatch.setattr(sb, "_probe_wsl2", fake_probe)
    assert sb.detect_backend(config_mode="standard") == "none"
    assert sb._backend is None  # NOT cached
    assert sb.detect_backend(config_mode="standard") == "none"
    assert len(calls) == 2, "transient failure must re-probe on the next call"


def test_detect_backend_off_mode_never_probes_wsl2(monkeypatch):
    monkeypatch.setattr(sb, "sys", _win32())
    probe_calls: list[int] = []
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: probe_calls.append(1) or "Ubuntu")
    assert sb.detect_backend(config_mode="off") == "none"
    assert probe_calls == [], "off mode must short-circuit before even checking wsl2 selection"


# ── _probe_wsl2: never-block-on-loop + retry-once discipline ──


def test_probe_wsl2_on_loop_defers_and_returns_false(monkeypatch):
    """Mirrors test_on_loop_cold_cache_returns_none_without_probing: the
    warm thread is itself mocked out (never started for real), so a probe
    call proves the loop tried to run one synchronously rather than a
    background-thread race deciding the outcome."""
    monkeypatch.setattr(
        sb,
        "_probe_wsl2_once",
        lambda distro: (_ for _ in ()).throw(AssertionError("probe called on loop!")),
    )
    monkeypatch.setattr(
        sb.threading,
        "Thread",
        lambda **kw: types.SimpleNamespace(start=lambda: None, is_alive=lambda: True, name="fake"),
    )

    async def _run():
        return sb._probe_wsl2("Ubuntu-26.04")

    import asyncio

    result = asyncio.run(_run())
    assert result is False, "must never probe synchronously on the event loop"
    transient, reason, _remedy = sb._last_wsl2_failure
    assert transient is True
    assert "deferred to background thread" in reason


def test_probe_wsl2_off_loop_retries_once_on_transient(monkeypatch):
    calls: list[int] = []

    def fake_once(distro):
        calls.append(1)
        if len(calls) == 1:
            return (False, True, "wsl.exe did not answer `-l -v`", "")
        return (True, False, "ok", "")

    monkeypatch.setattr(sb, "_probe_wsl2_once", fake_once)
    assert sb._probe_wsl2("Ubuntu-26.04") is True
    assert len(calls) == 2


def test_probe_wsl2_permanent_failure_does_not_retry(monkeypatch):
    calls: list[int] = []

    def fake_once(distro):
        calls.append(1)
        return (False, False, "no such WSL2 distribution", "REMEDY_WSL2_NO_DISTRO")

    monkeypatch.setattr(sb, "_probe_wsl2_once", fake_once)
    assert sb._probe_wsl2("Ubuntu-26.04") is False
    assert len(calls) == 1, "a permanent failure must not spend a second attempt"


def _probe_stubs(monkeypatch, returncode: int, stderr: str = ""):
    monkeypatch.setattr(sb.platform_compat, "trusted_system_bin", lambda name: "wsl.exe")
    monkeypatch.setattr(sb, "_list_wsl2_distros", lambda: {"Ubuntu-26.04": "Running"})
    seen: list[list[str]] = []

    def fake_wsl_run(argv, **kwargs):
        seen.append(argv)
        return types.SimpleNamespace(returncode=returncode, stdout="", stderr=stderr)

    monkeypatch.setattr(sb, "_wsl_run", fake_wsl_run)
    return seen


def test_probe_wsl2_once_checks_python3_before_unshare(monkeypatch):
    """A minimal distro can pass the namespace check and still fail every
    spawn: the launcher runs under python3, so its absence is a permanent,
    named failure at probe time rather than a per-spawn surprise."""
    seen = _probe_stubs(monkeypatch, sb._WSL2_PROBE_NO_PYTHON3_EXIT)
    ok, transient, reason, remedy = sb._probe_wsl2_once("Ubuntu-26.04")
    assert ok is False and transient is False
    assert remedy == "REMEDY_WSL2_NO_PYTHON3"
    assert "python3" in reason
    assert "command -v python3" in seen[0][-1]


def test_probe_wsl2_once_ok_when_python3_and_unshare_both_work(monkeypatch):
    _probe_stubs(monkeypatch, 0)
    assert sb._probe_wsl2_once("Ubuntu-26.04") == (True, False, "ok", "")


def test_probe_wsl2_once_userns_refusal_keeps_its_own_remedy(monkeypatch):
    _probe_stubs(monkeypatch, 1, stderr="unshare: unshare failed: Operation not permitted")
    ok, transient, _reason, remedy = sb._probe_wsl2_once("Ubuntu-26.04")
    assert (ok, transient, remedy) == (False, False, "REMEDY_WSL2_USERNS_REFUSED")


# ── _translate_windows_path_to_wsl2: pure function ──


@pytest.mark.parametrize(
    "windows_path,expected",
    [
        (r"C:\Users\alice\AppData\Local\Test", "/mnt/c/Users/alice/AppData/Local/Test"),
        (r"C:\Program Files\Some App", "/mnt/c/Program Files/Some App"),
        (r"D:\repo", "/mnt/d/repo"),
        ("C:\\", "/mnt/c"),
    ],
)
def test_translate_windows_path_to_wsl2(windows_path, expected):
    assert sb._translate_windows_path_to_wsl2("Ubuntu-26.04", windows_path) == expected


@pytest.mark.parametrize(
    "bad_path",
    [
        r"relative\path",
        "/already/posix",
        r"\\server\share\path",  # UNC: no drive letter
        "",
    ],
)
def test_translate_windows_path_to_wsl2_rejects_non_drive_paths(bad_path):
    with pytest.raises(ValueError):
        sb._translate_windows_path_to_wsl2("Ubuntu-26.04", bad_path)


# ── _wsl_env: the UTF-8 fix ──


def test_wsl_env_forces_utf8(monkeypatch):
    monkeypatch.delenv("WSL_UTF8", raising=False)
    env = sb._wsl_env()
    assert env["WSL_UTF8"] == "1"


# ── wrap_argv: wsl2 dispatch ──


def test_wrap_argv_wsl2_rejects_extra_hidden_dirs(monkeypatch):
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "wsl2")
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: "Ubuntu-26.04")
    with pytest.raises(sb.SandboxUnavailableError) as excinfo:
        sb.wrap_argv(
            ["/bin/bash", "-c", "echo hi"],
            mode="standard",
            extra_hidden_dirs=("/some/extra/dir",),
            posix_shell_argv=True,
        )
    assert "extra_hidden_dirs" in str(excinfo.value)


def test_wrap_argv_posix_shell_argv_false_bypasses_wsl2(monkeypatch):
    """A caller whose argv is not POSIX-shell-shaped (e.g. a native-Windows
    Python invocation) must never be routed through the wsl2 guest launcher
    -- it would append the argv verbatim after itself expecting a POSIX
    command, which a Windows path is not. posix_shell_argv=False makes such
    a caller see the SAME no-backend handling Windows already has, not a
    broken wsl2 dispatch."""
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "wsl2")
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: "Ubuntu-26.04")
    monkeypatch.setattr(sb, "_allow_unsandboxed_exec", lambda: False)

    called = {"wsl_namespace_argv": False}
    monkeypatch.setattr(
        sb, "wsl_namespace_argv", lambda *a, **kw: called.__setitem__("wsl_namespace_argv", True)
    )

    with pytest.raises(sb.SandboxUnavailableError):
        sb.wrap_argv(
            ["C:\\Python\\python.exe", "C:\\Temp\\script.py"],
            mode="standard",
            posix_shell_argv=False,
        )
    assert called["wsl_namespace_argv"] is False, (
        "wsl2 must never see this argv -- it cannot execute a Windows-shaped "
        "command inside the Linux guest"
    )


def test_wrap_argv_posix_shell_argv_defaults_false(monkeypatch):
    """wrap_argv/sandboxed_spawn_argv are shared chokepoints with dozens of
    callers, most building native-Windows executable invocations. The
    default must be opt-IN (False), not opt-out -- an opt-out default would
    silently hand every unexamined caller to the wsl2 guest launcher the
    moment an operator selected it, exactly the regression this test locks
    against. Confirmed on the actual function signatures, not just the wsl2
    dispatch branch, so a future edit to either cannot silently flip it back."""
    import inspect

    assert inspect.signature(sb.wrap_argv).parameters["posix_shell_argv"].default is False
    assert (
        inspect.signature(sb.sandboxed_spawn_argv).parameters["posix_shell_argv"].default is False
    )
    assert inspect.signature(sb.wrap_argv_async).parameters["posix_shell_argv"].default is False
    assert (
        inspect.signature(sb.sandboxed_spawn_argv_async).parameters["posix_shell_argv"].default
        is False
    )

    # And behaviorally: a caller that does NOT pass posix_shell_argv at all
    # (the shape of every one of the ~9 unrelated production call sites this
    # was caught against) must see wsl2 report unavailable, not be routed
    # through the guest launcher.
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "wsl2")
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: "Ubuntu-26.04")
    monkeypatch.setattr(sb, "_allow_unsandboxed_exec", lambda: False)
    called = {"wsl_namespace_argv": False}
    monkeypatch.setattr(
        sb, "wsl_namespace_argv", lambda *a, **kw: called.__setitem__("wsl_namespace_argv", True)
    )
    with pytest.raises(sb.SandboxUnavailableError):
        # A representative native-Windows call, e.g. an npm install or a git
        # command -- no posix_shell_argv passed, matching real call sites.
        sb.wrap_argv(["npm", "install"], mode="standard")
    assert called["wsl_namespace_argv"] is False


def test_wrap_argv_wsl2_setup_failure_becomes_sandbox_unavailable(monkeypatch):
    """detect_backend already confirmed the probe passes for this distro, so
    a failure reaching wsl_namespace_argv is reported as retryable
    (transient), not a permanent host verdict -- see wrap_argv's own comment
    at the wsl2 dispatch arm."""
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "wsl2")
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: "Ubuntu-26.04")

    def fake_wsl_namespace_argv(*args, **kwargs):
        raise RuntimeError("failed to stage WSL2 launcher script: disk full")

    monkeypatch.setattr(sb, "wsl_namespace_argv", fake_wsl_namespace_argv)
    with pytest.raises(sb.SandboxUnavailableError) as excinfo:
        sb.wrap_argv(["/bin/bash", "-c", "echo hi"], mode="standard", posix_shell_argv=True)
    assert excinfo.value.kind == "transient"
    assert "disk full" in str(excinfo.value)


def test_wrap_argv_wsl2_success_returns_no_cleanup(monkeypatch):
    """No Windows-reachable cleanup path exists for a guest-staged launcher
    -- confirms wrap_argv reports that honestly rather than returning a
    Windows path os.unlink would raise on."""
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "wsl2")
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: "Ubuntu-26.04")
    fake_wrapped = [
        "wsl.exe",
        "-d",
        "Ubuntu-26.04",
        "--",
        "python3",
        "/home/x/launcher.py",
        "/bin/bash",
        "-c",
        "echo hi",
    ]
    monkeypatch.setattr(sb, "wsl_namespace_argv", lambda *a, **kw: fake_wrapped)
    wrapped, cleanup = sb.wrap_argv(
        ["/bin/bash", "-c", "echo hi"],
        mode="standard",
        cwd=r"C:\Users\alice",
        posix_shell_argv=True,
    )
    assert wrapped == fake_wrapped
    assert cleanup is None


# ── wsl_namespace_argv: masking the operator's REAL Windows sensitive dirs ──
#
# Every test below drives wsl_namespace_argv directly (not through the
# wrap_argv-level mock the tests above use), because that mock is exactly
# what would hide a regression here: the masking logic lives entirely
# inside this function, between identity resolution and the
# _build_launcher_script call.


#: A fixed, Windows-shaped stand-in for the real host's home directory.
#: wsl_namespace_argv() calls the real Path.home() to build its Windows-side
#: masking list, and the CI matrix runs these Windows-only tests on Linux
#: runners (real value: /home/runner, not drive-rooted) -- unmocked, the
#: tests passed by accident on a Windows dev machine and failed on Linux CI.
_FAKE_WINDOWS_HOME = "C:\\Users\\alice"


def _stub_wsl_namespace_deps(monkeypatch, *, drvfs_ok: bool = True):
    """Common plumbing so each test only sets what it actually varies."""
    monkeypatch.setattr(sb, "_resolve_wsl2_identity", lambda distro: (1000, 1000, "/home/alice"))
    monkeypatch.setattr(sb, "_verify_wsl2_drvfs_mount", lambda distro: drvfs_ok)
    monkeypatch.setattr(sb.platform_compat, "trusted_system_bin", lambda name: "wsl.exe")
    monkeypatch.setattr(sb.Path, "home", staticmethod(lambda: sb.Path(_FAKE_WINDOWS_HOME)))
    # Both resolve via the REAL config_dir()/KIROCREW_HOME, which the test
    # harness pins to an isolated POSIX tmp dir on this (Linux CI) host --
    # inconsistent with the mocked Windows home above, and not what these
    # tests exist to exercise (that relocation-detection logic is
    # pre-existing and covered elsewhere). Neutralized to the common case.
    monkeypatch.setattr(sb, "_relocated_policy_cache_dirs", lambda: [])
    monkeypatch.setattr(sb, "_relocated_crew_targets", lambda leaves: [])
    monkeypatch.setattr(sb, "_voice_runtime_sandbox_paths", lambda: ())
    monkeypatch.setattr(sb, "_voice_runtime_parent_paths", lambda: ())

    def fake_wsl_run(argv, **kwargs):
        return types.SimpleNamespace(returncode=0, stdout="/home/alice/.kirocrew-sandbox-run/x")

    monkeypatch.setattr(sb, "_wsl_run", fake_wsl_run)


def test_wsl_namespace_argv_masks_real_windows_sensitive_dirs(monkeypatch):
    """The regression this function exists to close: DrvFs makes the
    operator's REAL Windows filesystem reachable inside the guest at
    /mnt/<drive>, so hiding only the guest's own (empty) home leaves real
    credentials and the governance keystone readable. This asserts the
    translated Windows-side paths actually reach _build_launcher_script."""
    _stub_wsl_namespace_deps(monkeypatch)
    captured: dict = {}

    def fake_build_launcher_script(sandbox_level, **kwargs):
        captured.update(kwargs)
        return "# launcher"

    monkeypatch.setattr(sb, "_build_launcher_script", fake_build_launcher_script)

    sb.wsl_namespace_argv(["/bin/bash", "-c", "echo hi"], distro="Ubuntu-26.04")

    assert captured["identity"] == (1000, 1000, "/home/alice")
    extra = captured["extra_hidden_dirs"]
    home = str(sb.Path.home())
    aws_guest = sb._translate_windows_path_to_wsl2("Ubuntu-26.04", sb.os.path.join(home, ".aws"))
    ssh_guest = sb._translate_windows_path_to_wsl2("Ubuntu-26.04", sb.os.path.join(home, ".ssh"))
    assert (
        aws_guest in extra
    ), "the operator's REAL .aws must be masked, not just the guest's empty one"
    assert ssh_guest in extra
    assert all(
        p.startswith("/") for p in extra
    ), "every masked path must already be guest-POSIX form"


def test_wsl_namespace_argv_seals_the_real_windows_ceilings_read_only(monkeypatch):
    """The crew governance tree keeps its three dispositions across DrvFs:
    secret leaves hidden, ceilings sealed read-only (hiding a ceiling would
    resolve it to the permissive default), both against the WINDOWS data
    home rather than the guest's empty one."""
    _stub_wsl_namespace_deps(monkeypatch)
    captured: dict = {}

    def fake_build_launcher_script(sandbox_level, **kwargs):
        captured.update(kwargs)
        return "# launcher"

    monkeypatch.setattr(sb, "_build_launcher_script", fake_build_launcher_script)

    sb.wsl_namespace_argv(["/bin/bash", "-c", "echo hi"], distro="Ubuntu-26.04")

    home = str(sb.Path.home())

    def guest(rel: str) -> str:
        return sb._translate_windows_path_to_wsl2("Ubuntu-26.04", sb.os.path.join(home, rel))

    hidden = set(captured["extra_hidden_dirs"])
    readonly = set(captured["extra_readonly_dirs"])
    for prefix in sb._CREW_HOME_PREFIXES:
        for leaf in sb._CREW_HIDDEN_LEAVES:
            assert guest(f"{prefix}/{leaf}") in hidden, f"{prefix}/{leaf} must be hidden"
        for leaf in sb._CREW_READONLY_LEAVES:
            assert guest(f"{prefix}/{leaf}") in readonly, f"{prefix}/{leaf} must be sealed"
    assert not hidden & readonly, "a path cannot be both hidden and sealed"
    assert captured["unlink_self"] is True, "a guest-staged launcher must clean itself up"


def test_wsl_namespace_argv_seals_the_kiro_agents_tree(monkeypatch):
    """Fork governance sanitizes the specs in this tree (allowedTools ceiling,
    autoApprove strip), so a sandboxed process that can rewrite one hands its
    next spawn forged grants. ``_build_launcher_script``'s own resolution of it
    is builder-host and names a WINDOWS path under an identity, which the guest
    resolves as relative, never matches, and silently skips -- so the seal has to
    be re-derived here, DrvFs-translated, like the crew ceilings."""
    _stub_wsl_namespace_deps(monkeypatch)
    monkeypatch.setattr(
        sb, "_resolved_kiro_agents_targets", lambda: [r"C:\Users\alice\.kiro\agents"]
    )
    captured: dict = {}

    def fake_build_launcher_script(sandbox_level, **kwargs):
        captured.update(kwargs)
        return "# launcher"

    monkeypatch.setattr(sb, "_build_launcher_script", fake_build_launcher_script)

    sb.wsl_namespace_argv(["/bin/bash", "-c", "echo hi"], distro="Ubuntu-26.04")

    assert "/mnt/c/Users/alice/.kiro/agents" in captured["extra_readonly_dirs"]
    assert not any(
        "\\" in path for path in captured["extra_readonly_dirs"]
    ), "no Windows-spelled path may reach the guest script"


def test_wsl2_windows_side_masking_carries_the_relocated_and_runtime_paths(monkeypatch):
    """Every builder-host resolution the launcher skips under an identity is
    re-derived here against the Windows side, so nothing is dropped."""
    monkeypatch.setattr(sb.Path, "home", staticmethod(lambda: sb.Path(_FAKE_WINDOWS_HOME)))
    monkeypatch.setattr(sb, "_relocated_policy_cache_dirs", lambda: [r"D:\crew\policy_cache"])
    monkeypatch.setattr(sb, "_voice_runtime_sandbox_paths", lambda: (r"D:\crew\run\voice-runtime",))
    monkeypatch.setattr(sb, "_voice_runtime_parent_paths", lambda: (r"D:\crew\run",))

    def fake_relocated(leaves):
        return [rf"D:\crew\{leaf}" for leaf in leaves]

    monkeypatch.setattr(sb, "_relocated_crew_targets", fake_relocated)

    hidden, readonly = sb._wsl2_windows_side_masking("strict")

    assert r"D:\crew\policy_cache" in hidden
    assert r"D:\crew\run\voice-runtime" in hidden
    assert rf"D:\crew\{sb._CREW_HIDDEN_LEAVES[0]}" in hidden
    assert sb.os.path.join(_FAKE_WINDOWS_HOME, ".ssh") in hidden
    assert r"D:\crew\security_policy.json" in readonly
    assert r"D:\crew\run" in readonly
    assert sb.os.path.join(_FAKE_WINDOWS_HOME, ".kiro/crew/security_policy.json") in readonly


def test_build_launcher_script_under_an_identity_uses_only_caller_supplied_host_paths(
    monkeypatch,
):
    """With an identity the builder is not the target host, so its own
    config_dir()/Path.home() resolutions must not leak Windows paths into
    the guest script; the caller-supplied sets land in both loops instead."""

    def _refuse(resolver: str):
        """Bound per name, not closed over the loop variable: a lambda closing
        over ``name`` reports whichever name the loop ended on, so every one of
        these failures blamed the last entry and sent the reader to the wrong
        call site."""

        def _raise(*_args, **_kwargs):
            raise AssertionError(f"{resolver} resolved under an identity")

        return _raise

    for name in (
        "_relocated_policy_cache_dirs",
        "_relocated_crew_targets",
        "_voice_runtime_sandbox_paths",
        "_voice_runtime_parent_paths",
        "_pod_os_home_targets",
        "_md_notebook_degraded_mask_dirs",
        # The last one to be gated. Ungated it named a WINDOWS path, which the
        # guest's own os.path.exists resolved as RELATIVE to the launcher's cwd,
        # never matched, and silently skipped -- so fork governance's sanitized
        # specs stayed writable through DrvFs.
        "_resolved_kiro_agents_targets",
    ):
        monkeypatch.setattr(sb, name, _refuse(name))
    script = sb._build_launcher_script(
        "strict",
        identity=(1000, 1000, "/home/alice"),
        extra_hidden_dirs=("/mnt/c/Users/alice/.aws",),
        extra_readonly_dirs=("/mnt/c/Users/alice/.kiro/crew/security_policy.json",),
        unlink_self=True,
    )
    assert "/mnt/c/Users/alice/.aws" in script
    assert "/mnt/c/Users/alice/.kiro/crew/security_policy.json" in script
    assert "/home/alice/.kiro/crew/security_policy.json" in script
    assert "os.unlink(os.path.abspath(__file__))" in script


@pytest.mark.skipif(_IS_WINDOWS, reason="_build_launcher_script bakes os.getuid/getgid, POSIX-only")
def test_build_launcher_script_native_path_never_unlinks_itself():
    """The Linux spawner deletes the launcher it staged; the self-unlink
    stanza is a guest-only opt-in and must stay out of the native script."""
    script = sb._build_launcher_script("strict")
    assert "os.unlink(os.path.abspath(__file__))" not in script


def test_wsl_namespace_argv_fails_closed_when_drvfs_unverifiable(monkeypatch):
    """No masking list can be trusted if DrvFs isn't confirmed at the
    location the translator assumes -- refuse rather than sandbox unmasked."""
    _stub_wsl_namespace_deps(monkeypatch, drvfs_ok=False)
    with pytest.raises(RuntimeError, match="cannot verify WSL2 distro"):
        sb.wsl_namespace_argv(["/bin/bash", "-c", "echo hi"], distro="Ubuntu-26.04")


def test_wsl_namespace_argv_fails_closed_on_root_guest_identity(monkeypatch):
    """A guest reporting uid=0 can unmount or chmod around whatever the
    launcher hides, so this backend's whole trust-the-guest-identity premise
    breaks -- refuse rather than build a launcher a root guest could defeat."""
    _stub_wsl_namespace_deps(monkeypatch)
    monkeypatch.setattr(sb, "_resolve_wsl2_identity", lambda distro: (0, 0, "/root"))
    with pytest.raises(RuntimeError, match="uid=0"):
        sb.wsl_namespace_argv(["/bin/bash", "-c", "echo hi"], distro="Ubuntu-26.04")


def test_wsl_namespace_argv_stages_the_launcher_in_one_round_trip(monkeypatch):
    """Two separate wsl.exe calls (create, then write) left an empty,
    discoverable file for a same-UID sibling to race between them. Staging
    must now be exactly one call, with noclobber protecting the write."""
    _stub_wsl_namespace_deps(monkeypatch)
    monkeypatch.setattr(sb, "_build_launcher_script", lambda *a, **kw: "# launcher")
    calls: list[list[str]] = []

    def fake_wsl_run(argv, **kwargs):
        calls.append(argv)
        return types.SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(sb, "_wsl_run", fake_wsl_run)

    sb.wsl_namespace_argv(["/bin/bash", "-c", "echo hi"], distro="Ubuntu-26.04")

    assert len(calls) == 1, "staging must be a single wsl.exe round trip, not create-then-write"
    script_arg = calls[0][-1]
    assert (
        "set -C" in script_arg
    ), "the write must be noclobber-protected against a same-UID sibling"
    assert f"-mmin +{sb._WSL2_STALE_LAUNCHER_MINUTES} -delete" in script_arg
    assert "|| true" in script_arg, "a failed sweep must never decide whether the spawn stages"


def test_wsl_namespace_argv_seals_the_guest_staging_dir_read_only(monkeypatch):
    """A launcher is read by the guest interpreter after staging and BEFORE
    unshare(), so a same-UID sibling that can replace the file in that window
    runs its own code with no isolation applied. ``set -C`` stops a sibling
    PRE-creating the name; only a read-only seal on the staging directory stops
    one REPLACING it. The native backend gets the same property free, from the
    seal on ``config_dir()/run``."""
    _stub_wsl_namespace_deps(monkeypatch)
    captured: dict = {}

    def fake_build_launcher_script(sandbox_level, **kwargs):
        captured.update(kwargs)
        return "# launcher"

    monkeypatch.setattr(sb, "_build_launcher_script", fake_build_launcher_script)

    argv = sb.wsl_namespace_argv(["/bin/bash", "-c", "echo hi"], distro="Ubuntu-26.04")

    run_dir = "/home/alice/.kirocrew-sandbox-run"
    assert run_dir in captured["extra_readonly_dirs"], (
        "the staging directory must be sealed inside every launcher's namespace, "
        "or a sandboxed sibling can swap a launcher out before isolation applies"
    )
    assert run_dir not in captured["extra_hidden_dirs"], (
        "hidden, not sealed, would mask the launcher the guest interpreter is " "about to read"
    )
    staged = argv[argv.index("python3") + 1 + len(sb._LAUNCHER_INTERPRETER_FLAGS)]
    assert staged.startswith(run_dir + "/"), "the seal must cover the path actually staged"


def test_guest_staging_seal_reaches_the_generated_launcher_readonly_dirs(monkeypatch):
    """End to end through the REAL builder: a seal that never reaches
    ``READONLY_DIRS`` is not a seal, and the mocked-builder test above cannot
    see the filter the builder applies to caller-supplied entries."""
    _stub_wsl_namespace_deps(monkeypatch)
    scripts: list[str] = []

    def fake_wsl_run(argv, **kwargs):
        if kwargs.get("input_text"):
            scripts.append(kwargs["input_text"])
        return types.SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(sb, "_wsl_run", fake_wsl_run)
    monkeypatch.setattr(sb, "_resolved_kiro_agents_targets", lambda: [])

    sb.wsl_namespace_argv(["/bin/bash", "-c", "echo hi"], "cc", distro="Ubuntu-26.04")

    readonly = json.loads(re.search(r"^READONLY_DIRS = (\[.*?\])$", scripts[0], re.M).group(1))
    assert "/home/alice/.kirocrew-sandbox-run" in readonly


def test_wsl_namespace_argv_materializes_the_windows_side_ceilings(monkeypatch):
    """A seal the launcher emits for an ABSENT path is not a seal: both guest
    loops skip a target that does not exist, so on a default install the
    Windows-side data home stayed writable through DrvFs at every ceiling name,
    ``computer_use.json`` included -- a sandboxed workload that can create it
    turns on desktop control for itself. The Windows builder has to create them,
    exactly as ``namespace_argv`` does on Linux, because it is the same
    filesystem the guest reaches at ``/mnt/<drive>``."""
    _stub_wsl_namespace_deps(monkeypatch)
    monkeypatch.setattr(sb, "_build_launcher_script", lambda *a, **kw: "# launcher")
    root = sb.config_dir()
    root.mkdir(parents=True, exist_ok=True)
    keystone = root / "computer_use.json"
    assert not keystone.exists(), "the per-test data home must start without a keystone"

    sb.wsl_namespace_argv(["/bin/bash", "-c", "echo hi"], distro="Ubuntu-26.04")

    assert keystone.exists(), (
        "the keystone must exist before the guest mounts, or its READONLY_DIRS "
        "entry is skipped and the sandbox can create it through DrvFs"
    )
    for leaf in sb._CREW_PRECREATE_READONLY_FILE_LEAVES:
        assert (root / leaf).exists(), f"{leaf} must be sealable"
    for leaf in sb._CREW_PRECREATE_READONLY_DIR_LEAVES:
        assert (root / leaf).is_dir(), f"{leaf} must be sealable"
    for leaf in sb._CREW_PRECREATE_HIDDEN_DIR_LEAVES:
        assert (root / leaf).is_dir(), f"{leaf} must be maskable"


def test_wrap_argv_wsl2_does_not_relabel_an_unsealable_ceiling_as_transient(monkeypatch):
    """A ceiling that cannot be made sealable is a permanent host problem an
    operator must fix, so it must reach the caller as itself rather than as the
    retryable probe-passed-then-changed failure the wsl2 arm reports for
    everything else. The namespace backend does not wrap it either."""
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "wsl2")
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: "Ubuntu-26.04")

    def fake_wsl_namespace_argv(*args, **kwargs):
        raise sb.SandboxCeilingUnsealable("cannot create the governance ceiling X")

    monkeypatch.setattr(sb, "wsl_namespace_argv", fake_wsl_namespace_argv)
    with pytest.raises(sb.SandboxCeilingUnsealable):
        sb.wrap_argv(["/bin/bash", "-c", "echo hi"], mode="standard", posix_shell_argv=True)


def test_wsl_namespace_argv_refuses_an_untranslatable_cwd_legibly(monkeypatch):
    """A *cwd* is the one input here that is neither ours nor the operator's
    config -- it is wherever the caller happens to be running. A UNC path or a
    mapped network drive is not drive-rooted, so the translator correctly
    refuses it, and a bare ValueError would escape ``wrap_argv`` (whose wsl2 arm
    catches RuntimeError) as an unhandled traceback at callers that expect a
    wrapped argv or SandboxUnavailableError."""
    _stub_wsl_namespace_deps(monkeypatch)
    monkeypatch.setattr(sb, "_build_launcher_script", lambda *a, **kw: "# launcher")
    with pytest.raises(RuntimeError, match="no\n?.*/mnt/<drive> equivalent|/mnt/<drive>"):
        sb.wsl_namespace_argv(
            ["/bin/bash", "-c", "echo hi"],
            distro="Ubuntu-26.04",
            cwd=r"\\server\share\app-sources\x",
        )


def test_wrap_argv_wsl2_reports_an_untranslatable_cwd_as_sandbox_unavailable(monkeypatch):
    """The same refusal seen from the chokepoint every caller actually uses."""
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "wsl2")
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: "Ubuntu-26.04")
    _stub_wsl_namespace_deps(monkeypatch)
    monkeypatch.setattr(sb, "_build_launcher_script", lambda *a, **kw: "# launcher")
    monkeypatch.setattr(sb, "_materialize_sealable_ceilings", lambda: [])
    monkeypatch.setattr(sb, "_materialize_maskable_dirs", lambda: [])
    with pytest.raises(sb.SandboxUnavailableError):
        sb.wrap_argv(
            ["/bin/bash", "-c", "echo hi"],
            mode="standard",
            cwd=r"\\server\share\app-sources\x",
            posix_shell_argv=True,
        )


def test_wsl_namespace_argv_staged_path_is_random_and_unique(monkeypatch):
    _stub_wsl_namespace_deps(monkeypatch)
    monkeypatch.setattr(sb, "_build_launcher_script", lambda *a, **kw: "# launcher")
    argv1 = sb.wsl_namespace_argv(["/bin/bash", "-c", "echo hi"], distro="Ubuntu-26.04")
    argv2 = sb.wsl_namespace_argv(["/bin/bash", "-c", "echo hi"], distro="Ubuntu-26.04")
    staged1 = argv1[argv1.index("python3") + 1 + len(sb._LAUNCHER_INTERPRETER_FLAGS)]
    staged2 = argv2[argv2.index("python3") + 1 + len(sb._LAUNCHER_INTERPRETER_FLAGS)]
    assert staged1.startswith("/home/alice/.kirocrew-sandbox-run/kirocrew_sandbox_")
    assert staged1 != staged2, "each spawn must get its own unpredictable name"


def test_wsl_namespace_argv_runs_the_launcher_with_the_native_interpreter_flags(monkeypatch):
    """Without ``-S`` a same-UID workload's usercustomize.py in the guest's
    site-packages runs before the script reaches unshare(), unconfined and
    with DrvFs in reach. The flags sit where namespace_argv puts them so
    _launcher_script_of reads the same slot on both backends."""
    _stub_wsl_namespace_deps(monkeypatch)
    monkeypatch.setattr(sb, "_build_launcher_script", lambda *a, **kw: "# launcher")
    argv = sb.wsl_namespace_argv(["/bin/bash", "-c", "echo hi"], distro="Ubuntu-26.04")
    i = argv.index("python3")
    assert tuple(argv[i + 1 : i + 1 + len(sb._LAUNCHER_INTERPRETER_FLAGS)]) == (
        sb._LAUNCHER_INTERPRETER_FLAGS
    )
    assert "-S" in sb._LAUNCHER_INTERPRETER_FLAGS
    assert argv[i + 1 + len(sb._LAUNCHER_INTERPRETER_FLAGS)].endswith(".py")


# ── wsl2_env_passthrough: WSLENV, without which a Windows env var never
# reaches the guest shell wsl.exe starts ──


def test_wsl2_env_passthrough_noop_when_wsl2_not_selected(monkeypatch):
    monkeypatch.setattr(sb, "wsl2_selected", lambda: False)
    env = {"PATH": "/usr/bin"}
    result = sb.wsl2_env_passthrough(env, ("KIROCREW_HOOK_EVENT",))
    assert result is env
    assert "WSLENV" not in result


def test_wsl2_env_passthrough_noop_on_empty_names(monkeypatch):
    monkeypatch.setattr(sb, "wsl2_selected", lambda: True)
    env = {"PATH": "/usr/bin"}
    result = sb.wsl2_env_passthrough(env, ())
    assert result is env


def test_wsl2_env_passthrough_sets_wslenv(monkeypatch):
    monkeypatch.setattr(sb, "wsl2_selected", lambda: True)
    env = {"KIROCREW_HOOK_EVENT": "Stop", "KIROCREW_HOOK_CONTEXT": "{}"}
    result = sb.wsl2_env_passthrough(env, ("KIROCREW_HOOK_EVENT", "KIROCREW_HOOK_CONTEXT"))
    assert result["WSLENV"] == "KIROCREW_HOOK_EVENT:KIROCREW_HOOK_CONTEXT"
    assert result is not env, "must return a new dict, not mutate the caller's"


def test_wsl2_env_passthrough_merges_with_existing_wslenv(monkeypatch):
    monkeypatch.setattr(sb, "wsl2_selected", lambda: True)
    env = {"WSLENV": "SOME_OTHER_VAR", "NEW_VAR": "1"}
    result = sb.wsl2_env_passthrough(env, ("NEW_VAR",))
    assert result["WSLENV"] == "SOME_OTHER_VAR:NEW_VAR"


# ── _no_backend_guidance: wsl2 remedy selection ──


@pytest.mark.parametrize(
    "remedy_token,expected_snippet",
    [
        ("REMEDY_WSL2_NOT_INSTALLED", "wsl --install"),
        ("REMEDY_WSL2_NO_DISTRO", "wsl -l -v"),
        ("REMEDY_WSL2_USERNS_REFUSED", "unshare(CLONE_NEWUSER)"),
        ("REMEDY_WSL2_USERNS_REFUSED", "apparmor_restrict_unprivileged_userns"),
        ("REMEDY_WSL2_NO_PYTHON3", "python3"),
    ],
)
def test_no_backend_guidance_names_the_wsl2_remedy(monkeypatch, remedy_token, expected_snippet):
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: "Ubuntu-26.04")
    sb._last_wsl2_failure = (False, "some probe reason", remedy_token)
    guidance = sb._no_backend_guidance()
    assert expected_snippet in guidance
    assert "sandbox_allow_unsandboxed_exec" in guidance  # opt-out still named as last resort


def test_no_backend_guidance_names_the_argv_shape_when_the_distro_works(monkeypatch):
    """A working wsl2 distro plus a non-POSIX argv lands on the no-backend
    path with no recorded probe failure; the guidance must say what was
    refused rather than send the operator after a probe that passed."""
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: "Ubuntu-26.04")
    sb._last_wsl2_failure = None
    guidance = sb._no_backend_guidance()
    assert "cannot confine" in guidance
    assert "probe" not in guidance
    assert "sandbox_allow_unsandboxed_exec" in guidance


def test_no_backend_guidance_ignored_when_wsl2_not_selected(monkeypatch):
    """Confirms the win32 branch is gated on selection, not just platform --
    a Windows host that never opted into wsl2 gets the generic message,
    which may MENTION wsl2 as an available option but must not describe a
    specific wsl2 failure reason nobody asked about."""
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: None)
    sb._last_wsl2_failure = (False, "some probe reason", "REMEDY_WSL2_NO_DISTRO")
    guidance = sb._no_backend_guidance()
    assert "some probe reason" not in guidance
    assert "sandbox_allow_unsandboxed_exec" in guidance


# ── wsl2_distro_choices: the picker's option list ──


def test_wsl2_distro_choices_non_windows_is_just_default(monkeypatch):
    monkeypatch.setattr(sb.platform_compat, "IS_WINDOWS", False)
    assert sb.wsl2_distro_choices() == [""]


def test_wsl2_distro_choices_filters_docker_desktop(monkeypatch):
    """Regression for the exact situation this feature was built for: a real
    host with one general-purpose distro plus Docker Desktop's own WSL2
    utility instances, which are not meant to run arbitrary workloads."""
    monkeypatch.setattr(sb.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        sb,
        "_list_wsl2_distros",
        lambda: {
            "Ubuntu-26.04": "Stopped",
            "docker-desktop": "Running",
            "docker-desktop-data": "Running",
        },
    )
    assert sb.wsl2_distro_choices() == ["", "Ubuntu-26.04"]


def test_wsl2_distro_choices_sorted_with_default_first(monkeypatch):
    monkeypatch.setattr(sb.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(
        sb, "_list_wsl2_distros", lambda: {"Zebra-Linux": "Stopped", "Alpine": "Running"}
    )
    assert sb.wsl2_distro_choices() == ["", "Alpine", "Zebra-Linux"]


def test_wsl2_distro_choices_degrades_to_default_only_on_listing_failure(monkeypatch):
    monkeypatch.setattr(sb.platform_compat, "IS_WINDOWS", True)
    monkeypatch.setattr(sb, "_list_wsl2_distros", lambda: None)
    assert sb.wsl2_distro_choices() == [""]


def test_list_wsl2_distros_is_cached_across_calls(monkeypatch):
    calls: list[int] = []

    def fake_wsl_run(argv, **kwargs):
        calls.append(1)
        result = types.SimpleNamespace(
            returncode=0, stdout="NAME  STATE  VERSION\nUbuntu-26.04  Stopped  2\n"
        )
        return result

    monkeypatch.setattr(sb.platform_compat, "trusted_system_bin", lambda name: "wsl.exe")
    monkeypatch.setattr(sb, "_wsl_run", fake_wsl_run)
    first = sb._list_wsl2_distros()
    second = sb._list_wsl2_distros()
    assert first == second == {"Ubuntu-26.04": "Stopped"}
    assert len(calls) == 1, "second call within the TTL window must hit the cache, not re-shell out"


# ── Which POSIX-shell-shaped spawns opt in to wsl2 ──
#
# ``posix_shell_argv`` is opt-IN, so a call site that forgets it does not fail
# loudly: wrap_argv quietly reports no backend and the spawn fail-closes on
# Windows. That is how the command-cron shell probe shipped without routing
# through wsl2 at all, which made the headline capability unreachable while
# every test stayed green. This table is the ratchet: every ``[shell, "-c", X]``
# argv handed to a sandbox chokepoint is listed with the answer it is supposed
# to give, so adding a site or flipping one has to be a deliberate edit here.


#: ``(module, the third argv element's spelling) -> expected posix_shell_argv``.
#: The ``False`` entries are DELIBERATE: each runs a command whose answer belongs
#: to the HOST, not the guest, so routing it into WSL2 would change what it means
#: rather than confine it. Each carries the same reasoning at its own call site.
_POSIX_SHELL_SPAWN_SITES: dict[tuple[str, str], bool] = {
    # An app's own lifecycle scripts and its install script: third-party code,
    # which is what this backend exists to confine.
    ("apps/lifecycle_scripts.py", "safe_script"): True,
    ("apps/registry.py", "safe_script"): True,
    # Command crons: the probe that decides whether a POSIX shell exists at all,
    # and the spawn that then uses it. The probe is the one that must opt in for
    # the other to ever be reached.
    ("cron_script.py", "'echo x.{a,a}'"): True,
    ("cron_script.py", "command"): True,
    # Script hooks.
    ("hooks.py", "hook.command"): True,
    # detectInstalled asks whether the app is already present ON THIS MACHINE.
    ("apps/registry.py", "detect_cmd"): False,
    # openCommand launches the app on the operator's own desktop.
    ("apps/routes.py", "open_cmd"): False,
}

_SANDBOX_CHOKEPOINTS = frozenset(
    {"wrap_argv", "wrap_argv_async", "sandboxed_spawn_argv", "sandboxed_spawn_argv_async"}
)


def _shell_shaped(node) -> bool:
    """True for an argv literal of the form ``[<shell>, "-c", <command>]``."""
    return (
        isinstance(node, ast.List)
        and len(node.elts) == 3
        and isinstance(node.elts[1], ast.Constant)
        and node.elts[1].value == "-c"
    )


def _command_spelling(node: ast.List) -> str:
    """How the third argv element is written, as the table keys it."""
    element = node.elts[2]
    if isinstance(element, ast.Name):
        return element.id
    if isinstance(element, ast.Attribute):
        return f"{getattr(element.value, 'id', '?')}.{element.attr}"
    if isinstance(element, ast.Constant):
        return repr(element.value)
    return ast.dump(element)


def _called_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _discover_posix_shell_spawn_sites() -> dict[tuple[str, str], bool]:
    """Every POSIX-shell argv reaching a sandbox chokepoint, and its opt-in state."""
    root = pathlib.Path(sb.__file__).parent
    found: dict[tuple[str, str], bool] = {}
    for path in sorted(root.rglob("*.py")):
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # Cheap pre-filter before the parse: the package is ~2k modules and
        # ast.parse on all of them costs about half a minute, which is a lot to
        # pay on every run for a handful of call sites. A module with no
        # chokepoint name in its text cannot contain a call to one.
        if not any(name in source for name in _SANDBOX_CHOKEPOINTS):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            # A module this test cannot parse is not one it can vouch for, but it
            # is also not this test's subject: the chokepoints live in files that
            # parse. Skipping keeps an unrelated syntax problem from surfacing
            # here as a sandbox finding.
            continue
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            # Resolve the common ``base_cmd = [...]`` indirection: the argv is
            # almost never written inline at the call itself.
            shell_argvs: dict[str, ast.List] = {}
            for node in ast.walk(func):
                if isinstance(node, ast.Assign) and _shell_shaped(node.value):
                    for target in node.targets:
                        if isinstance(target, ast.Name):
                            shell_argvs[target.id] = node.value
            for node in ast.walk(func):
                if not (isinstance(node, ast.Call) and _called_name(node) in _SANDBOX_CHOKEPOINTS):
                    continue
                if not node.args:
                    continue
                first = node.args[0]
                argv: ast.List | None = None
                if _shell_shaped(first):
                    argv = first  # type: ignore[assignment]
                elif isinstance(first, ast.Name):
                    argv = shell_argvs.get(first.id)
                if argv is None:
                    continue
                key = (path.relative_to(root).as_posix(), _command_spelling(argv))
                opted_in = any(
                    kw.arg == "posix_shell_argv"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                    for kw in node.keywords
                )
                found[key] = found.get(key, False) or opted_in
    return found


def test_every_posix_shell_spawn_declares_its_wsl2_disposition():
    """A POSIX-shell spawn that silently defaults to ``posix_shell_argv=False``
    reads as ordinary fail-closed-on-Windows rather than as a bug, so nothing
    goes red when one is missed. Pin the set and each site's answer instead."""
    assert _discover_posix_shell_spawn_sites() == _POSIX_SHELL_SPAWN_SITES


def test_the_command_cron_shell_probe_routes_through_wsl2():
    """This probe decides whether command crons exist on the host at all: left
    off the wsl2 backend it fail-closes, caches False, and
    ``_resolve_command_shell`` then returns None -- so a wsl2 host refused every
    command cron with "No POSIX shell available", while gaining a POSIX shell is
    the whole reason to select wsl2."""
    assert _discover_posix_shell_spawn_sites()[("cron_script.py", "'echo x.{a,a}'")] is True
