"""A pod must run ITS OWN worktree's code, and prove which port it holds.

Three mechanisms, one theme — a pod that reports success without having done the
thing asked of it:

* **The per-instance boot override.** The systemd unit is a TEMPLATE shared by
  every pod, so its ``ExecStart`` bakes ONE ``kirocrew`` for all of them —
  normally the globally installed build. Every pod therefore booted through that
  binary rather than the checkout it was pinned to, and an older global build
  simply ignores an env-file key it does not understand (``SEED=`` being the
  sharp one): the pod came up healthy, blank, and reported as ready. These pin
  each pod to its own checkout and hold teardown to the same zero-residue bar as
  the HOME.
* **The ownership proof's fallback.** Both POSIX listener tools answer by walking
  ``/proc/<pid>/fd``, so on a host that restricts those the port is visible and
  its owner is not — ownership was unprovable forever, the credential was
  withheld on every mint, and ``pod api`` could not run at all. The pod's own
  run-marker settles it without the socket table.
* **The post-health seed check.** ``pod up`` learns nothing about the seed from a
  successful start, so it has to look.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from kiro_crew.instances import run_marker
from kiro_crew.platform_compat import PortListener
from kiro_crew.pod import cli as pod_cli
from kiro_crew.pod import runtime as rt
from kiro_crew.pod import unit as unit_mod
from kiro_crew.pod.config import PodConfig


@pytest.fixture
def pod_plane(tmp_path: Path, monkeypatch) -> PodConfig:
    """A pod plane rooted in ``tmp_path``, with systemd's unit dir redirected.

    ``unit_path`` composes from ``Path.home()``, so the drop-in lands under the
    real ``~/.config`` unless HOME is redirected — and a test that writes a
    systemd unit into the operator's own configuration is not one to run twice.
    """
    monkeypatch.setattr(rt, "IS_MACOS", False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KIROCREW_POD_ROOT", str(tmp_path / "pods"))
    monkeypatch.setenv("KIROCREW_POD_ENV_DIR", str(tmp_path / "envs"))
    return PodConfig.load()


def _record_gateway_pid(cfg: PodConfig, name: str, port: int, pid: int) -> Path:
    """Write the run-marker sidecar a pod's own gateway writes for *port*."""
    run_dir = cfg.home_dir(name) / run_marker.RUN_DIR_NAME
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / run_marker.pid_file_name(port)
    path.write_text(f"{pid}\n")
    return path


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
        assert "/checkouts/wt/.venv/bin/kirocrew" in lines[1]
        assert lines[1].endswith("pod _run %i")

    def test_names_the_checkouts_own_binary_not_a_global_one(self, pod_plane: PodConfig) -> None:
        rendered = unit_mod.render_dropin(Path("/checkouts/wt"))
        assert str(rt.prov.venv_bin(Path("/checkouts/wt"))) in rendered

    def test_path_is_scoped_to_one_instance(self, pod_plane: PodConfig) -> None:
        # A drop-in on the TEMPLATE would pin every pod to one checkout, which is
        # the defect with extra steps. systemd resolves `<unit>@<name>.service.d`
        # per instance, so each pod carries its own.
        path = unit_mod.dropin_path(pod_plane, "wt")
        assert path.parent.name == f"{pod_plane.unit_prefix}@wt.service.d"
        assert path.parent != unit_mod.unit_path(pod_plane).parent / "shared"

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
        assert "/checkouts/new/" in text
        assert "/checkouts/old/" not in text

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
        assert "/checkouts/wt/" in unit_mod.dropin_path(pod_plane, "wt").read_text()
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

    def _wire(self, monkeypatch, cfg: PodConfig, name: str) -> list[tuple[str, ...]]:
        calls: list[tuple[str, ...]] = []

        def _fake(*args: str, timeout: int = 15) -> subprocess.CompletedProcess:
            calls.append(args)
            return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")

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


class TestOwnershipFromThePodsOwnRecord:
    """The fallback that makes a pod usable on a host where no listener can be
    attributed. Two independent facts have to agree — a pid recorded in a 0600
    file inside the pod's own 0700 home, and that same pid being the unit's
    CURRENT MainPID — which is why this is proof rather than a guess."""

    def _blind_socket_lookup(self, monkeypatch) -> None:
        """A host where the tools exist but can name no owner (restricted /proc)."""
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(rt, "find_port_listeners", lambda port: [])

    def test_a_matching_record_proves_the_pod_owns_the_port(
        self, pod_plane: PodConfig, monkeypatch
    ) -> None:
        self._blind_socket_lookup(monkeypatch)
        _record_gateway_pid(pod_plane, "wt", 7999, 4242)
        monkeypatch.setattr(rt, "main_pid", lambda cfg, name: 4242)
        assert rt.port_owner(pod_plane, "wt", 7999) == rt.OWNER_POD

    def test_a_stale_record_stays_unproven(self, pod_plane: PodConfig, monkeypatch) -> None:
        # MainPID moves on every restart, so a record left by a boot that died
        # names a pid the unit no longer runs — which must not vouch for a port.
        self._blind_socket_lookup(monkeypatch)
        _record_gateway_pid(pod_plane, "wt", 7999, 4242)
        monkeypatch.setattr(rt, "main_pid", lambda cfg, name: 5555)
        assert rt.port_owner(pod_plane, "wt", 7999) == rt.OWNER_UNPROVEN

    def test_a_record_for_another_port_does_not_transfer(
        self, pod_plane: PodConfig, monkeypatch
    ) -> None:
        # The sidecar is keyed by port on purpose: a pod that served :7999 last
        # boot must not vouch for :8000 this one.
        self._blind_socket_lookup(monkeypatch)
        _record_gateway_pid(pod_plane, "wt", 7999, 4242)
        monkeypatch.setattr(rt, "main_pid", lambda cfg, name: 4242)
        assert rt.port_owner(pod_plane, "wt", 8000) == rt.OWNER_UNPROVEN

    def test_no_record_stays_unproven(self, pod_plane: PodConfig, monkeypatch) -> None:
        self._blind_socket_lookup(monkeypatch)
        monkeypatch.setattr(rt, "main_pid", lambda cfg, name: 4242)
        assert rt.port_owner(pod_plane, "wt", 7999) == rt.OWNER_UNPROVEN

    def test_a_dead_unit_stays_unproven(self, pod_plane: PodConfig, monkeypatch) -> None:
        self._blind_socket_lookup(monkeypatch)
        _record_gateway_pid(pod_plane, "wt", 7999, 4242)
        monkeypatch.setattr(rt, "main_pid", lambda cfg, name: None)
        assert rt.port_owner(pod_plane, "wt", 7999) == rt.OWNER_UNPROVEN

    def test_a_foreign_listener_is_never_overridden_by_a_record(
        self, pod_plane: PodConfig, monkeypatch
    ) -> None:
        """The kernel's own view wins whenever it has one.

        The record only ever proves OUR ownership, so a squatter positively
        attributed to the port stays FOREIGN even with a matching sidecar on
        disk — otherwise a stale-but-consistent record would hand a credential
        for this pod to whatever answered.
        """
        monkeypatch.setattr(rt, "listening_pid_tool_available", lambda: True)
        monkeypatch.setattr(
            rt, "find_port_listeners", lambda port: [PortListener(9999, "127.0.0.1", "4")]
        )
        _record_gateway_pid(pod_plane, "wt", 7999, 4242)
        monkeypatch.setattr(rt, "main_pid", lambda cfg, name: 4242)
        assert rt.port_owner(pod_plane, "wt", 7999) == rt.OWNER_FOREIGN

    def test_mint_withholds_with_a_reason_that_names_the_real_cause(
        self, pod_plane: PodConfig, monkeypatch
    ) -> None:
        """The old wording blamed a cause that is usually false.

        It read "no lsof/netstat on this host" even where both are installed and
        merely cannot attribute the socket, so an operator was told to install a
        tool they already had and got the identical refusal next time.
        """
        self._blind_socket_lookup(monkeypatch)
        monkeypatch.setattr(rt, "trusted_system_bin", lambda name: f"/usr/bin/{name}")
        monkeypatch.setattr(rt, "main_pid", lambda cfg, name: None)
        secret = pod_plane.home_dir("wt") / ".local_secret"
        secret.parent.mkdir(parents=True, exist_ok=True)
        secret.write_text("shhh\n")
        monkeypatch.setattr(rt, "derive_port", lambda cfg, name: 7999)

        with pytest.raises(rt.PodOwnershipUnproven) as excinfo:
            rt.mint_token(pod_plane, "wt")

        rendered = str(excinfo.value)
        assert "no lsof/netstat on this host" not in rendered
        assert "restricted /proc" in rendered

    def test_the_reason_names_absent_tools_when_that_is_the_cause(
        self, pod_plane: PodConfig, monkeypatch
    ) -> None:
        monkeypatch.setattr(rt, "trusted_system_bin", lambda name: None)
        assert "is installed" in rt._unprovable_reason(pod_plane, "wt", 7999)


class TestUpVerifiesTheSeedLanded:
    """``pod up`` cannot learn from a successful start whether the seed landed,
    so it looks at the home. Reporting success without looking is what let a pod
    booted by an older global build come up blank and be announced as ready."""

    def test_a_fresh_home_without_the_fixture_fails_loudly(
        self, pod_plane: PodConfig, capsys: pytest.CaptureFixture
    ) -> None:
        pod_plane.home_dir("wt").mkdir(parents=True)
        with pytest.raises(SystemExit) as excinfo:
            pod_cli._verify_seed_landed(pod_plane, "wt", "crons-active", home_was_populated=False)
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "did NOT land" in err
        # The remedy has to name the mechanism, or the operator has nothing to
        # look at: the override is what routes the boot to the right binary.
        assert str(unit_mod.dropin_path(pod_plane, "wt")) in err

    def test_the_requested_scenario_present_is_silent(
        self, pod_plane: PodConfig, capsys: pytest.CaptureFixture
    ) -> None:
        rt.seed_home_from_scenario(pod_plane, "wt", "crons-active")
        pod_cli._verify_seed_landed(pod_plane, "wt", "crons-active", home_was_populated=False)
        assert capsys.readouterr().err == ""

    def test_a_different_scenario_in_a_fresh_home_still_fails(
        self, pod_plane: PodConfig, capsys: pytest.CaptureFixture
    ) -> None:
        rt.seed_home_from_scenario(pod_plane, "wt", "minimal")
        with pytest.raises(SystemExit):
            pod_cli._verify_seed_landed(pod_plane, "wt", "crons-active", home_was_populated=False)
        assert "scenario 'minimal' instead" in capsys.readouterr().err

    def test_an_already_populated_home_is_reported_not_refused(
        self, pod_plane: PodConfig, capsys: pytest.CaptureFixture
    ) -> None:
        """``boot`` deliberately never re-seeds a populated home — a
        crash-restart must not wipe the evidence the operator is podding the
        worktree to look at. So this case is a note, and it names what the home
        actually holds rather than leaving the request looking applied."""
        home = pod_plane.home_dir("wt")
        home.mkdir(parents=True)
        (home / "sessions").mkdir()
        pod_cli._verify_seed_landed(pod_plane, "wt", "crons-active", home_was_populated=True)
        err = capsys.readouterr().err
        assert "was NOT applied" in err
        assert "kirocrew pod down wt" in err

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
        assert rt.is_scenario_ref("crons-active") is True

    def test_verify_is_reached_only_with_a_scenario(self, monkeypatch) -> None:
        # Guards the wiring rather than the helper: `_up` must pass the resolved
        # scenario, never the raw --seed value, or a directory seed is verified
        # as though it were a fixture.
        source = Path(pod_cli.__file__).read_text()
        assert "if scenario:\n            _verify_seed_landed(" in source


def _namespace(**kwargs: object) -> argparse.Namespace:
    return argparse.Namespace(**kwargs)
