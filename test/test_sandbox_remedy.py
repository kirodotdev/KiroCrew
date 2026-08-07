"""The sandbox probe must report WHICH host mechanism denied the namespace.

Issue #1660: on Ubuntu the gate screen showed ``unshare(CLONE_NEWNS) failed with
errno 1 (EPERM)`` and a retry button, and nothing else. The probe already knew
the mechanism — it deliberately performs the two unshare steps separately so a
NEWNS denial can be told apart from a NEWUSER denial — but that knowledge died
inside a prose reason string. These tests pin the machine-readable token that
carries it out, and the invariant that it never outlives its failure.
"""

from __future__ import annotations

import errno
from typing import Any

import pytest

import kiro_crew.sandbox as sb


@pytest.fixture(autouse=True)
def _clean_probe_state(monkeypatch) -> Any:
    """Each test starts and ends with no cached backend or probe verdict.

    Joining the background warm thread is part of that isolation, not politeness:
    it writes the same `_last_unshare_failure` record these tests plant, so a thread still in flight from an earlier test lands mid-test
    and replaces a planted verdict with the real host's one. Joining (rather than
    sleeping) makes that deterministic.

    Clears ``KIROCREW_SANDBOX_ACTIVE`` to prevent the "already inside sandbox"
    passthrough from short-circuiting tests on sandboxed hosts.
    """
    monkeypatch.delenv("KIROCREW_SANDBOX_ACTIVE", raising=False)
    monkeypatch.setattr(
        sb, "_KIRO_INTERNAL_SETTINGS_PATH",
        "/nonexistent/kirocrew-test/amazon-internal.json",
    )
    _join_warm_thread()
    sb.reset_backend()
    yield
    _join_warm_thread()
    sb.reset_backend()


def _join_warm_thread() -> None:
    thread = sb._warm_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=10)
        assert not thread.is_alive(), "sandbox warm thread did not finish"


def _plant_failure(transient: bool, reason: str, remedy: str = "") -> None:
    """Record a probe verdict and confirm it survived to be read.

    The confirmation is what turns a lost race into a clear failure: the warm
    thread writes the same globals, so a stray one replaces this verdict with the
    real host's and the test then fails on a downstream value that looks like a
    product bug rather than on the state it actually lost.
    """
    sb._record_probe_failure(transient, reason, remedy)
    assert sb._last_unshare_failure == (transient, reason, remedy), (
        "planted probe verdict was overwritten before the test could use it "
        "(a background warm thread is in flight)"
    )


class TestRemedyForStep:
    """The (step, errno) -> mechanism mapping, which is pure and I/O-free."""

    def test_newns_eperm_is_the_apparmor_restriction(self) -> None:
        # NEWNS is only reached after NEWUSER SUCCEEDED, so this host does have
        # user namespaces — it is the restricted profile that lacks CAP_SYS_ADMIN.
        assert (
            sb._remedy_for_step(sb._PROBE_STEP_NEWNS, errno.EPERM) == sb.REMEDY_APPARMOR_USERNS
        )

    def test_newuser_eperm_is_a_different_mechanism_from_newns_eperm(self) -> None:
        # Same errno, different step, different fix: telling these apart is the
        # entire reason the probe splits the two unshare calls.
        assert sb._remedy_for_step(sb._PROBE_STEP_NEWUSER, errno.EPERM) == sb.REMEDY_USERNS_DENIED

    @pytest.mark.parametrize("err", [errno.ENOSPC, errno.EUSERS])
    def test_newuser_exhaustion_is_the_max_user_namespaces_cap(self, err: int) -> None:
        assert sb._remedy_for_step(sb._PROBE_STEP_NEWUSER, err) == sb.REMEDY_MAX_USER_NAMESPACES

    @pytest.mark.parametrize("err", [errno.EINVAL, errno.ENOSYS])
    def test_newuser_rejection_means_no_config_user_ns(self, err: int) -> None:
        assert sb._remedy_for_step(sb._PROBE_STEP_NEWUSER, err) == sb.REMEDY_NO_USER_NS

    @pytest.mark.parametrize("label", ["fork", "probe pipe", "probe handshake write"])
    def test_harness_failures_name_no_mechanism(self, label: str) -> None:
        # A fork or pipe failure is momentary pressure on OUR side. Presenting it
        # as a host misconfiguration would send the operator to change a sysctl
        # that was never the problem.
        assert sb._remedy_for_step(label, errno.EAGAIN) == ""

    def test_unmapped_errno_on_a_real_step_is_left_unclassified(self) -> None:
        # Better to fall back to the doctor pointer than to guess a remedy.
        assert sb._remedy_for_step(sb._PROBE_STEP_NEWNS, errno.EIO) == ""


class TestRemedyRecording:
    """The remedy travels INSIDE the recorded failure, never beside it."""

    def test_probe_failure_returns_the_token_in_band(self) -> None:
        _ok, _transient, _reason, remedy = sb._probe_failure(
            sb._PROBE_STEP_NEWNS, errno.EPERM
        )
        assert remedy == sb.REMEDY_APPARMOR_USERNS

    def test_a_harness_failure_returns_no_token(self) -> None:
        # A vanished child is not a kernel verdict, so it names no mechanism —
        # and because the token is in-band, an earlier probe's cannot leak in.
        _ok, _transient, _reason, remedy = sb._probe_harness_failure(
            "probe handshake write", errno.EPIPE
        )
        assert remedy == ""

    def test_no_module_state_carries_the_token(self) -> None:
        """The token is never module state, so concurrent probes cannot mix.

        Two probes running at once (the background warm thread and a synchronous
        off-loop probe) would otherwise interleave a stage and a read, recording
        one probe's reason with the other's mechanism.
        """
        assert not hasattr(sb, "_pending_probe_remedy")
        assert not hasattr(sb, "_take_pending_remedy")
        first = sb._probe_failure(sb._PROBE_STEP_NEWNS, errno.EPERM)
        second = sb._probe_failure("fork", errno.EAGAIN)
        # Interleaving cannot change what an already-returned verdict carries.
        assert first[3] == sb.REMEDY_APPARMOR_USERNS
        assert second[3] == ""

    def test_the_remedy_is_read_from_the_same_value_as_the_failure(self) -> None:
        """One tuple, so a concurrent re-probe cannot pair failure A with remedy B.

        A reader that took the failure and the remedy from two globals could be
        interrupted between them by the background warm thread and report the
        wrong host mechanism. Swapping the whole tuple moves both at once.
        """
        sb._record_probe_failure(False, "reason A", sb.REMEDY_APPARMOR_USERNS)
        assert sb.unavailable_remedy() == sb.REMEDY_APPARMOR_USERNS

        sb._record_probe_failure(True, "reason B")
        assert sb.unavailable_remedy() == ""
        transient, reason, remedy = sb._last_unshare_failure or (False, "", "x")
        assert (transient, reason, remedy) == (True, "reason B", "")

    def test_unavailable_remedy_is_empty_when_the_last_probe_succeeded(self) -> None:
        sb._probe_failure(sb._PROBE_STEP_NEWNS, errno.EPERM)
        sb._last_unshare_failure = None
        # The token is only meaningful alongside a recorded failure; reporting a
        # remedy for a host whose probe just passed would be nonsense advice.
        assert sb.unavailable_remedy() == ""

    def test_unavailable_remedy_reports_the_recorded_token(self) -> None:
        sb._record_probe_failure(
            False,
            "unshare(CLONE_NEWNS) failed with errno 1 (EPERM)",
            sb.REMEDY_APPARMOR_USERNS,
        )
        assert sb.unavailable_remedy() == sb.REMEDY_APPARMOR_USERNS

    def test_reset_backend_clears_the_token(self) -> None:
        sb._record_probe_failure(False, "x", sb.REMEDY_APPARMOR_USERNS)
        sb.reset_backend()
        assert sb.unavailable_remedy() == ""

    def test_a_deferred_on_loop_probe_reports_no_remedy(self) -> None:
        """The synthetic on-loop transient describes no host mechanism.

        It is emitted WITHOUT probing, so any token on record belongs to an
        earlier failure. Carrying it forward would attach a permanent-looking
        remedy to a condition that clears by itself in milliseconds.
        """
        import asyncio

        sb._record_probe_failure(False, "older", sb.REMEDY_APPARMOR_USERNS)

        async def probe_on_loop() -> None:
            sb._probe_unshare()

        # The deferral branch is Linux-only; elsewhere the platform guard runs
        # first and also leaves no remedy, which this same assertion covers.
        asyncio.run(probe_on_loop())
        assert sb.unavailable_remedy() == ""


class TestGuidanceProse:
    """Log/doctor/Slack read the message text, so the prose must move too."""

    def test_apparmor_guidance_names_the_command_that_fixes_it(self) -> None:
        guidance = sb._linux_remedy_guidance(sb.REMEDY_APPARMOR_USERNS)
        assert "kirocrew service install" in guidance
        # Naming the sysctl WITHOUT warning against setting it to 0 would invite
        # disabling a kernel-wide protection to satisfy one application.
        assert "Do NOT set the sysctl to 0" in guidance

    def test_apparmor_guidance_does_not_recommend_aa_exec(self) -> None:
        """`aa-exec -p` must never be offered as the remedy.

        Entering a named profile is not permitted for an unconfined user, and
        aa-exec execs the command unconfined instead of failing — so it reads as
        applied while leaving NEWNS denied. The guidance may mention aa-exec only
        to say it does not work, never as an instruction to run.
        """
        guidance = sb._linux_remedy_guidance(sb.REMEDY_APPARMOR_USERNS)
        assert "cannot fix that" in guidance
        assert "aa-exec -p kirocrew-userns -- kirocrew gateway" not in guidance

    def test_every_token_has_guidance(self) -> None:
        for token in (
            sb.REMEDY_APPARMOR_USERNS,
            sb.REMEDY_MAX_USER_NAMESPACES,
            sb.REMEDY_NO_USER_NS,
            sb.REMEDY_USERNS_DENIED,
        ):
            assert sb._linux_remedy_guidance(token), token

    def test_unknown_token_yields_no_guidance(self) -> None:
        assert sb._linux_remedy_guidance("") == ""
        assert sb._linux_remedy_guidance("something_else") == ""


class TestErrorCarriesRemedy:
    """``SandboxUnavailableError`` is the typed channel to the presentation layer."""

    def test_remedy_defaults_to_empty(self) -> None:
        exc = sb.SandboxUnavailableError("m", kind="no_backend", detail="d")
        assert exc.remedy == ""

    def test_remedy_is_carried_verbatim(self) -> None:
        exc = sb.SandboxUnavailableError(
            "m", kind="no_backend", detail="d", remedy=sb.REMEDY_APPARMOR_USERNS
        )
        assert exc.remedy == sb.REMEDY_APPARMOR_USERNS


class TestCapExhaustionReachability:
    """The `max_user_namespaces` remedy must reach the gate WITHOUT caching a verdict.

    `user.max_user_namespaces` exhaustion surfaces as ENOSPC, which is also what
    momentary fd/disk pressure looks like, so it stays in `_TRANSIENT_PROBE_ERRNOS`
    and the probe never caches it — a host that recovers must not be stuck with
    "no sandbox" until restart. That leaves a configured cap of 0 permanently
    reported as transient, so the remedy has to travel on the transient path or the
    one host this token exists for never sees its own fix.
    """

    @staticmethod
    def _cap_reason() -> str:
        return f"{sb._PROBE_STEP_NEWUSER} failed with errno {errno.ENOSPC} (ENOSPC)"

    def test_repeated_cap_failure_is_never_cached(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A cap failure on both attempts must leave the backend cache cold.

        The retry gap is milliseconds, so a brief burst of namespace pressure
        reproduces identically; promoting that to a permanent verdict would cache
        "none" for a host that is fine a moment later.
        """

        def probe() -> tuple[bool, bool, str]:
            return sb._probe_failure(sb._PROBE_STEP_NEWUSER, errno.ENOSPC)

        monkeypatch.setattr(sb, "_probe_unshare_once", probe)
        monkeypatch.setattr(sb, "_PROBE_TRANSIENT_RETRY_DELAY_SECS", 0)
        sb._backend = None

        sb._background_warm()

        assert sb._backend is None, "a transient cap failure must not be cached"
        transient, _reason, _remedy = sb._last_unshare_failure or (False, "", "")
        assert transient is True

    def test_transient_cap_failure_still_delivers_the_remedy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The remedy rides the transient verdict — that is the only path it has."""
        monkeypatch.setattr(sb, "detect_backend", lambda *_a, **_k: "none")
        monkeypatch.setattr(sb, "_allow_unsandboxed_exec", lambda: False)
        monkeypatch.setattr(sb, "_inside_macos_sandbox", lambda: False)
        _plant_failure(True, self._cap_reason(), sb.REMEDY_MAX_USER_NAMESPACES)
        with pytest.raises(sb.SandboxUnavailableError) as caught:
            sb.wrap_argv(["/bin/true"])

        assert caught.value.kind == "transient"
        assert caught.value.remedy == sb.REMEDY_MAX_USER_NAMESPACES
        assert "user.max_user_namespaces" in str(caught.value)

    def test_transient_failure_without_a_mechanism_carries_no_remedy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Delivering the token on the transient path must not invent one.

        A `fork` EAGAIN names no host mechanism, so the gate must get nothing to
        show rather than generic advice to reconfigure a merely busy host.
        """
        monkeypatch.setattr(sb, "detect_backend", lambda *_a, **_k: "none")
        monkeypatch.setattr(sb, "_allow_unsandboxed_exec", lambda: False)
        monkeypatch.setattr(sb, "_inside_macos_sandbox", lambda: False)
        sb._probe_failure("fork", errno.EAGAIN)
        _plant_failure(True, "fork failed with errno 11 (EAGAIN)")

        with pytest.raises(sb.SandboxUnavailableError) as caught:
            sb.wrap_argv(["/bin/true"])

        assert caught.value.kind == "transient"
        assert caught.value.remedy == ""


class TestWrapArgvWiring:
    """End-to-end: a recorded probe verdict must reach the raised refusal.

    This is the wiring the gate depends on. Asserting the classifier in isolation
    would not catch the token being dropped between ``_probe_failure`` and the
    exception, which is where every earlier attempt at this diagnosis was lost.
    """

    @staticmethod
    def _force_no_backend(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sb, "detect_backend", lambda *_a, **_k: "none")
        monkeypatch.setattr(sb, "_allow_unsandboxed_exec", lambda: False)
        # Off the macOS nesting path, so the verdict is a real host verdict.
        monkeypatch.setattr(sb, "_inside_macos_sandbox", lambda: False)

    def test_apparmor_verdict_reaches_the_refusal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._force_no_backend(monkeypatch)
        _plant_failure(
            False,
            f"{sb._PROBE_STEP_NEWNS} failed with errno 1 (EPERM)",
            sb.REMEDY_APPARMOR_USERNS,
        )

        with pytest.raises(sb.SandboxUnavailableError) as caught:
            sb.wrap_argv(["/bin/true"])

        assert caught.value.remedy == sb.REMEDY_APPARMOR_USERNS
        # The token is what the dashboard reads. The message prose for a
        # no-backend host is owned by _no_backend_guidance(), which picks the
        # command from how the gateway was launched, so this does not assert a
        # specific command here — the probe step must still survive for doctor,
        # the gateway logs and Slack, which read the message rather than the token.
        assert sb._PROBE_STEP_NEWNS in str(caught.value)

    def test_recording_a_failure_without_probing_clears_a_stale_remedy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The token is surfaced even for a transient verdict, so one left over from
        # an earlier probe would name the wrong host mechanism. Recording through
        # the single writer without a remedy is what drops it.
        self._force_no_backend(monkeypatch)
        # An older recorded failure must not lend its mechanism to this one.
        sb._record_probe_failure(False, "older", sb.REMEDY_APPARMOR_USERNS)
        _plant_failure(True, "fork failed with errno 11 (EAGAIN)")

        with pytest.raises(sb.SandboxUnavailableError) as caught:
            sb.wrap_argv(["/bin/true"])

        assert caught.value.kind == "transient"
        assert caught.value.remedy == ""
        assert "kirocrew service install" not in str(caught.value)
