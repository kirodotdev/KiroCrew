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

import types

import pytest

import kiro_crew.sandbox as sb


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
        )
    assert "extra_hidden_dirs" in str(excinfo.value)


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
        sb.wrap_argv(["/bin/bash", "-c", "echo hi"], mode="standard")
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
        ["/bin/bash", "-c", "echo hi"], mode="standard", cwd=r"C:\Users\alice"
    )
    assert wrapped == fake_wrapped
    assert cleanup is None


# ── _no_backend_guidance: wsl2 remedy selection ──


@pytest.mark.parametrize(
    "remedy_token,expected_snippet",
    [
        ("REMEDY_WSL2_NOT_INSTALLED", "wsl --install"),
        ("REMEDY_WSL2_NO_DISTRO", "wsl -l -v"),
        ("REMEDY_WSL2_USERNS_REFUSED", "unshare(CLONE_NEWUSER)"),
    ],
)
def test_no_backend_guidance_names_the_wsl2_remedy(monkeypatch, remedy_token, expected_snippet):
    monkeypatch.setattr(sb, "sys", _win32())
    monkeypatch.setattr(sb, "_operator_wants_wsl2", lambda: "Ubuntu-26.04")
    sb._last_wsl2_failure = (False, "some probe reason", remedy_token)
    guidance = sb._no_backend_guidance()
    assert expected_snippet in guidance
    assert "sandbox_allow_unsandboxed_exec" in guidance  # opt-out still named as last resort


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
