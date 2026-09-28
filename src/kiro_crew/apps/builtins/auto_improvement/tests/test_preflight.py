"""Phase-1 pre-flight: the three gates that decide whether a run may start at all.

`spine/preflight.py` calibrates the noise band, forces the canary and requires it clear
that band, then runs the do-not-pollute test — and refuses Phase 2 if any of the three
fails. It is the "the evaluator is the project" gate, and it was 29% covered: the raising
paths, which are the entire point, were never driven.

The band arithmetic is worth pinning precisely because it is where a broken ruler becomes
an accepted "win": a band that is too wide discards every real improvement, a band that is
too narrow (or NEGATIVE) accepts jitter, and a canary comparison with the wrong sign
accepts a regression as a win.

Fakes rather than a real profile: the spine consumes the profile only through the seam, so
the smallest thing that satisfies the seam is also the clearest statement of what the spine
actually requires.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import cast

import pytest

from kiro_crew.apps.builtins.auto_improvement.spine import preflight
from kiro_crew.apps.builtins.auto_improvement.spine.contracts import Measurement
from kiro_crew.apps.builtins.auto_improvement.spine.profile import (
    CalibrationParams,
    TargetProfile,
)


class _Ruler:
    """The narrowest thing satisfying the ruler seam that preflight uses."""

    def __init__(
        self,
        *,
        samples: list[float],
        canary: Measurement,
        direction: str = "minimize",
    ) -> None:
        self._samples = samples
        self._canary = canary
        self.direction = direction
        self.baseline_calls: list[int] = []

    def baseline_samples(self, *, base_src: Path, reps: int) -> list[float]:
        self.baseline_calls.append(reps)
        return self._samples

    def measure_canary(self, *, base_src: Path) -> Measurement:
        return self._canary


class _Isolation:
    def __init__(self, paths: list[Path], excludes: list[Path] | None = None) -> None:
        self._paths = paths
        self._excludes = excludes

    def do_not_pollute_paths(self) -> list[Path]:
        return self._paths

    def do_not_pollute_excludes(self) -> list[Path]:
        return self._excludes or []


class _NoExcludeIsolation:
    """A profile predating `do_not_pollute_excludes` — the back-compat path."""

    def __init__(self, paths: list[Path]) -> None:
        self._paths = paths

    def do_not_pollute_paths(self) -> list[Path]:
        return self._paths


class _Profile:
    def __init__(self, ruler: _Ruler, isolation: object, calibration: CalibrationParams) -> None:
        self.id = "fake"
        self.ruler = ruler
        self.isolation = isolation
        self.calibration = calibration


def _as_profile(fake: object) -> TargetProfile:
    """Present a fake as a ``TargetProfile``.

    `preflight` only ever touches ``ruler``, ``isolation`` and ``calibration``, so the
    fakes above implement exactly those. Casting once here — rather than sprinkling
    per-call ignores — keeps the narrowing in ONE place with its reason attached, and
    means a future preflight change that reaches for a seventh field fails loudly at that
    call site instead of being silently ignored everywhere.
    """
    return cast(TargetProfile, fake)


def _write(path: Path, text: str = "x") -> None:
    """A boot-callable-shaped write: ``Path.write_text`` returns an int, which does not
    satisfy ``BootCallable`` (``Callable[[], None]``)."""
    path.write_text(text, encoding="utf-8")


def _win(delta: float) -> Measurement:
    return Measurement(ok=True, primary_delta=delta)


class TestNoiseBand:
    """``max(2σ, floor)`` — and the sample-count edges around it."""

    def test_two_sigma_over_the_samples(self) -> None:
        # pstdev([10, 12]) == 1.0 -> band 2.0
        assert preflight.compute_noise_band([10.0, 12.0]) == pytest.approx(2.0)

    def test_the_floor_wins_when_the_host_is_quiet(self) -> None:
        """A deceptively quiet calibration window must not license sub-jitter wins."""
        assert preflight.compute_noise_band([10.0, 10.0], floor=5.0) == 5.0

    def test_two_sigma_wins_when_the_host_is_noisy(self) -> None:
        assert preflight.compute_noise_band([10.0, 20.0], floor=1.0) == pytest.approx(10.0)

    def test_no_samples_is_a_calibration_error_not_a_default(self) -> None:
        """Silently defaulting the band would hide a completely broken harness."""
        with pytest.raises(preflight.CalibrationError, match="no baseline samples"):
            preflight.compute_noise_band([])

    def test_one_sample_falls_back_to_the_floor(self) -> None:
        """A single boot has no spread; the floor is conservative but honest."""
        assert preflight.compute_noise_band([42.0], floor=3.0) == 3.0

    def test_a_negative_floor_can_never_produce_a_negative_band(self) -> None:
        """The invariant that keeps the canary comparison meaningful. With ``band < 0``,
        ``delta < -band`` is satisfiable by a POSITIVE delta — a regression would clear
        the band and the ruler would be declared trustworthy on the strength of it."""
        assert preflight.compute_noise_band([42.0], floor=-5.0) == 0.0
        assert preflight.compute_noise_band([10.0, 10.0], floor=-5.0) == 0.0

    def test_the_cap_narrows_a_ballooned_band(self) -> None:
        """The operator override: on a noisy shared host 2σ can swallow every real win."""
        assert preflight.compute_noise_band([0.0, 100.0], cap=10.0) == 10.0

    def test_the_cap_never_goes_below_the_floor(self) -> None:
        """The cap weakens the gate deliberately; the floor is the hard limit on how far."""
        assert preflight.compute_noise_band([0.0, 100.0], floor=25.0, cap=10.0) == 25.0

    def test_a_cap_wider_than_the_band_changes_nothing(self) -> None:
        assert preflight.compute_noise_band([10.0, 12.0], cap=999.0) == pytest.approx(2.0)

    @pytest.mark.parametrize("raw", ["0", "-1", "", "not-a-number"])
    def test_a_junk_env_cap_is_ignored(self, raw: str, monkeypatch) -> None:
        """A malformed override must not become a band of 0 (which would accept anything)."""
        monkeypatch.setenv("AUTO_IMPROVEMENT_BAND_CAP_MS", raw)
        assert preflight.compute_noise_band([10.0, 12.0]) == pytest.approx(2.0)

    def test_the_explicit_cap_outranks_the_env_cap(self, monkeypatch) -> None:
        """Config survives the measurement sandbox's env scrub; env does not. When both
        are present the one that actually reaches a sandboxed run must win."""
        monkeypatch.setenv("AUTO_IMPROVEMENT_BAND_CAP_MS", "50")
        assert preflight.compute_noise_band([0.0, 100.0], cap=10.0) == 10.0

    def test_the_env_cap_applies_when_no_explicit_cap_is_given(self, monkeypatch) -> None:
        monkeypatch.setenv("AUTO_IMPROVEMENT_BAND_CAP_MS", "10")
        assert preflight.compute_noise_band([0.0, 100.0]) == 10.0


class TestTheCanaryVerdictHasTheRightSign:
    """A sign error here would accept a REGRESSION as proof the ruler works."""

    def test_minimize_clears_only_on_a_big_enough_negative_delta(self) -> None:
        clears = preflight._canary_clears_band
        assert clears(_win(-10.0), band=2.0, direction="minimize") is True
        assert clears(_win(-1.0), band=2.0, direction="minimize") is False
        assert clears(_win(+10.0), band=2.0, direction="minimize") is False

    def test_maximize_clears_only_on_a_big_enough_positive_delta(self) -> None:
        clears = preflight._canary_clears_band
        assert clears(_win(+10.0), band=2.0, direction="maximize") is True
        assert clears(_win(+1.0), band=2.0, direction="maximize") is False
        assert clears(_win(-10.0), band=2.0, direction="maximize") is False

    def test_a_delta_exactly_on_the_band_does_not_clear_it(self) -> None:
        """Strict inequality: "indistinguishable from the band" is not a proven win."""
        clears = preflight._canary_clears_band
        assert clears(_win(-2.0), band=2.0, direction="minimize") is False
        assert clears(_win(+2.0), band=2.0, direction="maximize") is False

    def test_a_failed_or_deltaless_measurement_never_clears(self) -> None:
        clears = preflight._canary_clears_band
        assert (
            clears(Measurement(ok=False, primary_delta=-99.0), band=1.0, direction="minimize")
            is False
        )
        assert (
            clears(Measurement(ok=True, primary_delta=None), band=1.0, direction="minimize")
            is False
        )


class TestAllThreeGates:
    @staticmethod
    def _profile(tmp_path: Path, *, canary: Measurement, direction: str = "minimize"):
        watched = tmp_path / "home"
        watched.mkdir(parents=True, exist_ok=True)
        (watched / "seed.txt").write_text("seed", encoding="utf-8")
        ruler = _Ruler(samples=[10.0, 12.0], canary=canary, direction=direction)
        return (
            _Profile(
                ruler,
                _Isolation([watched]),
                CalibrationParams(baseline_reps=4, floor=0.0, canary_id="forced-win"),
            ),
            watched,
        )

    def test_all_three_passing_permits_phase_two(self, tmp_path: Path) -> None:
        profile, _ = self._profile(tmp_path, canary=_win(-50.0))
        result = preflight.calibrate_and_prove(
            _as_profile(profile), base_src=tmp_path, boot=lambda: None
        )
        assert result.ok is True
        assert result.canary_cleared is True
        assert result.noise_band == pytest.approx(2.0)
        assert result.baseline_n == 2
        assert result.canary_delta == -50.0
        assert result.pollute is not None and result.pollute.zero_diff is True

    def test_the_profiles_rep_count_is_the_one_used(self, tmp_path: Path) -> None:
        """A profile knob nobody passes is dead config."""
        profile, _ = self._profile(tmp_path, canary=_win(-50.0))
        preflight.calibrate_and_prove(_as_profile(profile), base_src=tmp_path, boot=lambda: None)
        assert profile.ruler.baseline_calls == [4]

    def test_a_canary_inside_the_band_refuses_phase_two(self, tmp_path: Path) -> None:
        """The ruler cannot resolve a win it is CONFIDENT is large — so it is broken."""
        profile, _ = self._profile(tmp_path, canary=_win(-0.5))
        with pytest.raises(preflight.RulerNotTrustedError, match="ruler not trusted"):
            preflight.calibrate_and_prove(
                _as_profile(profile), base_src=tmp_path, boot=lambda: None
            )

    def test_advisory_mode_warns_and_proceeds(self, tmp_path: Path, caplog) -> None:
        """The backend's `canaryStrict=false` policy: a noisy short calibration should not
        halt the whole run, because the keeper still gates every real win on the band."""
        profile, _ = self._profile(tmp_path, canary=_win(-0.5))
        with caplog.at_level(logging.WARNING):
            result = preflight.calibrate_and_prove(
                _as_profile(profile), base_src=tmp_path, boot=lambda: None, canary_advisory=True
            )
        assert result.ok is True
        assert result.canary_cleared is False, "a non-clearing canary was reported as cleared"
        assert "CANARY did NOT clear" in caplog.text

    def test_a_host_leak_blocks_even_in_advisory_mode(self, tmp_path: Path) -> None:
        """Sensitivity may be relaxed; SAFETY may not. A leak blocks unconditionally."""
        # A FRESH tree per iteration: the leak from the first run persists, so reusing one
        # tree would leave the second run's before-snapshot already containing the leaked
        # file — zero diff, and the test would pass for the wrong reason.
        for i, advisory in enumerate((False, True)):
            profile, watched = self._profile(tmp_path / f"case{i}", canary=_win(-50.0))

            def leak(w: Path = watched) -> None:
                _write(w / "leaked.json")

            with pytest.raises(preflight.HostPollutionError, match="BLOCKED"):
                preflight.calibrate_and_prove(
                    _as_profile(profile),
                    base_src=tmp_path,
                    boot=leak,
                    canary_advisory=advisory,
                )

    def test_the_leak_message_names_the_paths(self, tmp_path: Path) -> None:
        """The operator has to fix the leak, so the block has to say where it is."""
        profile, watched = self._profile(tmp_path, canary=_win(-50.0))
        with pytest.raises(preflight.HostPollutionError) as exc:
            preflight.calibrate_and_prove(
                _as_profile(profile),
                base_src=tmp_path,
                boot=lambda: _write(watched / "leak.json"),
            )
        assert str(watched) in str(exc.value)

    def test_a_broken_harness_raises_before_the_canary_runs(self, tmp_path: Path) -> None:
        """Zero baseline samples is a broken harness; there is nothing to calibrate
        against, so the run must stop there rather than proceed on a defaulted band."""
        watched = tmp_path / "home"
        watched.mkdir()
        profile = _Profile(
            _Ruler(samples=[], canary=_win(-50.0)),
            _Isolation([watched]),
            CalibrationParams(baseline_reps=2),
        )
        with pytest.raises(preflight.CalibrationError):
            preflight.calibrate_and_prove(
                _as_profile(profile), base_src=tmp_path, boot=lambda: None
            )

    def test_a_profile_without_excludes_still_works(self, tmp_path: Path) -> None:
        """Back-compat: `do_not_pollute_excludes` is optional on the seam."""
        watched = tmp_path / "home"
        watched.mkdir()
        profile = _Profile(
            _Ruler(samples=[10.0, 12.0], canary=_win(-50.0)),
            _NoExcludeIsolation([watched]),
            CalibrationParams(baseline_reps=2),
        )
        assert (
            preflight.calibrate_and_prove(
                _as_profile(profile), base_src=tmp_path, boot=lambda: None
            ).ok
            is True
        )

    def test_an_excluded_orchestrator_write_does_not_block(self, tmp_path: Path) -> None:
        """The app's own data dir lives under a snapshot root and is written by design."""
        watched = tmp_path / "home"
        watched.mkdir()
        mine = watched / "app-data"
        mine.mkdir()
        profile = _Profile(
            _Ruler(samples=[10.0, 12.0], canary=_win(-50.0)),
            _Isolation([watched], excludes=[mine]),
            CalibrationParams(baseline_reps=2),
        )
        result = preflight.calibrate_and_prove(
            _as_profile(profile),
            base_src=tmp_path,
            boot=lambda: _write(mine / "activity.jsonl", "log\n"),
        )
        assert result.ok is True

    def test_the_band_cap_reaches_the_calibration(self, tmp_path: Path) -> None:
        """`band_cap_ms` is the path that survives the sandbox env scrub, so it has to be
        plumbed all the way through — a cap that stops at the signature is inert."""
        watched = tmp_path / "home"
        watched.mkdir()
        profile = _Profile(
            _Ruler(samples=[0.0, 100.0], canary=_win(-20.0)),
            _Isolation([watched]),
            CalibrationParams(baseline_reps=2),
        )
        # Uncapped the band would be 100.0 and the -20.0 canary would NOT clear it.
        result = preflight.calibrate_and_prove(
            _as_profile(profile), base_src=tmp_path, boot=lambda: None, band_cap_ms=10.0
        )
        assert result.noise_band == 10.0
        assert result.canary_cleared is True

    def test_a_maximize_ruler_is_proven_by_a_positive_canary(self, tmp_path: Path) -> None:
        profile, _ = self._profile(tmp_path, canary=_win(+50.0), direction="maximize")
        assert (
            preflight.calibrate_and_prove(
                _as_profile(profile), base_src=tmp_path, boot=lambda: None
            ).canary_cleared
            is True
        )

    def test_a_boot_that_cannot_start_propagates(self, tmp_path: Path) -> None:
        """A runtime that fails to boot is a hard stop, not a hermetic pass."""
        profile, _ = self._profile(tmp_path, canary=_win(-50.0))

        def boom() -> None:
            raise RuntimeError("runtime failed to boot")

        with pytest.raises(RuntimeError, match="runtime failed to boot"):
            preflight.calibrate_and_prove(_as_profile(profile), base_src=tmp_path, boot=boom)
