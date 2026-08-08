"""Tests for sandbox backend probe classification + detect_backend cache policy.

Incident 2026-07-18: one transient fork() failure during a cron spawn burst was
cached by ``detect_backend()`` as "no sandbox backend", fail-closing every
subsequent spawn for ~1 hour until gateway restart. These tests pin the fix:

- transient probe failures (EAGAIN/ENOMEM/EMFILE/...) are classified, logged,
  retried once, and NEVER cached — the next spawn re-probes and self-heals;
- only permanent failures (kernel refuses user namespaces) cache ``"none"``;
- positive results are cached once across all non-"off" modes;
- the fail-closed RuntimeError carries the probe failure reason, and for a
  transient failure it does NOT advise disabling the sandbox.

Event-loop invariant (posts 24+25 fix): the loop NEVER runs/waits a probe.
On-loop cold cache returns transient "none" immediately and kicks a background
warm thread; boot prewarm fills the cache before any on-loop caller.
"""

from __future__ import annotations

import asyncio
import errno
import os
import shlex
import threading
import types

import pytest

import kiro_crew.sandbox as sb

_EAGAIN_REASON = "fork failed with errno 11 (EAGAIN)"
# The probe now mirrors the launcher's SPLIT sequence and names the failing
# step, so a permanent denial reads as the NEWNS call rather than a combined
# unshare — this is the exact string an Ubuntu >= 23.10 host produces.
_EPERM_REASON = "unshare(CLONE_NEWNS) failed with errno 1 (EPERM)"


@pytest.fixture(autouse=True)
def clean_backend(monkeypatch):
    """Reset cached backend + probe detail + warm thread; no real sleeping in retry path.

    Also neutralize the host's real kiro internal-sandbox setting: on a macOS
    dev box where ``~/.kiro/settings/amazon-internal.json`` has
    ``{"sandbox": true}``, the darwin kiro-delegation branch in ``wrap_argv``
    preempts the mocked ``detect_backend`` so the fail-closed ``RuntimeError``
    these tests assert on never raises. Point the settings path at a
    non-existent file so delegation is off by default.

    Clears ``KIROCREW_SANDBOX_ACTIVE`` to prevent the "already inside sandbox"
    passthrough from short-circuiting tests on sandboxed hosts.
    """
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.setattr(
        sb, "_KIRO_INTERNAL_SETTINGS_PATH", "/nonexistent/kirocrew-test/amazon-internal.json"
    )
    sb.reset_backend()
    sb._warm_thread = None
    monkeypatch.setattr(sb.time, "sleep", lambda _s: None)
    yield
    sb.reset_backend()
    sb._warm_thread = None


# ── _probe_unshare classification ──


def test_transient_errno_set_covers_resource_exhaustion():
    for code in (errno.EAGAIN, errno.ENOMEM, errno.EMFILE, errno.ENFILE, errno.ENOSPC):
        assert code in sb._TRANSIENT_PROBE_ERRNOS
    for code in (errno.EPERM, errno.EINVAL, errno.ENOSYS):
        assert code not in sb._TRANSIENT_PROBE_ERRNOS


def test_probe_success_clears_failure_detail(monkeypatch):
    monkeypatch.setattr(sb, "sys", types.SimpleNamespace(platform="linux"))  # hermetic: gate precedes the mocked probe
    monkeypatch.setattr(sb, "_probe_unshare_once", lambda: (True, False, "ok", ""))
    sb._last_unshare_failure = (True, "stale detail from a previous probe", "")
    assert sb._probe_unshare() is True
    assert sb._last_unshare_failure is None


def test_probe_transient_failure_retries_once(monkeypatch):
    monkeypatch.setattr(sb, "sys", types.SimpleNamespace(platform="linux"))  # hermetic: gate precedes the mocked probe
    calls: list[int] = []

    def fake_once():
        calls.append(1)
        return (False, True, _EAGAIN_REASON, "")

    monkeypatch.setattr(sb, "_probe_unshare_once", fake_once)
    assert sb._probe_unshare() is False
    assert len(calls) == 2  # one in-probe retry on transient failure
    assert sb._last_unshare_failure == (True, _EAGAIN_REASON, "")


def test_probe_permanent_failure_does_not_retry(monkeypatch):
    monkeypatch.setattr(sb, "sys", types.SimpleNamespace(platform="linux"))  # hermetic: gate precedes the mocked probe
    calls: list[int] = []

    def fake_once():
        calls.append(1)
        return (False, False, _EPERM_REASON, "")

    monkeypatch.setattr(sb, "_probe_unshare_once", fake_once)
    assert sb._probe_unshare() is False
    assert len(calls) == 1
    assert sb._last_unshare_failure == (False, _EPERM_REASON, "")


def test_probe_transient_then_success_recovers(monkeypatch):
    monkeypatch.setattr(sb, "sys", types.SimpleNamespace(platform="linux"))  # hermetic: gate precedes the mocked probe
    results = [(False, True, _EAGAIN_REASON, ""), (True, False, "ok", "")]
    monkeypatch.setattr(sb, "_probe_unshare_once", lambda: results.pop(0))
    assert sb._probe_unshare() is True
    assert sb._last_unshare_failure is None


def test_probe_non_linux_is_permanent(monkeypatch):
    monkeypatch.setattr(sb, "sys", types.SimpleNamespace(platform="darwin"))
    assert sb._probe_unshare() is False
    assert sb._last_unshare_failure == (False, "not Linux", "")


# ── detect_backend cache policy ──


def _install_probe(
    monkeypatch, outcomes: list[tuple[bool, bool, str, str]]
) -> list[int]:
    """Install a fake _probe_unshare fed by *outcomes*; returns the call log."""
    calls: list[int] = []

    def fake_probe() -> bool:
        calls.append(1)
        ok, transient, reason, remedy = outcomes.pop(0)
        sb._last_unshare_failure = None if ok else (transient, reason, remedy)
        return ok

    monkeypatch.setattr(sb, "_probe_unshare", fake_probe)
    monkeypatch.setattr(sb, "_probe_sandbox_exec", lambda: False)
    return calls


def test_transient_failure_is_not_cached(monkeypatch):
    calls = _install_probe(
        monkeypatch,
        [(False, True, _EAGAIN_REASON, ""), (False, True, _EAGAIN_REASON, "")],
    )
    assert sb.detect_backend(config_mode="auto") == "none"
    assert sb._backend is None  # transient "none" must not be cached
    assert sb.detect_backend(config_mode="auto") == "none"
    assert len(calls) == 2  # second call re-probed


def test_permanent_failure_is_cached(monkeypatch):
    calls = _install_probe(monkeypatch, [(False, False, _EPERM_REASON, "")])
    assert sb.detect_backend(config_mode="auto") == "none"
    assert sb._backend == "none"
    assert sb.detect_backend(config_mode="auto") == "none"
    assert len(calls) == 1  # cached — genuinely unsupported hosts probe once


def test_recovery_after_transient_failure(monkeypatch):
    """The incident scenario: a momentary EAGAIN must self-heal on next spawn."""
    calls = _install_probe(
        monkeypatch,
        [(False, True, _EAGAIN_REASON, ""), (True, False, "ok", "")],
    )
    assert sb.detect_backend(config_mode="auto") == "none"
    assert sb.detect_backend(config_mode="auto") == "namespace"
    assert sb._backend == "namespace"
    assert len(calls) == 2


def test_positive_result_cached_across_modes(monkeypatch):
    """Backend capability is mode-independent: no re-probe on mode alternation."""
    calls = _install_probe(monkeypatch, [(True, False, "ok", "")])
    assert sb.detect_backend(config_mode="auto") == "namespace"
    assert sb.detect_backend(config_mode="cc") == "namespace"
    assert sb.detect_backend(config_mode="strict") == "namespace"
    assert len(calls) == 1


def test_off_mode_short_circuits_without_probing(monkeypatch):
    calls = _install_probe(monkeypatch, [(True, False, "ok", "")])
    assert sb.detect_backend(config_mode="off") == "none"
    assert len(calls) == 0
    assert sb._backend is None  # "off" never touches the cache
    assert sb.detect_backend(config_mode="auto") == "namespace"
    assert len(calls) == 1


# ── wrap_argv fail-closed error detail ──


def test_fail_closed_transient_message_advises_retry_not_optout(monkeypatch):
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "none")
    monkeypatch.setattr(sb, "_allow_unsandboxed_exec", lambda: False)
    sb._last_unshare_failure = (True, _EAGAIN_REASON, "")
    with pytest.raises(RuntimeError) as excinfo:
        sb.wrap_argv(["kiro-cli", "acp"], mode="standard")
    msg = str(excinfo.value)
    assert f"Probe detail: {_EAGAIN_REASON}" in msg
    assert "TRANSIENT" in msg
    # A transient failure must NOT steer the operator into permanently
    # disabling isolation.
    assert "sandbox_allow_unsandboxed_exec=true" not in msg


def test_fail_closed_permanent_message_includes_optout_and_detail(monkeypatch):
    monkeypatch.setattr(sb, "detect_backend", lambda config_mode="auto": "none")
    monkeypatch.setattr(sb, "_allow_unsandboxed_exec", lambda: False)
    # Pin the nesting input: this test asserts the "host genuinely has no backend"
    # guidance, which is a DIFFERENT branch from the macOS-nesting one. Leaving it
    # implicit made the assertion depend on whether the test host happens to be
    # Seatbelt-confined — green on CI, red on a sandboxed dev machine.
    monkeypatch.setattr(sb, "_macos_sandbox_state", lambda: False)
    monkeypatch.setattr(sb, "kiro_internal_sandbox_enabled", lambda: False)
    sb._last_unshare_failure = (False, _EPERM_REASON, "")
    with pytest.raises(RuntimeError) as excinfo:
        sb.wrap_argv(["kiro-cli", "acp"], mode="standard")
    msg = str(excinfo.value)
    assert f"Probe detail: {_EPERM_REASON}" in msg
    assert "sandbox_allow_unsandboxed_exec=true" in msg


# ── Finding 2: signal-killed probe child classified transient ──


def test_probe_child_killed_by_signal_is_transient(monkeypatch):
    """A probe child killed by a signal (WIFEXITED=False) must be transient."""
    # Simulate fork -> child killed by SIGKILL (signal 9).
    # Status word for signal-killed: signal number in low 7 bits, no exit.
    signal_status = 9  # WIFSIGNALED(9)=True, WTERMSIG(9)=9, WIFEXITED(9)=False
    monkeypatch.setattr(os, "fork", lambda: 12345)
    monkeypatch.setattr(os, "waitpid", lambda pid, flags: (pid, signal_status))
    # Stub out the libc loading so we don't actually fork
    import ctypes as _ct
    import ctypes.util as _ctu

    monkeypatch.setattr(_ctu, "find_library", lambda _name: "/lib/x86_64-linux-gnu/libc.so.6")

    class FakeLibC:
        class unshare:
            argtypes = None
            restype = None

            def __call__(self, *a):
                return 0

    fake_libc = FakeLibC()
    monkeypatch.setattr(_ct, "CDLL", lambda *a, **kw: fake_libc)

    ok, transient, reason, _remedy = sb._probe_unshare_once()
    assert ok is False
    assert transient is True
    assert "signal 9" in reason


# ── Event-loop never-probe invariant (posts 24 + 25 fix) ──


def test_on_loop_cold_cache_returns_none_without_probing(monkeypatch):
    """On event loop + cold cache: returns False immediately, does NOT call _probe_unshare_once."""
    monkeypatch.setattr(sb, "sys", types.SimpleNamespace(platform="linux"))  # hermetic: gate precedes the mocked probe
    # Monkeypatch _probe_unshare_once to raise if called — proves loop never probes
    monkeypatch.setattr(
        sb, "_probe_unshare_once", lambda: (_ for _ in ()).throw(AssertionError("probe called on loop!"))
    )
    # Prevent actual thread from starting (would call _probe_unshare_once)
    monkeypatch.setattr(sb.threading, "Thread", lambda **kw: types.SimpleNamespace(
        start=lambda: None, is_alive=lambda: True, name="fake"
    ))

    async def _run():
        return sb._probe_unshare()

    result = asyncio.run(_run())
    assert result is False
    assert sb._last_unshare_failure is not None
    transient, reason, _remedy = sb._last_unshare_failure
    assert transient is True
    assert "deferred to background thread" in reason
    # Cache must stay None (transient → never cached)
    assert sb._backend is None


def test_on_loop_kicks_background_warm_that_populates_cache(monkeypatch):
    """On-loop call fires background thread that fills cache off-loop."""
    monkeypatch.setattr(sb, "sys", types.SimpleNamespace(platform="linux"))  # hermetic: gate precedes the mocked probe
    probe_thread_names: list[str] = []

    def recording_probe():
        probe_thread_names.append(threading.current_thread().name)
        return (True, False, "ok", "")

    monkeypatch.setattr(sb, "_probe_unshare_once", recording_probe)

    async def _run():
        sb._probe_unshare()  # kicks background warm
        # Wait for warm thread to complete
        if sb._warm_thread is not None:
            sb._warm_thread.join(timeout=5.0)

    asyncio.run(_run())
    # Probe ran on background thread, not on event loop
    assert len(probe_thread_names) >= 1
    assert all("sandbox-probe-warm" in n for n in probe_thread_names)
    # Cache is now populated
    assert sb._backend == "namespace"


def test_warm_dedupe_two_rapid_calls_start_at_most_one_thread(monkeypatch):
    """Two rapid on-loop calls start at most 1 background warm thread."""
    gate = threading.Event()

    def slow_probe():
        gate.wait(timeout=5.0)
        return (True, False, "ok", "")

    monkeypatch.setattr(sb, "_probe_unshare_once", slow_probe)

    # Patch Thread to track starts
    real_thread = threading.Thread
    thread_starts: list[int] = []

    class CountingThread(real_thread):
        def start(self):
            thread_starts.append(1)
            super().start()

    monkeypatch.setattr(sb.threading, "Thread", CountingThread)

    async def _run():
        sb._probe_unshare()  # first call - kicks warm
        sb._probe_unshare()  # second call - should dedupe

    try:
        asyncio.run(_run())
        # At most 1 thread started despite 2 calls
        assert len(thread_starts) <= 1
    finally:
        gate.set()  # unblock so thread can finish
        if sb._warm_thread is not None:
            sb._warm_thread.join(timeout=2.0)


def test_prewarm_backend_populates_cache(monkeypatch):
    """prewarm_backend() fires background probe that fills cache."""
    monkeypatch.setattr(sb, "_probe_unshare_once", lambda: (True, False, "ok", ""))
    monkeypatch.setattr(sb, "sys", types.SimpleNamespace(platform="linux"))

    sb.prewarm_backend()
    # Wait for thread
    if sb._warm_thread is not None:
        sb._warm_thread.join(timeout=5.0)
    assert sb._backend == "namespace"


# ── Blocking boot warm (warm_backend) ──


def test_warm_backend_returns_with_cache_already_populated(monkeypatch):
    """warm_backend() must not return until the probe has landed.

    This is the whole difference from ``prewarm_backend``: the caller is entitled
    to assume a settled verdict on return. The probe is made observably slow so a
    non-joining implementation loses the race and leaves the cache cold — the
    assertions below then fail rather than passing by luck.
    """
    monkeypatch.setattr(sb, "sys", types.SimpleNamespace(platform="linux"))

    def slow_probe():
        # Independent of sb.time.sleep (patched by the autouse fixture) so the
        # delay is real for this thread.
        threading.Event().wait(0.05)
        return (True, False, "ok", "")

    monkeypatch.setattr(sb, "_probe_unshare_once", slow_probe)

    sb.warm_backend()

    assert sb._backend == "namespace"
    # The join is what produced the guarantee — prove it happened rather than
    # inferring it from the cache alone.
    assert sb._warm_thread is not None
    assert not sb._warm_thread.is_alive()


def test_warm_backend_makes_on_loop_transient_path_unreachable(monkeypatch):
    """After a warmed boot, an on-loop caller sees the verdict, not a deferral.

    The user-visible symptom being fixed: an on-loop ``detect_backend`` racing a
    fire-and-forget prewarm logs a transient probe failure that reads as "this
    host has no sandbox backend".
    """
    monkeypatch.setattr(sb, "sys", types.SimpleNamespace(platform="linux"))
    probe_calls: list[str] = []

    def counting_probe():
        # Slow, for the same reason as the test above: with an instant probe this
        # assertion would hold even without the join, by winning the race rather
        # than by the guarantee under test.
        threading.Event().wait(0.05)
        probe_calls.append(threading.current_thread().name)
        return (True, False, "ok", "")

    monkeypatch.setattr(sb, "_probe_unshare_once", counting_probe)

    sb.warm_backend()
    assert len(probe_calls) == 1

    async def _on_loop() -> str:
        return sb.detect_backend()

    assert asyncio.run(_on_loop()) == "namespace"
    # No re-probe and no synthetic transient: the warm cache answered.
    assert len(probe_calls) == 1
    assert sb._last_unshare_failure is None


def test_warm_backend_permanent_failure_caches_none(monkeypatch):
    """A permanent denial is a real verdict and must be cached, not retried."""
    monkeypatch.setattr(sb, "sys", types.SimpleNamespace(platform="linux"))
    monkeypatch.setattr(sb, "_probe_unshare_once", lambda: (False, False, _EPERM_REASON, ""))

    sb.warm_backend()

    assert sb._backend == "none"


def test_warm_backend_wait_is_bounded_and_leaves_cache_cold(monkeypatch):
    """A wedged probe must not stall boot; the cold cache then self-heals.

    Exceeding the join timeout is explicitly not an error — it degrades to the
    pre-existing ``prewarm_backend`` behaviour (cache uncached, next caller
    re-probes) rather than failing startup.
    """
    monkeypatch.setattr(sb, "sys", types.SimpleNamespace(platform="linux"))
    release = threading.Event()

    def wedged_probe():
        release.wait(10.0)
        return (True, False, "ok", "")

    monkeypatch.setattr(sb, "_probe_unshare_once", wedged_probe)

    try:
        sb.warm_backend(timeout=0.05)
        # Returned while the probe is still in flight, so no verdict is cached.
        assert sb._backend is None
        assert sb._warm_thread is not None and sb._warm_thread.is_alive()
    finally:
        release.set()
        if sb._warm_thread is not None:
            sb._warm_thread.join(timeout=5.0)


def test_warm_backend_is_a_noop_off_linux(monkeypatch):
    """Probes are Linux-only — no thread, no cache write, no wait."""
    monkeypatch.setattr(sb, "sys", types.SimpleNamespace(platform="darwin"))

    def unexpected_probe():  # pragma: no cover - must never run
        raise AssertionError("probe must not run off Linux")

    monkeypatch.setattr(sb, "_probe_unshare_once", unexpected_probe)

    sb.warm_backend()

    assert sb._warm_thread is None
    assert sb._backend is None


# ── Off-loop behaviour preserved ──


def test_off_loop_transient_retry_sleeps(monkeypatch):
    """When no event loop is running, time.sleep IS called for the retry."""
    monkeypatch.setattr(sb, "sys", types.SimpleNamespace(platform="linux"))  # hermetic: gate precedes the mocked probe
    sleep_calls: list[float] = []
    monkeypatch.setattr(sb.time, "sleep", lambda s: sleep_calls.append(s))
    monkeypatch.setattr(sb, "_probe_unshare_once", lambda: (False, True, "EAGAIN", ""))
    sb._probe_unshare()
    assert len(sleep_calls) == 1
    assert sleep_calls[0] == sb._PROBE_TRANSIENT_RETRY_DELAY_SECS


def test_off_loop_does_not_kick_background_warm(monkeypatch):
    """Off-loop probes directly — no background thread involved."""
    monkeypatch.setattr(sb, "_probe_unshare_once", lambda: (True, False, "ok", ""))
    sb._probe_unshare()
    # No warm thread should be started for off-loop probes
    assert sb._warm_thread is None


# ── Fast-path regression: warm cache short-circuits on-loop ──


def test_probe_unshare_fast_path_on_loop_when_backend_set(monkeypatch):
    """When _backend == 'namespace', _probe_unshare() returns True immediately
    without calling _probe_unshare_once — even on a running event loop.

    Regression test for review-bot finding post 36: userns_available() was
    bypassing the warm cache and deferring on-loop, returning False even
    though the host is known-good.
    """
    monkeypatch.setattr(sb, "sys", types.SimpleNamespace(platform="linux"))  # hermetic: gate precedes the mocked probe
    import asyncio

    # Preset cache to "namespace" (simulates successful prewarm)
    monkeypatch.setattr(sb, "_backend", "namespace")
    # If _probe_unshare_once is called, the test MUST fail
    monkeypatch.setattr(sb, "_probe_unshare_once", lambda: (_ for _ in ()).throw(
        AssertionError("_probe_unshare_once must NOT be called when cache is warm")
    ))

    async def _check():
        # We're on a running event loop — previously this would defer and return False
        return sb._probe_unshare()

    result = asyncio.run(_check())
    assert result is True


# ── Mechanism-specific no-backend guidance (issue #1639) ──
#
# The generic "install a sandbox backend, or opt out" text is actively unhelpful
# on the most common affected host, stock Ubuntu 23.10+, where a backend exists
# and is one AppArmor profile away from working — and the only concrete thing it
# suggested was the opt-out that removes the isolation. These pin the replacement
# WITHOUT losing the escape hatch: the profile is named first, the opt-out last.


class TestNoBackendGuidanceNamesTheRightRemedy:
    """Which remedy is named depends on HOW Kiro Crew was launched."""

    @staticmethod
    def _apparmor_host(monkeypatch):
        monkeypatch.setattr(sb.sys, "platform", "linux")
        monkeypatch.setattr(sb, "_apparmor_userns_restricted", lambda: True)

    def test_appimage_launch_names_the_attach_command_with_the_path(self, monkeypatch):
        self._apparmor_host(monkeypatch)
        monkeypatch.setenv("APPIMAGE", "/home/user/Apps/kirocrew.AppImage")

        msg = sb._no_backend_guidance()

        assert "kirocrew sandbox install-profile" in msg
        assert "/home/user/Apps/kirocrew.AppImage" in msg
        assert "needs sudo" in msg, "must say why the app cannot do it"

    def test_non_appimage_launch_points_at_the_service(self, monkeypatch):
        """A foreground gateway has no safe executable to attach to."""
        self._apparmor_host(monkeypatch)
        monkeypatch.delenv("APPIMAGE", raising=False)

        msg = sb._no_backend_guidance()

        assert "kirocrew service install" in msg
        assert "sandbox install-profile" not in msg

    def test_never_advises_disabling_the_kernel_wide_protection(self, monkeypatch):
        """Setting the sysctl to 0 trades a host-wide protection for one app."""
        self._apparmor_host(monkeypatch)
        monkeypatch.setenv("APPIMAGE", "/home/user/Apps/kirocrew.AppImage")

        msg = sb._no_backend_guidance()

        assert "Do NOT set the sysctl to 0" in msg
        assert "=0" not in msg.replace("apparmor_restrict_unprivileged_userns=1", "")

    @pytest.mark.parametrize("appimage", ["/home/user/Apps/kirocrew.AppImage", None])
    def test_still_names_the_opt_out_but_only_after_the_real_fix(
        self, monkeypatch, appimage
    ):
        """Regression guard.

        Withholding ``sandbox_allow_unsandboxed_exec`` to stop people reaching for
        it leaves a genuinely stuck user with no way out, and it broke the
        pre-existing contract in
        ``test_fail_closed_permanent_message_includes_optout_and_detail``. The fix
        is ordering, not omission.
        """
        self._apparmor_host(monkeypatch)
        if appimage:
            monkeypatch.setenv("APPIMAGE", appimage)
        else:
            monkeypatch.delenv("APPIMAGE", raising=False)

        msg = sb._no_backend_guidance()

        assert "sandbox_allow_unsandboxed_exec=true" in msg
        assert "last resort" in msg
        profile_at = min(
            (msg.index(t) for t in ("install-profile", "service install") if t in msg),
            default=-1,
        )
        assert profile_at >= 0
        assert profile_at < msg.index("sandbox_allow_unsandboxed_exec")

    @pytest.mark.parametrize(
        "hostile",
        [
            "/home/user/KiroCrew-$(touch PWNED).AppImage",
            "/home/user/`id`.AppImage",
            "/home/user/a;rm -rf ~/b.AppImage",
            "/home/user/Bob's Apps/kirocrew.AppImage",
            "/home/user/a b/kirocrew.AppImage",
        ],
    )
    def test_the_path_is_shell_quoted_for_safe_pasting(self, monkeypatch, hostile):
        """Raised as blocking in review of #1653.

        This message is printed for the user to paste into a shell, and a filename
        is attacker-influenced in exactly the cases that matter — a downloaded or
        unpacked AppImage. Interpolating it bare turns a diagnostic into command
        injection at paste time.
        """
        self._apparmor_host(monkeypatch)
        monkeypatch.setenv("APPIMAGE", hostile)

        msg = sb._no_backend_guidance()

        quoted = shlex.quote(hostile)
        assert quoted in msg
        # The dangerous forms must never appear unquoted.
        for meta in ("$(", "`", ";"):
            if meta in hostile:
                assert f"--path {hostile}" not in msg
        # And the quoting must survive a round trip through a shell parser.
        assert shlex.split(f"cmd --path {quoted}")[-1] == hostile

    def test_the_remedy_names_a_cli_the_appimage_user_actually_has(
        self, monkeypatch, tmp_path
    ):
        """Raised as a design concern in review of #1653.

        The AppImage is documented as needing "no Python, pip, npm, or Node", so
        this persona has no `kirocrew` on PATH — the CLI lives inside the bundle.
        Naming the bare command would hand exactly the affected user a
        `command not found` and leave them with only the opt-out.
        """
        self._apparmor_host(monkeypatch)
        monkeypatch.setenv("APPIMAGE", "/home/user/Apps/kirocrew.AppImage")
        bundled = tmp_path / "kirocrew"
        bundled.write_text("#!/bin/sh\n")
        monkeypatch.setattr(sb.sys, "argv", [str(bundled)])

        msg = sb._no_backend_guidance()

        assert str(bundled) in msg
        assert "while Kiro Crew is open" in msg, "must say the path is live-only"

    def test_which_is_not_trusted_as_evidence_the_user_has_the_cli(
        self, monkeypatch, tmp_path
    ):
        """The gateway's PATH is not the user's PATH.

        This text is generated inside a process that inherited the AppImage's own
        environment but is pasted into the user's shell. A `which` hit would prove
        the bundle can find its own CLI, not that the user can, so the absolute
        path wins regardless.
        """
        self._apparmor_host(monkeypatch)
        monkeypatch.setenv("APPIMAGE", "/home/user/Apps/kirocrew.AppImage")
        bundled = tmp_path / "kirocrew"
        bundled.write_text("#!/bin/sh\n")
        monkeypatch.setattr(sb.sys, "argv", [str(bundled)])
        monkeypatch.setattr(sb.shutil, "which", lambda _n: "/usr/local/bin/kirocrew")

        msg = sb._no_backend_guidance()

        assert str(bundled) in msg

    @pytest.mark.parametrize("argv", [[], [""], ["-c"], ["/usr/bin/python3"]])
    def test_falls_back_to_the_bare_name_rather_than_inventing_a_path(
        self, monkeypatch, argv
    ):
        """Better a short command than a confidently wrong absolute path."""
        self._apparmor_host(monkeypatch)
        monkeypatch.setenv("APPIMAGE", "/home/user/Apps/kirocrew.AppImage")
        monkeypatch.setattr(sb.sys, "argv", argv)

        msg = sb._no_backend_guidance()

        assert "kirocrew sandbox install-profile" in msg
        assert "while Kiro Crew is open" not in msg

    def test_an_unaffected_host_keeps_the_original_text(self, monkeypatch):
        """macOS, Debian, and a relaxed sysctl must be untouched by this change."""
        monkeypatch.setattr(sb.sys, "platform", "darwin")

        msg = sb._no_backend_guidance()

        assert "sandbox_allow_unsandboxed_exec=true" in msg
        assert "AppArmor" not in msg

    def test_a_relaxed_sysctl_is_not_treated_as_the_apparmor_case(self, monkeypatch):
        monkeypatch.setattr(sb.sys, "platform", "linux")
        monkeypatch.setattr(sb, "_apparmor_userns_restricted", lambda: False)

        assert "AppArmor" not in sb._no_backend_guidance()

    def test_the_sysctl_reader_never_raises(self, monkeypatch):
        """A missing knob (Debian 13, older kernels) is a normal answer."""
        assert sb._apparmor_userns_restricted() in (True, False)
