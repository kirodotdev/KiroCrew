"""Checkout-pinned pod boot and post-health seed verification."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import make_dir_link, requires_symlinks
from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import unit as unit_mod
from kiro_crew.pod.config import PodConfig

pytestmark = pytest.mark.skipif(
    not rt.IS_POSIX,
    reason="pod drop-in lifecycle requires POSIX descriptor traversal",
)


@pytest.fixture
def pod_plane(tmp_path: Path, monkeypatch) -> PodConfig:
    """A pod plane rooted in ``tmp_path``, with systemd's unit dir redirected."""
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "envs"))
    unit_root = tmp_path / "systemd-user"
    monkeypatch.setattr(
        unit_mod,
        "unit_path",
        lambda cfg: unit_root / f"{cfg.unit_prefix}@.service",
    )
    cfg = PodConfig.load()
    # Production reaches drop-in installation only after the template unit is
    # current, which means its parent directory already exists. Direct unit
    # tests establish that same precondition without writing a real unit.
    unit_mod.unit_path(cfg).parent.mkdir(parents=True)
    return cfg


class TestDropInRendering:
    def test_resets_execstart_before_setting_it(self, pod_plane: PodConfig) -> None:
        """The empty ``ExecStart=`` is load-bearing, not decoration.

        For a ``Type=simple`` unit ``ExecStart`` is a LIST directive, so a
        drop-in that only adds a value APPENDS a second command instead of
        replacing the template's — the pod would still boot the global binary,
        and then boot a second gateway behind it.
        """
        rendered = unit_mod.render_dropin(Path("/checkouts/wt"))
        lines = [ln for ln in rendered.splitlines() if ln.startswith("ExecStart")]
        assert lines[0] == "ExecStart="
        assert len(lines) == 2
        expected = unit_mod.systemd_quote(str(rt.prov.venv_bin(Path("/checkouts/wt"))))
        assert expected in lines[1]
        assert lines[1].endswith("pod _run %i")

    def test_names_the_checkouts_own_binary_not_a_global_one(self, pod_plane: PodConfig) -> None:
        rendered = unit_mod.render_dropin(Path("/checkouts/wt"))
        expected = unit_mod.systemd_quote(str(rt.prov.venv_bin(Path("/checkouts/wt"))))
        assert expected in rendered

    def test_path_is_scoped_to_one_instance(self, pod_plane: PodConfig) -> None:
        # A drop-in on the TEMPLATE would pin every pod to one checkout, which is
        # the defect with extra steps. systemd resolves `<unit>@<name>.service.d`
        # per instance, so each pod carries its own.
        path = unit_mod.dropin_path(pod_plane, "wt")
        assert path.parent.name == f"{pod_plane.unit_prefix}@wt.service.d"
        assert path.parent != unit_mod.unit_path(pod_plane).parent / "shared"
        # `override.conf` is the conventional operator-owned filename created by
        # `systemctl edit`. Claiming it would overwrite the operator on up and
        # delete their configuration on down.
        assert path.name == "50-kirocrew-pod.conf"

    def test_operator_override_conf_survives_install_and_remove(self, pod_plane: PodConfig) -> None:
        directory = unit_mod.dropin_dir(pod_plane, "wt")
        directory.mkdir()
        operator = directory / "override.conf"
        operator.write_text("[Service]\nMemoryMax=1G\n")

        unit_mod.install_dropin(pod_plane, "wt", Path("/checkouts/wt"))
        assert operator.read_text() == "[Service]\nMemoryMax=1G\n"
        assert unit_mod.remove_dropin(pod_plane, "wt") is True
        assert operator.read_text() == "[Service]\nMemoryMax=1G\n"
        assert directory.is_dir()

    @requires_symlinks
    def test_install_refuses_a_symlink_at_its_owned_filename(self, pod_plane: PodConfig) -> None:
        directory = unit_mod.dropin_dir(pod_plane, "wt")
        directory.mkdir()
        victim = directory.parent / "victim.conf"
        victim.write_text("keep me")
        unit_mod.dropin_path(pod_plane, "wt").symlink_to(victim)

        with pytest.raises(OSError, match="symbolic link"):
            unit_mod.install_dropin(pod_plane, "wt", Path("/checkouts/wt"))
        assert victim.read_text() == "keep me"

    def test_install_refuses_a_linked_dropin_directory(self, pod_plane: PodConfig) -> None:
        outside = unit_mod.unit_path(pod_plane).parent / "outside"
        outside.mkdir()
        make_dir_link(unit_mod.dropin_dir(pod_plane, "wt"), outside)

        with pytest.raises(OSError, match=r"(?:symbolic link|symlink or junction)"):
            unit_mod.install_dropin(pod_plane, "wt", Path("/checkouts/wt"))
        assert not (outside / "50-kirocrew-pod.conf").exists()

    def test_install_then_remove_leaves_no_directory(self, pod_plane: PodConfig) -> None:
        unit_mod.install_dropin(pod_plane, "wt", Path("/checkouts/wt"))
        assert unit_mod.dropin_path(pod_plane, "wt").is_file()
        assert unit_mod.remove_dropin(pod_plane, "wt") is True
        # The directory goes too: an empty `<unit>@wt.service.d` is still a
        # directory named after a pod that no longer exists.
        assert not unit_mod.dropin_dir(pod_plane, "wt").exists()

    def test_install_rewrites_a_stale_override(self, pod_plane: PodConfig) -> None:
        # Re-`up`ped from a different checkout, the pod must not keep booting the
        # old one — the same staleness `unit_exec_ok` self-heals for the template.
        unit_mod.install_dropin(pod_plane, "wt", Path("/checkouts/old"))
        unit_mod.install_dropin(pod_plane, "wt", Path("/checkouts/new"))
        text = unit_mod.dropin_path(pod_plane, "wt").read_text()
        new_binary = unit_mod.systemd_quote(str(rt.prov.venv_bin(Path("/checkouts/new"))))
        old_binary = unit_mod.systemd_quote(str(rt.prov.venv_bin(Path("/checkouts/old"))))
        assert new_binary in text
        assert old_binary not in text

    def test_remove_keeps_a_foreign_dropin_in_the_directory(self, pod_plane: PodConfig) -> None:
        # Only the file this code writes is ours to delete; an operator's own
        # drop-in beside it keeps the directory alive.
        unit_mod.install_dropin(pod_plane, "wt", Path("/checkouts/wt"))
        foreign = unit_mod.dropin_dir(pod_plane, "wt") / "99-operator.conf"
        foreign.write_text("[Service]\nMemoryMax=1G\n")
        assert unit_mod.remove_dropin(pod_plane, "wt") is True
        assert foreign.is_file()


class TestStartPodPinsTheCheckout:
    """``start_pod`` is the single place a pod is brought up, so the override is
    written there — a pod started without one runs the wrong build."""

    def _systemctl(self, calls: list[tuple[str, ...]], rc: int = 0):
        def _fake(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
            calls.append(args)
            return subprocess.CompletedProcess(args=list(args), returncode=rc, stdout="", stderr="")

        return _fake

    def test_writes_the_override_and_reloads_before_starting(
        self, pod_plane: PodConfig, monkeypatch
    ) -> None:
        rt.pin_checkout(pod_plane, "wt", Path("/checkouts/wt"))
        monkeypatch.setattr(unit_mod, "unit_is_current", lambda cfg: True)
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(rt, "systemctl", self._systemctl(calls))

        cp = rt.start_pod(pod_plane, "wt")

        assert cp.returncode == 0
        expected = unit_mod.systemd_quote(str(rt.prov.venv_bin(Path("/checkouts/wt"))))
        assert expected in unit_mod.dropin_path(pod_plane, "wt").read_text()
        # systemd only reads a drop-in at load time, so the reload has to land
        # BETWEEN the write and the start or the pod boots the old definition.
        assert calls[0] == ("daemon-reload",)
        assert calls[-1] == ("start", rt.pod_unit(pod_plane, "wt"))

    def test_refuses_to_start_a_pod_with_no_pinned_checkout(
        self, pod_plane: PodConfig, monkeypatch
    ) -> None:
        # Starting anyway would fall back to the global binary silently, which is
        # the defect: the pod comes up, ignores this pod's settings, reports fine.
        monkeypatch.setattr(unit_mod, "unit_is_current", lambda cfg: True)
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(rt, "systemctl", self._systemctl(calls))

        cp = rt.start_pod(pod_plane, "wt")

        assert cp.returncode != 0
        assert "no pinned checkout" in cp.stderr
        assert not any(args[0] == "start" for args in calls)

    def test_control_character_checkout_path_refuses_without_traceback(
        self, pod_plane: PodConfig, monkeypatch
    ) -> None:
        rt.pin_checkout(pod_plane, "wt", Path("/checkouts/with\ttab"))
        monkeypatch.setattr(unit_mod, "unit_is_current", lambda cfg: True)
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(rt, "systemctl", self._systemctl(calls))

        cp = rt.start_pod(pod_plane, "wt")

        assert cp.returncode != 0
        assert "could not write the boot override" in cp.stderr
        assert "control character" in cp.stderr
        assert not any(args[0] == "start" for args in calls)

    def test_a_failed_reload_refuses_and_leaves_no_override(
        self, pod_plane: PodConfig, monkeypatch
    ) -> None:
        """An unreloaded write is worse than none: systemd would boot the pod
        under the template's global binary while the file on disk claims
        otherwise. Same invariant ``_write_and_load_unit`` keeps for the
        template — a definition present on disk has been loaded."""
        rt.pin_checkout(pod_plane, "wt", Path("/checkouts/wt"))
        monkeypatch.setattr(unit_mod, "unit_is_current", lambda cfg: True)
        calls: list[tuple[str, ...]] = []
        monkeypatch.setattr(rt, "systemctl", self._systemctl(calls, rc=1))

        cp = rt.start_pod(pod_plane, "wt")

        assert cp.returncode != 0
        assert "daemon-reload" in cp.stderr
        assert not unit_mod.dropin_path(pod_plane, "wt").exists()
        assert not any(args[0] == "start" for args in calls)


class TestStopPodReclaimsTheOverride:
    """Zero residue is a pod invariant, and the override is per-pod state that
    outlives the service exactly as the HOME does."""

    def _wire(
        self,
        monkeypatch,
        cfg: PodConfig,
        name: str,
        *,
        reload_rc: int = 0,
    ) -> list[tuple[str, ...]]:
        calls: list[tuple[str, ...]] = []

        def _fake(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
            calls.append(args)
            rc = reload_rc if args == ("daemon-reload",) else 0
            return subprocess.CompletedProcess(args=list(args), returncode=rc, stdout="", stderr="")

        monkeypatch.setattr(rt, "systemctl", _fake)
        monkeypatch.setattr(rt, "loaded_teardown_hook", lambda c, n: False)
        monkeypatch.setattr(rt, "cgroup_procs_file", lambda c, n: None)
        monkeypatch.setattr(rt, "cleanup_home", lambda c, n: 0)
        return calls

    def test_down_removes_it(self, pod_plane: PodConfig, monkeypatch) -> None:
        unit_mod.install_dropin(pod_plane, "wt", Path("/checkouts/wt"))
        calls = self._wire(monkeypatch, pod_plane, "wt")

        cp = rt.stop_pod(pod_plane, "wt")

        assert cp.returncode == 0
        assert not unit_mod.dropin_path(pod_plane, "wt").exists()
        # A drop-in systemd has LOADED outlives the file until a reload, so the
        # next `up` of this name would otherwise start under the removed one.
        assert ("daemon-reload",) in calls

    def test_a_failed_reload_after_removal_is_not_zero_residue(
        self, pod_plane: PodConfig, monkeypatch
    ) -> None:
        unit_mod.install_dropin(pod_plane, "wt", Path("/checkouts/wt"))
        self._wire(monkeypatch, pod_plane, "wt", reload_rc=1)

        cp = rt.stop_pod(pod_plane, "wt")

        assert cp.returncode != 0
        assert "daemon-reload" in cp.stderr
        assert "NOT zero-residue" in cp.stderr

    def test_an_unremovable_override_is_reported_not_swallowed(
        self, pod_plane: PodConfig, monkeypatch
    ) -> None:
        # A stale override would pin the NEXT pod of this name to a checkout its
        # operator never chose, so teardown must not claim zero residue.
        unit_mod.install_dropin(pod_plane, "wt", Path("/checkouts/wt"))
        self._wire(monkeypatch, pod_plane, "wt")
        monkeypatch.setattr(unit_mod, "remove_dropin", lambda cfg, name: False)

        cp = rt.stop_pod(pod_plane, "wt")

        assert cp.returncode != 0
        assert "NOT zero-residue" in cp.stderr
        assert str(unit_mod.dropin_path(pod_plane, "wt")) in cp.stderr


class TestUpVerifiesTheSeedLanded:
    """``pod up`` cannot learn from a successful start whether the seed landed,
    so it looks at the home. Reporting success without looking is what let a pod
    booted by an older global build come up blank and be announced as ready."""

    def test_a_fresh_home_without_the_fixture_fails_loudly(
        self, pod_plane: PodConfig, capsys: pytest.CaptureFixture, monkeypatch
    ) -> None:
        # This test owns the CLI decision, not POSIX descriptor traversal.
        monkeypatch.setattr(rt, "seeded_scenario_in_home", lambda cfg, name: "")
        pod_plane.home_dir("wt").mkdir(parents=True)
        with pytest.raises(SystemExit) as excinfo:
            pod_cli._verify_seed_landed(pod_plane, "wt", "minimal", home_was_populated=False)
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "did NOT land" in err
        # The remedy has to name the mechanism, or the operator has nothing to
        # look at: the override is what routes the boot to the right binary.
        assert str(unit_mod.dropin_path(pod_plane, "wt")) in err

    @pytest.mark.skipif(not rt.IS_POSIX, reason="pods require POSIX descriptor traversal")
    def test_the_requested_scenario_present_is_silent(
        self, pod_plane: PodConfig, capsys: pytest.CaptureFixture
    ) -> None:
        rt.seed_home_from_scenario(pod_plane, "wt", "minimal")
        pod_cli._verify_seed_landed(pod_plane, "wt", "minimal", home_was_populated=False)
        assert capsys.readouterr().err == ""

    @pytest.mark.skipif(not rt.IS_POSIX, reason="pods require POSIX descriptor traversal")
    def test_a_different_scenario_in_a_fresh_home_still_fails(
        self, pod_plane: PodConfig, capsys: pytest.CaptureFixture
    ) -> None:
        rt.seed_home_from_scenario(pod_plane, "wt", "minimal")
        with pytest.raises(SystemExit):
            pod_cli._verify_seed_landed(pod_plane, "wt", "rich", home_was_populated=False)
        assert "scenario 'minimal' instead" in capsys.readouterr().err

    def test_an_already_populated_home_refuses_the_unapplied_seed(
        self, pod_plane: PodConfig, capsys: pytest.CaptureFixture, monkeypatch
    ) -> None:
        """A populated home is never overwritten, but a requested seed that was
        not applied is still a failed command. Reporting success here makes
        automation continue against the wrong state — the exact condition the
        post-health verification exists to detect. Existing evidence survives."""
        # The manifest reader's descriptor contract has separate POSIX coverage.
        monkeypatch.setattr(rt, "seeded_scenario_in_home", lambda cfg, name: "")
        home = pod_plane.home_dir("wt")
        home.mkdir(parents=True)
        (home / "sessions").mkdir()
        with pytest.raises(SystemExit) as excinfo:
            pod_cli._verify_seed_landed(pod_plane, "wt", "minimal", home_was_populated=True)
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "was NOT applied" in err
        assert "kirocrew pod down wt" in err
        assert (home / "sessions").is_dir()

    def test_home_state_probe_reads_an_empty_home_as_empty(self, pod_plane: PodConfig) -> None:
        assert pod_cli._home_holds_state(pod_plane, "wt") is False
        pod_plane.home_dir("wt").mkdir(parents=True)
        assert pod_cli._home_holds_state(pod_plane, "wt") is False
        (pod_plane.home_dir("wt") / "config.json").write_text("{}")
        assert pod_cli._home_holds_state(pod_plane, "wt") is True

    def test_a_non_directory_home_counts_as_holding_state(self, pod_plane: PodConfig) -> None:
        # `boot` will not seed over one either, so calling it empty would make
        # the verification demand a fixture that was never going to be written.
        home = pod_plane.home_dir("wt")
        home.parent.mkdir(parents=True, exist_ok=True)
        home.write_text("stale file")
        assert pod_cli._home_holds_state(pod_plane, "wt") is True


class TestUpArgsCarryTheSeedVerification:
    """The verification only runs for a SCENARIO seed: the ``--seed <dir>`` form
    contributes a sanitized ``config.json`` and no fixture manifest, so judging
    it by the manifest would fail every directory seed."""

    def test_directory_seeds_are_not_judged_by_the_manifest(self, pod_plane: PodConfig) -> None:
        assert rt.is_scenario_ref("./some-dir") is False
        assert rt.is_scenario_ref("minimal") is True

    def test_verify_is_reached_only_with_a_scenario(self, monkeypatch) -> None:
        # Guards the wiring rather than the helper: `_up` must pass the resolved
        # scenario, never the raw --seed value, or a directory seed is verified
        # as though it were a fixture.
        source = Path(pod_cli.__file__).read_text()
        assert "if scenario and not home_was_populated:\n            _verify_seed_landed(" in source
